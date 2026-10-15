"""Offline wheel/sdist build, installation and external-CWD CLI smoke."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def run(args, *, cwd, env, log, expected_code=0):
    process = subprocess.run(args, cwd=cwd, env=env, capture_output=True)
    with log.open("ab") as stream:
        stream.write(process.stdout + process.stderr)
    if process.returncode != expected_code:
        raise RuntimeError("Package verification failed; inspect private build log.")
    return process.stdout


def smoke(build_tools):
    source = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix="agent-handoff-package-"))
    root.chmod(0o700)
    log = root / "build.log"
    log.touch(mode=0o600)
    staging = root / "source"
    staging.mkdir()
    for name in ("src", "schemas", "template", "tests", "scripts", "docs"):
        shutil.copytree(source / name, staging / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    for name in ("pyproject.toml", "MANIFEST.in", "README.md"):
        shutil.copy2(source / name, staging / name)
    sys.path.insert(0, str(source / "src"))
    from agent_handoff.host_guard import HostGuard, require_fresh_prefix
    from agent_handoff.paths import PrivateStore
    host_store = PrivateStore(root / "host-evidence", staging)
    host_guard = HostGuard(host_store)
    host_inspection = host_guard.inspect()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    build_env = dict(env, PYTHONPATH=str(build_tools))
    version = run([sys.executable, "-c", "import setuptools; print(setuptools.__version__)"],
                  cwd=root, env=build_env, log=log).decode().strip()
    if version != "75.8.0":
        raise RuntimeError("Build requires the pinned setuptools 75.8.0.")
    dist = root / "dist"
    dist.mkdir()
    for action in ("build_wheel", "build_sdist"):
        run([sys.executable, "-c", "from setuptools.build_meta import " + action +
             "; " + action + "(" + repr(str(dist)) + ")"], cwd=staging, env=build_env, log=log)
    wheel, = dist.glob("*.whl")
    sdist, = dist.glob("*.tar.gz")
    resources = [path for folder in ("schemas", "template")
                 for path in (staging / folder).rglob("*") if path.is_file()]
    with zipfile.ZipFile(wheel) as archive, tarfile.open(sdist) as source_archive:
        source_prefix = source_archive.getmembers()[0].name.split("/")[0]
        for path in resources:
            relative = path.relative_to(staging).as_posix()
            wheel_path = wheel.stem.split("-py3")[0] + ".data/data/share/agent-handoff/" + relative
            if archive.read(wheel_path) != path.read_bytes():
                raise RuntimeError("Wheel resource differs from source.")
            with source_archive.extractfile(source_prefix + "/" + relative) as stream:
                if stream.read() != path.read_bytes():
                    raise RuntimeError("Source archive resource differs from source.")
        for path in (staging / "src/agent_handoff").rglob("*.py"):
            if archive.read(path.relative_to(staging / "src").as_posix()) != path.read_bytes():
                raise RuntimeError("Wheel code differs from source.")
            with source_archive.extractfile(source_prefix + "/" + path.relative_to(staging).as_posix()) as stream:
                if stream.read() != path.read_bytes():
                    raise RuntimeError("Source archive code differs from source.")
    results = []
    for kind, artifact in (("wheel", wheel), ("sdist", sdist)):
        prefix = root / kind
        require_fresh_prefix(prefix)
        run([sys.executable, "-m", "venv", str(prefix)], cwd=root, env=env, log=log)
        python = prefix / "bin/python"
        run([str(python), "-m", "pip", "install", "--no-cache-dir", "--no-index", "--no-deps", "--no-build-isolation", str(artifact)],
            cwd=root, env=build_env if kind == "sdist" else env, log=log)
        for path in resources:
            installed = prefix / "share/agent-handoff" / path.relative_to(staging)
            if installed.read_bytes() != path.read_bytes():
                raise RuntimeError("Installed resource differs from source.")
        imported = json.loads(run([str(python), "-c",
            "import agent_handoff, json; print(json.dumps(agent_handoff.__file__))"], cwd=root, env=env, log=log))
        if not Path(imported).is_relative_to(prefix):
            raise RuntimeError("Import escaped the isolated installation.")
        work = root / (kind + "-fixture")
        work.mkdir()
        checkout, remote, private = work / "checkout", work / "remote.git", work / "private"
        private.mkdir(mode=0o700)
        from_env = dict(env, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="Package Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                        GIT_COMMITTER_NAME="Package Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        run(["git", "init", "-q", "--bare", str(remote)], cwd=work, env=from_env, log=log)
        run(["git", "init", "-q", "-b", "main", str(checkout)], cwd=work, env=from_env, log=log)
        (checkout / "code.txt").write_text("package fixture\n")
        run(["git", "add", "code.txt"], cwd=checkout, env=from_env, log=log)
        run(["git", "commit", "-qm", "fixture"], cwd=checkout, env=from_env, log=log)
        base = run(["git", "rev-parse", "HEAD"], cwd=checkout, env=from_env, log=log).decode().strip()
        original_index = (checkout / ".git/index").read_bytes()
        run(["git", "push", "-q", str(remote), "main"], cwd=checkout, env=from_env, log=log)
        context = private / "context.json"
        context.write_text(json.dumps({"schema_version": 1, "context": "a" * 64}))
        context.chmod(0o600)
        common = ["--checkout", str(checkout), "--private-root", str(private), "--authority", str(remote),
                  "--publication-ref", "refs/heads/publication", "--context-file", str(context)]
        expected = {"init": "completed", "acquire": "completed", "recover-session": "completed", "release": "completed"}
        revision = None
        for action, status in expected.items():
            extra = ["--base", base, "--mainline-ref", "refs/heads/main"] if action == "init" else []
            report = json.loads(run([str(prefix / "bin/agentctl"), action, *common, *extra], cwd=root, env=env, log=log))
            if report["status"] != status:
                raise RuntimeError("Installed CLI lifecycle failed.")
            if action in ("acquire", "recover-session") and report["ownership"] != "owned":
                raise RuntimeError("Installed CLI did not prove ownership.")
            if action == "recover-session":
                if run(["git", "rev-parse", "refs/heads/publication"], cwd=remote, env=from_env, log=log) != revision:
                    raise RuntimeError("Recovery unexpectedly published a revision.")
            if action == "release" and report["ownership"] != "released":
                raise RuntimeError("Installed CLI did not release ownership.")
            revision = run(["git", "rev-parse", "refs/heads/publication"], cwd=remote, env=from_env, log=log)
        def installed_cli(arguments, *, expected_code=0):
            data = run([str(prefix / "bin/agentctl"), *arguments], cwd=root, env=env, log=log,
                       expected_code=expected_code)
            report = json.loads(data)
            if report["status"] != ("decision_required" if expected_code == 3 else "completed"):
                raise RuntimeError("Installed extended CLI workflow failed.")
            return data, report

        host_config = work / "host-config"
        host_original = b"package-host-private-sentinel\x00\xff\n"
        host_config.write_bytes(host_original)
        host_config.chmod(0o640)
        host_mtime = host_config.stat().st_mtime_ns
        host_common = ["--checkout", str(checkout), "--private-root", str(private)]
        host_reports = []
        data, backup = installed_cli(["host", "backup", "--host-file", str(host_config), *host_common])
        host_reports.append(data)
        data, _ = installed_cli(["host", "verify-backup", "--backup-handle", backup["backup_handle"], *host_common])
        host_reports.append(data)
        host_config.write_bytes(b"reviewed-package-repair")
        host_config.chmod(0o600)
        data, current = installed_cli(["host", "backup", "--host-file", str(host_config), *host_common])
        host_reports.append(data)
        data, restored = installed_cli(["host", "restore", "--backup-handle", backup["backup_handle"],
                                       "--expected-handle", current["backup_handle"], *host_common])
        host_reports.append(data)
        data, resolved = installed_cli(["host", "resolve", "--operation-id", restored["operation_id"], *host_common])
        host_reports.append(data)
        host_fields = {"schema_version", "command", "status", "operation_id", "published_revision", "code_publication",
                       "ownership", "secrets_shared", "decision", "next_action", "backup_handle", "backup_verified", "recovery_outcome"}
        forbidden_host_values = [b"package-host-private-sentinel", str(host_config).encode(),
                                 base64.b64encode(host_original), hashlib.sha256(host_original).hexdigest().encode()]
        if (restored["recovery_outcome"] != "restored" or resolved["recovery_outcome"] != "restored" or
                host_config.read_bytes() != host_original or host_config.stat().st_mode & 0o777 != 0o640 or
                host_config.stat().st_mtime_ns != host_mtime or
                any(not set(json.loads(data)) <= host_fields or
                    any(value in data for value in forbidden_host_values) for data in host_reports)):
            raise RuntimeError("Installed host recovery or public-output privacy failed.")

        target_ref = "refs/heads/transition-target"
        target_flags = ["--target-authority", str(remote), "--target-publication-ref", target_ref]
        _, proposal = installed_cli(["authority", "propose", *common, *target_flags], expected_code=3)
        if proposal["decision"]["category"] != "authority_binding":
            raise RuntimeError("Installed authority transition did not request scoped consent.")
        _, selected = installed_cli(["authority", "apply", "--decision-id", proposal["decision"]["decision_id"],
                                     "--option-id", "switch", *common, *target_flags])
        if selected["transition_outcome"] != "selected" or selected["code_publication"] != "not_attempted":
            raise RuntimeError("Installed authority transition unexpectedly published.")
        installed_cli(["acquire", *common], expected_code=3)
        target_common = list(common)
        target_common[target_common.index("--publication-ref") + 1] = target_ref
        installed_cli(["init", *target_common, "--base", base, "--mainline-ref", "refs/heads/main"])
        _, target_acquired = installed_cli(["acquire", *target_common])
        _, target_released = installed_cli(["release", *target_common])
        if (target_acquired["ownership"] != "owned" or target_released["ownership"] != "released" or
                run(["git", "rev-parse", "refs/heads/publication"], cwd=remote, env=from_env, log=log) != revision):
            raise RuntimeError("Installed transition ownership isolation failed.")
        if (run(["git", "rev-parse", "refs/heads/main"], cwd=remote, env=from_env, log=log).decode().strip() != base or
                (checkout / ".git/index").read_bytes() != original_index or
                (checkout / "code.txt").read_text() != "package fixture\n"):
            raise RuntimeError("Installed CLI changed main or the original checkout.")
        owner = json.loads(run(["git", "show", "refs/heads/publication:.agent/owner.json"], cwd=remote, env=from_env, log=log))
        if owner["owner"] is not None:
            raise RuntimeError("Released owner record is not null.")
        target_owner = json.loads(run(["git", "show", target_ref + ":.agent/owner.json"], cwd=remote, env=from_env, log=log))
        if target_owner["owner"] is not None:
            raise RuntimeError("Installed target lifecycle retained ownership.")
        results.append({"installation": kind, "cli_lifecycle": list(expected), "resource_count": len(resources),
                        "host_recovery": ["backup", "verify-backup", "restore", "resolve"],
                        "authority_transition": ["propose", "apply", "source_acquire_fenced", "target_init", "target_acquire", "target_release"]})
    host_verification = host_guard.verify(host_inspection["preservation_handle"])
    host_store.close()
    if host_verification["changed_categories"]:
        raise RuntimeError("Protected host configuration changed; preserve evidence and investigate.")
    return {"artifact_root": str(root), "host_preservation": host_verification, "artifacts": [{"name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in (wheel, sdist)], "passed": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-tools", type=Path, required=True,
                        help="Directory containing pinned setuptools and wheel build dependencies")
    args = parser.parse_args()
    try:
        print(json.dumps(smoke(args.build_tools.absolute())))
    except Exception:
        print(json.dumps({"error": "Offline package smoke failed; inspect its private /tmp build log."}))
        raise SystemExit(1)
