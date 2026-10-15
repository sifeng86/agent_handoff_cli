"""Stdlib public-result boundaries; host outputs contain summaries only."""
import re
from pathlib import Path

from .contracts import RuntimeFailure
from .record_checks import decision as check_decision, DECISION_FIELDS


class PublicResultFailure(RuntimeFailure):
    """Unsafe output is distinct from rejected input or transport failure."""


def validate_decision(value):
    try:
        return check_decision(value)
    except RuntimeFailure:
        raise PublicResultFailure("Invalid public decision envelope.", "invalid", 7) from None


BASE = {"schema_version", "command", "status", "operation_id", "published_revision", "code_publication",
        "ownership", "secrets_shared", "decision", "next_action"}
STATUS = {"completed", "not_ready", "decision_required", "rejected", "unknown", "invalid", "unsupported"}
CATEGORIES = {"ssh", "git", "provider", "shell", "network", "vpn", "firewall", "users_security", "package_sources", "services"}
HOST_FIELDS = {
    "host": set(),
    "host inspect": {"preservation_handle", "inspected_categories", "unverifiable_categories", "host_changes_attempted", "scope"},
    "host verify": {"preservation_handle", "preserved_categories", "changed_categories", "unverifiable_categories", "host_changes_attempted", "scope"},
    "host backup": {"backup_handle", "backup_verified"},
    "host verify-backup": {"backup_handle", "backup_verified"},
    "host restore": {"backup_handle", "recovery_outcome"},
    "host resolve": {"backup_handle", "recovery_outcome"},
}
AUTHORITY_FIELDS = {
    "authority": set(),
    "authority propose": {"preservation_handle"},
    "authority apply": {"transition_id", "transition_outcome", "selected_authority_identity"},
    "authority resolve": {"transition_id", "transition_outcome", "selected_authority_identity"},
}
INFO_FIELDS = {"inspect": {"inspection"}, "status": {"inspection"}, "context": set(), "context create": {"context_file"}}
GIT_FIELDS = {
    "init": {"observed_revision"}, "acquire": {"preservation_handle"}, "takeover": set(),
    "release": set(), "recover-session": {"observed_revision"}, "operation": set(),
    "checkpoint": {"preservation_handle", "test_status"},
    "done": {"preservation_handle", "test_status"},
    "handoff": {"preservation_handle", "test_status", "secrets_result"},
}
SECRET_FIELDS = {
    "secrets": set(), "secrets configure": {"secrets_mode", "approved_keys"},
    "secrets inspect": {"availability", "secrets_mode", "destination_identity", "shared_read", "shared_read_failure", "local_work_available", "shared_available_keys"},
    "secrets verify": {"availability", "secrets_mode", "destination_identity", "shared_read", "shared_read_failure", "local_work_available", "shared_available_keys", "convergence"},
    "secrets reconcile": {"plan_id", "secret_plan", "conflicts"},
    "secrets apply": {"secret_operation_id", "verified_keys", "updated_keys", "preservation_handle"},
    "secrets resolve": {"secret_operation_id", "verified_keys", "local_converged", "baseline_retained"},
    "secrets decision apply": set(),
}
CONTROL_COMMANDS = {"decision", "decision propose-integrity", "decision apply"}
ERROR_COMMANDS = {"unknown", "recover-ownership"}
REGISTERED_COMMANDS = set(HOST_FIELDS) | set(AUTHORITY_FIELDS) | set(INFO_FIELDS) | set(GIT_FIELDS) | set(SECRET_FIELDS) | CONTROL_COMMANDS | ERROR_COMMANDS | {"resume"}


def identifier(value, *, nullable=False):
    return value is None and nullable or isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def local_control_base(value, *, allow_decision=False, allowed_ownership=("unknown",)):
    return (BASE <= value.keys() and type(value["schema_version"]) is int and value["schema_version"] == 1 and
            isinstance(value["status"], str) and value["status"] in STATUS and
            identifier(value["operation_id"], nullable=True) and value["published_revision"] is None and
            value["secrets_shared"] is None and (allow_decision or value["decision"] is None) and
            value["code_publication"] == "not_attempted" and value["ownership"] in allowed_ownership and
            (value["next_action"] is None or isinstance(value["next_action"], str)))


