"""Narrow, backup-first repair of an installed agentctl shebang."""
import os
from pathlib import Path
import re
import stat

from .contracts import RuntimeFailure
from .host_recovery import HostFileRecovery, canonical_path, snapshot
from .journal import opaque_id
from .output import result
from .secrets.local_env import open_directory


def _absolute_prefix(value):
    if not isinstance(value, (str, Path)):
        raise RuntimeFailure("Installer repair requires an approved absolute prefix.", "rejected", 4)
    original = os.fspath(value)
    prefix = Path(value)
    if (not prefix.is_absolute() or ".." in prefix.parts or original != str(prefix) or
            any(ord(char) < 32 for char in str(prefix))):
        raise RuntimeFailure("Installer repair requires a canonical absolute prefix.", "rejected", 4)
    fd = open_directory(prefix)
    try:
        opened, named = os.fstat(fd), os.stat(prefix, follow_symlinks=False)
        if (not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(named.st_mode) or
                (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
            raise RuntimeFailure("Approved installer prefix is not a directory.", "rejected", 4)
    finally:
        os.close(fd)
    return prefix


def _shebang(raw, *, bin_directory, require_healthy):
    if not isinstance(raw, bytes):
        raise RuntimeFailure("Installer launcher is not verified.", "rejected", 4)
    end = raw.find(b"\n")
    if end < 0 or end > 4096:
        raise RuntimeFailure("Installer launcher has no bounded shebang line.", "rejected", 4)
    line = raw[:end + 1]
    if not line.startswith(b"#!") or b"\r" in line:
        raise RuntimeFailure("Installer launcher shebang is not canonical.", "rejected", 4)
    try:
        interpreter_text = os.fsdecode(line[2:-1])
    except (UnicodeError, ValueError):
        raise RuntimeFailure("Installer launcher shebang is not canonical.", "rejected", 4) from None
    interpreter = Path(interpreter_text)
    if (not interpreter.is_absolute() or str(interpreter) != interpreter_text or ".." in interpreter.parts or
            any(char.isspace() or not char.isprintable() for char in interpreter_text)):
        raise RuntimeFailure("Installer launcher interpreter path is not canonical.", "rejected", 4)
    if require_healthy:
        if (interpreter.parent != bin_directory or
                re.fullmatch(r"python(?:3(?:\.\d+)?)?", interpreter.name) is None or
                not interpreter.is_file() or
                not os.access(interpreter, os.X_OK)):
            raise RuntimeFailure("Verified backup does not name an executable in the approved prefix.", "rejected", 4)
    return interpreter, end + 1


def repair_entrypoint(store, approved_prefix, known_good_handle, *, emit=None, fault=lambda _: None):
    if (emit is not None and not callable(emit)) or not callable(fault):
        raise RuntimeFailure("Installer repair callbacks are invalid.", "rejected", 4)
    prefix = _absolute_prefix(approved_prefix)
    private_root = Path(store.root).resolve()
    prefix_resolved = prefix.resolve()
    if (private_root == prefix_resolved or private_root.is_relative_to(prefix_resolved) or
            prefix_resolved.is_relative_to(private_root)):
        raise RuntimeFailure("Installer prefix and private recovery store must be separate.", "rejected", 4)
    bin_directory = prefix / "bin"
    bin_fd = open_directory(bin_directory)
    os.close(bin_fd)
    target = canonical_path(bin_directory / "agentctl")
    recovery = HostFileRecovery(store)

    healthy_record, healthy_raw = recovery.read(known_good_handle)
    if healthy_record["path"] != str(target):
        raise RuntimeFailure("Known-good installer backup has the wrong fixed scope.", "rejected", 4)
    healthy_interpreter, healthy_body_offset = _shebang(healthy_raw, bin_directory=bin_directory, require_healthy=True)
    healthy_meta = healthy_record["snapshot"]["metadata"]
    if not healthy_meta[2] & 0o111:
        raise RuntimeFailure("Known-good installer launcher is not executable.", "rejected", 4)

    try:
        current_snapshot, current_raw = snapshot(target)
    except (OSError, RuntimeFailure):
        raise RuntimeFailure("Current installer launcher cannot be verified safely.", "rejected", 4) from None
    if current_snapshot["metadata"][2:5] != healthy_meta[2:5]:
        raise RuntimeFailure("Current installer launcher differs beyond the approved shebang fault.", "rejected", 4)
    current_interpreter, current_body_offset = _shebang(current_raw, bin_directory=bin_directory, require_healthy=False)
    if current_raw[current_body_offset:] != healthy_raw[healthy_body_offset:]:
        raise RuntimeFailure("Current installer launcher body differs from the verified backup.", "rejected", 4)

    if current_interpreter == healthy_interpreter:
        return [result("host verify-backup", backup_handle=known_good_handle, backup_verified=True)]
    if os.path.lexists(current_interpreter):
        raise RuntimeFailure("Current launcher does not have the one approved missing-interpreter fault.", "rejected", 4)

    before_report = recovery.backup(target)
    before_handle = before_report["backup_handle"]
    before_record, before_raw = recovery.read(before_handle)
    if (before_record["path"] != str(target) or before_raw != current_raw or
            before_record["snapshot"] != current_snapshot or snapshot(target)[0] != current_snapshot):
        raise RuntimeFailure("Installer launcher changed while its pre-repair backup was being verified.",
                             "decision_required", 3)

    repair_id = opaque_id()
    store.write("installer-repair-" + repair_id + ".json", {
        "schema_version": 1,
        "repair_id": repair_id,
        "target": str(target),
        "known_good_handle": known_good_handle,
        "before_repair_handle": before_handle,
        "premise": "same_body_mode_owner_missing_interpreter_shebang_only",
    })
    backup_result = result("host backup", **before_report)
    reports = [backup_result]
    if emit is not None:
        emit(backup_result)

    restored = recovery.restore(known_good_handle, before_handle, fault=fault)
    reports.append(result("host restore", **restored))
    return reports
