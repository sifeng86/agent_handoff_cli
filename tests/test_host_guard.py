import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.host_guard import HostGuard, require_fresh_prefix
from agent_handoff.paths import PrivateStore
from test_record_schemas import validator


class HostGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.config = self.root / "host"
        self.config.mkdir()
        self.roots = {}
        for category in ("ssh", "vpn", "firewall", "provider", "git", "network", "shell"):
            path = self.config / category
            path.write_text("private-host-fixture-" + category)
            self.roots[category] = [path]
        self.store = PrivateStore(self.root / "private", self.checkout)
        self.addCleanup(self.store.close)
        self.guard = HostGuard(self.store, self.roots)

    def test_A57_fresh_venv_preserves_existing_host_config_without_public_hashes(self):
        before = {path: path.read_bytes() for paths in self.roots.values() for path in paths}
        inspected = self.guard.inspect()
        prefix = self.root / "venv"
        require_fresh_prefix(prefix)
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(prefix)], check=True, capture_output=True)
        verified = self.guard.verify(inspected["preservation_handle"])
        self.assertEqual(verified["status"], "completed")
        self.assertEqual(verified["preserved_categories"], sorted(self.roots))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        public = json.dumps([inspected, verified])
        for value in before.values():
            self.assertNotIn(value.decode(), public)
            self.assertNotIn(hashlib.sha256(value).hexdigest(), public)
        validator("host-preservation").validate(self.store.read("host-preservation-" + inspected["preservation_handle"] + ".json"))

    def test_changed_config_detected_without_repair_or_rollback(self):
        inspected = self.guard.inspect()
        path = self.roots["ssh"][0]
        path.write_text("editor-change")
        verified = self.guard.verify(inspected["preservation_handle"])
        self.assertEqual(verified["status"], "not_ready")
        self.assertEqual(verified["changed_categories"], ["ssh"])
        self.assertEqual(path.read_text(), "editor-change")

    def test_unreadable_configuration_never_claims_preserved(self):
        with patch("agent_handoff.host_guard.tree", side_effect=PermissionError):
            inspected = self.guard.inspect()
            verified = self.guard.verify(inspected["preservation_handle"])
        self.assertEqual(verified["status"], "not_ready")
        self.assertEqual(verified["preserved_categories"], [])
        self.assertEqual(verified["unverifiable_categories"], sorted(self.roots))

    def test_symlink_targets_never_followed_or_claimed_verified(self):
        outside = self.root / "outside"
        outside.write_text("private-outside-target")
        path = self.roots["ssh"][0]
        path.unlink()
        path.symlink_to(outside)
        inspected = self.guard.inspect()
        outside.write_text("changed-outside-target")
        verified = self.guard.verify(inspected["preservation_handle"])
        self.assertIn("ssh", verified["unverifiable_categories"])
        self.assertNotIn("ssh", verified["preserved_categories"])
        record = self.store.read("host-preservation-" + inspected["preservation_handle"] + ".json")
        self.assertNotIn(hashlib.sha256(b"private-outside-target").hexdigest(), json.dumps(record))

    def test_disappearing_entry_during_read_is_unverifiable_not_absent(self):
        with patch("agent_handoff.host_guard.tree", side_effect=FileNotFoundError):
            inspected = self.guard.inspect()
            verified = self.guard.verify(inspected["preservation_handle"])
        self.assertEqual(verified["status"], "not_ready")
        self.assertEqual(verified["unverifiable_categories"], sorted(self.roots))

    def test_missing_ancestor_is_absent_but_broken_symlink_ancestor_is_unverifiable(self):
        link = self.root / "broken-link"
        link.symlink_to(self.root / "missing-target", target_is_directory=True)
        guard = HostGuard(self.store, {"missing": [self.root / "no-directory/config"], "linked": [link / "config"]})
        inspected = guard.inspect()
        verified = guard.verify(inspected["preservation_handle"])
        self.assertEqual(verified["preserved_categories"], ["missing"])
        self.assertEqual(verified["unverifiable_categories"], ["linked"])

    def test_A58_existing_prefix_and_symlink_refused_before_replacement(self):
        prefix = self.root / "existing"
        prefix.mkdir()
        (prefix / "settings").write_text("retain-existing-installation")
        link = self.root / "link"
        link.symlink_to(prefix, target_is_directory=True)
        for target in (prefix, link):
            with self.subTest(target=target.name), self.assertRaises(RuntimeFailure):
                require_fresh_prefix(target)
        self.assertEqual((prefix / "settings").read_text(), "retain-existing-installation")
        self.assertTrue(link.is_symlink())

    def test_corrupt_record_rejected_before_host_observation(self):
        inspected = self.guard.inspect()
        name = "host-preservation-" + inspected["preservation_handle"] + ".json"
        original = self.store.read(name)
        corrupt = json.loads(json.dumps(original))
        path = str(self.roots["ssh"][0])
        corrupt["observations"]["ssh"][path]["tree"]["value"] = "private-invalid-hash"
        self.store.write(name, corrupt)
        with patch("agent_handoff.host_guard.observe") as observe:
            with self.assertRaises(RuntimeFailure):
                self.guard.verify(inspected["preservation_handle"])
            observe.assert_not_called()

    def test_host_cli_requires_no_authority_and_returns_only_summary(self):
        common = ["--checkout", str(self.checkout), "--private-root", str(self.root / "cli-private")]
        with patch("agent_handoff.host_guard.default_roots", return_value=self.roots):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["host", "inspect", *common]), 0)
            inspected = json.loads(output.getvalue())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["host", "verify", "--preservation-handle", inspected["preservation_handle"], *common]), 0)
            self.assertNotIn("private-host-fixture", output.getvalue())
