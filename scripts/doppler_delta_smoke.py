"""A45 live remote-only delta using one disposable key in the authorized test config."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.secrets.doppler import Doppler
from agent_handoff.secrets.service import SecretService
from test_publication import RepositoryFixture


def save_private(path, record):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())


class ExternalFixtureWriter:
    """Setup events emulate an external editor, never the runtime upload adapter."""
    def __init__(self, key, evidence):
        self.key = key
        self.evidence = evidence
        self.uncertain = False
        self.calls = 0

    def change(self, action, value=None):
        if self.uncertain:
            raise RuntimeFailure("Unknown fixture write blocks further writes.", "unknown", 5)
        save_private(self.evidence, {"key": self.key, "action": action, "phase": "intent"})
        args = ["doppler", "--no-check-version", "--no-read-env", "--attempts", "1",
                "--timeout", "10s", "--silent", "secrets", action, self.key,
                "--project", "agent-handoff-test", "--config", "dev"]
        if action == "delete":
            args.append("--yes")
        elif action != "set" or not isinstance(value, str):
            raise ValueError("Invalid fixture write.")
        self.calls += 1
        try:
            env = {key: item for key, item in os.environ.items() if not key.startswith("DOPPLER_")}
            proc = subprocess.run(args, input=(value + "\n").encode() if value is not None else None,
                                  capture_output=True, timeout=15, env=env)
        except (OSError, subprocess.TimeoutExpired):
            self.uncertain = True
            raise RuntimeFailure("Fixture write outcome is unknown; no retry or cleanup.", "unknown", 5) from None
        if proc.returncode:
            self.uncertain = True
            raise RuntimeFailure("Fixture write did not acknowledge success; no retry or cleanup.", "unknown", 5)
        save_private(self.evidence, {"key": self.key, "action": action, "phase": "acknowledged"})


def run(project, config, *, evidence_pointer="/tmp/agent-handoff-doppler-a45-evidence.pointer"):
    if (project, config) != ("agent-handoff-test", "dev"):
        raise ValueError("Only the explicitly authorized isolated test config is supported.")
    evidence = Path(tempfile.mkdtemp(prefix="agent-handoff-doppler-a45-"))
    evidence.chmod(0o700)
    runner_source = Path(__file__).read_bytes()
    save_private(evidence / "runner-source.json", {"source": runner_source.decode()})
    key = "RUNTIME_A45_" + uuid.uuid4().hex.upper()
    initial, changed = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    save_private(evidence / "recovery.json", {"project": project, "config": config,
                 "key": key, "initial": initial, "changed": changed})
    fixture = RepositoryFixture()
    writer = ExternalFixtureWriter(key, evidence / "fixture-write.json")
    created = False
    report = {"acceptance_id": "A45", "passed": False, "runtime_remote_write_attempted": False,
              "cleanup_verified": False, "provider": "Doppler", "project": project, "config": config,
              "runner_sha256": hashlib.sha256(runner_source).hexdigest()}
    public_results = []
    try:
        fixture.setUp()
        fixture.runtime.acquire()
        adapter = Doppler(project, config, fixture.checkout)
        if adapter.read({key}):
            raise RuntimeFailure("Disposable test key collision; no write performed.")
        writer.change("set", initial)
        created = True
        if adapter.read({key}) != {key: initial}:
            raise RuntimeFailure("Fixture seed verification failed.")
        service = SecretService(fixture.runtime,
            {"schema_version": 1, "keys": {key: {"classification": "shared", "required": True}}}, adapter)

        def forbid_runtime_write(*args, **kwargs):
            report["runtime_remote_write_attempted"] = True
            raise RuntimeFailure("Runtime upload is forbidden in the remote-only fixture.")

        adapter.write_key = forbid_runtime_write
        binding = service.propose_binding("doppler", project=project, config=config, upload_authorized=False)
        public_results.append(binding)
        public_results.append(service.apply_decision(binding["decision"]["decision_id"], "approve-secret-binding"))
        plan = service.reconcile()
        public_results.extend([plan, service.apply(plan["plan_id"])])
        baseline = service._baseline(service._binding())
        if baseline.read() != {key: initial} or service.environment.read().values != {key: initial}:
            raise RuntimeFailure("Initial baseline/local convergence failed.")
        before_baseline = fixture.runtime.store.read(baseline.name)
        head = fixture.observe()
        owner = fixture.runtime.backend.records(head)["owner"]["owner"]
        source_head = fixture.git(fixture.checkout, "rev-parse", "HEAD")
        source_index = (fixture.checkout / ".git/index").read_bytes()
        writer.change("set", changed)
        if (adapter.read({key}) != {key: changed} or baseline.read() != {key: initial} or
                service.environment.read().values != {key: initial}):
            raise RuntimeFailure("Remote-only delta precondition failed.")
        plan = service.reconcile()
        if fixture.runtime.store.read(baseline.name) != before_baseline:
            raise RuntimeFailure("Planning advanced the baseline prematurely.")
        original_document = service.environment.read()
        old_backups = set(fixture.runtime.store.root.glob("secret-backup-*.json"))
        preserved_before_replace = False

        def verify_preservation(point):
            nonlocal preserved_before_replace
            if point == "before_local_rename":
                backups = set(fixture.runtime.store.root.glob("secret-backup-*.json")) - old_backups
                if len(backups) != 1:
                    raise RuntimeFailure("Expected one new private local preservation record.")
                backup = fixture.runtime.store.read(next(iter(backups)).name)
                if (backup["raw"] != original_document.raw or
                        service.environment.read().raw != original_document.raw):
                    raise RuntimeFailure("Old local data was not preserved before replacement.")
                save_private(evidence / "preservation-check.json",
                             {"backup": backup, "local_before_replace": original_document.raw})
                preserved_before_replace = True

        fixture.runtime.fault = verify_preservation
        applied = service.apply(plan["plan_id"])
        verified = service.verify()
        public_results.extend([plan, applied, verified])
        after_baseline = fixture.runtime.store.read(baseline.name)
        if (applied.get("secrets_shared") is not True or verified.get("secrets_shared") is not True or
                baseline.read() != {key: changed} or service.environment.read().values != {key: changed} or
                after_baseline["verified_sync_id"] != applied["secret_operation_id"] or
                fixture.observe() != head or fixture.runtime.backend.records(head)["owner"]["owner"] != owner or
                fixture.git(fixture.checkout, "rev-parse", "HEAD") != source_head or
                (fixture.checkout / ".git/index").read_bytes() != source_index or
                fixture.git(fixture.remote, "rev-parse", "refs/heads/main") != fixture.base or
                report["runtime_remote_write_attempted"] or not preserved_before_replace):
            raise RuntimeFailure("Live delta or preservation verification failed.")
        public_text = json.dumps(public_results)
        if initial in public_text or changed in public_text:
            raise RuntimeFailure("Synthetic fixture value disclosed in public runtime output.")
        report.update(passed=True, remote_only_delta_verified=True, local_material_verified=True,
                      old_local_backed_up_before_replacement=True,
                      persisted_baseline_verified=True, baseline_operation_identity_verified=True,
                      source_head_index_ref_owner_mainline_unchanged=True, public_value_leak=False)
    except RuntimeFailure as error:
        report.update(failure_status=error.status, failure_code=error.code)
    except Exception:
        report.update(failure_status="private_fixture_failure")
    finally:
        if created and not writer.uncertain:
            try:
                if adapter.read({key}).get(key) not in (initial, changed):
                    raise RuntimeFailure("Fixture key changed externally; cleanup withheld.")
                writer.change("delete")
                report["cleanup_verified"] = adapter.read({key}) == {}
            except Exception:
                report["cleanup_verified"] = False
        report["fixture_external_write_calls"] = writer.calls
        report["fixture_write_outcome_unknown"] = writer.uncertain
        report["passed"] = report["passed"] and report["cleanup_verified"]
        save_private(evidence / "result.json", report)
        if evidence_pointer is not None:
            pointer = os.open(evidence_pointer,
                              os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(pointer, "w") as stream:
                stream.write(str(evidence) + "\n")
        fixture.doCleanups()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        result = run(args.project, args.config)
    except ValueError:
        result = {"acceptance_id": "A45", "passed": False, "failure_status": "unauthorized_fixture_destination"}
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["passed"] else 1)
