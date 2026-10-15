"""Strict private control records; validation never emits record contents."""
from datetime import datetime
import re

from .contracts import RuntimeFailure


ID = r"[0-9a-f]{32}"
IDENTITY = r"[0-9a-f]{64}"
OID = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
SESSION_FIELDS = {"schema_version", "protocol_version", "repository_identity", "publication_ref", "session_id",
                  "acquisition_operation_id", "acquisition_oid", "checkout_identity", "context_identity",
                  "machine_label", "provider_label", "revoked"}
JOURNAL_FIELDS = {"schema_version", "operation_id", "operation_type", "repository_identity", "publication_ref",
                  "expected_oid", "prepared_commit_oid", "payload_identity", "session_id", "created_at",
                  "phase", "result", "candidate", "decision_id"}
DECISION_CATEGORIES = {"takeover", "publication_integrity", "authority_binding", "secret_conflict", "secret_deletion"}
DECISION_FIELDS = {"schema_version", "decision_id", "category", "repository_identity", "publication_ref",
                   "observed_head", "observed_owner_session", "destination_identity", "reason", "options", "invalidated_by"}
SECRET_DETAILS = {"key_names", "secrets_mode", "upload_authorized", "secret_manifest", "secrets_project", "secrets_config"}


def decision(value):
    if not isinstance(value, dict):
        raise RuntimeFailure("Invalid decision envelope.")
    details = SECRET_DETAILS if SECRET_DETAILS & value.keys() else set()
    shape(value, DECISION_FIELDS | details)
    if (not isinstance(value["category"], str) or value["category"] not in DECISION_CATEGORIES or not match(value["decision_id"], ID) or
            not match(value["repository_identity"], IDENTITY) or not branch(value["publication_ref"]) or
            not match(value["observed_head"], OID, nullable=True) or
            not match(value["observed_owner_session"], ID, nullable=True) or
            not match(value["destination_identity"], IDENTITY) or
            not isinstance(value["reason"], str) or not value["reason"].strip()):
        raise RuntimeFailure("Invalid decision observation.")
    options = value["options"]
    if not isinstance(options, list) or not options:
        raise RuntimeFailure("Decision requires explicit options.")
    ids = set()
    for option in options:
        if (not isinstance(option, dict) or set(option) != {"option_id", "effect"} or
                not match(option["option_id"], r"[a-z][a-z0-9-]*") or
                not isinstance(option["effect"], str) or not option["effect"].strip() or option["option_id"] in ids):
            raise RuntimeFailure("Invalid or duplicated decision option.")
        ids.add(option["option_id"])
    if value["invalidated_by"] != ["authority_head_change", "owner_change", "destination_change", "relevant_secret_state_change"]:
        raise RuntimeFailure("Invalid decision invalidation rules.")
    if details:
        from .secrets.manifest import parse
        from .secrets.adapter import destination_identity
        import json
        manifest = parse(json.dumps(value["secret_manifest"]))
        if (value["category"] != "authority_binding" or value["key_names"] != sorted(manifest["keys"]) or
                value["secrets_mode"] not in ("local-env", "doppler") or type(value["upload_authorized"]) is not bool or
                (value["secrets_mode"] == "local-env" and value["upload_authorized"])):
            raise RuntimeFailure("Invalid public secret approval intent.")
        if value["destination_identity"] != destination_identity(value["secrets_mode"], value["secrets_project"], value["secrets_config"]):
            raise RuntimeFailure("Public secret destination differs from approval identity.")
    return value


def decision_state(value):
    shape(value, {"schema_version", "status", "checkout_identity", "context_identity"})
    if (value["status"] not in ("pending", "applied", "aborted", "stale") or
            not match(value["checkout_identity"], IDENTITY) or not match(value["context_identity"], IDENTITY)):
        raise RuntimeFailure("Invalid decision lifecycle record.")
    return value


