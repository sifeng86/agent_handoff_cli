"""Explicit two-authority consent; local selection only, no remote mutation."""
from contextlib import ExitStack, contextmanager

from .contracts import RuntimeFailure
from .decisions import Decisions
from .integrity import validate
from .integrity_decisions import digest
from .journal import validate_id
from .locks import lock
from .output import result
from .preservation import preserve, read_record
from .record_checks import shape, match, OID, ID, IDENTITY, decision_state, session as validate_session
from .secrets.journal import SecretJournal
from .secrets.binding import validate as validate_secret_binding
from .authority import Authority
from .paths import PrivateStore


def transition_options(authority, observed):
    return [{"option_id": "switch", "effect":
             f"Select {authority.destination} {authority.publication_ref}; reviewed HEAD {observed['head']} "
             f"owner {observed['owner']}. Do not migrate sessions or secret grants."},
            {"option_id": "abort", "effect": "Retain the current authority selection and all local work."}]


def read_transition_evidence(store, checkout, source, target, decision_id):
    validate_id(decision_id)
    envelope = Decisions(store, source).read(decision_id)
    value = store.read("authority-decision-" + decision_id + ".json")
    shape(value, {"schema_version", "decision_id", "source_identity", "target_identity", "source", "target", "preservation_handle", "target_destination", "target_publication_ref"})
    for side in ("source", "target"):
        observation = value[side]
        if not isinstance(observation, dict) or set(observation) != {"head", "owner", "dependency_digest"}:
            raise RuntimeFailure("Invalid authority transition observation.")
        if (not match(observation["head"], OID, nullable=side == "target") or
                not match(observation["owner"], ID, nullable=True) or
                not match(observation["dependency_digest"], IDENTITY) or
                (observation["head"] is None and observation["owner"] is not None)):
            raise RuntimeFailure("Invalid authority transition evidence.")
    if (value["decision_id"] != decision_id or
            value["target_destination"] != target.destination or value["target_publication_ref"] != target.publication_ref or
            envelope["repository_identity"] != source.identity or envelope["publication_ref"] != source.publication_ref or
            value["source_identity"] != source.identity or
            value["target_identity"] != target.identity or envelope["category"] != "authority_binding" or
            envelope["destination_identity"] != target.identity or
            envelope["observed_head"] != value["source"]["head"] or
            envelope["observed_owner_session"] != value["source"]["owner"] or
            envelope["options"] != transition_options(target, value["target"])):
        raise RuntimeFailure("Authority transition evidence differs from reviewed intent.")
    read_record(store, value["preservation_handle"], checkout=checkout)
    return value


def finalize_in_store(store, checkout, journal, outcome):
    """Validate linked consent before closing the transition barrier."""
    binding = store.read("bindings.json")
    shape(binding, {"schema_version", "repository_identity", "destination", "publication_ref"})
    source = Authority(binding["destination"], binding["publication_ref"])
    source.require_binding(store)
    if source.identity != journal["before"]["repository_identity"]:
        raise RuntimeFailure("Transition source binding differs from durable intent.")
    decision_id = journal["decision_id"]
    value = store.read("authority-decision-" + validate_id(decision_id) + ".json")
    if not isinstance(value.get("target_destination"), str) or not isinstance(value.get("target_publication_ref"), str):
        raise RuntimeFailure("Transition target descriptor is missing.")
    target = Authority(value["target_destination"], value["target_publication_ref"])
    if target.identity != journal["after"]["repository_identity"]:
        raise RuntimeFailure("Transition target differs from durable intent.")
    read_transition_evidence(store, checkout, source, target, decision_id)
    lifecycle = decision_state(store.read("decision-state-" + decision_id + ".json"))
    if any(lifecycle[field] != journal["before"][field] for field in ("checkout_identity", "context_identity")):
        raise RuntimeFailure("Transition decision caller differs from durable intent.")
    Decisions(store, source).finish(decision_id, "applied" if outcome == "selected" else "stale")


def finalize_recovery(private_root, checkout, journal, outcome):
    """Called under selection lock; no Git backend or remote access."""
    with PrivateStore(private_root / "repos" / journal["before"]["repository_identity"], checkout) as store:
        with lock(store, "repository.lock"):
            finalize_in_store(store, checkout, journal, outcome)


