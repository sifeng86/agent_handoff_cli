"""Single coordinator for ownership mutation, durable intent, CAS and resolution."""
from contextlib import contextmanager
import json
from pathlib import Path
import re

from .authority import Authority
from .contracts import RuntimeFailure, parse_record
from .decisions import Decisions
from .git_backend import GitBackend
from .integrity import remember, validate
from .journal import Journal, opaque_id, validate_id
from .locks import lock
from .output import result
from .paths import PrivateStore
from .sessions import Sessions, checkout_identity
from .preservation import preserve, snapshot, safe_path, checkout_git
from .plans import validate as validate_plans, complete as complete_plan
from .materialization import materialize
from .integrity_decisions import dependencies as integrity_dependencies, read as read_integrity_decision
from .authority_selection import AuthoritySelection


def encode(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class Runtime:
    def __init__(self, checkout: Path, private_root: Path, authority: Authority, context: str | None = None, *, fault=None):
        self.checkout = checkout.resolve()
        if not self.checkout.is_dir():
            raise RuntimeFailure("Checkout directory is missing.")
        self.authority = authority
        self.store = PrivateStore(private_root / "repos" / authority.identity, self.checkout)
        self.backend = GitBackend(self.store.root / "objects.git", authority.destination, authority.publication_ref)
        self.journal = Journal(self.store)
        self.decisions = Decisions(self.store, authority)
        self.sessions = Sessions(self.store, authority, self.checkout, context) if context is not None else None
        self.selection_store = None
        self.selection = None
        if self.sessions is not None:
            self.selection_store = PrivateStore(private_root / "selections", self.checkout)
            self.selection = AuthoritySelection(self.selection_store, self.sessions.checkout_id, self.sessions.context_id)
        self.fault = fault or (lambda point: None)

    def close(self):
        if self.selection_store is not None:
            self.selection_store.close()
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def locked(self):
        if self.selection is not None:
            with self.selection.locked(), lock(self.store, "repository.lock"):
                self.selection.require_clear()
                if self.selection.read() is not None:
                    self.selection.require(self.authority.identity)
                yield
            return
        with lock(self.store, "repository.lock"):
            yield

    def checkout_lock(self):
        return lock(self.store, "checkout-" + checkout_identity(self.checkout) + ".lock")

    def _context(self):
        if self.sessions is None:
            raise RuntimeFailure("Mutation requires an explicit private runtime context.")

    def _observe(self, *, initial: bool = False):
        oid = self.backend.observe()
        records = validate(self.backend, self.store, oid, initial=initial)
        self._reject_mapped_material(oid)
        if self.selection is not None:
            self.selection.register(self.authority.identity)
        return oid, records

    def _reject_mapped_material(self, oid):
        binding = self.store.read("secret-binding.json", missing=True)
        if binding is None:
            return
        from .secrets.manifest import parse as parse_manifest
        from .secrets.binding import validate as validate_secret_binding
        validate_secret_binding(binding)
        if binding["repository_identity"] != self.authority.identity:
            raise RuntimeFailure("Secret binding belongs to a different authority.", "invalid", 7)
        manifest = parse_manifest(json.dumps(binding["manifest"]))
        for declaration in manifest["keys"].values():
            if declaration["classification"] == "file-backed" and self.backend.text(
                    "--literal-pathspecs", "ls-tree", "-r", "--name-only", oid, "--", declaration["file"]):
                raise RuntimeFailure("Mapped secret material cannot enter publication.", "invalid", 7)

    def initialize(self, base_oid: str | None = None, *, mainline_ref: str | None = None) -> dict:
        with self.locked(), self.checkout_lock():
            previous = self.store.read("bindings.json", missing=True)
            self.journal.require_clear()
            if previous is not None:
                self.authority.require_binding(self.store)
            current = self.backend.observe()
            if current is not None:
                records = validate(self.backend, self.store, current, initial=previous is None)
                self.authority.bind(self.store)
                remember(self.store, self.authority.identity, current)
                if self.selection is not None:
                    self.selection.register(self.authority.identity)
                return result("init", observed_revision=current,
                              ownership="unowned" if records["owner"]["owner"] is None else "unknown")
            if previous is not None:
                raise RuntimeFailure("Registered publication branch is missing; cannot recreate it.", "invalid", 7)
            if base_oid is None or not re.fullmatch(r"[0-9a-f]{40}", base_oid):
                raise RuntimeFailure("Initialization requires an explicitly approved SHA-1 base commit.")
            if mainline_ref is None or not mainline_ref.startswith("refs/heads/"):
                raise RuntimeFailure("Initialization requires an explicit full mainline ref.")
            self.backend.text("check-ref-format", mainline_ref)
            if mainline_ref == self.authority.publication_ref:
                raise RuntimeFailure("Mainline and publication refs must be distinct.")
            self.backend.text("fetch", "--quiet", "--no-tags", "--", self.authority.destination, base_oid)
            self.backend.text("cat-file", "-e", base_oid + "^{commit}")
            paths = self.backend.text("ls-tree", "-r", "--name-only", base_oid).splitlines()
            for path in paths:
                safe_path(path, controls=True)
            changes = {
                ".agent/protocol.json": encode({"schema_version": 1, "protocol_version": "1.2", "required_features": []}),
                ".agent/owner.json": encode({"schema_version": 1, "owner": None}),
                ".agent/state.json": encode({"schema_version": 1, "status": "idle", "active_plan": None,
                                             "current_step": None, "blocker": None, "summary": ""}),
                ".agent/config.yaml": encode({"schema_version": 1, "authority": self.authority.destination,
                                              "publication_ref": self.authority.publication_ref,
                                              "mainline_ref": mainline_ref, "plan_directory": "plans"}),
            }
            self.authority.bind(self.store)
            if self.selection is not None:
                self.selection.register(self.authority.identity)
            return self._publish("init", None, base_oid, changes)

    def recover(self) -> dict:
        self._context()
        with self.locked():
            self.authority.require_binding(self.store)
            self.journal.require_clear()
            oid, records = self._observe()
            session = self.sessions.prove(self.backend, oid, records)
            with lock(self.store, "session-" + session["session_id"] + ".lock"):
                with self.checkout_lock():
                    remember(self.store, self.authority.identity, oid)
                    return result("recover-session", ownership="owned", observed_revision=oid)

    def acquire(self) -> dict:
        self._context()
        with self.locked():
            self.authority.require_binding(self.store)
            self.journal.require_clear()
            oid, records = self._observe()
            owner = records["owner"]["owner"]
            if owner is not None:
                try:
                    self.sessions.prove(self.backend, oid, records)
                except RuntimeFailure:
                    preservation = preserve(self.checkout, self.store)
                    envelope = self.decisions.create("takeover", oid, owner["session_id"],
                        checkout_identity=self.sessions.checkout_id, context_identity=self.sessions.context_id,
                        reason="Publication is owned by another or unprovable session. Original checkout is retained.",
                        options=[{"option_id": "takeover", "effect": "Acquire a new session and fence the previous owner."},
                                 {"option_id": "abort", "effect": "Retain local work and leave ownership unchanged."}])
                    return result("acquire", status="decision_required", ownership="unowned", decision=envelope,
                                  preservation_handle=preservation["handle_id"])
                raise RuntimeFailure("Caller already owns this session; use recover-session.", "rejected", 4)
            binding = self.store.read("checkout-binding.json", missing=True)
            if binding is not None:
                # A known revoked/fenced record may be replaced, but never silently share its capability.
                existing = self.store.read("session-" + validate_id(binding["session_id"]) + ".json")
                if existing.get("revoked") is not True and (binding.get("checkout_identity") != self.sessions.checkout_id or
                                                           binding.get("context_identity") != self.sessions.context_id):
                    raise RuntimeFailure("An existing write checkout binding requires explicit transfer.", "rejected", 4)
            return self._acquire("acquire", oid)

    def publish_snapshot(self, command: str, included: list[str], *, summary: str,
                         state: dict | None = None, release_intent: bool | None = None, secret_service=None) -> dict:
        if command not in ("checkpoint", "handoff", "done"):
            raise RuntimeFailure("Invalid snapshot command.")
        if not isinstance(summary, str):
            raise RuntimeFailure("An explicit non-secret summary is required.")
        if command == "done" and type(release_intent) is not bool:
            raise RuntimeFailure("Done requires an explicit true/false release intention.")
        self._context()
        with self.locked():
            self.authority.require_binding(self.store)
            self.journal.require_clear()
            oid, records = self._observe()
            session = self.sessions.prove(self.backend, oid, records)
            with lock(self.store, "session-" + session["session_id"] + ".lock"):
                with self.checkout_lock():
                    preservation = preserve(self.checkout, self.store)
                    changes = snapshot(self.checkout, included)
                    new_state = dict(records["state"] if state is None else state, summary=summary)
                    parse_record(encode(new_state).decode(), "state")
                    releasing = command == "handoff" or (command == "done" and release_intent)
                    if command == "done":
                        if state is not None:
                            raise RuntimeFailure("Done updates the committed active Plan; custom state is not accepted.")
                        plans = validate_plans(self.backend, oid, records["state"])
                        active = records["state"]["active_plan"]
                        if active is None:
                            raise RuntimeFailure("Done requires a committed active READY Plan.")
                        plan = plans[active]
                        if plan["path"] in changes:
                            raise RuntimeFailure("Done cannot simultaneously replace its active Plan.")
                        changes[plan["path"]] = complete_plan(plan)
                        new_state.update(status="idle", active_plan=None, current_step=None, blocker=None)
                    changes[".agent/state.json"] = encode(new_state)
                    if releasing:
                        changes[".agent/owner.json"] = encode({"schema_version": 1, "owner": None})
                    sync = None
                    def before_send():
                        nonlocal sync
                        try:
                            sync = secret_service.synchronize_locked(oid, records, session)
                        except RuntimeFailure as error:
                            sync = result("secrets reconcile", status=error.status,
                                          secrets_shared="unknown" if error.code == 5 else False,
                                          next_action="Code handoff may proceed; shared secrets need separate recovery.")
                        except Exception:
                            sync = result("secrets reconcile", status="unknown", secrets_shared="unknown",
                                          next_action="Preserve private secret evidence before another synchronization.")
                    callback = before_send if command == "handoff" and secret_service is not None else None
                    published = self._publish(command, oid, oid, changes, session=session, before_send=callback)
                    if sync is not None:
                        published["secrets_shared"] = sync["secrets_shared"]
                        allowed = ("status", "secret_operation_id", "plan_id", "decision", "next_action", "availability", "conflicts", "verified_keys")
                        published["secrets_result"] = {key: sync[key] for key in allowed if key in sync}
                    published["preservation_handle"] = preservation["handle_id"]
                    published["test_status"] = "unknown"
                    return published

    def _acquire(self, command: str, oid: str, decision_id: str | None = None):
        operation_id = opaque_id()
        candidate = self.sessions.candidate(operation_id)
        owner = {key: candidate[key] for key in ("session_id", "acquisition_operation_id", "machine_label", "provider_label")}
        changes = {".agent/owner.json": encode({"schema_version": 1, "owner": owner})}
        with lock(self.store, "session-" + candidate["session_id"] + ".lock"):
            with self.checkout_lock():
                return self._publish(command, oid, oid, changes, candidate=candidate,
                                     operation_id=operation_id, decision_id=decision_id)

    def apply_decision(self, decision_id: str, option_id: str) -> dict:
        self._context()
        with self.locked():
            self.authority.require_binding(self.store)
            category = self.decisions.read(decision_id)["category"]
            if category == "publication_integrity":
                reviewed = self.decisions.read(decision_id)
                private = read_integrity_decision(self.store, decision_id, reviewed, checkout=self.checkout)
                if any(private[key] != value for key, value in integrity_dependencies(self.store, self.authority.identity).items()):
                    self.decisions.finish(decision_id, "stale")
                    raise RuntimeFailure("Integrity review dependencies changed; request a fresh decision.", "decision_required", 3)
                current = self.backend.observe()
                owner_id = None
                valid = False
                if current is not None:
                    try:
                        records = self.backend.records(current)
                        self._reject_mapped_material(current)
                        validate_plans(self.backend, current, records["state"])
                        owner = records["owner"]["owner"]
                        owner_id = owner["session_id"] if owner else None
                        valid = True
                    except RuntimeFailure:
                        pass
                envelope, option = self.decisions.check(decision_id, option_id, current, owner_id,
                    checkout_identity=self.sessions.checkout_id, context_identity=self.sessions.context_id)
                if option["option_id"] == "abort":
                    self.decisions.finish(decision_id, "aborted")
                    return result("decision apply", ownership="unowned")
                if option["option_id"] != "adopt" or not valid:
                    raise RuntimeFailure("Invalid authority cannot be adopted.", "invalid", 7)
                self.journal.require_clear()
                remember(self.store, self.authority.identity, current)
                self.decisions.finish(decision_id, "applied")
                return result("decision apply", observed_revision=current)
            self.journal.require_clear()
            oid, records = self._observe()
            owner = records["owner"]["owner"]
            envelope, option = self.decisions.check(decision_id, option_id, oid,
                owner["session_id"] if owner else None, checkout_identity=self.sessions.checkout_id,
                context_identity=self.sessions.context_id)
            if option["option_id"] == "abort":
                self.decisions.finish(decision_id, "aborted")
                return result("decision apply", ownership="unowned")
            if envelope["category"] != "takeover" or option["option_id"] != "takeover" or owner is None:
                raise RuntimeFailure("Unsupported decision executor.", "unsupported", 8)
            try:
                self.sessions.prove(self.backend, oid, records)
            except RuntimeFailure:
                pass
            else:
                raise RuntimeFailure("Recoverable owner cannot be replaced through takeover.", "rejected", 4)
            return self._acquire("takeover", oid, decision_id)

    def release(self) -> dict:
        self._context()
        with self.locked():
            self.authority.require_binding(self.store)
            self.journal.require_clear()
            oid, records = self._observe()
            session = self.sessions.prove(self.backend, oid, records)
            with lock(self.store, "session-" + session["session_id"] + ".lock"):
                with self.checkout_lock():
                    return self._publish("release", oid, oid,
                        {".agent/owner.json": encode({"schema_version": 1, "owner": None})}, session=session)

    def resume(self) -> dict:
        self._context()
        from .secrets.journal import SecretJournal
        secrets_journal = SecretJournal(self.store)
        # Acquire/recover are independently coordinated; each phase re-observes authority.
        with self.locked():
            self.authority.require_binding(self.store)
            self.journal.require_clear()
            secrets_journal.require_clear()
            oid, records = self._observe()
            preservation = preserve(self.checkout, self.store)
            owner = records["owner"]["owner"]
        acquired = owner is None
        if acquired:
            ownership = self.acquire()
            if ownership["status"] != "completed":
                return dict(ownership, command="resume")
        else:
            try:
                ownership = self.recover()
            except RuntimeFailure as error:
                if error.code != 4:
                    raise
                return dict(self.acquire(), command="resume")
        with self.locked():
            self.journal.require_clear()
            secrets_journal.require_clear()
            oid, records = self._observe()
            session = self.sessions.prove(self.backend, oid, records)
            with lock(self.store, "session-" + session["session_id"] + ".lock"):
                with self.checkout_lock():
                    try:
                        if (self.checkout.is_relative_to(self.store.root / "checkouts") and
                                checkout_git(self.checkout, "rev-parse", "HEAD").decode().strip() == oid and
                                not preservation["tracked_changes"] and not preservation["untracked"]):
                            managed = self.checkout
                        else:
                            managed = materialize(self.backend, self.store, session, oid)
                        current, latest = self._observe()
                        self.sessions.prove(self.backend, current, latest)
                        if current != oid:
                            raise RuntimeFailure("Authority advanced during materialization; refresh resume.", "rejected", 4)
                    except RuntimeFailure:
                        # Release below must reacquire locks; never clean up the preserved source/worktree.
                        failed = True
                    else:
                        failed = False
                        managed_id = checkout_identity(managed)
                        session = dict(session, checkout_identity=managed_id)
                        self.store.write("session-" + session["session_id"] + ".json", session)
                        self.store.write("checkout-binding.json", {"schema_version": 1, "session_id": session["session_id"],
                            "checkout_identity": managed_id, "context_identity": session["context_identity"]})
                        needs_integration = preservation["tracked_changes"] > 0 or preservation["untracked"] > 0
                        known = self.backend.run("cat-file", "-e", preservation["HEAD"] + "^{commit}").returncode == 0
                        if not known or not self.backend.ancestor(preservation["HEAD"], oid):
                            needs_integration = True
                        availability = None
                        binding = self.store.read("secret-binding.json", missing=True)
                        if binding is not None:
                            from .secrets.local_env import LocalEnvironment
                            from .secrets.manifest import parse as parse_manifest
                            try:
                                manifest = parse_manifest(json.dumps(binding["manifest"]))
                                availability = LocalEnvironment(managed / ".env", self.store).inspect(manifest)
                            except (RuntimeFailure, KeyError):
                                availability = {"status": "not_ready", "required_missing": [], "next_action": "Inspect private secret setup before dependent work."}
                        missing_environment = availability is not None and availability["status"] != "completed"
                        return result("resume", status="not_ready" if needs_integration or missing_environment else "ready", ownership="owned",
                                      observed_revision=oid, managed_checkout=str(managed),
                                      preservation_handle=preservation["handle_id"],
                                      secret_availability=availability, secrets_shared=False if binding is not None else None,
                                      next_action="Coding Agent must integrate preserved local edits before continuing." if needs_integration else
                                      "Required local secret material is missing; stop dependent commands." if missing_environment else None)
        if failed and acquired:
            try:
                released = self.release()
            except RuntimeFailure:
                released = result("release", status="unknown", ownership="ownership_pending")
            return result("resume", status="not_ready", ownership=released["ownership"],
                          preservation_handle=preservation["handle_id"], next_action="Materialization failed; original checkout is retained.")
        try:
            self.recover()
        except RuntimeFailure as error:
            final_ownership = "fenced" if error.code == 4 else "ownership_pending"
        else:
            final_ownership = "owned"
        return result("resume", status="not_ready", ownership=final_ownership, preservation_handle=preservation["handle_id"],
                      next_action="Materialization failed; original checkout is retained.")

    def propose_integrity(self) -> dict:
        self._context()
        with self.locked():
            self.authority.require_binding(self.store)
            current = self.backend.observe()
            owner = None
            snapshot_valid = False
            dependencies = integrity_dependencies(self.store, self.authority.identity)
            options = [{"option_id": "abort", "effect": "Retain local work and leave authority unchanged."}]
            if current is not None:
                try:
                    records = self.backend.records(current)
                    self._reject_mapped_material(current)
                    validate_plans(self.backend, current, records["state"])
                    owner_record = records["owner"]["owner"]
                    owner = owner_record["session_id"] if owner_record else None
                except RuntimeFailure:
                    pass
                else:
                    snapshot_valid = True
                    if not self.journal.pending():
                        options.insert(0, {"option_id": "adopt", "effect": "Trust this exact valid authority revision without rewriting history."})
            preservation = preserve(self.checkout, self.store)
            envelope = self.decisions.create("publication_integrity", current, owner,
                checkout_identity=self.sessions.checkout_id, context_identity=self.sessions.context_id,
                reason="Explicit review of publication integrity; original checkout is retained.", options=options)
            self.store.write("integrity-decision-" + envelope["decision_id"] + ".json",
                dict(dependencies, schema_version=1, decision_id=envelope["decision_id"],
                     repository_identity=self.authority.identity, observed_head=current, observed_owner_session=owner,
                     snapshot_valid=snapshot_valid, preservation_handle=preservation["handle_id"]))
            return result("decision propose-integrity", status="decision_required", decision=envelope,
                          preservation_handle=preservation["handle_id"])

    def _publish(self, command: str, expected: str | None, parent: str, changes: dict,
                 *, candidate: dict | None = None, session: dict | None = None,
                 operation_id: str | None = None, decision_id: str | None = None, before_send=None) -> dict:
        operation_id = operation_id or opaque_id()
        session_id = (candidate or session or {}).get("session_id")
        def message(tree):
            return (f"Agent Handoff: {command}\n\nAgent-Operation-Id: {operation_id}\n"
                    f"Agent-Operation-Type: {command}\nAgent-Session-Id: {session_id or 'none'}\n"
                    f"Agent-Payload-Identity: {tree}\n")
        commit, tree = self.backend.prepare(parent, changes, message)
        prepared_records = self.backend.records(commit)
        self._reject_mapped_material(commit)
        validate_plans(self.backend, commit, prepared_records["state"], expected)
        self.backend.pin(operation_id, commit)
        record = self.journal.prepare(self.authority, command, expected, commit, tree, session_id,
                                      operation_id=operation_id, candidate=candidate, decision_id=decision_id)
        self.fault("after_journal_fsync")
        if before_send is not None:
            before_send()
        record["phase"] = "sending"
        self.journal.write(record)
        self.fault("before_remote_send")
        response = self.backend.push(commit, expected)
        self.fault("after_server_accept_before_client_reply")
        record.update(phase="verifying", result=response)
        self.journal.write(record)
        if response == "rejected":
            record.update(phase="resolved", result="rejected")
            self.journal.write(record)
            if decision_id:
                self.decisions.finish(decision_id, "stale")
            return result(command, status="rejected", operation_id=operation_id,
                          code_publication="rejected", ownership="fenced")
        return self._resolve(record)

    def resolve(self, operation_id: str) -> dict:
        with self.locked():
            self.authority.require_binding(self.store)
            record = self.journal.read(operation_id)
            if record["repository_identity"] != self.authority.identity or record["publication_ref"] != self.authority.publication_ref:
                raise RuntimeFailure("Operation authority mismatch.")
            if record["session_id"] is not None:
                with lock(self.store, "session-" + validate_id(record["session_id"]) + ".lock"):
                    with self.checkout_lock():
                        return self._resolve(record)
            with self.checkout_lock():
                return self._resolve(record)

    def _resolve(self, record: dict) -> dict:
        operation_id = record["operation_id"]
        commit = record["prepared_commit_oid"]
        if record["phase"] == "resolved" and record["result"] == "local_only":
            return result(record["operation_type"], operation_id=operation_id, code_publication="local_only")
        if record["phase"] == "resolved" and record["result"] == "rejected":
            return result(record["operation_type"], status="rejected", operation_id=operation_id,
                          code_publication="rejected", ownership="fenced")
        try:
            current = self.backend.observe()
            records = validate(self.backend, self.store, current,
                               initial=record["operation_type"] == "init")
            published = current == commit or self.backend.ancestor(commit, current)
            message = self.backend.text("show", "-s", "--format=%B", commit)
            if (f"Agent-Operation-Id: {operation_id}" not in message.splitlines() or
                    f"Agent-Payload-Identity: {record['payload_identity']}" not in message.splitlines()):
                raise RuntimeFailure("Prepared operation metadata mismatch.", "invalid", 7)
        except RuntimeFailure as error:
            record.update(result="publication_unknown")
            self.journal.write(record)
            return result(record["operation_type"], status="invalid" if error.code == 7 else "unknown",
                          operation_id=operation_id, code_publication="publication_unknown", ownership="ownership_pending",
                          next_action="Resolve publication evidence before further mutation.")
        if not published:
            if record["phase"] == "prepared":
                # The durable sending transition necessarily precedes every network write.
                record.update(phase="resolved", result="local_only")
                self.journal.write(record)
                return result(record["operation_type"], operation_id=operation_id, code_publication="local_only")
            # Even an unchanged HEAD cannot prove that a timed-out server command has ended.
            record.update(result="publication_unknown")
            self.journal.write(record)
            return result(record["operation_type"], status="unknown", operation_id=operation_id,
                          code_publication="publication_unknown", ownership="ownership_pending")
        owner = records["owner"]["owner"]
        candidate = record.get("candidate")
        owned = owner is not None and owner["session_id"] == record["session_id"]
        if candidate is not None:
            # Recovery can reconstruct durable proof after publication without granting the resolver's caller access.
            if owned:
                self.fault("before_capability_write")
                candidate = dict(candidate, acquisition_oid=commit)
                existing = self.store.read("session-" + candidate["session_id"] + ".json", missing=True)
                if existing is None:
                    self.store.write("session-" + candidate["session_id"] + ".json", candidate)
                    self.store.write("checkout-binding.json", {"schema_version": 1, "session_id": candidate["session_id"],
                        "checkout_identity": candidate["checkout_identity"], "context_identity": candidate["context_identity"]})
                elif existing.get("revoked") is True:
                    owned = False
        if not owned and record["session_id"] is not None:
            local = self.store.read("session-" + record["session_id"] + ".json", missing=True)
            if local is not None:
                self.store.write("session-" + record["session_id"] + ".json", dict(local, revoked=True))
        if record.get("decision_id"):
            self.decisions.finish(record["decision_id"], "applied")
        remember(self.store, self.authority.identity, current)
        record.update(phase="resolved", result="published")
        self.journal.write(record)
        ownership = "released" if owner is None else "fenced"
        if owned:
            ownership = "unknown"
            if self.sessions is not None:
                try:
                    self.sessions.prove(self.backend, current, records)
                except RuntimeFailure:
                    pass
                else:
                    ownership = "owned"
        return result(record["operation_type"], operation_id=operation_id, published_revision=commit,
                      code_publication="published", ownership=ownership)