def validate_host_result(value, *, require_host=False):
    if not isinstance(value, dict):
        raise PublicResultFailure("Public result must be a structured object.", "invalid", 7)
    command = value.get("command")
    if not isinstance(command, str) or not (command == "host" or command.startswith("host ")):
        if require_host:
            raise PublicResultFailure("Host result command identity changed.", "invalid", 7)
        return value
    def reject():
        raise PublicResultFailure("Host public result violates its approved contract.", "invalid", 7)
    if (command not in HOST_FIELDS or not value.keys() <= BASE | HOST_FIELDS[command] or
            not local_control_base(value)):
        reject()
    for field in HOST_FIELDS[command] & value.keys():
        item = value[field]
        if field.endswith("_handle"):
            valid = identifier(item)
        elif field.endswith("_categories"):
            valid = (isinstance(item, list) and all(isinstance(label, str) and label in CATEGORIES for label in item)
                     and len(item) == len(set(item)))
        elif field == "host_changes_attempted":
            valid = item is False
        elif field == "scope":
            valid = item == "selected_linux_configuration_entries"
        elif field == "backup_verified":
            valid = item is True
        else:
            valid = item in ("restored", "not_restored")
        if not valid:
            reject()
    if command not in ("host restore", "host resolve") and value["operation_id"] is not None:
        reject()
    if value["status"] == "completed":
        if command == "host" or not HOST_FIELDS[command] <= value.keys():
            reject()
        if command in ("host restore", "host resolve") and value["operation_id"] is None:
            reject()
        if command == "host verify" and (value["changed_categories"] or value["unverifiable_categories"]):
            reject()
    if command == "host inspect" and "inspected_categories" in value and "unverifiable_categories" in value:
        if not set(value["unverifiable_categories"]) <= set(value["inspected_categories"]):
            reject()
    if command == "host verify" and "preserved_categories" in value:
        if set(value["preserved_categories"]) & set(value.get("changed_categories", []) + value.get("unverifiable_categories", [])):
            reject()
    return value


def validate_authority_result(value, *, require_authority=False):
    if not isinstance(value, dict):
        raise PublicResultFailure("Public result must be a structured object.", "invalid", 7)
    command = value.get("command")
    if not isinstance(command, str) or not (command == "authority" or command.startswith("authority ")):
        if require_authority:
            raise PublicResultFailure("Authority result command identity changed.", "invalid", 7)
        return value
    def reject():
        raise PublicResultFailure("Authority public result violates its approved contract.", "invalid", 7)
    if (command not in AUTHORITY_FIELDS or not value.keys() <= BASE | AUTHORITY_FIELDS[command] or
            not local_control_base(value, allow_decision=command == "authority propose") or
            value["operation_id"] is not None):
        reject()
    extras = AUTHORITY_FIELDS[command] & value.keys()
    for field in extras:
        item = value[field]
        if field in ("preservation_handle", "transition_id"):
            valid = identifier(item)
        elif field == "selected_authority_identity":
            valid = isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) is not None
        else:
            valid = item in ("selected", "not_selected") and (command != "authority apply" or item == "selected")
        if not valid:
            reject()
    if command == "authority propose":
        envelope = value["decision"]
        if value["status"] == "decision_required":
            if not isinstance(envelope, dict) or set(envelope) != DECISION_FIELDS or "preservation_handle" not in value:
                reject()
            try:
                validate_decision(envelope)
            except RuntimeFailure:
                reject()
            if (envelope["category"] != "authority_binding" or envelope["observed_head"] is None or
                    envelope["repository_identity"] == envelope["destination_identity"] or
                    [option["option_id"] for option in envelope["options"]] != ["switch", "abort"]):
                reject()
        elif envelope is not None or value["status"] == "completed":
            reject()
    elif command in ("authority apply", "authority resolve"):
        if extras and extras != AUTHORITY_FIELDS[command]:
            reject()
        if command == "authority resolve" and value["status"] == "completed" and not extras:
            reject()
    elif value["status"] == "completed":
        reject()
    return value


