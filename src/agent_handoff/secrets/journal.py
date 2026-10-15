"""Sensitive per-key apply journal; unknown writes block dependent mutations."""
import json

from ..contracts import RuntimeFailure
from ..journal import opaque_id, validate_id
from ..record_checks import shape, match, IDENTITY, OID
from .dotenv import KEY, parse as parse_dotenv
from .manifest import parse as parse_manifest, shared_scope
from .file_material import validate_pem
from .binding import validate as validate_binding


SHARED_FIELDS = {"repository_identity", "observed_head", "session_id", "checkout_identity", "context_identity",
                 "binding", "manifest", "local_raw", "local_values", "baseline", "local", "remote",
                 "target", "plan_id", "decision_id"}


def shared_evidence(record, *, check_intent=True):
    evidence = record["evidence"]
    if set(evidence) != SHARED_FIELDS:
        raise RuntimeFailure("Invalid private shared secret evidence fields.")
    binding = validate_binding(evidence["binding"])
    if (binding["mode"] != "doppler" or binding["repository_identity"] != evidence["repository_identity"] or
            binding["manifest"] != evidence["manifest"] or not match(evidence["observed_head"], OID)):
        raise RuntimeFailure("Shared secret evidence differs from approved authority or scope.")
    for field in ("repository_identity", "context_identity", "checkout_identity"):
        if not match(evidence[field], IDENTITY):
            raise RuntimeFailure("Invalid private shared secret identity.")
    for field in ("session_id", "plan_id"):
        validate_id(evidence[field])
    if evidence["decision_id"] is not None:
        validate_id(evidence["decision_id"])
    scope = shared_scope(binding["manifest"])
    for field in ("local_values", "baseline", "local", "remote", "target"):
        values = evidence[field]
        if field == "baseline" and values is None:
            continue
        if (not isinstance(values, dict) or
                any(not isinstance(key, str) or not KEY.fullmatch(key) for key in values) or
                any(not isinstance(value, str) for value in values.values()) or
                (field != "local_values" and not set(values) <= scope)):
            raise RuntimeFailure("Invalid private shared secret observations.")
        if field != "local_values":
            for key, value in values.items():
                if binding["manifest"]["keys"][key]["classification"] == "file-backed":
                    validate_pem(value)
    if not isinstance(evidence["local_raw"], str) or parse_dotenv(evidence["local_raw"]).values != evidence["local_values"]:
        raise RuntimeFailure("Private dotenv evidence differs from parsed observations.")
    for key in scope:
        declaration = binding["manifest"]["keys"][key]
        if declaration["classification"] == "file-backed":
            if key in evidence["local_values"] and evidence["local_values"][key] != declaration["file"]:
                raise RuntimeFailure("Private file-backed pointer differs from approved mapping.")
        elif ((key in evidence["local"]) != (key in evidence["local_values"]) or
              evidence["local"].get(key) != evidence["local_values"].get(key)):
            raise RuntimeFailure("Private local observations differ from dotenv evidence.")
    absent = object()
    if not check_intent:
        return
    for intent, source in (("uploads", "remote"), ("downloads", "local")):
        expected = {key: evidence["target"].get(key) for key in scope
                    if evidence[source].get(key, absent) != evidence["target"].get(key, absent)}
        if record[intent] != expected:
            raise RuntimeFailure("Private per-key intent differs from reviewed observations.")
    if record["uploads"] and not binding["upload_authorized"]:
        raise RuntimeFailure("Private shared write intent lacks its approved grant.")


