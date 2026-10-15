"""Durable operation intents and pending-operation barriers."""
from datetime import datetime, timezone
import re
import secrets

from .contracts import RuntimeFailure
from .record_checks import journal as validate_journal


def opaque_id() -> str:
    return secrets.token_hex(16)


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise RuntimeFailure("Invalid opaque identifier.")
    return value


class Journal:
    def __init__(self, store):
        self.store = store

    def prepare(self, authority, operation: str, expected: str | None, commit: str,
                payload: str, session_id: str | None, *, operation_id: str, candidate=None, decision_id=None):
        value = {"schema_version": 1, "operation_id": operation_id, "operation_type": operation,
                 "repository_identity": authority.identity, "publication_ref": authority.publication_ref,
                 "expected_oid": expected, "prepared_commit_oid": commit, "payload_identity": payload,
                 "session_id": session_id, "created_at": datetime.now(timezone.utc).isoformat(),
                 "phase": "prepared", "result": "local_only", "candidate": candidate, "decision_id": decision_id}
        self.write(value)
        return value

    def write(self, record: dict):
        validate_journal(record)
        validate_id(record["operation_id"])
        self.store.write("operation-" + record["operation_id"] + ".json", record)

    def read(self, operation_id: str) -> dict:
        record = validate_journal(self.store.read("operation-" + validate_id(operation_id) + ".json"))
        if record["operation_id"] != operation_id:
            raise RuntimeFailure("Private journal filename differs from operation identity.")
        return record

    def pending(self) -> list[dict]:
        return [record for name in self.store.names() if name.startswith("operation-") and name.endswith(".json")
                if (record := self.read(name[len("operation-"):-len(".json")]))["phase"] != "resolved"]

    def require_clear(self):
        if self.pending():
            raise RuntimeFailure("Resolve pending publication operations before mutation.", "unknown", 5)