def validate_public_result(value, *, expected_family=None):
    if not isinstance(value, dict) or not isinstance(value.get("command"), str) or value["command"] not in REGISTERED_COMMANDS:
        raise PublicResultFailure("Unregistered public result command.", "invalid", 7)
    if value["command"] in ERROR_COMMANDS:
        if set(value) != BASE or not local_control_base(value) or value["operation_id"] is not None or value["status"] == "completed":
            raise PublicResultFailure("Invalid generic error result.", "invalid", 7)
    validate_host_result(value, require_host=expected_family == "host")
    validate_authority_result(value, require_authority=expected_family == "authority")
    validate_info_result(value, expected_command=expected_family if expected_family in ("inspect", "status", "context") else None)
    validate_git_result(value, require_git=expected_family == "git")
    validate_resume_result(value, require_resume=expected_family == "resume")
    validate_control_result(value, require_proposal=expected_family == "decision-proposal")
    return validate_secrets_result(value, require_secrets=expected_family == "secrets")


def validate_secrets_result(value, *, require_secrets=False):
    command = value.get("command") if isinstance(value, dict) else None
    def reject():
        raise PublicResultFailure("Secrets public result violates its approved contract.", "invalid", 7)
    if not isinstance(command, str) or not (command == "secrets" or command.startswith("secrets ")):
        if require_secrets:
            reject()
        return value
    if (command not in SECRET_FIELDS or not BASE <= value.keys() or not value.keys() <= BASE | SECRET_FIELDS[command] or
            type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["status"] not in tuple(STATUS) or
            value["operation_id"] is not None or value["published_revision"] is not None or value["code_publication"] != "not_attempted" or
            value["ownership"] != "unknown" or not (value["secrets_shared"] is None or type(value["secrets_shared"]) is bool or value["secrets_shared"] in ("partial", "unknown")) or
            value["next_action"] is not None and not isinstance(value["next_action"], str)):
        reject()
    for field in SECRET_FIELDS[command] & value.keys():
        item = value[field]
        if field in ("secret_operation_id", "plan_id", "preservation_handle"):
            valid = identifier(item)
        elif field in ("verified_keys", "updated_keys", "approved_keys", "shared_available_keys"):
            valid = key_names(item)
        elif field == "secrets_mode":
            valid = item in ("local-env", "doppler")
        elif field == "destination_identity":
            valid = isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) is not None
        elif field in ("local_converged", "baseline_retained", "local_work_available"):
            valid = type(item) is bool
        elif field == "shared_read":
            valid = item in ("available", "unavailable")
        elif field == "shared_read_failure":
            valid = item in ("authentication_required", "unavailable")
        elif field == "convergence":
            valid = item in ("verified", "unverified")
        elif field == "availability":
            validate_secret_summary({"status": value["status"], "availability": item})
            valid = True
        elif field == "conflicts":
            validate_secret_summary({"status": value["status"], "conflicts": item})
            valid = True
        else:
            if (not isinstance(item, dict) or set(item) != {"status", "upload_keys", "download_keys", "conflicts"} or
                    item["status"] not in ("completed", "decision_required") or not key_names(item["upload_keys"]) or not key_names(item["download_keys"])):
                reject()
            validate_secret_summary({"status": item["status"], "conflicts": item["conflicts"]})
            valid = not item["conflicts"] if item["status"] == "completed" else bool(item["conflicts"])
        if not valid:
            reject()
    envelope = value["decision"]
    if envelope is not None:
        if (command not in ("secrets configure", "secrets reconcile") or value["status"] != "decision_required"):
            reject()
        validate_decision(envelope)
        if envelope["category"] not in ({"authority_binding"} if command == "secrets configure" else {"secret_conflict", "secret_deletion"}):
            reject()
    if command in ("secrets configure", "secrets inspect", "secrets reconcile", "secrets decision apply") and value["secrets_shared"] not in (None, False):
        reject()
    if value["status"] == "completed":
        if command == "secrets":
            reject()
        if command in ("secrets inspect", "secrets verify") and not {"availability", "secrets_mode", "destination_identity"} <= value.keys():
            reject()
        if command in ("secrets inspect", "secrets verify") and (value["availability"]["status"] != "completed" or value["availability"]["required_missing"]):
            reject()
        if command in ("secrets apply", "secrets resolve") and "secret_operation_id" not in value and "updated_keys" not in value:
            reject()
    if command == "secrets verify" and value["secrets_shared"] is True and value.get("convergence") != "verified":
        reject()
    if "shared_read_failure" in value and (value.get("secrets_mode") != "doppler" or
            value.get("shared_read") != "unavailable" or value.get("secrets_shared") is not False):
        reject()
    return value


