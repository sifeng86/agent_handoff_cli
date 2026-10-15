"""Explicit Markdown metadata, immutable plan IDs and validated state references."""
import json
import re

from .contracts import RuntimeFailure, _unique_pairs


BLOCK = re.compile(r"\A<!-- agent-handoff-plan\n(.*?)\n-->\n", re.DOTALL)
PLAN_FIELDS = {"schema_version", "id", "status", "goal", "scope", "non_goals", "steps", "acceptance_criteria"}


def parse(raw: str) -> dict:
    match = BLOCK.match(raw)
    if not match:
        raise RuntimeFailure("Plan requires an explicit leading agent-handoff-plan metadata block.")
    try:
        value = json.loads(match[1], object_pairs_hook=_unique_pairs)
    except (ValueError, RecursionError):
        raise RuntimeFailure("Invalid Plan metadata.") from None
    if not isinstance(value, dict) or set(value) != PLAN_FIELDS:
        raise RuntimeFailure("Invalid Plan metadata fields.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RuntimeFailure("Unsupported Plan schema.", "unsupported", 8)
    if not isinstance(value["id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value["id"]):
        raise RuntimeFailure("Invalid Plan ID.")
    if value["status"] not in ("DRAFT", "READY", "DONE"):
        raise RuntimeFailure("Invalid Plan status.")
    for key in ("goal", "scope", "non_goals", "acceptance_criteria"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise RuntimeFailure("Plan descriptions must be explicit nonempty text.")
    steps = value["steps"]
    if not isinstance(steps, list) or any(not isinstance(step, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", step) for step in steps):
        raise RuntimeFailure("Invalid stable Step IDs.")
    if len(set(steps)) != len(steps):
        raise RuntimeFailure("Duplicate Step ID.")
    return value


def collect(backend, oid: str) -> dict:
    listing = backend.run("ls-tree", "-r", "-z", oid, "--", "plans/")
    if listing.returncode:
        raise RuntimeFailure("Plan set cannot be read.")
    plans = {}
    for entry in listing.stdout.split(b"\x00"):
        if not entry:
            continue
        header, path = entry.split(b"\t", 1)
        if not path.endswith(b".md"):
            continue
        if not header.startswith(b"100644 blob "):
            raise RuntimeFailure("Plans must be regular Markdown files.")
        try:
            path = path.decode()
            raw = backend.text("show", f"{oid}:{path}")
            metadata = parse(raw + "\n")
        except UnicodeError:
            raise RuntimeFailure("Plan text must be UTF-8.") from None
        if metadata["id"] in plans:
            raise RuntimeFailure("Duplicate immutable Plan ID.")
        plans[metadata["id"]] = {"metadata": metadata, "path": path, "raw": raw}
    return plans


def validate(backend, oid: str, state: dict, previous_oid: str | None = None):
    plans = collect(backend, oid)
    if previous_oid is not None:
        previous = collect(backend, previous_oid)
        if previous.keys() - plans.keys():
            raise RuntimeFailure("Allocated Plans cannot be deleted or reuse another ID.")
        for identifier, record in previous.items():
            if record["metadata"]["status"] == "DONE" and plans[identifier]["metadata"]["status"] != "DONE":
                raise RuntimeFailure("Completed Plan IDs cannot be reused.")
    active, cursor = state["active_plan"], state["current_step"]
    if state["status"] == "idle":
        if active is not None or cursor is not None:
            raise RuntimeFailure("Idle state requires null Plan and Step references.")
    else:
        if active not in plans or plans[active]["metadata"]["status"] != "READY":
            raise RuntimeFailure("Working or blocked state requires an existing READY Plan.")
        steps = plans[active]["metadata"]["steps"]
        if (steps and cursor not in steps) or (not steps and cursor is not None):
            raise RuntimeFailure("Agent must explicitly select a valid current Step.")
    return plans


def complete(record: dict) -> bytes:
    raw = record["raw"]
    metadata = dict(record["metadata"], status="DONE")
    match = BLOCK.match(raw + "\n")
    replacement = "<!-- agent-handoff-plan\n" + json.dumps(metadata, sort_keys=True) + "\n-->\n"
    return (replacement + (raw + "\n")[match.end():]).encode()
