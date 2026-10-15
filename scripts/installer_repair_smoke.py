"""Isolated wheel launcher repair evidence; repair implementation remains source-only."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import zipfile


WHEEL_NAME = "agent_handoff-0.1.0-py3-none-any.whl"
POINTER = Path("/tmp/agent-handoff-installer-repair-evidence.pointer")


def _private_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _run(args, *, cwd, env, log, expected=None, timeout=120):
    try:
        process = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("bounded_subprocess_failed") from None
    with log.open("ab") as stream:
        stream.write(b"\n--- command output ---\n")
        stream.write(process.stdout)
        stream.write(process.stderr)
    if expected is not None and process.returncode != expected:
        raise RuntimeError("unexpected_subprocess_status")
    return process


def _broken_launcher_fails(launcher, *, cwd, env, log):
    launcher = Path(launcher)
    if not launcher.is_file() or not launcher.read_bytes().startswith(b"#!"):
        raise RuntimeError("broken_fixture_launcher_invalid")
    try:
        process = subprocess.run([str(launcher), "--help"], cwd=cwd, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=30, check=False)
    except FileNotFoundError as failure:
        if failure.errno != errno.ENOENT or not launcher.is_file():
            raise RuntimeError("broken_launcher_failure_not_missing_interpreter") from None
        with log.open("ab") as stream:
            stream.write(b"\n--- expected missing-interpreter ENOENT ---\n")
        return True
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("broken_launcher_failure_not_missing_interpreter") from None
    with log.open("ab") as stream:
        stream.write(b"\n--- broken launcher process status ---\n")
        stream.write(process.stdout)
        stream.write(process.stderr)
    return process.returncode != 0


def _tree(root, excluded):
    found = {}
    for parent, dirs, files in os.walk(root, topdown=True, followlinks=False):
        parent = Path(parent)
        for name in list(dirs):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                dirs.remove(name)
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                found[relative] = ("link", os.readlink(path))
                dirs.remove(name)
            else:
                found[relative] = ("dir", stat.S_IMODE(info.st_mode))
        for name in files:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                found[relative] = ("link", os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                found[relative] = ("file", stat.S_IMODE(info.st_mode), path.read_bytes())
            else:
                raise RuntimeError("unexpected_prefix_entry_type")
    return found


def _manifest_sha(manifest, name):
    for item in manifest.get("artifacts", []):
        if item.get("name") == name:
            return item.get("sha256")
    return None


def run(wheel, *, source_root=None):
    wheel = Path(wheel).resolve()
    source = Path(source_root or Path(__file__).resolve().parents[1]).resolve()
    if wheel.name != WHEEL_NAME or not wheel.is_file():
        raise RuntimeError("required_wheel_missing")
    manifest_path = source / "dist/package-verification.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_hash = _manifest_sha(manifest, wheel.name)
    if not expected_hash or hashlib.sha256(wheel.read_bytes()).hexdigest() != expected_hash:
        raise RuntimeError("wheel_manifest_mismatch")

    root = Path(tempfile.mkdtemp(prefix="agent-handoff-installer-repair-"))
    root.chmod(0o700)
    log = root / "private.log"
    _private_file(log, b"")
    prefix, checkout, private = root / "prefix", root / "checkout", root / "private"
    checkout.mkdir(mode=0o700)
    private.mkdir(mode=0o700)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PIP_NO_INDEX"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_CONFIG_FILE"] = os.devnull

    _run([sys.executable, "-m", "venv", str(prefix)], cwd=root, env=env, log=log, expected=0)
    python = prefix / "bin/python"
    pip = prefix / "bin/pip"
    _run([str(pip), "install", "--no-index", "--no-deps", str(wheel)],
         cwd=root, env=env, log=log, expected=0)
    with zipfile.ZipFile(wheel) as archive:
        wheel_contains_repair = any(name.endswith("agent_handoff/installer_repair.py")
                                    for name in archive.namelist())
    launcher = prefix / "bin/agentctl"
    if not launcher.is_file():
        raise RuntimeError("installed_launcher_missing")
    help_before = _run([str(launcher), "--help"], cwd=root, env=env, log=log, expected=0)
    import_before = _run([str(python), "-c", "import agent_handoff; print(agent_handoff.__file__)"],
                         cwd=root, env=env, log=log, expected=0)
    installed_import = Path(import_before.stdout.decode("utf-8", "strict").strip()).is_relative_to(prefix)
    if not installed_import:
        raise RuntimeError("installed_import_escaped_prefix")

    sys.path.insert(0, str(source / "src"))
    from agent_handoff.host_recovery import HostFileRecovery
    from agent_handoff.paths import PrivateStore
    from agent_handoff.public_results import validate_host_result

    original = launcher.read_bytes()
    mode = stat.S_IMODE(launcher.stat().st_mode)
    owner = (launcher.stat().st_uid, launcher.stat().st_gid)
    first_line, separator, body = original.partition(b"\n")
    if not separator or not first_line.startswith(b"#!"):
        raise RuntimeError("installed_launcher_shebang_missing")
    # Keep the existing launcher body exactly; only make the interpreter path nonexistent.
    missing_interpreter = root / "missing-runtime" / "bin" / "python3"
    broken = b"#!" + os.fsencode(missing_interpreter) + b"\n" + body
    with PrivateStore(private / "host", checkout) as store:
        backup = HostFileRecovery(store).backup(launcher)
        backup_record, backup_bytes = HostFileRecovery(store).read(backup["backup_handle"])
        if backup_bytes != original or backup_record["path"] != str(launcher):
            raise RuntimeError("healthy_backup_not_verified")
        excluded = {launcher.relative_to(prefix).as_posix()}
        before_tree = _tree(prefix, excluded)
        launcher.write_bytes(broken)
        os.chmod(launcher, mode)
        os.chown(launcher, *owner)
        with launcher.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            parent_fd = os.open(launcher.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError:
            pass
        failed_launch = _broken_launcher_fails(launcher, cwd=root, env=env, log=log)
        if not failed_launch:
            raise RuntimeError("broken_launcher_unexpectedly_ran")
        if launcher.read_bytes() != broken or (launcher.stat().st_uid, launcher.stat().st_gid) != owner:
            raise RuntimeError("fault_fixture_changed_unexpectedly")

        repair_script = source / "scripts/installer_repair.py"
        repair = _run([sys.executable, str(repair_script), "--prefix", str(prefix),
                       "--known-good-handle", backup["backup_handle"], "--private-root", str(private),
                       "--checkout", str(checkout)], cwd=root, env=env, log=log, expected=0)
        records = [json.loads(line) for line in repair.stdout.splitlines() if line.strip()]
        if len(records) != 2:
            raise RuntimeError("repair_event_count_invalid")
        for record in records:
            validate_host_result(record, require_host=True)
        if ([record.get("command") for record in records] != ["host backup", "host restore"] or
                records[0].get("backup_verified") is not True or
                records[1].get("recovery_outcome") != "restored"):
            raise RuntimeError("repair_event_sequence_invalid")
        pre_repair_handle = records[0].get("backup_handle")
        pre_record, pre_bytes = HostFileRecovery(store).read(pre_repair_handle)
        if pre_bytes != broken or pre_record["path"] != str(launcher):
            raise RuntimeError("broken_launcher_backup_not_preserved")
        restored = launcher.read_bytes()
        restored_mode = stat.S_IMODE(launcher.stat().st_mode)
        restored_owner = (launcher.stat().st_uid, launcher.stat().st_gid)
        if restored != original or restored_mode != mode or restored_owner != owner:
            raise RuntimeError("launcher_restore_mismatch")
        after_tree = _tree(prefix, excluded)
        if before_tree != after_tree:
            raise RuntimeError("unrelated_prefix_content_changed")
        help_after = _run([str(launcher), "--help"], cwd=root, env=env, log=log, expected=0)
        import_after = _run([str(python), "-c", "import agent_handoff; print(agent_handoff.__file__)"],
                            cwd=root, env=env, log=log, expected=0)
        import_stays_installed = Path(import_after.stdout.decode("utf-8", "strict").strip()).is_relative_to(prefix)
        final_tree = _tree(prefix, excluded)
        source_unchanged = not any(checkout.iterdir())
        if not import_stays_installed or not source_unchanged or before_tree != final_tree:
            raise RuntimeError("post_repair_scope_check_failed")

    evidence = {
        "schema_version": 1,
        "acceptance_id": "A58",
        "passed": True,
        "evidence_scope": "one isolated Linux venv wheel installation; source repair script; one backed-up agentctl shebang repair",
        "wheel_artifact_manifest_verified": True,
        "wheel_contains_installer_repair_module": wheel_contains_repair,
        "repair_executed_from_current_source": True,
        "isolated_venv_installed": True,
        "installed_cli_help_before_and_after": bool(help_before.returncode == 0 and help_after.returncode == 0),
        "installed_import_resolved_inside_prefix_before_and_after": bool(installed_import and import_stays_installed),
        "healthy_backup_verified": True,
        "broken_shebang_launch_failed": True,
        "repair_jsonl_valid_and_ordered": True,
        "repair_jsonl_temporal_order_proven_by_subprocess": False,
        "pre_repair_backup_preserved_broken_launcher": True,
        "launcher_bytes_mode_owner_restored": True,
        "unrelated_prefix_bytes_modes_symlinks_preserved": True,
        "empty_fixture_checkout_unchanged": True,
        "global_host_changes_attempted": False,
        "global_host_snapshot_performed": False,
        "scope_limitation": "No comprehensive global host configuration snapshot; all mutations were confined to the isolated temporary prefix.",
        "provider_or_network_used": False,
        "full_suite_or_package_build_run": False,
    }
    _private_file(root / "evidence.json", json.dumps(evidence, sort_keys=True).encode())
    POINTER.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pointer_fd = os.open(POINTER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(pointer_fd, "wb") as stream:
        stream.write((str(root) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(POINTER, 0o600)
    return evidence, root


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    evidence, _root = run(args.wheel, source_root=args.source_root)
    output = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
