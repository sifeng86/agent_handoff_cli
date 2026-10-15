"""Immutable control-plane decisions with private lifecycle and exact observations."""
from .contracts import RuntimeFailure
from .journal import opaque_id, validate_id
from .record_checks import DECISION_CATEGORIES, SECRET_DETAILS, decision, decision_state


CATEGORIES = DECISION_CATEGORIES


class Decisions:
    def __init__(self, store, authority):
        self.store = store
        self.authority = authority

    def create(self, category: str, oid: str | None, owner: str | None, *, checkout_identity: str,
               context_identity: str, reason: str, options: list[dict], destination_identity: str | None = None,
               secrets_details: dict | None = None) -> dict:
        if not isinstance(category, str) or category not in CATEGORIES:
            raise RuntimeFailure("Project semantic decisions are outside runtime authority.")
        decision_id = opaque_id()
        envelope = {"schema_version": 1, "decision_id": decision_id, "category": category,
                    "repository_identity": self.authority.identity, "publication_ref": self.authority.publication_ref,
                    "observed_head": oid, "observed_owner_session": owner,
                    "destination_identity": self.authority.identity if destination_identity is None else destination_identity,
                    "reason": reason, "options": options,
                    "invalidated_by": ["authority_head_change", "owner_change", "destination_change", "relevant_secret_state_change"]}
        if secrets_details is not None:
            if not isinstance(secrets_details, dict) or set(secrets_details) != SECRET_DETAILS:
                raise RuntimeFailure("Invalid secret approval details.")
            envelope.update(secrets_details)
        decision(envelope)
        lifecycle = decision_state({"schema_version": 1, "status": "pending", "checkout_identity": checkout_identity,
                                    "context_identity": context_identity})
        self.store.write("decision-" + decision_id + ".json", envelope)
        self.store.write("decision-state-" + decision_id + ".json", lifecycle)
        return envelope

    def read(self, decision_id):
        envelope = decision(self.store.read("decision-" + validate_id(decision_id) + ".json"))
        if envelope["decision_id"] != decision_id:
            raise RuntimeFailure("Decision filename differs from its identity.")
        return envelope

    def check(self, decision_id: str, option_id: str, oid: str | None, owner: str | None,
              *, checkout_identity: str, context_identity: str, destination_identity: str | None = None) -> tuple[dict, dict]:
        validate_id(decision_id)
        envelope = self.read(decision_id)
        lifecycle = self.require_pending(decision_id)
        if (envelope["observed_head"] != oid or envelope["observed_owner_session"] != owner or
                envelope["repository_identity"] != self.authority.identity or
                envelope["destination_identity"] != (self.authority.identity if destination_identity is None else destination_identity) or
                envelope["publication_ref"] != self.authority.publication_ref or
                lifecycle["checkout_identity"] != checkout_identity or lifecycle["context_identity"] != context_identity):
            self.finish(decision_id, "stale")
            raise RuntimeFailure("Decision observation changed; request a fresh decision.", "decision_required", 3)
        options = [option for option in envelope["options"] if option["option_id"] == option_id]
        if len(options) != 1:
            raise RuntimeFailure("Invalid decision option.")
        return envelope, options[0]

    def require_pending(self, decision_id: str) -> dict:
        self.read(decision_id)
        lifecycle = decision_state(self.store.read("decision-state-" + validate_id(decision_id) + ".json"))
        if lifecycle["status"] != "pending":
            raise RuntimeFailure("Decision is no longer pending.", "rejected", 4)
        return lifecycle

    def finish(self, decision_id: str, status: str):
        if status not in {"applied", "aborted", "stale"}:
            raise RuntimeFailure("Invalid terminal decision status.")
        self.read(decision_id)
        lifecycle = decision_state(self.store.read("decision-state-" + validate_id(decision_id) + ".json"))
        if lifecycle["status"] == status:
            return
        if lifecycle["status"] != "pending":
            raise RuntimeFailure("Terminal decision status cannot be changed.", "rejected", 4)
        self.store.write("decision-state-" + decision_id + ".json", dict(lifecycle, status=status))
