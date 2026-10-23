"""JSON-only host file recovery and real process-crash fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.paths import PrivateStore


class HostRecoveryCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.private = self.root / "private"
        self.config = self.root / "config"
        self.config.write_bytes(b"private-host-sentinel\x00\xff")

    def invoke(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(["host", *args, "--checkout", str(self.checkout), "--private-root", str(self.private)])
        self.assertEqual(error.getvalue(), "")
        self.assertNotIn("private-host-sentinel", output.getvalue())
        self.assertNotIn(str(self.config), output.getvalue())
        return code, json.loads(output.getvalue())

    def backup(self):
        code, result = self.invoke("backup", "--host-file", str(self.config))
        self.assertEqual(code, 0)
        self.assertTrue(result["backup_verified"])
        return result["backup_handle"]

    def test_cli_backup_verify_restore_scope_and_required_handles(self):
        original = self.config.read_bytes()
        handle = self.backup()
        self.assertEqual(self.invoke("verify-backup", "--backup-handle", handle)[0], 0)
        self.assertEqual(self.invoke("restore", "--backup-handle", handle)[0], 2)
        self.config.write_bytes(b"reviewed-repair")
        expected = self.backup()
        code, report = self.invoke("restore", "--backup-handle", handle, "--expected-handle", expected)
        self.assertEqual(code, 0)
        self.assertEqual(report["recovery_outcome"], "restored")
        self.assertEqual(report["code_publication"], "not_attempted")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.invoke("resolve", "--operation-id", report["operation_id"])[0], 0)

    def test_corrupt_backup_and_stale_current_file_return_safe_errors(self):
        handle = self.backup()
        with PrivateStore(self.private / "host", self.checkout) as store:
            name = "host-backup-" + handle + ".json"
            original = store.read(name)
            store.write(name, dict(original, raw_base64="private-host-sentinel"))
            self.assertEqual(self.invoke("verify-backup", "--backup-handle", handle)[0], 2)
            store.write(name, original)
        self.config.write_bytes(b"latest-editor")
        self.assertEqual(self.invoke("restore", "--backup-handle", handle, "--expected-handle", handle)[0], 3)
        self.assertEqual(self.config.read_bytes(), b"latest-editor")

    def test_noncompleted_host_results_propagate_exit_code(self):
        handle = "a" * 32
        cases = [
            ("agent_handoff.host_recovery.HostFileRecovery.restore",
             {"status": "unknown", "operation_id": "b" * 32, "backup_handle": handle},
             ("restore", "--backup-handle", handle, "--expected-handle", "c" * 32), 5),
            ("agent_handoff.host_guard.HostGuard.verify",
             {"status": "not_ready", "preservation_handle": handle, "preserved_categories": [],
              "changed_categories": ["ssh"], "unverifiable_categories": [],
              "host_changes_attempted": False, "scope": "selected_linux_configuration_entries"},
             ("verify", "--preservation-handle", handle), 6),
        ]
        for target, report, args, expected in cases:
            with self.subTest(action=args[0]), patch(target, return_value=report):
                code, public = self.invoke(*args)
                self.assertEqual(code, expected)
                self.assertEqual(public["status"], report["status"])

    def test_process_exit_before_replace_resolves_without_replay(self):
        self._crash("after_host_recovery_journal", "not_restored")

    def test_process_exit_after_replace_resolves_without_replay(self):
        self._crash("after_host_recovery_rename", "restored")

    def test_process_exit_with_journaled_staging_cleans_only_proven_temp(self):
        self._crash("before_host_recovery_rename", "not_restored")

    def _crash(self, boundary, outcome):
        handle = self.backup()
        self.config.write_bytes(b"reviewed-repair")
        expected = self.backup()
        code = """
import os, sys
from pathlib import Path
from agent_handoff.paths import PrivateStore
from agent_handoff.host_recovery import HostFileRecovery
private, checkout, original, expected, boundary = sys.argv[1:]
with PrivateStore(Path(private) / 'host', Path(checkout)) as store:
    HostFileRecovery(store).restore(original, expected, fault=lambda point: os._exit(77) if point == boundary else None)
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.private), str(self.checkout), handle, expected, boundary],
            capture_output=True, timeout=15,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")))
        self.assertEqual(child.returncode, 77)
        with PrivateStore(self.private / "host", self.checkout) as store:
            names = [name for name in store.names() if name.startswith("host-recovery-op-")]
            self.assertEqual(len(names), 1)
            operation_id = store.read(names[0])["operation_id"]
        staged = self.config.parent / (".agent-handoff-host-" + operation_id)
        self.assertEqual(staged.exists(), boundary == "before_host_recovery_rename")
        self.assertEqual(self.invoke("restore", "--backup-handle", handle, "--expected-handle", expected)[0], 5)
        actual = self.config.read_bytes()
        replace = os.replace
        def journal_only(source, destination, *args, **kwargs):
            if destination == self.config.name or Path(destination) == self.config:
                raise AssertionError("Do not replay host replacement")
            return replace(source, destination, *args, **kwargs)
        with patch("agent_handoff.host_recovery.os.replace", side_effect=journal_only):
            for _ in range(2):
                code, report = self.invoke("resolve", "--operation-id", operation_id)
                self.assertEqual(code, 0)
                self.assertEqual(report["recovery_outcome"], outcome)
                self.assertEqual(self.config.read_bytes(), actual)
        self.assertFalse(staged.exists())
