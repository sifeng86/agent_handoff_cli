"""Isolated host recovery byte/metadata, crash and editor safety evidence."""
import json
import errno
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.host_recovery import HostFileRecovery
from agent_handoff.paths import PrivateStore


class HostRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.config = self.root / "config"
        self.config.write_bytes(b"private-original\x00\xff\n")
        self.config.chmod(0o640)
        self.store = PrivateStore(self.root / "private", self.checkout)
        self.addCleanup(self.store.close)
        self.recovery = HostFileRecovery(self.store)

    def backup(self):
        report = self.recovery.backup(self.config)
        self.assertNotIn("private-original", json.dumps(report))
        self.assertNotIn(str(self.config), json.dumps(report))
        return report["backup_handle"]

    def test_verified_backup_restores_only_reviewed_file_and_metadata(self):
        original = self.config.read_bytes()
        mtime = self.config.stat().st_mtime_ns
        handle = self.backup()
        unrelated = self.root / "unrelated"
        unrelated.write_text("retain existing behavior")
        self.config.write_bytes(b"necessary-repair-version")
        self.config.chmod(0o600)
        expected = self.backup()
        restored = self.recovery.restore(handle, expected)
        self.assertEqual(restored["recovery_outcome"], "restored")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.config.stat().st_mtime_ns, mtime)
        self.assertEqual(unrelated.read_text(), "retain existing behavior")
        self.assertEqual(self.recovery.resolve(restored["operation_id"]), restored)

    def test_directory_sync_failure_remains_pending_until_verified_sync(self):
        original = self.backup()
        raw = self.config.read_bytes()
        self.config.write_bytes(b"reviewed-repair")
        expected = self.backup()
        parent_inode = self.config.parent.stat().st_ino
        fsync = os.fsync
        def fail_parent(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_ino == parent_inode:
                raise OSError(errno.EIO, "Fixture directory sync failure")
            return fsync(fd)
        with patch("agent_handoff.host_recovery.os.fsync", side_effect=fail_parent):
            report = self.recovery.restore(original, expected)
            self.assertEqual(report["status"], "unknown")
            self.assertEqual(self.config.read_bytes(), raw)
            self.assertEqual(self.recovery.resolve(report["operation_id"])["status"], "unknown")
            journal = self.store.read("host-recovery-op-" + report["operation_id"] + ".json")
            self.assertEqual(journal["phase"], "prepared")
        replace = os.replace
        def journal_only(source, destination, *args, **kwargs):
            if destination == self.config.name:
                raise AssertionError("Recovery must not replay host replacement")
            return replace(source, destination, *args, **kwargs)
        with patch("agent_handoff.host_recovery.os.replace", side_effect=journal_only):
            self.assertEqual(self.recovery.resolve(report["operation_id"])["recovery_outcome"], "restored")

    def test_lost_read_permission_during_resolution_preserves_pending_evidence(self):
        original = self.backup()
        self.config.write_bytes(b"reviewed-repair")
        expected = self.backup()
        with self.assertRaises(SystemExit):
            self.recovery.restore(original, expected, fault=lambda _: (_ for _ in ()).throw(SystemExit(77)))
        name = next(name for name in self.store.names() if name.startswith("host-recovery-op-"))
        operation_id = self.store.read(name)["operation_id"]
        open_file = os.open
        def deny_target(path, *args, **kwargs):
            if path == self.config.name:
                raise PermissionError(errno.EACCES, "Fixture target permission denied")
            return open_file(path, *args, **kwargs)
        with patch("agent_handoff.host_recovery.os.open", side_effect=deny_target):
            self.assertEqual(self.recovery.resolve(operation_id)["status"], "unknown")
        self.assertEqual(self.store.read(name)["phase"], "prepared")
        self.assertEqual(self.config.read_bytes(), b"reviewed-repair")
        self.assertEqual(self.recovery.resolve(operation_id)["recovery_outcome"], "not_restored")

    def test_editor_change_during_sync_keeps_operation_unknown(self):
        original = self.backup()
        self.config.write_bytes(b"reviewed-repair")
        expected = self.backup()
        with self.assertRaises(SystemExit):
            self.recovery.restore(original, expected, fault=lambda _: (_ for _ in ()).throw(SystemExit(77)))
        name = next(name for name in self.store.names() if name.startswith("host-recovery-op-"))
        operation_id = self.store.read(name)["operation_id"]
        fsync = os.fsync
        parent_inode = self.config.parent.stat().st_ino
        def edit_after_sync(fd):
            fsync(fd)
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_ino == parent_inode:
                self.config.write_bytes(b"latest-editor-during-sync")
        with patch("agent_handoff.host_recovery.os.fsync", side_effect=edit_after_sync):
            self.assertEqual(self.recovery.resolve(operation_id)["status"], "unknown")
        self.assertEqual(self.store.read(name)["phase"], "prepared")
        self.assertEqual(self.config.read_bytes(), b"latest-editor-during-sync")

    def test_crash_before_and_after_rename_resolves_without_replay(self):
        for boundary, outcome in (("after_host_recovery_journal", "not_restored"),
                                  ("after_host_recovery_rename", "restored")):
            with self.subTest(boundary=boundary):
                original = self.backup()
                self.config.write_bytes(b"changed")
                expected = self.backup()
                def crash(point):
                    if point == boundary:
                        raise SystemExit(77)
                with self.assertRaises(SystemExit):
                    self.recovery.restore(original, expected, fault=crash)
                name = next(name for name in self.store.names() if name.startswith("host-recovery-op-") and
                            self.store.read(name)["phase"] == "prepared")
                operation_id = self.store.read(name)["operation_id"]
                with self.assertRaises(RuntimeFailure) as caught:
                    self.recovery.restore(original, expected)
                self.assertEqual(caught.exception.status, "unknown")
                actual = self.config.read_bytes()
                self.assertEqual(self.recovery.resolve(operation_id)["recovery_outcome"], outcome)
                self.assertEqual(self.config.read_bytes(), actual)

    def test_editor_change_during_restore_preserved_and_unknown_not_replayed(self):
        original = self.backup()
        self.config.write_bytes(b"reviewed-repair")
        expected = self.backup()
        def edit(point):
            if point == "before_host_recovery_rename":
                self.config.write_bytes(b"latest-editor-work")
        report = self.recovery.restore(original, expected, fault=edit)
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(self.recovery.resolve(report["operation_id"])["status"], "unknown")
        self.assertEqual(self.config.read_bytes(), b"latest-editor-work")

    def test_edited_staging_never_published_or_removed(self):
        original = self.backup()
        self.config.write_bytes(b"reviewed-repair")
        expected = self.backup()
        staging = []
        def edit(point):
            if point == "before_host_recovery_rename":
                name = next(name for name in self.store.names() if name.startswith("host-recovery-op-"))
                operation_id = self.store.read(name)["operation_id"]
                path = self.config.parent / (".agent-handoff-host-" + operation_id)
                path.write_bytes(b"editor-staging-work")
                staging.append(path)
        report = self.recovery.restore(original, expected, fault=edit)
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(self.config.read_bytes(), b"reviewed-repair")
        self.assertEqual(staging[0].read_bytes(), b"editor-staging-work")
        self.assertEqual(self.recovery.resolve(report["operation_id"])["status"], "unknown")
        self.assertEqual(staging[0].read_bytes(), b"editor-staging-work")

    def test_unproven_staging_collision_preserved(self):
        original = self.backup()
        self.config.write_bytes(b"reviewed-repair")
        expected = self.backup()
        staging = []
        def collision(point):
            if point == "after_host_recovery_journal":
                name = next(name for name in self.store.names() if name.startswith("host-recovery-op-"))
                operation_id = self.store.read(name)["operation_id"]
                path = self.config.parent / (".agent-handoff-host-" + operation_id)
                path.write_bytes(b"unrelated-collision-work")
                staging.append(path)
        report = self.recovery.restore(original, expected, fault=collision)
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(self.recovery.resolve(report["operation_id"])["status"], "unknown")
        self.assertEqual(self.config.read_bytes(), b"reviewed-repair")
        self.assertEqual(staging[0].read_bytes(), b"unrelated-collision-work")

    def test_corrupt_backup_and_wrong_scope_fail_before_replacement(self):
        handle = self.backup()
        name = "host-backup-" + handle + ".json"
        original = self.store.read(name)
        self.store.write(name, dict(original, raw_base64="cHJpdmF0ZS10YW1wZXI="))
        with self.assertRaises(RuntimeFailure):
            self.recovery.restore(handle, handle)
        self.store.write(name, original)
        other = self.root / "other"
        other.write_text("unrelated")
        other_handle = self.recovery.backup(other)["backup_handle"]
        with self.assertRaises(RuntimeFailure):
            self.recovery.restore(handle, other_handle)
        self.assertEqual(self.config.read_bytes(), b"private-original\x00\xff\n")

    def test_symlinks_hardlinks_and_extended_attributes_refused(self):
        linked = self.root / "linked"
        linked.symlink_to(self.config)
        with self.assertRaises((RuntimeFailure, OSError)):
            self.recovery.backup(linked)
        linked.unlink()
        linked.hardlink_to(self.config)
        with self.assertRaises(RuntimeFailure):
            self.recovery.backup(self.config)
        linked.unlink()
        with patch("agent_handoff.host_recovery.os.listxattr", return_value=["user.fixture"]):
            with self.assertRaises(RuntimeFailure):
                self.recovery.backup(self.config)

    def test_generated_backup_journal_and_invalid_schema_structures(self):
        from test_record_schemas import validator
        handle = self.backup()
        backup = self.store.read("host-backup-" + handle + ".json")
        schema = validator("host-backup")
        schema.validate(backup)
        for changes in ({"schema_version": True}, {"extra": "private"}, {"verification": "unverified"}):
            self.assertFalse(schema.is_valid(dict(backup, **changes)))
        malformed = dict(backup, snapshot=dict(backup["snapshot"], metadata=[False] * 8))
        self.assertFalse(schema.is_valid(malformed))
        self.config.write_bytes(b"reviewed-repair")
        expected = self.backup()
        with self.assertRaises(SystemExit):
            self.recovery.restore(handle, expected, fault=lambda _: (_ for _ in ()).throw(SystemExit(77)))
        name = next(name for name in self.store.names() if name.startswith("host-recovery-op-"))
        journal = self.store.read(name)
        schema = validator("host-recovery")
        schema.validate(journal)
        self.assertFalse(schema.is_valid(dict(journal, phase="resolved")))
        self.assertFalse(schema.is_valid(dict(journal, outcome="restored")))
        self.recovery.resolve(journal["operation_id"])
        schema.validate(self.store.read(name))
