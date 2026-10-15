"""Private reconciliation plans: strict shape and immutable source-derived intent."""
from ..contracts import RuntimeFailure
from ..journal import validate_id
from ..record_checks import shape
from .journal import SHARED_FIELDS, shared_evidence
from .manifest import shared_scope
from .file_material import validate_pem
from .reconcile import reconcile


def validate(record):
    fields = {"schema_version", "plan_id", "evidence", "target", "conflicts", "status"}
    if isinstance(record, dict) and "decision_id" in record:
        fields.add("decision_id")
    shape(record, fields)
    validate_id(record["plan_id"])
    if record["status"] not in ("pending", "applied", "aborted", "stale"):
        raise RuntimeFailure("Invalid private secret plan lifecycle.")
    evidence = record["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != SHARED_FIELDS - {"target", "plan_id", "decision_id"}:
        raise RuntimeFailure("Invalid private secret plan observations.")
    shared_evidence({"evidence": dict(evidence, target=evidence["remote"], plan_id=record["plan_id"], decision_id=None)},
                    check_intent=False)
    scope = shared_scope(evidence["manifest"])
    if (not isinstance(record["target"], dict) or not set(record["target"]) <= scope or
            any(not isinstance(value, str) for value in record["target"].values()) or
            not isinstance(record["conflicts"], dict) or not set(record["conflicts"]) <= scope or
            any(value not in ("secret_conflict", "secret_deletion", "scope_grant_required")
                for value in record["conflicts"].values())):
        raise RuntimeFailure("Invalid private secret plan intent.")
    for key, value in record["target"].items():
        if evidence["manifest"]["keys"][key]["classification"] == "file-backed":
            validate_pem(value)
    if "decision_id" in record:
        validate_id(record["decision_id"])
        if record["status"] != "applied" or record["conflicts"]:
            raise RuntimeFailure("Decision-derived stored plan must be terminal and conflict-free.")
    else:
        expected = reconcile(evidence["baseline"], evidence["local"], evidence["remote"], scope,
                             allow_upload=evidence["binding"]["upload_authorized"])
        if record["target"] != expected.baseline or record["conflicts"] != expected.conflicts:
            raise RuntimeFailure("Private secret plan differs from source-derived reconciliation.")
    return record
