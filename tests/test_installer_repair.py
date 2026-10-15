"""Narrow installer launcher repair acceptance tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.host_recovery import HostFileRecovery
from agent_handoff.installer_repair import repair_entrypoint
from agent_handoff.paths import PrivateStore
from agent_handoff.public_results import validate_host_result


class InstallerRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.prefix = self.root / "prefix"
        self.bin = self.prefix / "bin"
        self.bin.mkdir(parents=True)
        self.unrelated = self.prefix / "lib" / "agentctl.conf"
        self.unrelated.parent.mkdir()
        self.unrelated.write_bytes(b"unrelated installation configuration\n")
        self.python = self.bin / "python"
        self.python.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.python.chmod(0o755)
        self.target = self.bin / "agentctl"
        self.body = b"import sys\nfrom agent_handoff.cli import main\nraise SystemExit(main())\n"
        self.healthy = b"#!" + os.fsencode(self.python) + b"\n" + self.body
        self.target.write_bytes(self.healthy)
        self.target.chmod(0o755)
        self.store = PrivateStore(self.root / "private" / "host", self.checkout)
        self.addCleanup(self.store.close)
        self.recovery = HostFileRecovery(self.store)
        self.known_good = self.recovery.backup(self.target)["backup_handle"]
        self.missing_python = self.root / "removed-prefix" / "bin" / "python"

    def break_shebang(self):
        broken = b"#!" + os.fsencode(self.missing_python) + b"\n" + self.body
        self.target.write_bytes(broken)
        self.target.chmod(0o755)
        return broken

    def test_repairs_only_shebang_and_keeps_verified_pre_repair_backup(self):
        broken = self.break_shebang()
        unrelated_before = self.unrelated.read_bytes()
        reports = repair_entrypoint(self.store, self.prefix, self.known_good)
        self.assertEqual([report["command"] for report in reports], ["host backup", "host restore"])
        for report in reports:
            validate_host_result(report, require_host=True)
        before_handle = reports[0]["backup_handle"]
        before, raw = self.recovery.read(before_handle)
        self.assertEqual(raw, broken)
        self.assertEqual(before["path"], str(self.target))
        self.assertEqual(reports[1]["recovery_outcome"], "restored")
        repaired = self.target.read_bytes()
        self.assertEqual(repaired, self.healthy)
        self.assertEqual(repaired.split(b"\n", 1)[1], self.body)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.unrelated.read_bytes(), unrelated_before)
        records = [self.store.read(name) for name in self.store.names() if name.startswith("installer-repair-")]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["known_good_handle"], self.known_good)
        self.assertEqual(records[0]["before_repair_handle"], before_handle)
        encoded = json.dumps(reports)
        self.assertNotIn(str(self.target), encoded)
        self.assertNotIn("agent_handoff.cli", encoded)
        self.assertNotIn(before["snapshot"]["sha256"], encoded)

    def test_same_healthy_launcher_is_verified_without_replacement(self):
        before = self.recovery.read(self.known_good)[0]["snapshot"]
        reports = repair_entrypoint(self.store, self.prefix, self.known_good)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["command"], "host verify-backup")
        self.assertTrue(reports[0]["backup_verified"])
        self.assertEqual(self.recovery.read(self.known_good)[0]["snapshot"], before)
        self.assertFalse(any(name.startswith("host-recovery-op-") for name in self.store.names()))

    def test_non_executable_backup_is_rejected_without_replacement(self):
        self.target.chmod(0o644)
        handle = self.recovery.backup(self.target)["backup_handle"]
        broken = self.break_shebang()
        self.target.chmod(0o644)
        with self.assertRaises(RuntimeFailure) as raised:
            repair_entrypoint(self.store, self.prefix, handle)
        self.assertEqual(raised.exception.status, "rejected")
        self.assertEqual(self.target.read_bytes(), broken)
        self.assertFalse(any(name.startswith("host-recovery-op-") for name in self.store.names()))

    def test_emit_failure_stops_before_replace_and_keeps_recovery_backup(self):
        broken = self.break_shebang()
        def fail_emit(_):
            self.assertEqual(self.target.read_bytes(), broken)
            self.assertEqual(self.recovery.read(_["backup_handle"])[1], broken)
            raise RuntimeError("private emit failure")
        with self.assertRaises(RuntimeError):
            repair_entrypoint(self.store, self.prefix, self.known_good, emit=fail_emit)
        self.assertEqual(self.target.read_bytes(), broken)
        self.assertGreaterEqual(len([name for name in self.store.names() if name.startswith("host-backup-")]), 2)
        self.assertFalse(any(name.startswith("host-recovery-op-") for name in self.store.names()))

    def test_wrong_scope_body_change_valid_other_interpreter_and_symlink_are_rejected(self):
        broken = self.break_shebang()
        other = self.root / "other-host-file"
        other.write_bytes(self.healthy)
        wrong_handle = self.recovery.backup(other)["backup_handle"]
        with self.assertRaises(RuntimeFailure):
            repair_entrypoint(self.store, self.prefix, wrong_handle)
        self.assertEqual(self.target.read_bytes(), broken)

        self.target.write_bytes(broken + b"# unrelated body change\n")
        changed = self.target.read_bytes()
        with self.assertRaises(RuntimeFailure):
            repair_entrypoint(self.store, self.prefix, self.known_good)
        self.assertEqual(self.target.read_bytes(), changed)

        second_python = self.bin / "python-alt"
        second_python.write_bytes(b"#!/bin/sh\nexit 0\n")
        second_python.chmod(0o755)
        self.target.write_bytes(b"#!" + os.fsencode(second_python) + b"\n" + self.body)
        valid_other = self.target.read_bytes()
        with self.assertRaises(RuntimeFailure):
            repair_entrypoint(self.store, self.prefix, self.known_good)
        self.assertEqual(self.target.read_bytes(), valid_other)

        self.target.write_bytes(broken)
        moved_bin = self.prefix / "bin-moved"
        self.bin.rename(moved_bin)
        self.bin.symlink_to(moved_bin, target_is_directory=True)
        with self.assertRaises(RuntimeFailure):
            repair_entrypoint(self.store, self.prefix, self.known_good)
        self.assertEqual((moved_bin / "agentctl").read_bytes(), broken)

    def test_backup_failure_and_corrupt_backup_never_modify_target(self):
        broken = self.break_shebang()
        with patch.object(HostFileRecovery, "backup", side_effect=RuntimeFailure("safe backup refusal")):
            with self.assertRaises(RuntimeFailure):
                repair_entrypoint(self.store, self.prefix, self.known_good)
        self.assertEqual(self.target.read_bytes(), broken)

        name = "host-backup-" + self.known_good + ".json"
        record = self.store.read(name)
        self.store.write(name, dict(record, raw_base64="broken-private-record"))
        with self.assertRaises(RuntimeFailure):
            repair_entrypoint(self.store, self.prefix, self.known_good)
        self.assertEqual(self.target.read_bytes(), broken)

    def test_journaled_fault_is_unknown_then_resolves_without_replaying(self):
        broken = self.break_shebang()
        seen = []
        def fault(point):
            if point == "after_host_recovery_journal":
                seen.append(point)
                self.assertEqual(self.target.read_bytes(), broken)
                self.assertGreaterEqual(len([n for n in self.store.names() if n.startswith("host-backup-")]), 2)
                raise OSError("private simulated interruption")
        reports = repair_entrypoint(self.store, self.prefix, self.known_good, fault=fault)
        self.assertEqual(seen, ["after_host_recovery_journal"])
        self.assertEqual(reports[1]["status"], "unknown")
        self.assertEqual(self.target.read_bytes(), broken)
        operation_id = reports[1]["operation_id"]
        resolved = self.recovery.resolve(operation_id)
        self.assertEqual(resolved["recovery_outcome"], "not_restored")
        self.assertEqual(self.target.read_bytes(), broken)

    def test_editor_race_after_journal_is_unknown_and_never_overwritten(self):
        self.break_shebang()
        latest = b"#!" + os.fsencode(self.missing_python) + b"\n" + self.body + b"# editor update\n"
        def fault(point):
            if point == "before_host_recovery_rename":
                self.target.write_bytes(latest)
        reports = repair_entrypoint(self.store, self.prefix, self.known_good, fault=fault)
        self.assertEqual(reports[1]["status"], "unknown")
        self.assertEqual(self.target.read_bytes(), latest)
        self.assertEqual(self.recovery.resolve(reports[1]["operation_id"])["status"], "unknown")
        self.assertEqual(self.target.read_bytes(), latest)

    def test_private_store_must_not_overlap_approved_prefix(self):
        self.break_shebang()
        nested_store = PrivateStore(self.prefix / "private", self.checkout)
        try:
            with self.assertRaises(RuntimeFailure):
                repair_entrypoint(nested_store, self.prefix, self.known_good)
        finally:
            nested_store.close()
        self.assertTrue(self.target.read_bytes().startswith(b"#!" + os.fsencode(self.missing_python)))

    def test_script_emits_backup_then_restore_json_without_host_material(self):
        self.break_shebang()
        script = Path(__file__).resolve().parents[1] / "scripts/installer_repair.py"
        process = subprocess.run([sys.executable, str(script), "--prefix", str(self.prefix),
            "--known-good-handle", self.known_good, "--private-root", str(self.root / "private"),
            "--checkout", str(self.checkout)], capture_output=True, timeout=30,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")))
        self.assertEqual(process.returncode, 0)
        lines = process.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        reports = [json.loads(line) for line in lines]
        self.assertEqual([report["command"] for report in reports], ["host backup", "host restore"])
        for report in reports:
            validate_host_result(report, require_host=True)
        output = process.stdout.decode("utf-8")
        self.assertNotIn(str(self.target), output)
        self.assertNotIn("agent_handoff.cli", output)
        self.assertEqual(self.target.read_bytes(), self.healthy)


if __name__ == "__main__":
    unittest.main()
