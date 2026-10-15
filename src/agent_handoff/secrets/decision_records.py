"""Private secret decision intent; explicit kind and immutable decision linkage."""
from ..contracts import RuntimeFailure
from ..journal import validate_id
from ..record_checks import shape, decision
from .binding import validate as validate_binding


def validate(record):
    if not isinstance(record, dict) or record.get("kind") not in ("binding", "reconcile"):
        raise RuntimeFailure("Invalid private secret decision kind.")
    fields = {"schema_version", "decision_id", "kind"}
    shape(record, fields | ({"candidate", "previous_binding"} if record["kind"] == "binding" else {"plan_id"}))
    validate_id(record["decision_id"])
    if record["kind"] == "reconcile":
        validate_id(record["plan_id"])
    else:
        candidate = validate_binding(record["candidate"])
        previous = record["previous_binding"]
        if previous is not None:
            validate_binding(previous)
            if previous["repository_identity"] != candidate["repository_identity"]:
                raise RuntimeFailure("Secret binding transition spans different repositories.")
    return record


def read(store, decision_id, *, missing=False):
    validate_id(decision_id)
    record = store.read("secret-decision-" + decision_id + ".json", missing=missing)
    if record is None:
        return None
    validate(record)
    if record["decision_id"] != decision_id:
        raise RuntimeFailure("Private secret decision filename differs from its identity.")
    envelope = decision(store.read("decision-" + decision_id + ".json"))
    categories = ("authority_binding",) if record["kind"] == "binding" else ("secret_conflict", "secret_deletion")
    if (envelope["decision_id"] != decision_id or envelope["category"] not in categories or
            (record["kind"] == "binding" and
             record["candidate"]["repository_identity"] != envelope["repository_identity"])):
        raise RuntimeFailure("Private secret intent differs from its public decision kind or authority.")
    return record
