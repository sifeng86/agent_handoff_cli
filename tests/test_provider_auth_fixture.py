"""Offline safety tests for the isolated, unauthenticated Doppler fixture."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts.provider_auth_fixture import (
    CONFIG, PROJECT, DopplerAuthFixture, FixtureEvidenceError, snapshot_host_config,
)


FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(%r).open('a', encoding='utf-8').write(json.dumps({
  'argv': sys.argv[1:],
  'doppler_env': sorted(k for k in os.environ if k.startswith('DOPPLER_')),
  'config_dir': str(Path(sys.argv[sys.argv.index('--config-dir') + 1])),
}, separators=(',', ':')) + '\\n')
mode = os.environ.get('FAKE_DOPPLER_MODE', 'auth')
if '--version' in sys.argv or '--help' in sys.argv:
    raise SystemExit(0)
if mode == 'auth':
    sys.stderr.write('Error: you must provide a token to use this command\\n')
    raise SystemExit(1)
if mode == 'generic-error':
    sys.stderr.write('Error: connection refused\\n')
    raise SystemExit(1)
if mode == 'success-secret':
    sys.stdout.write('PRIVATE_SENTINEL_VALUE\\n')
    raise SystemExit(0)
if mode == 'mutate-host':
    Path(os.environ['FAKE_DOPPLER_MUTATE']).write_text('changed', encoding='utf-8')
    sys.stderr.write('Error: you must provide a token\\n')
    raise SystemExit(1)
raise SystemExit(0)
"""


class ProviderAuthFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.audit = self.root / "cli-audit.jsonl"
        self.cli = self.root / "doppler-real-fixture"
        self.cli.write_text(FAKE_CLI % str(self.audit), encoding="utf-8")
        self.cli.chmod(0o700)
        self.host = self.root / ".doppler"
        self.host.mkdir(mode=0o700)
        (self.host / "config.yaml").write_text("private config fixture", encoding="utf-8")

    def fixture(self):
        return DopplerAuthFixture(self.root / "isolated", executable=self.cli,
                                  approved_keys={"RUNTIME_KEY"}, host_config=self.host)

    def audit_entries(self):
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]

    def test_probe_injects_fixed_isolated_read_and_strips_all_doppler_environment(self):
        with patch.dict(os.environ, {"DOPPLER_TOKEN": "do-not-forward", "DOPPLER_PROJECT": "wrong",
                                     "DOPPLER_CONFIG": "wrong", "DOPPLER_CONFIG_DIR": "/tmp/wrong"}):
            fixture = self.fixture()
            self.assertEqual(fixture.config_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(fixture.config_dir.iterdir()), [])
            observation = fixture.probe_missing_auth({"RUNTIME_KEY"})
        self.assertTrue(observation["no_credentials_classified"])
        self.assertTrue(observation["environment_credentials_removed"])
        self.assertTrue(observation["no_remote_write"])
        self.assertEqual(observation["request_scope"], ["DOPPLER_CONFIG", "DOPPLER_PROJECT", "RUNTIME_KEY"])
        audit = self.audit_entries()[0]
        self.assertEqual(audit["doppler_env"], [])
        self.assertEqual(audit["config_dir"], str(fixture.config_dir))
        argv = audit["argv"]
        for expected in ("--no-read-env", "--no-check-version", "--attempts", "1", "--timeout", "10s",
                         "--json", "--no-exit-on-missing-secret", "--project", PROJECT, "--config", CONFIG):
            self.assertIn(expected, argv)
        self.assertEqual(argv[:2], ["--config-dir", str(fixture.config_dir)])
        requested = argv[argv.index("get") + 1:argv.index("--json")]
        self.assertEqual(set(requested), {"RUNTIME_KEY", "DOPPLER_PROJECT", "DOPPLER_CONFIG"})
        for path in fixture.log_dir.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.host.joinpath("config.yaml").read_text(), "private config fixture")

    def test_help_allowlist_is_readonly_and_unknown_or_login_execution_is_not_spawned(self):
        fixture = self.fixture()
        self.assertEqual(fixture.version()["returncode"], 0)
        self.assertEqual(fixture.help("login")["command"], "login_help")
        self.assertEqual(fixture.help("secrets-get")["command"], "secrets-get_help")
        count = len(self.audit_entries())
        with self.assertRaisesRegex(FixtureEvidenceError, "readonly_help_command_rejected"):
            fixture.help("login --yes")
        self.assertEqual(len(self.audit_entries()), count)
        self.assertTrue(all(entry["doppler_env"] == [] for entry in self.audit_entries()))

    def test_rejects_unapproved_scope_before_process(self):
        fixture = self.fixture()
        with self.assertRaisesRegex(FixtureEvidenceError, "requested_key_scope_rejected"):
            fixture.probe_missing_auth({"UNAPPROVED"})
        self.assertFalse(self.audit.exists())

    def test_generic_transport_error_does_not_count_as_missing_auth(self):
        fixture = self.fixture()
        with patch.dict(os.environ, {"FAKE_DOPPLER_MODE": "generic-error"}):
            with self.assertRaisesRegex(FixtureEvidenceError, "missing_auth_not_proven"):
                fixture.probe_missing_auth({"RUNTIME_KEY"})

    def test_successful_secret_stdout_is_private_and_invalid_for_unauthenticated_fixture(self):
        fixture = self.fixture()
        with patch.dict(os.environ, {"FAKE_DOPPLER_MODE": "success-secret"}):
            with self.assertRaisesRegex(FixtureEvidenceError, "unexpected_authenticated_doppler") as caught:
                fixture.probe_missing_auth({"RUNTIME_KEY"})
        self.assertNotIn("PRIVATE_SENTINEL_VALUE", str(caught.exception))
        private_stdout = next(fixture.log_dir.glob("*-stdout.bin"))
        self.assertEqual(private_stdout.stat().st_mode & 0o777, 0o600)
        self.assertIn(b"PRIVATE_SENTINEL_VALUE", private_stdout.read_bytes())

    def test_detects_mutation_of_existing_host_doppler_config(self):
        fixture = self.fixture()
        with patch.dict(os.environ, {"FAKE_DOPPLER_MODE": "mutate-host",
                                     "FAKE_DOPPLER_MUTATE": str(self.host / "config.yaml")}):
            with self.assertRaisesRegex(FixtureEvidenceError, "host_doppler_config_changed"):
                fixture.probe_missing_auth({"RUNTIME_KEY"})
        self.assertEqual((self.host / "config.yaml").read_text(), "changed")

    def test_root_metadata_replacement_and_symlink_are_fail_closed_before_spawn(self):
        fixture = self.fixture()
        before = snapshot_host_config(self.host)
        self.host.chmod(0o750)
        self.assertNotEqual(snapshot_host_config(self.host), before)
        with self.assertRaisesRegex(FixtureEvidenceError, "host_doppler_config_changed"):
            fixture.version()
        self.assertFalse(self.audit.exists())

        self.host.chmod(0o700)
        old = self.root / "old-doppler-config"
        self.host.rename(old)
        self.host.mkdir(mode=0o700)
        (self.host / "config.yaml").write_text("private config fixture", encoding="utf-8")
        with self.assertRaisesRegex(FixtureEvidenceError, "host_doppler_config_changed"):
            fixture.version()
        self.assertFalse(self.audit.exists())

        link = self.root / "doppler-config-link"
        link.symlink_to(self.host, target_is_directory=True)
        with self.assertRaisesRegex(FixtureEvidenceError, "host_config_snapshot_unsupported"):
            DopplerAuthFixture(self.root / "linked-isolated", executable=self.cli,
                               approved_keys={"RUNTIME_KEY"}, host_config=link)
        self.assertFalse(self.audit.exists())


if __name__ == "__main__":
    unittest.main()