def validate_control_result(value, *, require_proposal=False):
    command = value.get("command") if isinstance(value, dict) else None
    commands = {"decision": set(), "decision propose-integrity": {"preservation_handle"}, "decision apply": {"observed_revision"}}
    if require_proposal and command not in ("decision", "decision propose-integrity"):
        raise PublicResultFailure("Integrity proposal command identity changed.", "invalid", 7)
    if not isinstance(command, str) or command not in commands:
        if isinstance(command, str) and command.startswith("decision "):
            raise PublicResultFailure("Unregistered control result command.", "invalid", 7)
        return value
    def reject():
        raise PublicResultFailure("Control decision result violates its approved contract.", "invalid", 7)
    if (not value.keys() <= BASE | commands[command] or
            not local_control_base(value, allow_decision=command == "decision propose-integrity", allowed_ownership=("unknown", "unowned")) or
            value["operation_id"] is not None):
        reject()
    if "observed_revision" in value and not revision(value["observed_revision"]):
        reject()
    if "preservation_handle" in value and not identifier(value["preservation_handle"]):
        reject()
    if command == "decision propose-integrity":
        envelope = value["decision"]
        if value["status"] == "decision_required":
            if not isinstance(envelope, dict) or set(envelope) != DECISION_FIELDS or "preservation_handle" not in value:
                reject()
            validate_decision(envelope)
            options = [option["option_id"] for option in envelope["options"]]
            if (envelope["category"] != "publication_integrity" or set(options) not in ({"abort"}, {"adopt", "abort"}) or
                    "adopt" in options and envelope["observed_head"] is None):
                reject()
        elif envelope is not None or value["status"] == "completed":
            reject()
    elif command == "decision" and value["status"] == "completed":
        reject()
    return value


def validate_resume_result(value, *, require_resume=False):
    command = value.get("command") if isinstance(value, dict) else None
    if command != "resume":
        if require_resume:
            raise PublicResultFailure("Resume result command identity changed.", "invalid", 7)
        return value
    def reject():
        raise PublicResultFailure("Resume public result violates its approved contract.", "invalid", 7)
    allowed = {"observed_revision", "managed_checkout", "preservation_handle", "secret_availability"}
    if (not BASE <= value.keys() or not value.keys() <= BASE | allowed or
            type(value["schema_version"]) is not int or value["schema_version"] != 1 or
            value["status"] not in (*STATUS, "ready") or not identifier(value["operation_id"], nullable=True) or
            not revision(value["published_revision"], nullable=True) or value["secrets_shared"] is not None and value["secrets_shared"] is not False or
            value["code_publication"] not in ("not_attempted", "publication_unknown", "rejected", "local_only", "published") or
            value["ownership"] not in ("owned", "released", "fenced", "ownership_pending", "unowned", "unknown") or
            value["next_action"] is not None and not isinstance(value["next_action"], str)):
        reject()
    if value["code_publication"] == "published":
        if value["operation_id"] is None or value["published_revision"] is None:
            reject()
    elif value["published_revision"] is not None:
        reject()
    if "observed_revision" in value and not revision(value["observed_revision"]):
        reject()
    if "preservation_handle" in value and not identifier(value["preservation_handle"]):
        reject()
    if "managed_checkout" in value:
        path = value["managed_checkout"]
        if not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts or any(ord(char) < 32 for char in path):
            reject()
    availability = value.get("secret_availability")
    if availability is not None:
        keys = {"status", "available_keys", "required_missing", "unsupported_keys", "secrets_shared", "next_action"}
        if (not isinstance(availability, dict) or not availability.keys() <= keys or
                not {"status", "required_missing"} <= availability.keys() or availability["status"] not in ("completed", "not_ready") or
                any(field in availability and not key_names(availability[field]) for field in ("available_keys", "required_missing", "unsupported_keys")) or
                "secrets_shared" in availability and availability["secrets_shared"] is not False or
                "next_action" in availability and not isinstance(availability["next_action"], str)):
            reject()
    if value["decision"] is not None:
        envelope = value["decision"]
        if (value["status"] != "decision_required" or not isinstance(envelope, dict) or set(envelope) != DECISION_FIELDS or
                "preservation_handle" not in value):
            reject()
        validate_decision(envelope)
        if envelope["category"] != "takeover" or envelope["observed_head"] is None or envelope["observed_owner_session"] is None:
            reject()
    if value["status"] == "ready":
        if (value["ownership"] != "owned" or value["decision"] is not None or
                value["code_publication"] not in ("not_attempted", "published") or
                not {"observed_revision", "managed_checkout", "preservation_handle"} <= value.keys()):
            reject()
        if availability is not None and (availability["status"] != "completed" or availability["required_missing"] or
                not {"available_keys", "unsupported_keys", "secrets_shared"} <= availability.keys()):
            reject()
    return value


