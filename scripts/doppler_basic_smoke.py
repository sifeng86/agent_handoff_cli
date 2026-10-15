"""A44/A46 basic Doppler write acceptance; live use requires an explicit flag."""
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

PROJECT, CONFIG = "agent-handoff-test", "dev"


def save_private(path, record):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())


class ExternalFixtureWriter:
    """CLI setup and cleanup writes are accounted separately from service writes."""
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = 0
        self.uncertain = False

    def change(self, key, action, value=None):
        if self.uncertain:
            raise RuntimeFailure("Fixture write outcome is unknown; no further write is allowed.", "unknown", 5)
        if action not in ("set", "delete") or (action == "set" and not isinstance(value, str)):
            raise ValueError("Invalid fixture write.")
        save_private(self.evidence, {"key": key, "action": action, "phase": "intent"})
        args = ["doppler", "--no-check-version", "--no-read-env", "--attempts", "1", "--timeout", "10s",
                "--silent", "secrets", action, key, "--project", PROJECT, "--config", CONFIG]
        if action == "set":
            args.append("--no-interactive")
        else:
            args.append("--yes")
        payload = value.encode("utf-8") if action == "set" else None
        if payload is not None and value.endswith("\n"):
            payload += b"\n"
        env = {name: item for name, item in os.environ.items() if not name.startswith("DOPPLER_")}
        self.calls += 1
        try:
            response = subprocess.run(args, input=payload, capture_output=True, timeout=15, env=env)
        except (OSError, subprocess.TimeoutExpired):
            self.uncertain = True
            raise RuntimeFailure("Fixture write outcome is unknown; preserve private recovery evidence.", "unknown", 5) from None
        if response.returncode:
            self.uncertain = True
            raise RuntimeFailure("Fixture write was not acknowledged; preserve private recovery evidence.", "unknown", 5)
        save_private(self.evidence, {"key": key, "action": action, "phase": "acknowledged"})


def _apply_local(service, key, value):
    document = service.environment.read()
    service.environment.apply({key: value}, expected=document.values,
                              scope=set(service.manifest["keys"]))


def _ensure_result(result):
    if result.get("status") not in (None, "completed") or result.get("secrets_shared") is not True:
        raise RuntimeFailure("Secret synchronization did not complete.", result.get("status", "unknown"), 5)


