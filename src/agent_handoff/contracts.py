"""Strict versioned control records and safe public errors."""
import json
import re


class RuntimeFailure(Exception):
    def __init__(self, message: str, status: str = "invalid", code: int = 2):
        super().__init__(message)
        self.status = status
        self.code = code


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeFailure("Duplicate JSON key.")
        result[key] = value
    return result


def parse_record(raw: str, kind: str) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               RuntimeFailure("Invalid JSON constant.")))
    except (ValueError, RecursionError):
        raise RuntimeFailure("Invalid control record JSON.") from None
    fields = {
        "protocol": {"schema_version", "protocol_version", "required_features"},
        "owner": {"schema_version", "owner"},
        "state": {"schema_version", "status", "active_plan", "current_step", "blocker", "summary"},
    }
    required = fields[kind]
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - {"extensions"}:
        raise RuntimeFailure("Invalid control record fields.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RuntimeFailure("Unsupported schema version.", "unsupported", 8)
    if "extensions" in value and not isinstance(value["extensions"], dict):
        raise RuntimeFailure("Invalid extensions namespace.")
    if kind == "protocol":
        features = value["required_features"]
        if not isinstance(features, list) or any(not isinstance(f, str) for f in features):
            raise RuntimeFailure("Invalid required features.")
        if value["protocol_version"] != "1.2" or features:
            raise RuntimeFailure("Unsupported protocol or required feature.", "unsupported", 8)
    elif kind == "owner" and value["owner"] is not None:
        owner = value["owner"]
        names = {"session_id", "acquisition_operation_id", "machine_label", "provider_label"}
        if not isinstance(owner, dict) or set(owner) != names or any(
            not isinstance(owner[k], str) or not owner[k] for k in names
        ):
            raise RuntimeFailure("Invalid owner record.")
        if any(not re.fullmatch(r"[0-9a-f]{32}", owner[key]) for key in ("session_id", "acquisition_operation_id")):
            raise RuntimeFailure("Owner identifiers must be opaque 128-bit IDs.")
    elif kind == "state":
        if value["status"] not in ("idle", "working", "blocked") or not isinstance(value["summary"], str):
            raise RuntimeFailure("Invalid state record.")
        for key in ("active_plan", "current_step"):
            if value[key] is not None and (not isinstance(value[key], str) or not value[key]):
                raise RuntimeFailure("Invalid plan or step reference.")
        if value["blocker"] is not None:
            blocker = value["blocker"]
            if (not isinstance(blocker, dict) or set(blocker) != {"kind", "summary"} or
                    blocker["kind"] != "advisory" or not isinstance(blocker["summary"], str)):
                raise RuntimeFailure("Invalid advisory blocker record.")
    return value
