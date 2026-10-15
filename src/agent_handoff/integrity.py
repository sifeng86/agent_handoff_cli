"""Last trusted observations cannot silently adopt rewritten authority history."""
from .contracts import RuntimeFailure
from .plans import validate as validate_plans
from .record_checks import shape, match, IDENTITY, OID
from .authority import Authority


def observation(value):
    shape(value, {"schema_version", "repository_identity", "last_trusted_oid"})
    if not match(value["repository_identity"], IDENTITY) or not match(value["last_trusted_oid"], OID):
        raise RuntimeFailure("Invalid private trusted observation.", "invalid", 7)
    return value


def validate(backend, store, oid: str | None, *, initial: bool = False) -> dict:
    previous = store.read("integrity.json", missing=True)
    if previous is not None:
        observation(previous)
        if previous["repository_identity"] != Authority(backend.destination, backend.ref).identity:
            raise RuntimeFailure("Trusted observation belongs to a different authority.", "invalid", 7)
    if oid is None:
        raise RuntimeFailure("Registered publication authority is missing.", "invalid", 7)
    try:
        records = backend.records(oid)
        validate_plans(backend, oid, records["state"], previous.get("last_trusted_oid") if previous else None)
    except RuntimeFailure as error:
        if error.code == 8:
            raise
        raise RuntimeFailure("Publication control snapshot is invalid.", "invalid", 7) from None
    if previous is None and not initial:
        raise RuntimeFailure("Trusted authority observation is missing; rebind explicitly.", "decision_required", 3)
    if previous is not None:
        trusted = previous.get("last_trusted_oid")
        if not isinstance(trusted, str) or not backend.ancestor(trusted, oid):
            raise RuntimeFailure("Publication history was rewritten; explicit integrity decision is required.", "invalid", 7)
    return records


def remember(store, identity: str, oid: str):
    value = observation({"schema_version": 1, "repository_identity": identity, "last_trusted_oid": oid})
    store.write("integrity.json", value)
