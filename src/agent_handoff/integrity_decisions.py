"""Private integrity review observations; dependency changes invalidate consent."""
import hashlib
import json

from .contracts import RuntimeFailure
from .integrity import observation
from .journal import validate_id
from .record_checks import shape, match, IDENTITY, OID


def digest(value):
    return None if value is None else hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dependencies(store, identity):
    trust = store.read("integrity.json", missing=True)
    status, trusted_oid = "missing", None
    if trust is not None:
        try:
            observation(trust)
            if trust["repository_identity"] != identity:
                raise RuntimeFailure("Foreign trusted observation.")
        except RuntimeFailure:
            status = "invalid"
        else:
            status, trusted_oid = "valid", trust["last_trusted_oid"]
    return {"previous_trust_digest": digest(trust), "previous_trust_status": status,
            "previous_trusted_oid": trusted_oid,
            "secret_binding_digest": digest(store.read("secret-binding.json", missing=True))}


def validate(record):
    shape(record, {"schema_version", "decision_id", "repository_identity", "observed_head", "observed_owner_session",
                   "snapshot_valid", "preservation_handle", "previous_trust_digest", "previous_trust_status",
                   "previous_trusted_oid", "secret_binding_digest"})
    for field in ("decision_id", "preservation_handle"):
        validate_id(record[field])
    if (not match(record["repository_identity"], IDENTITY) or
            not match(record["observed_head"], OID, nullable=True) or
            type(record["snapshot_valid"]) is not bool or
            record["previous_trust_status"] not in ("valid", "missing", "invalid")):
        raise RuntimeFailure("Invalid private integrity decision observation.")
    if record["observed_owner_session"] is not None:
        validate_id(record["observed_owner_session"])
    for field in ("previous_trust_digest", "secret_binding_digest"):
        if not match(record[field], IDENTITY, nullable=True):
            raise RuntimeFailure("Invalid private integrity dependency evidence.")
    if ((record["previous_trust_status"] == "missing") != (record["previous_trust_digest"] is None) or
            (record["previous_trust_status"] == "valid" and not match(record["previous_trusted_oid"], OID)) or
            (record["previous_trust_status"] != "valid" and record["previous_trusted_oid"] is not None) or
            (record["snapshot_valid"] and record["observed_head"] is None)):
        raise RuntimeFailure("Inconsistent private integrity observations.")
    return record


def read(store, decision_id, envelope, *, checkout=None):
    record = validate(store.read("integrity-decision-" + validate_id(decision_id) + ".json"))
    if (record["decision_id"] != decision_id or envelope["category"] != "publication_integrity" or
            any(record[key] != envelope[key] for key in ("repository_identity", "observed_head", "observed_owner_session")) or
            (any(option["option_id"] == "adopt" for option in envelope["options"]) and not record["snapshot_valid"])):
        raise RuntimeFailure("Private integrity observation differs from reviewed intent.")
    from .preservation import read_record
    read_record(store, record["preservation_handle"], checkout=checkout)
    return record
