"""Stable JSON public result records."""
from .public_results import validate_public_result


def result(command: str, **fields) -> dict:
    value = {"schema_version": 1, "command": command, "status": "completed", "operation_id": None,
             "published_revision": None, "code_publication": "not_attempted", "ownership": "unknown",
             "secrets_shared": None, "decision": None, "next_action": None}
    value.update(fields)
    return validate_public_result(value)


def exit_code(value: dict) -> int:
    return {"completed": 0, "ready": 0, "not_ready": 6, "decision_required": 3,
            "rejected": 4, "unknown": 5, "invalid": 7, "unsupported": 8}[value["status"]]