def revision(value, *, nullable=False):
    return value is None and nullable or isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is not None


def key_names(value):
    return (isinstance(value, list) and all(isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in value)
            and len(value) == len(set(value)))


def validate_secret_summary(value):
    allowed = {"status", "secret_operation_id", "plan_id", "decision", "next_action", "availability", "conflicts", "verified_keys"}
    if not isinstance(value, dict) or not value.keys() <= allowed or "status" not in value or value["status"] not in tuple(STATUS):
        raise PublicResultFailure("Invalid public secret summary.", "invalid", 7)
    for name in ("secret_operation_id", "plan_id"):
        if name in value and not identifier(value[name]):
            raise PublicResultFailure("Invalid public secret summary identity.", "invalid", 7)
    if "next_action" in value and value["next_action"] is not None and not isinstance(value["next_action"], str):
        raise PublicResultFailure("Invalid public secret summary action.", "invalid", 7)
    if "verified_keys" in value and not key_names(value["verified_keys"]):
        raise PublicResultFailure("Invalid public secret key summary.", "invalid", 7)
    if "decision" in value and value["decision"] is not None:
        validate_decision(value["decision"])
    if "conflicts" in value:
        conflicts = value["conflicts"]
        if (not isinstance(conflicts, list) or any(not isinstance(item, dict) or set(item) != {"key", "category"} or
                not key_names([item["key"]]) or item["category"] not in ("secret_conflict", "secret_deletion", "scope_grant_required") for item in conflicts)):
            raise PublicResultFailure("Invalid public conflict summary.", "invalid", 7)
    if "availability" in value:
        available = value["availability"]
        if (not isinstance(available, dict) or not available.keys() <= {"status", "available_keys", "required_missing", "unsupported_keys", "secrets_shared", "next_action"} or
                not {"status", "available_keys", "required_missing", "unsupported_keys", "secrets_shared"} <= available.keys() or
                available["status"] not in ("completed", "not_ready") or available["secrets_shared"] is not False or
                any(not key_names(available[field]) for field in ("available_keys", "required_missing", "unsupported_keys")) or
                "next_action" in available and not isinstance(available["next_action"], str)):
            raise PublicResultFailure("Invalid public availability summary.", "invalid", 7)