class AuthorityTransition:
    def __init__(self, source, target):
        source._context()
        target._context()
        if (source.authority.identity == target.authority.identity or
                source.checkout != target.checkout or
                source.sessions.context_id != target.sessions.context_id or
                source.selection_store.root != target.selection_store.root):
            raise RuntimeFailure("Authority transition requires distinct authorities and the same private caller/checkout.")
        self.source, self.target = source, target

    @contextmanager
    def locked(self):
        with ExitStack() as stack:
            stack.enter_context(self.source.selection.locked())
            for runtime in sorted((self.source, self.target), key=lambda item: item.authority.identity):
                stack.enter_context(lock(runtime.store, "repository.lock"))
            stack.enter_context(self.source.checkout_lock())
            self.source.selection.require(self.source.authority.identity)
            yield

    def _observe(self, runtime, *, target=False):
        binding = runtime.store.read("bindings.json", missing=True)
        if binding is not None or not target:
            runtime.authority.require_binding(runtime.store)
        runtime.journal.require_clear()
        SecretJournal(runtime.store).require_clear()
        secret_binding = runtime.store.read("secret-binding.json", missing=True)
        if secret_binding is not None:
            validate_secret_binding(secret_binding)
            if secret_binding["repository_identity"] != runtime.authority.identity:
                raise RuntimeFailure("Secret binding belongs to a different authority.")
        for name in runtime.store.names():
            if name.startswith("session-") and name.endswith(".json"):
                session = validate_session(runtime.store.read(name), active=True)
                if name != "session-" + session["session_id"] + ".json":
                    raise RuntimeFailure("Session evidence identity differs from filename.")
                if session["checkout_identity"] == runtime.sessions.checkout_id and not session["revoked"]:
                    raise RuntimeFailure("Release existing checkout capability before authority transition.", "rejected", 4)
        oid = runtime.backend.observe()
        owner = None
        if oid is None:
            if not target or binding is not None or runtime.store.read("integrity.json", missing=True) is not None:
                raise RuntimeFailure("Registered authority is missing.", "invalid", 7)
        else:
            records = validate(runtime.backend, runtime.store, oid, initial=target and binding is None)
            runtime._reject_mapped_material(oid)
            owner = records["owner"]["owner"]
            owner = owner["session_id"] if owner else None
        dependencies = {name: runtime.store.read(name, missing=True)
                        for name in ("bindings.json", "integrity.json", "secret-binding.json")}
        return {"head": oid, "owner": owner, "dependency_digest": digest(dependencies)}

    def _options(self, observed):
        return transition_options(self.target.authority, observed)

    def propose(self):
        with self.locked():
            source = self._observe(self.source)
            target = self._observe(self.target, target=True)
            preservation = preserve(self.source.checkout, self.source.store)
            envelope = self.source.decisions.create("authority_binding", source["head"], source["owner"],
                checkout_identity=self.source.sessions.checkout_id, context_identity=self.source.sessions.context_id,
                destination_identity=self.target.authority.identity,
                reason="Explicit authority selection; no remote publication or ownership transfer.",
                options=self._options(target))
            self.source.store.write("authority-decision-" + envelope["decision_id"] + ".json",
                {"schema_version": 1, "decision_id": envelope["decision_id"],
                 "source_identity": self.source.authority.identity, "target_identity": self.target.authority.identity,
                 "target_destination": self.target.authority.destination,
                 "target_publication_ref": self.target.authority.publication_ref,
                 "source": source, "target": target, "preservation_handle": preservation["handle_id"]})
            return result("authority propose", status="decision_required", decision=envelope,
                          preservation_handle=preservation["handle_id"])

    def _read(self, decision_id):
        return read_transition_evidence(self.source.store, self.source.checkout,
                                        self.source.authority, self.target.authority, decision_id)

    def apply(self, decision_id, option_id):
        with self.locked():
            evidence = self._read(decision_id)
            self.source.decisions.require_pending(decision_id)
            try:
                source, target = self._observe(self.source), self._observe(self.target, target=True)
            except RuntimeFailure:
                self.source.decisions.finish(decision_id, "stale")
                raise
            if evidence["source"] != source or evidence["target"] != target:
                self.source.decisions.finish(decision_id, "stale")
                raise RuntimeFailure("Authority transition observations changed; propose a fresh decision.", "decision_required", 3)
            self.source.decisions.check(decision_id, option_id, source["head"], source["owner"],
                checkout_identity=self.source.sessions.checkout_id, context_identity=self.source.sessions.context_id,
                destination_identity=self.target.authority.identity)
            if option_id == "abort":
                self.source.decisions.finish(decision_id, "aborted")
                return result("authority apply")
            committed = self.source.selection.switch(self.source.authority.identity, self.target.authority.identity,
                decision_id, fault=self.source.fault,
                finalize=lambda journal, outcome: finalize_in_store(self.source.store, self.source.checkout, journal, outcome))
            return result("authority apply", transition_id=committed["transition_id"], transition_outcome="selected",
                          selected_authority_identity=self.target.authority.identity)
