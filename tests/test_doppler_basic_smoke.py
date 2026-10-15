"""Offline controls for the explicitly opt-in A44/A46 acceptance driver."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.secrets.adapter import Adapter
from agent_handoff.secrets.service import SecretService
from scripts import doppler_basic_smoke as smoke
from test_publication import RepositoryFixture


class FakeDoppler(Adapter):
    basic_writes = True
    remote = {}
    writes = []
    lose_next_ack = False
    deny_gate = False

    def __init__(self, project, config, checkout):
        super().__init__(project, config)
        self.checkout = checkout
        self.statuses = {}
        self.version_verified = True

    def require_write_gate(self):
        if type(self).deny_gate:
            raise RuntimeFailure("Fake upload grant denied.", "unsupported", 8)

    def lease(self, **facts):
        self.require_write_gate()
        return None

    def read(self, scope):
        return {key: type(self).remote[key] for key in scope if key in type(self).remote}

    def write_key(self, key, value, *, expected, lease, operation_id):
        self.require_write_gate()
        type(self).writes.append(key)
        remote = type(self).remote
        if (key in remote) != (expected is not None) or remote.get(key) != expected:
            self.statuses[operation_id] = "rejected"
            raise RuntimeFailure("Fake expected-value conflict.", "rejected", 4)
        if value is None:
            remote.pop(key, None)
        else:
            remote[key] = value
        if type(self).lose_next_ack:
            type(self).lose_next_ack = False
            self.statuses[operation_id] = "unknown"
            raise RuntimeFailure("Fake lost acknowledgement after write.", "unknown", 5)
        self.statuses[operation_id] = "published"

    def operation_status(self, operation_id):
        return self.statuses.get(operation_id, "unknown")


def fake_fixture_write(writer, key, action, value=None):
    writer.calls += 1
    if action == "set":
        FakeDoppler.remote[key] = value
    else:
        FakeDoppler.remote.pop(key, None)


def fake_auth_denial(args, **kwargs):
    token_index = args.index("--token") + 1
    if not args[token_index].startswith("synthetic-invalid-") or "--no-read-env" not in args:
        raise AssertionError("Auth probe must use a synthetic token and isolated environment.")
    if any(name.startswith("DOPPLER_") for name in kwargs["env"]):
        raise AssertionError("Auth probe must not inherit Doppler environment values.")
    return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b'{"error":"Invalid token"}')


class DopplerBasicSmokeTests(unittest.TestCase):
    def setUp(self):
        FakeDoppler.remote = {}
        FakeDoppler.writes = []
        FakeDoppler.lose_next_ack = False
        FakeDoppler.deny_gate = False

    def test_live_and_destination_guards_precede_fixture_or_provider_access(self):
        with patch.object(smoke, "RepositoryFixture") as fixture, patch.object(smoke.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                smoke.run(run_live=False)
            with self.assertRaises(ValueError):
                smoke.run("other-project", "prod", run_live=True)
            fixture.assert_not_called()
            run.assert_not_called()

    def test_full_A44_A46_lifecycle_uses_local_git_and_cleans_only_own_keys(self):
        with tempfile.TemporaryDirectory() as evidence_root, \
             patch.object(smoke, "Doppler", FakeDoppler), \
             patch.object(smoke.ExternalFixtureWriter, "change", fake_fixture_write), \
             patch.object(smoke, "_authentication_denial_probe", return_value=True):
            report = smoke.run(run_live=True, evidence_parent=evidence_root)
            self.assertTrue(report["passed"])
            self.assertTrue(report["A44_verified"])
            self.assertTrue(report["A44_baseline_verified"])
            self.assertTrue(report["A46_verified"])
            self.assertTrue(report["A46_baseline_verified"])
            self.assertTrue(report["multiline_roundtrip_verified"])
            self.assertTrue(report["owned_resume_verified"])
            self.assertTrue(report["stale_plan_rejected_before_write"])
            self.assertTrue(report["lost_ack_injected_after_actual_adapter_success"])
            self.assertTrue(report["lost_ack_resolved_by_fresh_read"])
            self.assertTrue(report["lost_ack_no_replay"])
            self.assertTrue(report["authentication_denial_verified"])
            self.assertFalse(report["scope_specific_403_proven"])
            self.assertTrue(report["source_head_index_ref_owner_mainline_unchanged"])
            self.assertEqual(report["runtime_write_count"], 3)
            self.assertEqual(report["setup_write_count"], 5)
            self.assertEqual(report["cleanup_write_count"], 2)
            self.assertTrue(report["cleanup_verified"])
            self.assertEqual(FakeDoppler.remote, {})
            self.assertEqual(len(FakeDoppler.writes), 5)
            self.assertEqual(FakeDoppler.writes[0], FakeDoppler.writes[1])
            self.assertEqual(FakeDoppler.writes[1], FakeDoppler.writes[2])
            self.assertNotEqual(FakeDoppler.writes[3], FakeDoppler.writes[4])
            encoded = json.dumps(report)
            self.assertNotIn("basic multiline value", encoded)
            self.assertNotIn("second line", encoded)

    def test_unknown_runtime_write_preserves_recovery_and_withholds_cleanup(self):
        FakeDoppler.lose_next_ack = True
        with tempfile.TemporaryDirectory() as evidence_root, \
             patch.object(smoke, "Doppler", FakeDoppler), \
             patch.object(smoke.ExternalFixtureWriter, "change", fake_fixture_write), \
             patch.object(smoke, "_authentication_denial_probe", return_value=True):
            report = smoke.run(run_live=True, evidence_parent=evidence_root)
            self.assertFalse(report["passed"])
            self.assertTrue(report["cleanup_withheld"])
            self.assertEqual(report["runtime_write_count"], 1)
            self.assertEqual(report["cleanup_write_count"], 0)
            evidence = next(Path(evidence_root).iterdir())
            self.assertTrue((evidence / "recovery.json").is_file())
            public = json.dumps(report)
            self.assertNotIn("basic multiline value", public)

    def test_auth_probe_does_not_accept_network_error_json(self):
        with patch.object(smoke.subprocess, "run", side_effect=fake_auth_denial):
            self.assertTrue(smoke._authentication_denial_probe("agent-handoff-test", "dev", "RUNTIME_BASIC_TEST"))
        response = subprocess.CompletedProcess(["doppler"], 1, stdout=b"",
                                               stderr=b'{"error":"connection timeout"}')
        with patch.object(smoke.subprocess, "run", return_value=response):
            self.assertFalse(smoke._authentication_denial_probe("agent-handoff-test", "dev", "RUNTIME_BASIC_TEST"))

    def _local_service(self):
        fixture = RepositoryFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.runtime.acquire()
        FakeDoppler.remote = {"A": "base-a", "B": "base-b"}
        adapter = FakeDoppler("agent-handoff-test", "dev", fixture.checkout)
        manifest = {"schema_version": 1, "keys": {
            "A": {"classification": "shared", "required": True},
            "B": {"classification": "shared", "required": True}}}
        service = SecretService(fixture.runtime, manifest, adapter)
        proposal = service.propose_binding("doppler", project=adapter.project, config=adapter.config,
                                           upload_authorized=True)
        service.apply_decision(proposal["decision"]["decision_id"], "approve-secret-binding")
        plan = service.reconcile()
        service.apply(plan["plan_id"])
        return fixture, service, adapter

    def test_stale_real_config_conflict_is_rejected_before_any_runtime_write(self):
        fixture, service, adapter = self._local_service()
        service.environment.apply({"A": "local-a"}, expected=service.environment.read().values,
                                  scope=service.scope)
        plan = service.reconcile()
        baseline = service._baseline(service._binding()).read()
        head = fixture.observe()
        FakeDoppler.remote["A"] = "external-conflict"
        with self.assertRaises(RuntimeFailure):
            service.apply(plan["plan_id"])
        self.assertEqual(FakeDoppler.writes, [])
        self.assertEqual(service._baseline(service._binding()).read(), baseline)
        self.assertEqual(fixture.observe(), head)
        self.assertEqual(adapter.read(service.scope)["A"], "external-conflict")

    def test_lost_ack_after_actual_fake_write_resolves_by_observation_without_replay(self):
        fixture, service, adapter = self._local_service()
        service.environment.apply({"A": "desired-a"}, expected=service.environment.read().values,
                                  scope=service.scope)
        plan = service.reconcile()
        FakeDoppler.lose_next_ack = True
        pending = service.apply(plan["plan_id"])
        self.assertEqual(pending["status"], "unknown")
        self.assertEqual(FakeDoppler.writes, ["A"])
        resolved = service.resolve(pending["secret_operation_id"])
        self.assertTrue(resolved["secrets_shared"])
        self.assertEqual(FakeDoppler.writes, ["A"])
        self.assertEqual(adapter.read(service.scope), {"A": "desired-a", "B": "base-b"})

    def test_denied_upload_grant_stops_before_runtime_provider_write(self):
        fixture = RepositoryFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.runtime.acquire()
        FakeDoppler.deny_gate = True
        adapter = FakeDoppler("agent-handoff-test", "dev", fixture.checkout)
        manifest = {"schema_version": 1, "keys": {"A": {"classification": "shared", "required": True}}}
        service = SecretService(fixture.runtime, manifest, adapter)
        with self.assertRaises(RuntimeFailure):
            service.propose_binding("doppler", project=adapter.project, config=adapter.config,
                                    upload_authorized=True)
        self.assertEqual(FakeDoppler.writes, [])


if __name__ == "__main__":
    unittest.main()