def validate_git_result(value, *, require_git=False):
    command = value.get("command") if isinstance(value, dict) else None
    if not isinstance(command, str) or command not in GIT_FIELDS:
        if require_git:
            raise PublicResultFailure("Git result command identity changed.", "invalid", 7)
        return value
    def reject():
        raise PublicResultFailure("Git public result violates its approved contract.", "invalid", 7)
    if (not BASE <= value.keys() or not value.keys() <= BASE | GIT_FIELDS[command] or
            type(value["schema_version"]) is not int or value["schema_version"] != 1 or
            value["status"] not in tuple(STATUS) or not identifier(value["operation_id"], nullable=True) or
            not revision(value["published_revision"], nullable=True) or
            value["code_publication"] not in ("published", "rejected", "publication_unknown", "local_only", "not_attempted") or
            value["ownership"] not in ("owned", "released", "fenced", "ownership_pending", "unowned", "unknown") or
            not (value["secrets_shared"] is None or type(value["secrets_shared"]) is bool or value["secrets_shared"] in ("partial", "unknown")) or
            value["next_action"] is not None and not isinstance(value["next_action"], str)):
        reject()
    if value["code_publication"] == "published":
        if value["operation_id"] is None or value["published_revision"] is None:
            reject()
    elif value["published_revision"] is not None:
        reject()
    if value["code_publication"] in ("rejected", "local_only") and value["operation_id"] is None:
        reject()
    if value["code_publication"] == "rejected" and value["status"] != "rejected":
        reject()
    if value["code_publication"] == "publication_unknown" and value["status"] not in ("unknown", "invalid"):
        reject()
    if command != "handoff" and value["secrets_shared"] is not None:
        reject()
    if command == "recover-session" and (value["code_publication"] != "not_attempted" or value["operation_id"] is not None):
        reject()
    for field in GIT_FIELDS[command] & value.keys():
        if field == "observed_revision" and not revision(value[field]) or field == "preservation_handle" and not identifier(value[field]):
            reject()
        if field == "test_status" and value[field] != "unknown":
            reject()
        if field == "secrets_result":
            validate_secret_summary(value[field])
    if value["decision"] is not None:
        envelope = value["decision"]
        if command != "acquire" or value["status"] != "decision_required" or not isinstance(envelope, dict) or set(envelope) != DECISION_FIELDS:
            reject()
        validate_decision(envelope)
        if (envelope["category"] != "takeover" or envelope["observed_head"] is None or
                envelope["observed_owner_session"] is None or "preservation_handle" not in value):
            reject()
    if value["status"] == "completed":
        if command == "operation":
            reject()
        if command == "recover-session" and (value["ownership"] != "owned" or "observed_revision" not in value):
            reject()
        if command == "init" and value["code_publication"] == "not_attempted" and "observed_revision" not in value:
            reject()
        if command in ("acquire", "takeover", "release", "checkpoint", "handoff", "done") and value["code_publication"] not in ("published", "local_only"):
            reject()
    if value["ownership"] == "owned" and value["code_publication"] != "published" and command != "recover-session":
        reject()
    if value["ownership"] == "released" and value["code_publication"] != "published":
        reject()
    return value


def validate_info_result(value, *, expected_command=None):
    if not isinstance(value, dict):
        raise PublicResultFailure("Public result must be a structured object.", "invalid", 7)
    command = value.get("command")
    def reject():
        raise PublicResultFailure("Inspection/context public result violates its approved contract.", "invalid", 7)
    if expected_command is not None and (not isinstance(command, str) or
            command not in ({"context", "context create"} if expected_command == "context" else {expected_command})):
        reject()
    if not isinstance(command, str) or command not in INFO_FIELDS:
        if isinstance(command, str) and any(command.startswith(prefix + " ") for prefix in ("inspect", "status", "context")):
            reject()
        return value
    if not value.keys() <= BASE | INFO_FIELDS[command] or not local_control_base(value) or value["operation_id"] is not None:
        reject()
    if "inspection" in value:
        observed = value["inspection"]
        if (not isinstance(observed, dict) or set(observed) != {"observed_revision", "owner_present", "runtime_status", "protocol_version"} or
                not isinstance(observed["observed_revision"], str) or
                re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", observed["observed_revision"]) is None or
                type(observed["owner_present"]) is not bool or
                observed["runtime_status"] not in ("idle", "working", "blocked") or observed["protocol_version"] != "1.2"):
            reject()
    if "context_file" in value:
        path = value["context_file"]
        if (not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts or
                any(ord(char) < 32 for char in path)):
            reject()
    if value["status"] == "completed" and (command == "context" or not INFO_FIELDS[command] <= value.keys()):
        reject()
    return value
