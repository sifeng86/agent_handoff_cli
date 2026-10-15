"""Offline safety checks for the live disposable-key test driver."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from scripts import doppler_delta_smoke as smoke


class DopplerDeltaSmokeTests(unittest.TestCase):
    def test_other_destination_rejected_before_any_fixture_or_provider_access(self):
        with patch.object(smoke, "RepositoryFixture") as fixture:
            with self.assertRaises(ValueError):
                smoke.run("production", "prd")
            fixture.assert_not_called()

    def test_timeout_blocks_retry_and_cleanup_write(self):
        with tempfile.TemporaryDirectory() as root:
            writer = smoke.ExternalFixtureWriter("RUNTIME_A45_TEST", Path(root) / "intent.json")
            with patch.object(smoke.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 15)) as proc:
                with self.assertRaises(RuntimeFailure):
                    writer.change("set", "synthetic-value")
                with self.assertRaises(RuntimeFailure):
                    writer.change("delete")
                self.assertTrue(writer.uncertain)
                self.assertEqual(writer.calls, 1)
                self.assertEqual(proc.call_count, 1)

    def test_driver_runs_real_local_service_then_cleans_only_its_disposable_key(self):
        remote = {}

        def read(adapter, scope):
            return {key: remote[key] for key in scope if key in remote}

        def external(writer, action, value=None):
            writer.calls += 1
            if action == "set":
                remote[writer.key] = value
            else:
                remote.pop(writer.key)

        with patch.object(smoke.Doppler, "read", read), patch.object(smoke.ExternalFixtureWriter, "change", external):
            report = smoke.run("agent-handoff-test", "dev", evidence_pointer=None)
        self.assertTrue(report["passed"])
        self.assertTrue(report["persisted_baseline_verified"])
        self.assertTrue(report["old_local_backed_up_before_replacement"])
        self.assertTrue(report["source_head_index_ref_owner_mainline_unchanged"])
        self.assertFalse(report["runtime_remote_write_attempted"])
        self.assertTrue(report["cleanup_verified"])
        self.assertEqual(report["fixture_external_write_calls"], 3)
        self.assertEqual(remote, {})