def validate(record):
    fields = {"schema_version", "operation_id", "phase", "evidence", "uploads", "downloads", "keys", "local_applied", "result"}
    if isinstance(record, dict) and "resolved_shared" in record:
        fields.add("resolved_shared")
    shape(record, fields)
    validate_id(record["operation_id"])
    if (record["phase"] not in ("prepared", "sending", "applying_local", "resolved") or
            record["result"] not in ("pending", "unknown", "local_only", "rejected", "verified", "partial") or
            type(record["local_applied"]) is not bool or not isinstance(record["evidence"], dict)):
        raise RuntimeFailure("Invalid private secret journal state.")
    for field in ("uploads", "downloads"):
        if (not isinstance(record[field], dict) or
                any(not isinstance(key, str) or not KEY.fullmatch(key) for key in record[field]) or
                any(not isinstance(value, str) and value is not None for value in record[field].values())):
            raise RuntimeFailure("Invalid private secret journal payload.")
    if not isinstance(record["keys"], dict) or record["keys"].keys() != record["uploads"].keys():
        raise RuntimeFailure("Secret journal per-key scope differs from upload intent.")
    operation_ids = set()
    for progress in record["keys"].values():
        if not isinstance(progress, dict) or set(progress) != {"operation_id", "status"}:
            raise RuntimeFailure("Invalid private per-key evidence.")
        validate_id(progress["operation_id"])
        if progress["operation_id"] in operation_ids or progress["operation_id"] == record["operation_id"]:
            raise RuntimeFailure("Per-key operation identities must be distinct.")
        operation_ids.add(progress["operation_id"])
        if progress["status"] not in ("prepared", "sending", "verified", "rejected", "unknown"):
            raise RuntimeFailure("Invalid per-key journal status.")
    if record["phase"] == "resolved" and record["result"] in ("pending", "unknown"):
        raise RuntimeFailure("Unknown secret operation cannot be terminal.")
    statuses = {progress["status"] for progress in record["keys"].values()}
    if record["phase"] == "resolved" and statuses & {"sending", "unknown"}:
        raise RuntimeFailure("Unproven secret sends cannot bypass the pending barrier.")
    if record["phase"] == "prepared" and (statuses - {"prepared"} or record["local_applied"]):
        raise RuntimeFailure("Prepared secret operation cannot contain sent or applied evidence.")
    if record["result"] == "verified" and not record["local_applied"]:
        raise RuntimeFailure("Verified secret operation requires local convergence evidence.")
    if record["result"] == "local_only" and statuses - {"prepared"}:
        raise RuntimeFailure("Local-only secret outcome cannot contain sent evidence.")
    if record["result"] == "verified" and "rejected" in statuses:
        raise RuntimeFailure("Verified convergence cannot include rejected writes.")
    if "resolved_shared" in record and not (type(record["resolved_shared"]) is bool or record["resolved_shared"] == "partial"):
        raise RuntimeFailure("Invalid recorded secret convergence.")
    if record["evidence"].get("kind") == "local_patch":
        evidence = record["evidence"]
        if set(evidence) != {"kind", "binding", "repository_identity", "context_identity", "checkout_identity",
                             "session_id", "target_env", "target_files", "original_env", "original_files"}:
            raise RuntimeFailure("Invalid private local material evidence fields.")
        for field in ("repository_identity", "context_identity", "checkout_identity"):
            if not match(evidence[field], IDENTITY):
                raise RuntimeFailure("Invalid private local material identity.")
        validate_id(evidence["session_id"])
        if (record["uploads"] or record["downloads"] or not isinstance(evidence["binding"], dict) or
                evidence["binding"].get("mode") != "local-env" or
                not isinstance(evidence["original_env"], str)):
            raise RuntimeFailure("Invalid private local material operation.")
        binding = validate_binding(evidence["binding"])
        if (binding["repository_identity"] != evidence["repository_identity"] or
                record["result"] == "verified"):
            raise RuntimeFailure("Invalid private local material binding or outcome.")
        original_env = parse_dotenv(evidence["original_env"]).values
        manifest = parse_manifest(json.dumps(evidence["binding"].get("manifest")))
        for field in ("target_env", "target_files", "original_files"):
            values = evidence[field]
            if (not isinstance(values, dict) or any(not isinstance(key, str) or not KEY.fullmatch(key) for key in values) or
                    any(not isinstance(value, str) and not (field != "target_env" and value is None) for value in values.values())):
                raise RuntimeFailure("Invalid private local material observations.")
        if evidence["target_files"].keys() != evidence["original_files"].keys():
            raise RuntimeFailure("Local material observation scope mismatch.")
        for key, target in evidence["target_files"].items():
            declaration = manifest["keys"].get(key)
            if (not declaration or declaration["classification"] != "file-backed" or
                    (key in evidence["target_env"] if target is None else
                     evidence["target_env"].get(key) != declaration["file"])):
                raise RuntimeFailure("Local material target differs from approved mapping.")
            for value in (target, evidence["original_files"][key]):
                if value is not None:
                    validate_pem(value)
        absent = object()
        changed = {key for key in original_env.keys() | evidence["target_env"].keys()
                   if original_env.get(key, absent) != evidence["target_env"].get(key, absent)}
        allowed = {key for key, declaration in manifest["keys"].items()
                   if declaration["classification"] != "file-backed"} | set(evidence["target_files"])
        if not changed <= allowed:
            raise RuntimeFailure("Local material target changes exceed approved manifest scope.")
        if "resolved_shared" in record and record["resolved_shared"] is not False:
            raise RuntimeFailure("Local material operations cannot claim shared convergence.")
    else:
        shared_evidence(record)
    return record


class SecretJournal:
    def __init__(self, store):
        self.store = store

    def create(self, evidence: dict, uploads: dict[str, str | None], downloads: dict[str, str | None]) -> dict:
        operation_id = opaque_id()
        record = {"schema_version": 1, "operation_id": operation_id, "phase": "prepared", "evidence": evidence,
                  "uploads": uploads, "downloads": downloads, "keys": {key: {"operation_id": opaque_id(), "status": "prepared"}
                    for key in uploads}, "local_applied": False, "result": "pending"}
        self.write(record)
        return record

    def write(self, record: dict):
        validate(record)
        self.store.write("secret-operation-" + validate_id(record["operation_id"]) + ".json", record)

    def read(self, operation_id: str) -> dict:
        record = validate(self.store.read("secret-operation-" + validate_id(operation_id) + ".json"))
        if record["operation_id"] != operation_id:
            raise RuntimeFailure("Secret journal filename differs from operation identity.")
        return record

    def pending(self) -> list[dict]:
        records = []
        for name in self.store.names():
            if name.startswith("secret-operation-") and name.endswith(".json"):
                record = self.read(name[len("secret-operation-"):-len(".json")])
                if record["phase"] != "resolved":
                    records.append(record)
        return records

    def require_clear(self):
        if self.pending():
            raise RuntimeFailure("Resolve private secret operations before reconciliation or shared writes.", "unknown", 5)