def _authentication_denial_probe(project, config, key):
    """Require an auth-specific JSON error from a read-only, synthetic-token get."""
    invalid_token = "synthetic-invalid-" + secrets.token_urlsafe(24)
    args = ["doppler", "--token", invalid_token, "--no-check-version", "--no-read-env",
            "--attempts", "1", "--timeout", "10s", "--silent", "secrets", "get", key,
            "--json", "--no-exit-on-missing-secret", "--project", project, "--config", config]
    env = {name: item for name, item in os.environ.items() if not name.startswith("DOPPLER_")}
    try:
        response = subprocess.run(args, capture_output=True, timeout=15, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if response.returncode == 0:
        return False
    for raw in (response.stderr, response.stdout):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (AttributeError, UnicodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        error = payload.get("error")
        if isinstance(error, str):
            normalized = error.casefold()
            if "token" in normalized and any(word in normalized for word in
                    ("invalid", "unauthorized", "authentication")):
                return True
    return False


def run(project=PROJECT, config=CONFIG, *, run_live=False, evidence_parent=None, adapter_factory=None):
    # All authorization and opt-in checks precede fixture creation or provider access.
    if not run_live:
        raise ValueError("Live acceptance requires explicit --run-live.")
    if (project, config) != (PROJECT, CONFIG):
        raise ValueError("Only the explicitly authorized isolated test config is supported.")

    evidence = Path(tempfile.mkdtemp(prefix="agent-handoff-doppler-basic-", dir=evidence_parent))
    evidence.chmod(0o700)
    sources = {name: (ROOT / name).read_bytes() for name in
               ("scripts/doppler_basic_smoke.py", "src/agent_handoff/secrets/doppler.py",
                "src/agent_handoff/secrets/service.py")}
    save_private(evidence / "source-hashes.json", {name: hashlib.sha256(data).hexdigest()
                 for name, data in sources.items()})

    suffix = uuid.uuid4().hex.upper()
    key_a, key_b = "RUNTIME_BASIC_A_" + suffix, "RUNTIME_BASIC_B_" + suffix
    initial_a, initial_b = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    changed_a = "basic multiline value\nsecond line\n"
    conflict_local_a, conflict_remote_a = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    lost_ack_a = secrets.token_urlsafe(24)
    local_a = "basic A46 local edit\n"
    remote_b = secrets.token_urlsafe(32)
    local_only = "preserve local-only material " + secrets.token_urlsafe(12)
    save_private(evidence / "recovery.json", {"project": project, "config": config,
        "keys": [key_a, key_b], "initial": {key_a: initial_a, key_b: initial_b},
        "A44": {key_a: changed_a}, "A46_local": {key_a: local_a},
        "stale_plan": {"local": conflict_local_a, "external": conflict_remote_a},
        "injected_lost_ack": {key_a: lost_ack_a},
        "A46_external": {key_b: remote_b}, "local_only": local_only})

    report = {"acceptance_id": "A44-A46-basic", "passed": False,
              "destination_authorized": True, "runtime_write_count": 0,
              "setup_write_count": 0, "cleanup_write_attempt_count": 0, "cleanup_write_count": 0,
              "cleanup_verified": False,
              "source_hashes_recorded": True, "multiline_roundtrip_verified": False,
              "doppler_version_verified": False, "authentication_denial_verified": False,
              "scope_specific_403_proven": False,
              "A44_verified": False, "A44_baseline_verified": False,
              "A46_verified": False, "A46_baseline_verified": False, "owned_resume_verified": False,
              "stale_plan_rejected_before_write": False,
              "lost_ack_injected_after_actual_adapter_success": False,
              "lost_ack_resolved_by_fresh_read": False, "lost_ack_no_replay": False,
              "source_head_index_ref_owner_mainline_unchanged": False,
              "cleanup_withheld": False}
    report["runner_sha256"] = hashlib.sha256(sources["scripts/doppler_basic_smoke.py"]).hexdigest()
    fixture, adapter = RepositoryFixture(), None
    writer = ExternalFixtureWriter(evidence / "fixture-write.json")
    runtime_operations = []
    created_keys = set()
    expected_remote = {}
    uncertain = False
    public_results = []
    base_write = None
    original_operation_status = None
    injected_lost_ack_value = None
    lost_ack_operation_ids = set()
    try:
        fixture.setUp()
        fixture.runtime.acquire()
        adapter_type = adapter_factory or Doppler
        adapter = adapter_type(project, config, fixture.checkout)
        if adapter.read({key_a, key_b}):
            raise RuntimeFailure("Disposable key collision; no provider write was performed.", "rejected", 4)

        # Setup writes are deliberately kept outside the runtime adapter count.
        writer.change(key_a, "set", initial_a)
        created_keys.add(key_a)
        writer.change(key_b, "set", initial_b)
        created_keys.add(key_b)
        if adapter.read({key_a, key_b}) != {key_a: initial_a, key_b: initial_b}:
            raise RuntimeFailure("Private fixture seed verification failed.")
        expected_remote = {key_a: initial_a, key_b: initial_b}
        report["doppler_version_verified"] = bool(getattr(adapter, "version_verified", True))
        report["authentication_denial_verified"] = _authentication_denial_probe(project, config, key_a)
        if not report["authentication_denial_verified"]:
            raise RuntimeFailure("Read-only synthetic-token probe did not receive an auth-specific JSON error.",
                                 "not_ready", 6)

        original_write = adapter.write_key
        base_write = original_write
        original_operation_status = adapter.operation_status

        def observed_operation_status(operation_id):
            if operation_id in lost_ack_operation_ids:
                return "unknown"
            return original_operation_status(operation_id)

        adapter.operation_status = observed_operation_status

        def counted_write(key, value, *, expected, lease, operation_id):
            runtime_operations.append({"key": key, "operation_id": operation_id, "value": value,
                                      "phase": "intent"})
            save_private(evidence / "runtime-writes.json", runtime_operations)
            original_write(key, value, expected=expected, lease=lease, operation_id=operation_id)
            runtime_operations[-1]["phase"] = "acknowledged"
            save_private(evidence / "runtime-writes.json", runtime_operations)
            if (injected_lost_ack_value is not None and value == injected_lost_ack_value and
                    original_operation_status(operation_id) == "published"):
                lost_ack_operation_ids.add(operation_id)
                runtime_operations[-1]["phase"] = "acknowledged_then_injected_lost_ack"
                save_private(evidence / "runtime-writes.json", runtime_operations)
                raise RuntimeFailure("Injected lost acknowledgement after adapter success.", "unknown", 5)

        adapter.write_key = counted_write
        manifest = {"schema_version": 1, "keys": {
            key_a: {"classification": "shared", "required": True},
            key_b: {"classification": "shared", "required": True},
            "LOCAL_ONLY": {"classification": "local-only", "required": False}}}
        service = SecretService(fixture.runtime, manifest, adapter)
        service.environment.apply({"LOCAL_ONLY": local_only}, expected={}, scope=set(manifest["keys"]))
        binding = service.propose_binding("doppler", project=project, config=config, upload_authorized=True)
        public_results.append(binding)
        approved = service.apply_decision(binding["decision"]["decision_id"], "approve-secret-binding")
        public_results.append(approved)

        baseline_plan = service.reconcile()
        public_results.append(baseline_plan)
        _ensure_result(service.apply(baseline_plan["plan_id"]))
        if service.environment.read().values.get("LOCAL_ONLY") != local_only:
            raise RuntimeFailure("Local-only value was not retained.")

        # A44: only the local A value changes; service upload and readback are real.
        _apply_local(service, key_a, changed_a)
        plan_a44 = service.reconcile()
        public_results.append(plan_a44)
        applied_a44 = service.apply(plan_a44["plan_id"])
        public_results.append(applied_a44)
        _ensure_result(applied_a44)
        if adapter.read({key_a, key_b}) != {key_a: changed_a, key_b: initial_b}:
            raise RuntimeFailure("A44 scoped provider readback failed.")
        report["A44_verified"] = True
        report["A44_baseline_verified"] = (
            service._baseline(service._binding()).read() == {key_a: changed_a, key_b: initial_b})
        if not report["A44_baseline_verified"]:
            raise RuntimeFailure("A44 verified baseline did not advance with the accepted write.")
        expected_remote = {key_a: changed_a, key_b: initial_b}
        report["multiline_roundtrip_verified"] = adapter.read({key_a}).get(key_a) == changed_a

        # A stale plan must be rejected before any shared mutation; restore only
        # the fixture value just observed and acknowledged by the external writer.
        _apply_local(service, key_a, conflict_local_a)
        stale_plan = service.reconcile()
        public_results.append(stale_plan)
        baseline_before_stale = service._baseline(service._binding()).read()
        runtime_before_stale = len(runtime_operations)
        writer.change(key_a, "set", conflict_remote_a)
        if adapter.read({key_a, key_b}) != {key_a: conflict_remote_a, key_b: initial_b}:
            raise RuntimeFailure("Stale-plan conflict fixture readback failed.")
        expected_remote = {key_a: conflict_remote_a, key_b: initial_b}
        try:
            service.apply(stale_plan["plan_id"])
        except RuntimeFailure as failure:
            rejected_stale = failure.status == "decision_required" and failure.code == 3
        else:
            rejected_stale = False
        if (not rejected_stale or len(runtime_operations) != runtime_before_stale or
                service._baseline(service._binding()).read() != baseline_before_stale):
            raise RuntimeFailure("Stale plan was not rejected before provider write.")
        writer.change(key_a, "set", changed_a)
        if adapter.read({key_a, key_b}) != {key_a: changed_a, key_b: initial_b}:
            raise RuntimeFailure("Known fixture value restoration failed.")
        expected_remote = {key_a: changed_a, key_b: initial_b}
        _apply_local(service, key_a, changed_a)
        report["stale_plan_rejected_before_write"] = True

        # Inject acknowledgement loss only after the real adapter reports its
        # successful CLI write; service resolve must observe, then never replay.
        _apply_local(service, key_a, lost_ack_a)
        lost_ack_plan = service.reconcile()
        public_results.append(lost_ack_plan)
        before_lost_ack = len(runtime_operations)
        injected_lost_ack_value = lost_ack_a
        lost_ack_result = service.apply(lost_ack_plan["plan_id"])
        public_results.append(lost_ack_result)
        if lost_ack_result.get("status") != "unknown":
            raise RuntimeFailure("Injected lost acknowledgement did not leave an unknown operation.")
        pending_operation = lost_ack_result.get("secret_operation_id")
        if (len(runtime_operations) - before_lost_ack != 1 or not pending_operation or
                runtime_operations[-1]["phase"] != "acknowledged_then_injected_lost_ack"):
            raise RuntimeFailure("Lost acknowledgement was not injected after adapter success.")
        report["lost_ack_injected_after_actual_adapter_success"] = True
        injected_lost_ack_value = None
        resolved_lost_ack = service.resolve(pending_operation)
        public_results.append(resolved_lost_ack)
        lost_ack_operation_ids.discard(runtime_operations[-1]["operation_id"])
        final_after_resolve = adapter.read({key_a, key_b})
        expected_remote = {key_a: lost_ack_a, key_b: initial_b}
        if (resolved_lost_ack.get("secrets_shared") is not True or
                final_after_resolve != expected_remote or
                service._baseline(service._binding()).read() != expected_remote):
            raise RuntimeFailure("Fresh observation did not resolve the injected lost acknowledgement.")
        report["lost_ack_resolved_by_fresh_read"] = True
        report["lost_ack_no_replay"] = len(runtime_operations) - before_lost_ack == 1

        # A46: capture immutable publication/source evidence before divergent edits.
        head = fixture.observe()
        owner = fixture.runtime.backend.records(head)["owner"]["owner"]
        source_head = fixture.git(fixture.checkout, "rev-parse", "HEAD")
        source_index = (fixture.checkout / ".git/index").read_bytes()
        mainline = fixture.git(fixture.remote, "rev-parse", "refs/heads/main")
        _apply_local(service, key_a, local_a)
        writer.change(key_b, "set", remote_b)
        if adapter.read({key_a, key_b}) != {key_a: lost_ack_a, key_b: remote_b}:
            raise RuntimeFailure("A46 remote fixture observation failed.")
        expected_remote = {key_a: lost_ack_a, key_b: remote_b}
        before_count = len(runtime_operations)
        plan_a46 = service.reconcile()
        public_results.append(plan_a46)
        applied_a46 = service.apply(plan_a46["plan_id"])
        public_results.append(applied_a46)
        _ensure_result(applied_a46)
        final_values = adapter.read({key_a, key_b})
        baseline_values = service._baseline(service._binding()).read()
        local_values = service.environment.read().values
        report["A46_verified"] = (final_values == {key_a: local_a, key_b: remote_b} and
            local_values.get(key_a) == local_a and local_values.get(key_b) == remote_b and
            local_values.get("LOCAL_ONLY") == local_only and len(runtime_operations) - before_count == 1 and
            runtime_operations[-1]["key"] == key_a)
        report["A46_baseline_verified"] = baseline_values == {key_a: local_a, key_b: remote_b}
        if not report["A46_verified"] or not report["A46_baseline_verified"]:
            raise RuntimeFailure("A46 merged scope or single-key upload verification failed.")
        expected_remote = {key_a: local_a, key_b: remote_b}
        after = fixture.observe()
        recovery = fixture.runtime.recover()
        report["owned_resume_verified"] = recovery.get("ownership") == "owned"
        report["source_head_index_ref_owner_mainline_unchanged"] = (
            after == head and fixture.runtime.backend.records(after)["owner"]["owner"] == owner and
            report["owned_resume_verified"] and
            fixture.git(fixture.checkout, "rev-parse", "HEAD") == source_head and
            (fixture.checkout / ".git/index").read_bytes() == source_index and
            fixture.git(fixture.remote, "rev-parse", "refs/heads/main") == mainline)
        if not report["source_head_index_ref_owner_mainline_unchanged"]:
            raise RuntimeFailure("Git source or runtime ownership evidence changed.")
        public_text = json.dumps(public_results, sort_keys=True)
        private_values = (initial_a, initial_b, changed_a, conflict_local_a,
                          conflict_remote_a, lost_ack_a, local_a, remote_b, local_only)
        if any(value in public_text or json.dumps(value)[1:-1] in public_text for value in private_values):
            raise RuntimeFailure("Private value appeared in a public service result.")
        report.update(public_value_leak=False)
        report["passed"] = all(report[name] for name in (
            "destination_authorized", "source_hashes_recorded", "doppler_version_verified",
            "authentication_denial_verified", "A44_verified", "A44_baseline_verified",
            "multiline_roundtrip_verified", "stale_plan_rejected_before_write",
            "lost_ack_injected_after_actual_adapter_success", "lost_ack_resolved_by_fresh_read",
            "lost_ack_no_replay", "A46_verified", "A46_baseline_verified",
            "owned_resume_verified", "source_head_index_ref_owner_mainline_unchanged")
            ) and report["public_value_leak"] is False
    except RuntimeFailure as failure:
        report["failure_status"] = failure.status
        report["failure_code"] = failure.code
        uncertain = (getattr(writer, "uncertain", False) or any(
            adapter.operation_status(item["operation_id"]) == "unknown" for item in runtime_operations
        )) if adapter is not None else getattr(writer, "uncertain", False)
    except Exception:
        report["failure_status"] = "private_fixture_failure"
        uncertain = True
    finally:
        report["runtime_write_count"] = len(runtime_operations)
        report["setup_write_count"] = writer.calls
        if created_keys and not uncertain and adapter is not None:
            try:
                observed = adapter.read(created_keys)
                expected = dict(expected_remote)
                if observed != expected:
                    raise RuntimeFailure("Fixture values changed; cleanup withheld.", "rejected", 4)
                for key in sorted(created_keys):
                    cleanup_id = uuid.uuid4().hex
                    save_private(evidence / "cleanup-intent.json", {"key": key, "expected": expected[key],
                                 "operation_id": cleanup_id, "phase": "intent"})
                    report["cleanup_write_attempt_count"] += 1
                    if base_write is None:
                        raise RuntimeFailure("Cleanup writer was not initialized; recovery is preserved.", "unknown", 5)
                    base_write(key, None, expected=expected[key], lease=None, operation_id=cleanup_id)
                    if adapter.operation_status(cleanup_id) != "published":
                        raise RuntimeFailure("Cleanup deletion was not acknowledged.", "unknown", 5)
                    report["cleanup_write_count"] += 1
                    save_private(evidence / "cleanup-intent.json", {"key": key,
                                 "operation_id": cleanup_id, "phase": "acknowledged"})
                report["cleanup_verified"] = adapter.read(created_keys) == {}
            except Exception:
                report["cleanup_verified"] = False
                report["cleanup_withheld"] = True
                uncertain = True
        elif created_keys:
            report["cleanup_withheld"] = True
        report["setup_write_count"] = writer.calls
        report["passed"] = report["passed"] and report["cleanup_verified"]
        save_private(evidence / "result.json", report)
        pointer = Path("/tmp/agent-handoff-doppler-basic-evidence.pointer")
        try:
            fd = os.open(pointer, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(str(evidence) + "\n")
        except OSError:
            pass
        if hasattr(fixture, "doCleanups") and fixture._cleanups:
            fixture.doCleanups()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true", required=True)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--config", default=CONFIG)
    args = parser.parse_args(argv)
    try:
        report = run(args.project, args.config, run_live=args.run_live)
    except ValueError:
        report = {"acceptance_id": "A44-A46-basic", "passed": False,
                  "failure_status": "live_opt_in_or_destination_rejected"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
