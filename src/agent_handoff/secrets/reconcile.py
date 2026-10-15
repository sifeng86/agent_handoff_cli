"""Pure scoped B/L/R reconciliation. Secret values never enter public reports."""
from dataclasses import dataclass, field

from ..contracts import RuntimeFailure
from .dotenv import KEY


ABSENT = object()


@dataclass(repr=False)
class Plan:
    uploads: dict[str, str] = field(default_factory=dict, repr=False)
    downloads: dict[str, str] = field(default_factory=dict, repr=False)
    baseline: dict[str, str] = field(default_factory=dict, repr=False)
    conflicts: dict[str, str] = field(default_factory=dict)

    def public(self) -> dict:
        return {"status": "decision_required" if self.conflicts else "completed",
                "upload_keys": sorted(self.uploads), "download_keys": sorted(self.downloads),
                "conflicts": [{"key": key, "category": category} for key, category in sorted(self.conflicts.items())]}


def reconcile(baseline: dict | None, local: dict, remote: dict, scope: set[str], *,
              allow_upload: bool, allow_download: bool = True) -> Plan:
    if not isinstance(scope, set) or any(not isinstance(key, str) or not KEY.fullmatch(key) for key in scope):
        raise RuntimeFailure("Invalid approved shared scope.")
    for source in (baseline, local, remote):
        if source is not None and (not isinstance(source, dict) or any(not isinstance(v, str) for k, v in source.items() if k in scope)):
            raise RuntimeFailure("Invalid private secret source.")
    plan = Plan()
    for key in sorted(scope):
        previous = (baseline or {}).get(key, ABSENT)
        left, right = local.get(key, ABSENT), remote.get(key, ABSENT)
        if baseline is not None and previous is not ABSENT and (left is ABSENT or right is ABSENT):
            plan.conflicts[key] = "secret_deletion"
        elif left == right:
            if left is not ABSENT:
                plan.baseline[key] = left
        elif baseline is None:
            if left is not ABSENT and right is not ABSENT:
                plan.conflicts[key] = "secret_conflict"
            elif left is ABSENT and allow_download:
                plan.downloads[key] = right
                plan.baseline[key] = right
            elif right is ABSENT and allow_upload:
                plan.uploads[key] = left
                plan.baseline[key] = left
            else:
                plan.conflicts[key] = "scope_grant_required"
        elif left == previous and right is not ABSENT:
            if allow_download:
                plan.downloads[key] = right
                plan.baseline[key] = right
            else:
                plan.conflicts[key] = "scope_grant_required"
        elif right == previous and left is not ABSENT:
            if allow_upload:
                plan.uploads[key] = left
                plan.baseline[key] = left
            else:
                plan.conflicts[key] = "scope_grant_required"
        else:
            plan.conflicts[key] = "secret_conflict"
    if plan.conflicts:
        # No partial mutation, including no partial baseline advancement.
        plan.uploads.clear()
        plan.downloads.clear()
        plan.baseline.clear()
    return plan
