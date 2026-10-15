"""Private capability proof, explicit caller context, persistent checkout binding."""
import hashlib
from pathlib import Path

from .contracts import RuntimeFailure
from .journal import opaque_id
from .record_checks import session as validate_session


def checkout_identity(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()


def context_identity(context: str) -> str:
    if not isinstance(context, str) or len(context) < 32:
        raise RuntimeFailure("An explicit private runtime context of at least 128 bits is required.")
    return hashlib.sha256(context.encode()).hexdigest()


class Sessions:
    def __init__(self, store, authority, checkout: Path, context: str):
        self.store = store
        self.authority = authority
        self.checkout_id = checkout_identity(checkout)
        self.context_id = context_identity(context)

    def candidate(self, operation_id: str, *, machine_label="local", provider_label="agent") -> dict:
        return {"schema_version": 1, "protocol_version": "1.2", "repository_identity": self.authority.identity,
                "publication_ref": self.authority.publication_ref, "session_id": opaque_id(),
                "acquisition_operation_id": operation_id, "acquisition_oid": None,
                "checkout_identity": self.checkout_id, "context_identity": self.context_id,
                "machine_label": machine_label, "provider_label": provider_label, "revoked": False}

    def activate(self, candidate: dict, oid: str):
        candidate = dict(candidate, acquisition_oid=oid)
        validate_session(candidate, active=True)
        self.store.write("session-" + candidate["session_id"] + ".json", candidate)
        self.store.write("checkout-binding.json", {"schema_version": 1, "session_id": candidate["session_id"],
                                                   "checkout_identity": candidate["checkout_identity"],
                                                   "context_identity": candidate["context_identity"]})

    def current(self) -> dict | None:
        binding = self.store.read("checkout-binding.json", missing=True)
        if binding is None:
            return None
        if binding.get("checkout_identity") != self.checkout_id or binding.get("context_identity") != self.context_id:
            raise RuntimeFailure("Another checkout or process context holds the session capability.", "rejected", 4)
        return self.historical(binding["session_id"], allow_revoked=False)

    def historical(self, session_id: str, *, allow_revoked=True) -> dict:
        """Read private historical proof; this never grants current ownership."""
        from .journal import validate_id
        session = self.store.read("session-" + validate_id(session_id) + ".json", missing=True)
        if session is None:
            raise RuntimeFailure("Local session capability is missing or unprovable.", "rejected", 4)
        validate_session(session, active=True)
        if session["revoked"] and not allow_revoked:
            raise RuntimeFailure("Local session capability is invalid or revoked.", "rejected", 4)
        for key, expected in (("repository_identity", self.authority.identity), ("publication_ref", self.authority.publication_ref),
                              ("checkout_identity", self.checkout_id), ("context_identity", self.context_id),
                              ("session_id", session_id)):
            if session[key] != expected:
                raise RuntimeFailure("Local capability binding mismatch.", "rejected", 4)
        return session

    def prove(self, backend, oid: str, records: dict) -> dict:
        session = self.current()
        owner = records["owner"]["owner"]
        if session is None or owner is None or owner["session_id"] != session["session_id"]:
            raise RuntimeFailure("Session is unprovable or fenced by the current owner.", "rejected", 4)
        if owner["acquisition_operation_id"] != session["acquisition_operation_id"]:
            raise RuntimeFailure("Session acquisition history cannot be verified.", "rejected", 4)
        self.verify_history(backend, oid, session)
        return session

    def verify_history(self, backend, oid: str, session: dict):
        if not backend.ancestor(session["acquisition_oid"], oid):
            raise RuntimeFailure("Session acquisition history cannot be verified.", "rejected", 4)
        message = backend.text("show", "-s", "--format=%B", session["acquisition_oid"])
        if f"Agent-Operation-Id: {session['acquisition_operation_id']}" not in message.splitlines():
            raise RuntimeFailure("Session acquisition metadata is invalid.", "rejected", 4)

    def revoke(self, session: dict):
        self.store.write("session-" + session["session_id"] + ".json", dict(session, revoked=True))
        # Binding remains as evidence; a future acquisition explicitly replaces it.