def config(value):
    shape(value, {"schema_version", "authority", "publication_ref", "mainline_ref", "plan_directory"})
    if (not isinstance(value["authority"], str) or not branch(value["publication_ref"]) or
            not branch(value["mainline_ref"]) or value["publication_ref"] == value["mainline_ref"]):
        raise RuntimeFailure("Invalid authority configuration.")
    from .git_backend import validate_destination
    validate_destination(value["authority"])
    if value["plan_directory"] != "plans":
        raise RuntimeFailure("Custom Plan directories are not supported.", "unsupported", 8)
    return value


def match(value, pattern, *, nullable=False):
    return (nullable and value is None) or (isinstance(value, str) and re.fullmatch(pattern, value) is not None)


def shape(value, fields):
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeFailure("Invalid private control record fields.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RuntimeFailure("Unsupported private control record version.", "unsupported", 8)


def branch(value):
    return (isinstance(value, str) and value.startswith("refs/heads/") and len(value) > len("refs/heads/") and
            not any(ord(character) < 32 or ord(character) == 127 for character in value))


def session(value, *, active=False):
    shape(value, SESSION_FIELDS)
    if value["protocol_version"] != "1.2":
        raise RuntimeFailure("Unsupported private session protocol.", "unsupported", 8)
    valid = (all(match(value[key], ID) for key in ("session_id", "acquisition_operation_id")) and
             all(match(value[key], IDENTITY) for key in ("repository_identity", "checkout_identity", "context_identity")) and
             match(value["acquisition_oid"], OID, nullable=not active) and branch(value["publication_ref"]) and
             type(value["revoked"]) is bool and
             all(isinstance(value[key], str) and value[key] for key in ("machine_label", "provider_label")))
    if not valid:
        raise RuntimeFailure("Invalid private session record.")
    return value


def journal(value):
    shape(value, JOURNAL_FIELDS)
    if (not match(value["operation_id"], ID) or not match(value["repository_identity"], IDENTITY) or
            not match(value["expected_oid"], OID, nullable=True) or not match(value["prepared_commit_oid"], OID) or
            not match(value["payload_identity"], OID) or not match(value["session_id"], ID, nullable=True) or
            not match(value["decision_id"], ID, nullable=True) or not branch(value["publication_ref"]) or
            value["operation_type"] not in ("init", "acquire", "takeover", "checkpoint", "handoff", "done", "release") or
            value["phase"] not in ("prepared", "sending", "verifying", "resolved") or
            value["result"] not in ("local_only", "published", "rejected", "publication_unknown")):
        raise RuntimeFailure("Invalid private Git journal record.")
    try:
        if not match(value["created_at"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"):
            raise ValueError()
        timestamp = datetime.fromisoformat(value["created_at"])
        if timestamp.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeFailure("Invalid diagnostic journal timestamp.") from None
    if value["phase"] == "resolved" and value["result"] == "publication_unknown":
        raise RuntimeFailure("Unknown publication cannot have a terminal journal phase.")
    candidate = value["candidate"]
    if candidate is not None:
        session(candidate)
        if (candidate["session_id"] != value["session_id"] or
                candidate["acquisition_operation_id"] != value["operation_id"] or
                candidate["repository_identity"] != value["repository_identity"] or
                candidate["publication_ref"] != value["publication_ref"] or candidate["revoked"] or
                candidate["acquisition_oid"] not in (None, value["prepared_commit_oid"])):
            raise RuntimeFailure("Private acquisition candidate differs from journal intent.")
    acquiring = value["operation_type"] in ("acquire", "takeover")
    if acquiring != (candidate is not None):
        raise RuntimeFailure("Acquisition journal requires exactly its private candidate.")
    if value["operation_type"] == "init":
        if value["expected_oid"] is not None or value["session_id"] is not None:
            raise RuntimeFailure("Initialization requires expected-absent intent.")
    elif value["expected_oid"] is None or value["session_id"] is None:
        raise RuntimeFailure("Mutation journal requires an expected revision and session.")
    return value
