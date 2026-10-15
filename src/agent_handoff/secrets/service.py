"""Scoped secret grants, private observations, decisions and apply coordination."""
from contextlib import contextmanager
import json
import hashlib

from ..contracts import RuntimeFailure
from ..journal import opaque_id, validate_id
from ..locks import lock
from ..output import result
from ..public_results import PublicResultFailure
from ..preservation import checkout_git
from .adapter import AuthenticationRequired, SharedReadUnavailable, destination_identity
from .baseline import Baseline
from .journal import SecretJournal
from .local_env import LocalEnvironment
from .manifest import parse as parse_manifest, shared_scope
from .reconcile import reconcile
from .dotenv import KEY, parse as parse_dotenv, patch as patch_dotenv
from .file_material import FileMaterial, validate_pem
from .binding import validate as validate_binding
from .plan_records import validate as validate_plan_record
from .decision_records import read as read_private_decision


class SecretService:
    def __init__(self, runtime, manifest: dict, adapter=None):
        self.runtime = runtime
        self.store = runtime.store
        self.manifest = parse_manifest(json.dumps(manifest))
        self.scope = shared_scope(self.manifest)
        self.adapter = adapter
        self.environment = LocalEnvironment(runtime.checkout / ".env", self.store)
        self.journal = SecretJournal(self.store)

    def _binding(self) -> dict:
        return self._validate_binding(self.store.read("secret-binding.json"))

    def _validate_binding(self, binding) -> dict:
        validate_binding(binding)
        if binding["repository_identity"] != self.runtime.authority.identity:
            raise RuntimeFailure("Invalid private secret binding.")
        if binding["manifest"] != self.manifest:
            raise RuntimeFailure("Manifest scope changed; approve a new secret binding decision.", "decision_required", 3)
        if binding["mode"] == "doppler":
            if self.adapter is None or self.adapter.destination_identity != binding["destination_identity"]:
                raise RuntimeFailure("Secret adapter does not match the approved destination.", "rejected", 4)
        return binding

    def _baseline(self, binding):
        policy = json.dumps([binding["destination_identity"], binding["manifest"]], sort_keys=True, separators=(",", ":"))
        identity = hashlib.sha256(policy.encode()).hexdigest()
        return Baseline(self.store, self.runtime.authority.identity, identity, self.scope)

    def _remote(self, binding):
        if binding["mode"] != "doppler":
            raise RuntimeFailure("Local-env mode has no shared reconciliation backend.", "unsupported", 8)
        try:
            values = self.adapter.read(self.scope)
        except RuntimeFailure as error:
            if isinstance(error, AuthenticationRequired):
                raise AuthenticationRequired() from None
            raise SharedReadUnavailable(error.status, error.code) from None
        except Exception:
            raise SharedReadUnavailable() from None
        if not isinstance(values, dict) or any(key not in self.scope or not isinstance(value, str) for key, value in values.items()):
            raise SharedReadUnavailable("invalid", 2)
        return values

    def _sources(self, binding):
        document = self.environment.read()
        baseline = self._baseline(binding).read()
        remote = self._remote(binding)
        local = self._local(document)
        for key in remote:
            if self.manifest["keys"][key]["classification"] == "file-backed":
                validate_pem(remote[key])
        return document, baseline, local, remote

    def _operation_status(self, operation_id):
        try:
            status = self.adapter.operation_status(operation_id)
        except Exception:
            return "unknown"
        return status if isinstance(status, str) and status in {"published", "rejected"} else "unknown"

    def _material(self, key):
        return FileMaterial(self.runtime.checkout, self._mapped_path(key), self.store)

    def _mapped_path(self, key):
        mapping = self.manifest["keys"][key]["file"]
        if checkout_git(self.runtime.checkout, "--literal-pathspecs", "ls-files", "-z", "--", mapping):
            raise RuntimeFailure("File-backed mapping overlaps tracked source.", "rejected", 4)
        return mapping

    def _local(self, document):
        values = {}
        for key in sorted(self.scope):
            declaration = self.manifest["keys"][key]
            if declaration["classification"] == "file-backed":
                if key in document.values and document.values[key] != declaration["file"]:
                    raise RuntimeFailure("Local file-backed path differs from approved mapping.", "decision_required", 3)
                value = self._material(key).read()
                if value is not None:
                    values[key] = value
            elif key in document.values:
                values[key] = document.values[key]
        return values

    @contextmanager
    def writer(self, *, pending_allowed: bool = False):
        runtime = self.runtime
        runtime._context()
        with runtime.locked():
            runtime.authority.require_binding(self.store)
            runtime.journal.require_clear()
            if not pending_allowed:
                self.journal.require_clear()
            oid, records = runtime._observe()
            session = runtime.sessions.prove(runtime.backend, oid, records)
            with lock(self.store, "session-" + session["session_id"] + ".lock"):
                with runtime.checkout_lock():
                    yield oid, records, session

    def propose_binding(self, mode: str, *, project=None, config=None, upload_authorized=False) -> dict:
        self.runtime._context()
        identity = destination_identity(mode, project, config)
        if type(upload_authorized) is not bool or (mode == "local-env" and upload_authorized):
            raise RuntimeFailure("Invalid secret upload grant.")
        if upload_authorized:
            if self.adapter is None or self.adapter.destination_identity != identity:
                raise RuntimeFailure("An approved matching adapter is required for upload grants.")
            self.adapter.require_write_gate()
        candidate = {"schema_version": 1, "repository_identity": self.runtime.authority.identity,
                     "destination_identity": identity, "mode": mode, "project": project, "config": config,
                     "manifest": self.manifest, "upload_authorized": upload_authorized}
        with self.runtime.locked():
            self.runtime.authority.require_binding(self.store)
            self.runtime.journal.require_clear()
            self.journal.require_clear()
            oid, records = self.runtime._observe()
            owner = records["owner"]["owner"]
            for key, declaration in self.manifest["keys"].items():
                if declaration["classification"] == "file-backed":
                    self._mapped_path(key)
            envelope = self.runtime.decisions.create("authority_binding", oid, owner["session_id"] if owner else None,
                checkout_identity=self.runtime.sessions.checkout_id, context_identity=self.runtime.sessions.context_id,
                destination_identity=identity,
                secrets_details={"key_names": sorted(self.manifest["keys"]), "secrets_mode": mode,
                                 "upload_authorized": upload_authorized, "secret_manifest": self.manifest,
                                 "secrets_project": project, "secrets_config": config},
                reason="Approve the explicit secrets mode, destination, key classifications and persistent grant.",
                options=[{"option_id": "approve-secret-binding", "effect": "Use this secrets destination and the listed approved scope."},
                         {"option_id": "abort", "effect": "Keep the current secret binding and all values unchanged."}])
            self.store.write("secret-decision-" + envelope["decision_id"] + ".json",
                {"schema_version": 1, "decision_id": envelope["decision_id"], "kind": "binding", "candidate": candidate,
                 "previous_binding": self.store.read("secret-binding.json", missing=True)})
            return result("secrets configure", status="decision_required", decision=envelope, secrets_shared=False)

    def inspect(self) -> dict:
        binding = self._binding()
        local = self.environment.inspect(self.manifest)
        report = result("secrets inspect", status=local["status"], secrets_shared=False,
                        availability=local, secrets_mode=binding["mode"], destination_identity=binding["destination_identity"])
        if binding["mode"] == "doppler":
            try:
                remote = self._remote(binding)
            except RuntimeFailure as failure:
                report.update(shared_read="unavailable", local_work_available=not local["required_missing"])
                report["shared_read_failure"] = ("authentication_required" if isinstance(failure, AuthenticationRequired)
                                                  else "unavailable")
            else:
                report.update(shared_read="available", shared_available_keys=sorted(remote))
        return report

    def verify(self) -> dict:
        report = self.inspect()
        report["command"] = "secrets verify"
        binding = self._binding()
        if binding["mode"] == "local-env":
            return report
        try:
            document, baseline, local, remote = self._sources(binding)
        except RuntimeFailure as failure:
            report.update(status="not_ready", secrets_shared=False, convergence="unverified")
            if isinstance(failure, SharedReadUnavailable):
                report.update(shared_read="unavailable", shared_read_failure=(
                    "authentication_required" if isinstance(failure, AuthenticationRequired) else "unavailable"))
                report.pop("shared_available_keys", None)
            return report
        report["shared_read"] = "available"
        report.pop("shared_read_failure", None)
        report["shared_available_keys"] = sorted(remote)
        converged = baseline is not None and baseline == local == remote
        report.update(status="completed" if converged and not report["availability"]["required_missing"] else "not_ready",
                      secrets_shared=converged, convergence="verified" if converged else "unverified")
        return report

    def _evidence(self, oid, session, binding, document, baseline, local, remote) -> dict:
        return {"repository_identity": self.runtime.authority.identity, "observed_head": oid,
                "session_id": session["session_id"], "checkout_identity": self.runtime.sessions.checkout_id,
                "context_identity": self.runtime.sessions.context_id, "binding": binding, "manifest": self.manifest,
                "local_raw": document.raw, "local_values": document.values, "baseline": baseline,
                "local": local, "remote": remote}

    def reconcile(self) -> dict:
        with self.writer() as (oid, records, session):
            return self._plan_locked(oid, session)

    def _plan_locked(self, oid, session) -> dict:
        binding = self._binding()
        document, baseline, local, remote = self._sources(binding)
        plan = reconcile(baseline, local, remote, self.scope, allow_upload=binding["upload_authorized"])
        evidence = self._evidence(oid, session, binding, document, baseline, local, remote)
        plan_id = opaque_id()
        record = {"schema_version": 1, "plan_id": plan_id, "evidence": evidence, "target": plan.baseline,
                  "conflicts": plan.conflicts, "status": "pending"}
        self.store.write("secret-plan-" + plan_id + ".json", record)
        if plan.conflicts:
            options = [{"option_id": "use-remote", "effect": "Adopt shared values for conflicting keys, including approved local deletions where shared keys are absent."},
                       {"option_id": "abort", "effect": "Leave local, shared and baseline values unchanged."}]
            if binding["upload_authorized"]:
                try:
                    self.adapter.require_write_gate()
                except RuntimeFailure:
                    pass
                else:
                    options.insert(0, {"option_id": "keep-local", "effect": "Use local values for conflicting keys, including approved shared deletions where local keys are absent."})
            category = "secret_deletion" if "secret_deletion" in plan.conflicts.values() else "secret_conflict"
            envelope = self.runtime.decisions.create(category, oid, session["session_id"],
                checkout_identity=self.runtime.sessions.checkout_id, context_identity=self.runtime.sessions.context_id,
                destination_identity=binding["destination_identity"], reason="Scoped secret sources diverged; no values or hashes are exposed.", options=options)
            self.store.write("secret-decision-" + envelope["decision_id"] + ".json",
                             {"schema_version": 1, "decision_id": envelope["decision_id"], "kind": "reconcile", "plan_id": plan_id})
            return result("secrets reconcile", status="decision_required", decision=envelope,
                          conflicts=plan.public()["conflicts"], plan_id=plan_id, secrets_shared=False)
        return result("secrets reconcile", plan_id=plan_id, secret_plan=plan.public(), secrets_shared=False)

    def _check_sources(self, record, oid, session):
        evidence = record["evidence"]
        binding = self._binding()
        document, baseline, local, remote = self._sources(binding)
        current = self._evidence(oid, session, binding, document, baseline, local, remote)
        if current != evidence:
            self.store.write("secret-plan-" + record["plan_id"] + ".json", dict(record, status="stale"))
            raise RuntimeFailure("Secret observation changed; reconcile again without reusing previous consent.", "decision_required", 3)
        return binding, document, local, remote

    def _read_plan(self, plan_id):
        validate_id(plan_id)
        record = validate_plan_record(self.store.read("secret-plan-" + plan_id + ".json"))
        if record["plan_id"] != plan_id:
            raise RuntimeFailure("Secret plan filename differs from its identity.")
        return record

    def apply_decision(self, decision_id: str, option_id: str) -> dict:
        private = read_private_decision(self.store, decision_id)
        if private["kind"] == "binding":
            return self._apply_binding(decision_id, option_id, private)
        with self.writer() as (oid, records, session):
            self.runtime.decisions.require_pending(decision_id)
            record = self._read_plan(private["plan_id"])
            try:
                binding, document, local, remote = self._check_sources(record, oid, session)
            except RuntimeFailure:
                self.runtime.decisions.finish(decision_id, "stale")
                raise
            envelope, option = self.runtime.decisions.check(decision_id, option_id, oid, session["session_id"],
                checkout_identity=self.runtime.sessions.checkout_id, context_identity=self.runtime.sessions.context_id,
                destination_identity=binding["destination_identity"])
            if option["option_id"] == "abort":
                self.runtime.decisions.finish(decision_id, "aborted")
                self.store.write("secret-plan-" + record["plan_id"] + ".json", dict(record, status="aborted"))
                return result("secrets decision apply", secrets_shared=False)
            if option["option_id"] not in ("keep-local", "use-remote"):
                raise RuntimeFailure("Unsupported secret decision executor.")
            baseline = dict(record["evidence"]["baseline"] or {})
            virtual_local, virtual_remote = dict(local), dict(remote)
            chosen = local if option["option_id"] == "keep-local" else remote
            for key in record["conflicts"]:
                if key in chosen:
                    virtual_local[key] = virtual_remote[key] = chosen[key]
                else:
                    virtual_local.pop(key, None)
                    virtual_remote.pop(key, None)
                    baseline.pop(key, None)
            plan = reconcile(baseline, virtual_local, virtual_remote, self.scope, allow_upload=binding["upload_authorized"])
            if plan.conflicts:
                raise RuntimeFailure("Selected option cannot resolve every scoped conflict.", "decision_required", 3)
            record = dict(record, target=plan.baseline, conflicts={}, decision_id=decision_id)
            report = self._apply_locked(record, oid, session, binding, document, local, remote)
            if report["status"] == "completed":
                self.runtime.decisions.finish(decision_id, "applied")
            return report

    def _apply_binding(self, decision_id, option_id, private):
        self.runtime._context()
        with self.runtime.locked():
            self.runtime.decisions.require_pending(decision_id)
            self.runtime.authority.require_binding(self.store)
            self.runtime.journal.require_clear()
            self.journal.require_clear()
            oid, records = self.runtime._observe()
            owner = records["owner"]["owner"]
            candidate = private["candidate"]
            if (candidate["manifest"] != self.manifest or private["previous_binding"] != self.store.read("secret-binding.json", missing=True)):
                self.runtime.decisions.finish(decision_id, "stale")
                raise RuntimeFailure("Secret binding observation changed; propose a new grant.", "decision_required", 3)
            envelope, option = self.runtime.decisions.check(decision_id, option_id, oid, owner["session_id"] if owner else None,
                checkout_identity=self.runtime.sessions.checkout_id, context_identity=self.runtime.sessions.context_id,
                destination_identity=candidate["destination_identity"])
            if option["option_id"] == "abort":
                self.runtime.decisions.finish(decision_id, "aborted")
                return result("secrets configure", secrets_shared=False)
            if option["option_id"] != "approve-secret-binding":
                raise RuntimeFailure("Invalid secret binding option.")
            approved = {"key_names": sorted(candidate["manifest"]["keys"]), "secret_manifest": candidate["manifest"],
                        "secrets_mode": candidate["mode"], "upload_authorized": candidate["upload_authorized"],
                        "secrets_project": candidate["project"], "secrets_config": candidate["config"]}
            if any(envelope.get(key) != value for key, value in approved.items()):
                self.runtime.decisions.finish(decision_id, "stale")
                raise RuntimeFailure("Private secret candidate differs from the reviewed approval intent.", "decision_required", 3)
            for key, declaration in self.manifest["keys"].items():
                if declaration["classification"] == "file-backed":
                    self._mapped_path(key)
            if candidate["upload_authorized"]:
                if self.adapter is None or self.adapter.destination_identity != candidate["destination_identity"]:
                    raise RuntimeFailure("Secret destination changed.", "decision_required", 3)
                self.adapter.require_write_gate()
            # Keep old baseline material; scope migration needs a separate verified reconciliation.
            self._validate_binding(candidate)
            self.store.write("secret-binding.json", candidate)
            self.runtime.decisions.finish(decision_id, "applied")
            return result("secrets configure", secrets_shared=False, secrets_mode=candidate["mode"],
                          approved_keys=sorted(candidate["manifest"]["keys"]))

    def apply(self, plan_id: str) -> dict:
        with self.writer() as (oid, records, session):
            record = self._read_plan(plan_id)
            if record["status"] != "pending" or record["conflicts"]:
                raise RuntimeFailure("A pending conflict-free secret plan or valid decision is required.", "decision_required", 3)
            binding, document, local, remote = self._check_sources(record, oid, session)
            return self._apply_locked(record, oid, session, binding, document, local, remote)

    def _apply_locked(self, record, oid, session, binding, document, local, remote) -> dict:
        target = record["target"]
        absent = object()
        uploads = {key: target.get(key) for key in sorted(self.scope) if remote.get(key, absent) != target.get(key, absent)}
        downloads = {key: target.get(key) for key in sorted(self.scope) if local.get(key, absent) != target.get(key, absent)}
        lease = None
        if uploads:
            if not binding["upload_authorized"]:
                raise RuntimeFailure("Shared write is outside the persistent approved grant.", "decision_required", 3)
            self.adapter.require_write_gate()
            try:
                lease = self.adapter.lease(repository_identity=self.runtime.authority.identity,
                    publication_ref=self.runtime.authority.publication_ref, session_id=session["session_id"], observed_head=oid)
            except RuntimeFailure as error:
                raise RuntimeFailure("Provider rejected or could not verify the writer lease.", error.status, error.code) from None
            except Exception:
                raise RuntimeFailure("Provider writer lease is unavailable.", "not_ready", 6) from None
        journal = self.journal.create(dict(record["evidence"], target=target, plan_id=record["plan_id"],
                                          decision_id=record.get("decision_id")), uploads, downloads)
        self.runtime.fault("before_secret_key_write")
        try:
            for key, value in uploads.items():
                progress = journal["keys"][key]
                # Basic providers do not fence a request at the server; recheck Git
                # ownership immediately before each send instead.
                if getattr(self.adapter, "basic_writes", False) is True:
                    try:
                        current, records = self.runtime._observe()
                        current_session = self.runtime.sessions.prove(self.runtime.backend, current, records)
                        if current != oid or current_session["session_id"] != session["session_id"]:
                            raise RuntimeFailure("Writer changed before shared upload.", "rejected", 4)
                    except RuntimeFailure:
                        progress["status"] = "rejected"
                        self.journal.write(journal)
                        raise
                progress["status"] = "sending"
                journal["phase"] = "sending"
                self.journal.write(journal)
                try:
                    self.adapter.write_key(key, value, expected=remote.get(key), lease=lease, operation_id=progress["operation_id"])
                except RuntimeFailure:
                    pass
                outcome = self._operation_status(progress["operation_id"])
                if outcome != "published":
                    progress["status"] = "rejected" if outcome == "rejected" else "unknown"
                    raise RuntimeFailure("A scoped shared write is not verified.", "unknown", 5)
                verified = self._remote(binding)
                if verified.get(key, absent) != (absent if value is None else value):
                    progress["status"] = "unknown"
                    raise RuntimeFailure("Shared value changed before verification.", "unknown", 5)
                progress["status"] = "verified"
                self.journal.write(journal)
                self.runtime.fault("after_secret_key_write")
            # Re-read all scope, not only changed keys, before local mutation.
            if self._remote(binding) != target:
                raise RuntimeFailure("Shared scope changed during reconciliation.", "unknown", 5)
            current, records = self.runtime._observe()
            self.runtime.sessions.prove(self.runtime.backend, current, records)
            if current != oid:
                raise RuntimeFailure("Authority changed during secret apply.", "unknown", 5)
            journal["phase"] = "applying_local"
            self.journal.write(journal)
            env_changes = dict(downloads)
            if self.environment.read().raw != document.raw or self._local(document) != local:
                raise RuntimeFailure("Local secret source changed before material apply.", "unknown", 5)
            for key in sorted(self.scope):
                declaration = self.manifest["keys"][key]
                if declaration["classification"] != "file-backed":
                    continue
                if key in downloads:
                    self._material(key).apply(downloads[key], expected=local.get(key), fault=self.runtime.fault)
                pointer = declaration["file"] if key in target else None
                env_changes.pop(key, None)
                if document.values.get(key) != pointer:
                    env_changes[key] = pointer
            if env_changes:
                self.environment.apply(env_changes, expected=document.values, scope=self.scope,
                    approved_deletions={key for key, value in env_changes.items() if value is None}, fault=self.runtime.fault)
            elif self.environment.read().raw != document.raw:
                raise RuntimeFailure("Local secret source changed before convergence.", "unknown", 5)
            journal["local_applied"] = True
            self.journal.write(journal)
            if self._remote(binding) != target:
                raise RuntimeFailure("Shared scope changed after local apply.", "unknown", 5)
            if self._local(self.environment.read()) != target:
                raise RuntimeFailure("Local material changed before baseline verification.", "unknown", 5)
            self.runtime.fault("before_baseline_write")
            self._baseline(binding).verified(target, journal["operation_id"])
            journal.update(phase="resolved", result="verified")
            self.journal.write(journal)
            self.store.write("secret-plan-" + record["plan_id"] + ".json", dict(record, status="applied"))
            return result("secrets apply", secret_operation_id=journal["operation_id"], secrets_shared=True,
                          verified_keys=sorted(target))
        except PublicResultFailure as failure:
            failure.secret_operation_id = journal["operation_id"]
            raise
        except Exception:
            # Baseline remains the last verified full convergence; no rollback/replay.
            statuses = [progress["status"] for progress in journal["keys"].values()]
            if "rejected" in statuses and not any(status in ("verified", "unknown", "sending") for status in statuses):
                journal.update(phase="resolved", result="rejected")
                self.journal.write(journal)
                return result("secrets apply", status="rejected", secret_operation_id=journal["operation_id"], secrets_shared=False)
            journal["result"] = "unknown"
            self.journal.write(journal)
            any_verified = any(progress["status"] == "verified" for progress in journal["keys"].values())
            return result("secrets apply", status="unknown", secret_operation_id=journal["operation_id"],
                          secrets_shared="partial" if any_verified else "unknown",
                          next_action="Resolve per-key private evidence before further synchronization.")

    @contextmanager
    def resolution(self, operation_id):
        runtime = self.runtime
        runtime._context()
        with runtime.locked():
            runtime.authority.require_binding(self.store)
            runtime.journal.require_clear()
            journal = self.journal.read(operation_id)
            evidence = journal["evidence"]
            if (evidence["context_identity"] != runtime.sessions.context_id or
                    evidence["checkout_identity"] != runtime.sessions.checkout_id):
                raise RuntimeFailure("Historical secret operation caller mismatch.", "rejected", 4)
            session = runtime.sessions.historical(evidence["session_id"])
            oid, records = runtime._observe()
            runtime.sessions.verify_history(runtime.backend, oid, session)
            with lock(self.store, "session-" + session["session_id"] + ".lock"), runtime.checkout_lock():
                yield session

    def resolve(self, operation_id: str) -> dict:
        with self.resolution(operation_id) as session:
            journal = self.journal.read(operation_id)
            evidence = journal["evidence"]
            binding = self._binding()
            if (evidence["binding"] != binding or evidence["repository_identity"] != self.runtime.authority.identity or
                    evidence["context_identity"] != self.runtime.sessions.context_id or evidence["session_id"] != session["session_id"]):
                raise RuntimeFailure("Secret operation capability or destination mismatch.", "rejected", 4)
            if journal["phase"] != "resolved" and evidence.get("kind") != "local_patch":
                source_plan = self._read_plan(evidence["plan_id"])
                if source_plan["evidence"] != {key: value for key, value in evidence.items()
                                               if key not in ("target", "plan_id", "decision_id")}:
                    raise RuntimeFailure("Secret operation differs from its original plan observations.")
            if journal["phase"] == "resolved":
                return result("secrets resolve", secret_operation_id=operation_id,
                              secrets_shared=journal.get("resolved_shared", journal["result"] == "verified"),
                              next_action="Recorded result; reconcile fresh state before new synchronization.")
            if evidence.get("kind") == "local_patch":
                converged = self.environment.read().values == evidence["target_env"]
                for key, value in evidence["target_files"].items():
                    converged = self._material(key).read() == value and converged
                journal.update(phase="resolved", result="local_only" if converged else "partial",
                               local_applied=converged, resolved_shared=False)
                self.journal.write(journal)
                return result("secrets resolve", secret_operation_id=operation_id, secrets_shared=False,
                              local_converged=converged,
                              next_action="No local writes replayed; build a fresh patch from current observations.")
            if journal["phase"] == "prepared":
                journal.update(phase="resolved", result="local_only")
                self.journal.write(journal)
                return result("secrets resolve", secret_operation_id=operation_id, secrets_shared=False,
                              next_action="No shared write was sent; reconcile a new observation.")
            target = evidence["target"]
            basic = getattr(self.adapter, "basic_writes", False) is True
            # Basic confirmation observes current values; it does not replay a write
            # or claim a provider-side terminal receipt for a timed-out request.
            remote = self._remote(binding) if basic else None
            absent = object()
            published, rejected = set(), set()
            for key, progress in journal["keys"].items():
                if progress["status"] == "prepared":
                    continue  # Durable evidence proves this key was never sent.
                status = ("published" if progress["status"] == "verified" else
                          "rejected" if progress["status"] == "rejected" else
                          self._operation_status(progress["operation_id"]))
                if basic and status == "unknown":
                    status = "published" if remote.get(key, absent) == target.get(key, absent) else "rejected"
                if status not in {"published", "rejected"}:
                    return result("secrets resolve", status="unknown", secret_operation_id=operation_id, secrets_shared="unknown")
                (published if status == "published" else rejected).add(key)
                progress["status"] = "verified" if status == "published" else "rejected"
                self.journal.write(journal)
            if remote is None:
                remote = self._remote(binding)
            local = self.environment.read()
            if (rejected or remote != target or self._local(local) != target or
                    any(local.values.get(key) != declaration["file"] for key, declaration in self.manifest["keys"].items()
                        if declaration["classification"] == "file-backed" and key in target)):
                shared = "partial" if published else False
                journal.update(phase="resolved", result="partial", resolved_shared=shared)
                self.journal.write(journal)
                plan = self._read_plan(evidence["plan_id"])
                self.store.write("secret-plan-" + plan["plan_id"] + ".json", dict(plan, status="stale"))
                if evidence.get("decision_id"):
                    self.runtime.decisions.finish(evidence["decision_id"], "stale")
                return result("secrets resolve", secret_operation_id=operation_id, secrets_shared=shared,
                              baseline_retained=True, next_action=(
                                  "Current shared state inspected; reconcile fresh observations. No writes were replayed."
                                  if basic else "All sends are terminal; reconcile fresh observations. No writes were replayed."))
            self._baseline(binding).verified(target, operation_id)
            journal.update(phase="resolved", result="verified", local_applied=True)
            self.journal.write(journal)
            plan = self._read_plan(evidence["plan_id"])
            self.store.write("secret-plan-" + plan["plan_id"] + ".json", dict(plan, status="applied"))
            if evidence.get("decision_id"):
                self.runtime.decisions.finish(evidence["decision_id"], "applied")
            return result("secrets resolve", secret_operation_id=operation_id, secrets_shared=True, verified_keys=sorted(target))

    def apply_local(self, private_patch: dict) -> dict:
        with self.writer() as (oid, records, session):
            binding = self._binding()
            if binding["mode"] != "local-env":
                raise RuntimeFailure("Local patch requires explicitly approved local-env mode.", "rejected", 4)
            fields = {"schema_version", "expected", "changes", "approved_deletions"}
            if not isinstance(private_patch, dict) or set(private_patch) not in (fields, fields | {"expected_files", "file_changes"}):
                raise RuntimeFailure("Invalid private local patch record.")
            if (type(private_patch["schema_version"]) is not int or private_patch["schema_version"] != 1 or
                    not isinstance(private_patch["expected"], dict) or not isinstance(private_patch["changes"], dict) or
                    not isinstance(private_patch["approved_deletions"], list) or
                    any(not isinstance(key, str) for key in private_patch["approved_deletions"])):
                raise RuntimeFailure("Invalid private local patch fields.")
            for field in ("expected", "changes"):
                if (any(not isinstance(key, str) or not KEY.fullmatch(key) for key in private_patch[field]) or
                        any(not isinstance(value, str) and not (field == "changes" and value is None)
                            for value in private_patch[field].values())):
                    raise RuntimeFailure("Invalid private local patch values.")
            file_changes = private_patch.get("file_changes", {})
            expected_files = private_patch.get("expected_files", {})
            if (not isinstance(file_changes, dict) or not isinstance(expected_files, dict) or
                    file_changes.keys() != expected_files.keys() or
                    any(not isinstance(key, str) or not KEY.fullmatch(key) for key in file_changes) or
                    any(value is not None and not isinstance(value, str)
                        for values in (file_changes, expected_files) for value in values.values())):
                raise RuntimeFailure("Invalid private file material patch.")
            deletions = private_patch["approved_deletions"]
            selected = dict(private_patch["changes"], **file_changes)
            if (len(set(deletions)) != len(deletions) or
                    any(key not in selected or selected[key] is not None for key in deletions)):
                raise RuntimeFailure("Deletion approval must identify only selected absent targets.")
            scope = {key for key, value in self.manifest["keys"].items() if value["classification"] != "file-backed"}
            return self._apply_local_patch(private_patch, binding, session, scope)

    def _apply_local_patch(self, private_patch, binding, session, scope):
        changes = private_patch["changes"]
        files = private_patch.get("file_changes", {})
        expected_files = private_patch.get("expected_files", {})
        file_scope = set(self.manifest["keys"]) - scope
        deletions = set(private_patch["approved_deletions"])
        if (set(changes) & set(files) or not set(changes) <= scope or not set(files) <= file_scope or
                any(value is None and key not in deletions for key, value in dict(changes, **files).items())):
            raise RuntimeFailure("Local patch exceeds approved scope or deletion intent.", "decision_required", 3)
        document = self.environment.read()
        if document.values != private_patch["expected"]:
            raise RuntimeFailure("Local secret observation changed; rebuild the private patch.", "decision_required", 3)
        env_changes = dict(changes)
        for key, value in files.items():
            material = self._material(key)
            if value is not None:
                validate_pem(value)
            if material.read() != expected_files[key]:
                raise RuntimeFailure("File-backed observation changed.", "decision_required", 3)
            if key in document.values and document.values[key] != material.mapping:
                raise RuntimeFailure("Local file-backed path differs from approved mapping.", "decision_required", 3)
            env_changes[key] = material.mapping if value is not None else None
        target_env = parse_dotenv(patch_dotenv(document, env_changes)).values
        evidence = {"kind": "local_patch", "binding": binding,
                    "repository_identity": self.runtime.authority.identity,
                    "context_identity": self.runtime.sessions.context_id,
                    "checkout_identity": self.runtime.sessions.checkout_id,
                    "session_id": session["session_id"], "target_env": target_env, "target_files": files,
                    "original_env": document.raw, "original_files": expected_files}
        journal = self.journal.create(evidence, {}, {})
        journal["phase"] = "applying_local"
        self.journal.write(journal)
        try:
            self.runtime.fault("before_local_material_apply")
            for key in sorted(files):
                self._material(key).apply(files[key], expected=expected_files[key], fault=self.runtime.fault)
                self.runtime.fault("after_local_material_apply")
            applied = self.environment.apply(env_changes, expected=document.values, scope=set(self.manifest["keys"]),
                approved_deletions=deletions, fault=self.runtime.fault)
            if (self.environment.read().values != target_env or
                    any(self._material(key).read() != value for key, value in files.items())):
                raise RuntimeFailure("Local material convergence is unverified.", "unknown", 5)
            journal.update(phase="resolved", result="local_only", local_applied=True, resolved_shared=False)
            self.journal.write(journal)
            return result("secrets apply", secrets_shared=False, updated_keys=sorted(env_changes),
                          secret_operation_id=journal["operation_id"], preservation_handle=applied["preservation_handle"])
        except PublicResultFailure as failure:
            failure.secret_operation_id = journal["operation_id"]
            raise
        except Exception:
            journal["result"] = "unknown"
            self.journal.write(journal)
            return result("secrets apply", status="unknown", secrets_shared=False,
                          secret_operation_id=journal["operation_id"],
                          next_action="Resolve private local evidence before applying a fresh patch; no rollback was attempted.")

    def synchronize_locked(self, oid, records, session) -> dict:
        self.journal.require_clear()
        binding = self._binding()
        if binding["mode"] == "local-env":
            return self.inspect()
        planned = self._plan_locked(oid, session)
        if planned["status"] != "completed":
            return planned
        record = self._read_plan(planned["plan_id"])
        binding, document, local, remote = self._check_sources(record, oid, session)
        return self._apply_locked(record, oid, session, binding, document, local, remote)
