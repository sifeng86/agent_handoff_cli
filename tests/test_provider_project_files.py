import os
import hashlib
from pathlib import Path
import subprocess
import stat
import sys
import unittest
from unittest.mock import patch

from agent_handoff.authority import Authority
from agent_handoff.paths import PrivateStore
from agent_handoff.publication import Runtime
from scripts.provider_project_files import EDIT_PREFIX, ProjectFiles, ProjectFileError
from test_publication import RepositoryFixture


class ProjectFilesTests(unittest.TestCase):
    git = staticmethod(RepositoryFixture.git)
    new_runtime = RepositoryFixture.new_runtime
    observe = RepositoryFixture.observe

    def setUp(self):
        RepositoryFixture.setUp(self)
        self.private_root = self.root / "private"
        with PrivateStore(self.private_root, self.checkout) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
        self.context_file = self.private_root / "context.json"
        self.managed_root = self.private_root / "repos"

    def project_files(self, paths=("code.txt",)):
        return ProjectFiles(source=self.checkout, private_root=self.private_root, authority=self.authority,
                            context_file=self.context_file, managed_root=self.managed_root,
                            approved_paths=list(paths))

    def resume_managed(self):
        self.assertEqual(self.runtime.acquire()["ownership"], "owned")
        resumed = self.runtime.resume()
        self.assertEqual((resumed["status"], resumed["ownership"]), ("ready", "owned"))
        return Path(resumed["managed_checkout"])

    def crash_write_process(self, managed, fault_point):
        code = """
import hashlib, os, sys
from pathlib import Path
from agent_handoff.authority import Authority
from scripts.provider_project_files import ProjectFiles
source, private, destination, ref, checkout, point = sys.argv[1:]
private = Path(private)
root = private / \"repos\"
reader = ProjectFiles(source=Path(source), private_root=private, authority=Authority(destination, ref),
    context_file=private / \"context.json\", managed_root=root, approved_paths=[\"code.txt\"])
target = Path(checkout) / \"code.txt\"
before = target.read_bytes()
reader.write(Path(checkout), \"code.txt\", \"child process edit\\n\", hashlib.sha256(before).hexdigest(),
    fault=lambda observed: os._exit(77) if observed == point else None)
"""
        completed = subprocess.run(
            [sys.executable, "-c", code, str(self.checkout), str(self.private_root), self.authority.destination,
             self.authority.publication_ref, str(managed), fault_point],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 77)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")
        pending = []
        for filename in self.runtime.store.names():
            if filename.startswith(EDIT_PREFIX) and filename.endswith(".json"):
                record = self.runtime.store.read(filename)
                if record["phase"] == "prepared":
                    pending.append(record)
        self.assertEqual(len(pending), 1)
        return pending[0]["operation_id"]

    def assert_no_managed_replace(self, managed, action):
        real_replace = os.replace
        parent_info = (managed.stat().st_dev, managed.stat().st_ino)
        calls = []

        def guarded_replace(src, dst, *args, **kwargs):
            source_fd = kwargs.get("src_dir_fd")
            destination_fd = kwargs.get("dst_dir_fd")
            if source_fd is not None and destination_fd is not None:
                source_info = os.fstat(source_fd)
                destination_info = os.fstat(destination_fd)
                if ((source_info.st_dev, source_info.st_ino) == parent_info and
                        (destination_info.st_dev, destination_info.st_ino) == parent_info):
                    calls.append((src, dst))
                    raise AssertionError("resolve attempted a managed checkout rename")
            return real_replace(src, dst, *args, **kwargs)

        with patch("scripts.provider_project_files.os.replace", side_effect=guarded_replace):
            result = action()
        self.assertEqual(calls, [])
        return result

    def test_unowned_source_and_unbound_managed_checkout_are_denied(self):
        reader = self.project_files()
        with self.assertRaises(ProjectFileError) as caught:
            reader.read(self.checkout, "code.txt")
        self.assertEqual(str(caught.exception), "project_file_denied")

        unbound = self.managed_root / "unbound"
        unbound.mkdir(parents=True)
        (unbound / "code.txt").write_text("public\n", encoding="utf-8")
        with self.assertRaises(ProjectFileError):
            reader.read(unbound, "code.txt")

    def test_managed_read_and_release_or_takeover_fence(self):
        managed = self.resume_managed()
        reader = self.project_files()
        result = reader.read(managed, "code.txt")
        self.assertEqual(result["content"], "original\n")
        self.assertEqual(result["path"], "code.txt")
        self.assertEqual(len(result["sha256"]), 64)

        with Runtime(managed, self.private_root, self.authority, self.context) as owner:
            self.assertEqual(owner.release()["ownership"], "released")
        with self.assertRaises(ProjectFileError):
            reader.read(managed, "code.txt")

    def test_takeover_fences_previous_managed_reader(self):
        managed = self.resume_managed()
        reader = self.project_files()
        other_private = self.root / "other-private"
        with Runtime(self.checkout, other_private, self.authority, "b" * 64) as other:
            other.initialize(self.base, mainline_ref="refs/heads/main")
            proposal = other.acquire()
            self.assertEqual(proposal["status"], "decision_required")
            self.assertEqual(proposal["decision"]["category"], "takeover")
            other.apply_decision(proposal["decision"]["decision_id"], "takeover")
        with self.assertRaises(ProjectFileError):
            reader.read(managed, "code.txt")

    def test_approved_paths_and_untrusted_file_types_fail_closed(self):
        reader = self.project_files()
        for path in ("../code.txt", ".env", "nested/.env.local", ".agent/state.json",
                     "AGENTS.md", "claude.md", "Context.json", "a//b", "./code.txt"):
            with self.subTest(path=path), self.assertRaises(ProjectFileError):
                self.project_files((path,))
        managed = self.resume_managed()

        (managed / "link.txt").symlink_to("code.txt")
        with self.assertRaises(ProjectFileError):
            self.project_files(("link.txt",)).read(managed, "link.txt")

        os.link(managed / "code.txt", managed / "hardlink.txt")
        with self.assertRaises(ProjectFileError):
            self.project_files(("hardlink.txt",)).read(managed, "hardlink.txt")

        os.mkfifo(managed / "pipe.txt")
        with self.assertRaises(ProjectFileError):
            self.project_files(("pipe.txt",)).read(managed, "pipe.txt")

        (managed / "binary.txt").write_bytes(b"\xff\xfe")
        with self.assertRaises(ProjectFileError):
            self.project_files(("binary.txt",)).read(managed, "binary.txt")

        (managed / "large.txt").write_bytes(b"x" * 65_537)
        with self.assertRaises(ProjectFileError):
            self.project_files(("large.txt",)).read(managed, "large.txt")

    def test_context_token_and_secret_material_never_leave_reader(self):
        managed = self.resume_managed()
        reader = self.project_files()
        index = (self.checkout / ".git/index").read_bytes()
        remote = self.observe()
        (managed / "code.txt").write_text("context=" + self.context, encoding="utf-8")
        with self.assertRaises(ProjectFileError) as caught:
            reader.read(managed, "code.txt")
        self.assertEqual(str(caught.exception), "project_file_denied")
        escaped = "".join("\\u%04x" % ord(char) for char in self.context)
        for encoded in ('"' + escaped + '"', '{"value":"' + escaped + '","value":"public"}'):
            (managed / "code.txt").write_text(encoded, encoding="utf-8")
            with self.assertRaises(ProjectFileError):
                reader.read(managed, "code.txt")
        (managed / "code.txt").write_text("-----BEGIN PRIVATE KEY-----\nsynthetic\n", encoding="utf-8")
        with self.assertRaises(ProjectFileError):
            reader.read(managed, "code.txt")
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual(self.observe(), remote)

    def test_file_backed_mapping_is_never_readable_as_project_code(self):
        managed = self.resume_managed()
        from agent_handoff.secrets.adapter import destination_identity
        manifest = {"schema_version": 1, "keys": {"PRIVATE_FILE": {
            "classification": "file-backed", "required": False, "file": "code.txt"}}}
        binding = {"schema_version": 1, "repository_identity": self.authority.identity,
                   "destination_identity": destination_identity("local-env", None, None),
                   "mode": "local-env", "project": None, "config": None,
                   "manifest": manifest, "upload_authorized": False}
        with Runtime(managed, self.private_root, self.authority, self.context) as owner:
            owner.store.write("secret-binding.json", binding)
        index = (self.checkout / ".git/index").read_bytes()
        remote = self.observe()
        with self.assertRaises(ProjectFileError):
            self.project_files().read(managed, "code.txt")
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual(self.observe(), remote)

    def test_editor_change_during_bounded_read_fails_without_repository_mutation(self):
        managed = self.resume_managed()
        (managed / "code.txt").write_text("initial contents\n", encoding="utf-8")
        reader = self.project_files()
        head = self.git(self.checkout, "rev-parse", "HEAD")
        index = (self.checkout / ".git/index").read_bytes()
        status = self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        remote = self.observe()
        original_read = __import__("scripts.provider_project_files", fromlist=["_read_bounded"])._read_bounded

        def edit_then_read(fd):
            (managed / "code.txt").write_text("editor changed during read\n", encoding="utf-8")
            return original_read(fd)

        with patch("scripts.provider_project_files._read_bounded", side_effect=edit_then_read):
            with self.assertRaises(ProjectFileError):
                reader.read(managed, "code.txt")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), status)
        self.assertEqual(self.observe(), remote)

    def test_write_is_managed_only_and_stale_digest_never_overwrites(self):
        managed = self.resume_managed()
        reader = self.project_files()
        original_mode = (managed / "code.txt").stat().st_mode
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        source_status = self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        remote = self.observe()
        binding = self.runtime.store.read("checkout-binding.json")
        session_id = binding["session_id"]
        content = "answer = 42\n"
        result = reader.write(managed, "code.txt", content, hashlib.sha256(b"original\n").hexdigest())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["sha256"], hashlib.sha256(content.encode()).hexdigest())
        self.assertTrue(result["operation_id"])
        self.assertNotIn("content", result)
        self.assertEqual((managed / "code.txt").read_text(), content)
        self.assertEqual(stat.S_IMODE((managed / "code.txt").stat().st_mode), stat.S_IMODE(original_mode))
        with self.assertRaises(ProjectFileError) as stale:
            reader.write(managed, "code.txt", "answer = 43\n", hashlib.sha256(b"original\n").hexdigest())
        self.assertEqual(stale.exception.code, "project_file_stale")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), source_status)
        self.assertEqual(self.observe(), remote)
        self.assertEqual(self.runtime.store.read("checkout-binding.json")["session_id"], session_id)

    def test_write_before_replace_unknown_resolves_not_applied_without_replay(self):
        managed = self.resume_managed()
        reader = self.project_files()
        initial = (managed / "code.txt").read_bytes()
        digest = hashlib.sha256(initial).hexdigest()
        index = (self.checkout / ".git/index").read_bytes()
        remote = self.observe()

        def crash(point):
            if point == "before_project_file_replace":
                raise OSError("synthetic private fault")

        pending = reader.write(managed, "code.txt", "new content\n", digest, fault=crash)
        self.assertEqual(pending["status"], "unknown")
        self.assertNotIn("content", pending)
        self.assertEqual((managed / "code.txt").read_bytes(), initial)
        with self.assertRaises(ProjectFileError):
            reader.write(managed, "code.txt", "other\n", digest)
        resolved = reader.resolve(managed, pending["operation_id"])
        self.assertEqual((resolved["status"], resolved["outcome"]), ("completed", "not_applied"))
        self.assertEqual((managed / "code.txt").read_bytes(), initial)
        self.assertFalse(any(path.name.startswith(".agent-handoff-edit-") for path in managed.iterdir()))
        self.assertEqual(reader.resolve(managed, pending["operation_id"]), resolved)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual(self.observe(), remote)

    def test_write_after_replace_unknown_resolves_applied_and_never_replays(self):
        managed = self.resume_managed()
        reader = self.project_files()
        initial = (managed / "code.txt").read_bytes()
        desired = b"answer = 42\n"
        digest = hashlib.sha256(initial).hexdigest()
        head = self.git(self.checkout, "rev-parse", "HEAD")
        index = (self.checkout / ".git/index").read_bytes()
        remote = self.observe()

        def crash(point):
            if point == "after_project_file_replace":
                raise OSError("synthetic private fault")

        pending = reader.write(managed, "code.txt", desired.decode(), digest, fault=crash)
        self.assertEqual(pending["status"], "unknown")
        self.assertEqual((managed / "code.txt").read_bytes(), desired)
        with self.assertRaises(ProjectFileError):
            reader.write(managed, "code.txt", "different\n", hashlib.sha256(desired).hexdigest())
        resolved = reader.resolve(managed, pending["operation_id"])
        self.assertEqual((resolved["status"], resolved["outcome"]), ("completed", "applied"))
        self.assertEqual((managed / "code.txt").read_bytes(), desired)
        (managed / "code.txt").write_bytes(b"editor changed after resolution\n")
        self.assertEqual(reader.resolve(managed, pending["operation_id"]), resolved)
        self.assertEqual((managed / "code.txt").read_bytes(), b"editor changed after resolution\n")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual(self.observe(), remote)

    def test_editor_change_and_unproven_stage_remain_unknown_without_overwrite(self):
        managed = self.resume_managed()
        reader = self.project_files(("code.txt", "other.txt"))
        (managed / "other.txt").write_text("before\n", encoding="utf-8")
        before = (managed / "code.txt").read_bytes()
        desired = "requested\n"
        digest = hashlib.sha256(before).hexdigest()

        def edit(point):
            if point == "before_project_file_replace":
                (managed / "code.txt").write_text("editor version\n", encoding="utf-8")

        pending = reader.write(managed, "code.txt", desired, digest, fault=edit)
        self.assertEqual(pending["status"], "unknown")
        self.assertEqual((managed / "code.txt").read_text(), "editor version\n")
        self.assertEqual(reader.resolve(managed, pending["operation_id"])["status"], "unknown")
        self.assertEqual((managed / "code.txt").read_text(), "editor version\n")

        other_before = (managed / "other.txt").read_bytes()
        staged = reader.write(managed, "other.txt", "replacement\n", hashlib.sha256(other_before).hexdigest(),
                              fault=lambda point: (_ for _ in ()).throw(OSError()) if point == "before_project_file_replace" else None)
        self.assertEqual(staged["status"], "unknown")
        stage = managed / (".agent-handoff-edit-" + staged["operation_id"])
        stage.unlink()
        stage.write_text("unproven replacement\n", encoding="utf-8")
        self.assertEqual(reader.resolve(managed, staged["operation_id"])["status"], "unknown")
        self.assertEqual(stage.read_text(), "unproven replacement\n")
        self.assertEqual((managed / "other.txt").read_bytes(), other_before)

    def test_subprocess_crashes_before_and_after_replace_resolve_without_replay(self):
        managed = self.resume_managed()
        reader = self.project_files()
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        remote = self.observe()

        initial = (managed / "code.txt").read_bytes()
        operation_id = self.crash_write_process(managed, "before_project_file_replace")
        self.assertEqual((managed / "code.txt").read_bytes(), initial)
        result = self.assert_no_managed_replace(managed, lambda: reader.resolve(managed, operation_id))
        self.assertEqual((result["status"], result["outcome"]), ("completed", "not_applied"))
        self.assertEqual(reader.resolve(managed, operation_id), result)

        operation_id = self.crash_write_process(managed, "after_project_file_replace")
        self.assertEqual((managed / "code.txt").read_text(), "child process edit\n")
        result = self.assert_no_managed_replace(managed, lambda: reader.resolve(managed, operation_id))
        self.assertEqual((result["status"], result["outcome"]), ("completed", "applied"))
        self.assertEqual(reader.resolve(managed, operation_id), result)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
        self.assertEqual(self.observe(), remote)

    def test_managed_parent_fsync_failures_remain_resolvable(self):
        managed = self.resume_managed()
        reader = self.project_files()
        target = managed / "code.txt"
        before = target.read_bytes()
        before_digest = hashlib.sha256(before).hexdigest()
        parent_info = (managed.stat().st_dev, managed.stat().st_ino)
        real_fsync = os.fsync
        failed = []

        def fail_parent_once(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == parent_info and not failed:
                failed.append(True)
                raise OSError("synthetic directory durability failure")
            return real_fsync(fd)

        with patch("scripts.provider_project_files.os.fsync", side_effect=fail_parent_once):
            applied_pending = reader.write(managed, "code.txt", "durable edit\n", before_digest)
        self.assertEqual(applied_pending["status"], "unknown")
        self.assertEqual(target.read_text(), "durable edit\n")
        with self.assertRaises(ProjectFileError):
            reader.write(managed, "code.txt", "blocked\n", hashlib.sha256(b"durable edit\n").hexdigest())
        applied = reader.resolve(managed, applied_pending["operation_id"])
        self.assertEqual((applied["status"], applied["outcome"]), ("completed", "applied"))

        before_not_applied = target.read_bytes()
        pending = reader.write(
            managed, "code.txt", "not installed\n", hashlib.sha256(before_not_applied).hexdigest(),
            fault=lambda point: (_ for _ in ()).throw(OSError()) if point == "before_project_file_replace" else None,
        )
        self.assertEqual(pending["status"], "unknown")
        stage = managed / (".agent-handoff-edit-" + pending["operation_id"])
        self.assertTrue(stage.exists())
        failed.clear()
        with patch("scripts.provider_project_files.os.fsync", side_effect=fail_parent_once):
            unresolved = reader.resolve(managed, pending["operation_id"])
        self.assertEqual(unresolved["status"], "unknown")
        self.assertEqual(target.read_bytes(), before_not_applied)
        self.assertFalse(stage.exists())
        not_applied = reader.resolve(managed, pending["operation_id"])
        self.assertEqual((not_applied["status"], not_applied["outcome"]), ("completed", "not_applied"))
        self.assertEqual(target.read_bytes(), before_not_applied)

    def test_private_store_directory_fsync_failures_preserve_recovery_receipts(self):
        managed = self.resume_managed()
        reader = self.project_files()
        target = managed / "code.txt"
        before = target.read_bytes()
        store_info = os.fstat(self.runtime.store.fd)
        store_identity = (store_info.st_dev, store_info.st_ino)
        real_fsync = os.fsync

        def fail_store_directory_once(counter):
            failed = []

            def inject(fd):
                info = os.fstat(fd)
                if (stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == store_identity
                        and counter(info) and not failed):
                    failed.append(True)
                    raise OSError("synthetic private store directory sync failure")
                return real_fsync(fd)

            return inject, failed

        # The prepared receipt has already been renamed into the store when its
        # directory fsync fails. The writer must return unknown and retain stage.
        injector, failed = fail_store_directory_once(lambda _info: True)
        with patch("scripts.provider_project_files.os.fsync", side_effect=injector):
            pending = reader.write(managed, "code.txt", "prepared receipt\n", hashlib.sha256(before).hexdigest())
        self.assertEqual(pending["status"], "unknown")
        self.assertEqual(failed, [True])
        self.assertEqual(target.read_bytes(), before)
        stage = managed / (".agent-handoff-edit-" + pending["operation_id"])
        self.assertTrue(stage.exists())
        not_applied = reader.resolve(managed, pending["operation_id"])
        self.assertEqual((not_applied["status"], not_applied["outcome"]), ("completed", "not_applied"))
        self.assertFalse(stage.exists())
        self.assertEqual(target.read_bytes(), before)

        # Fail only the terminal receipt's store-directory fsync: the intent
        # rename and target replacement have occurred, but completion stays unknown.
        before_apply = target.read_bytes()
        directory_sync_count = []

        def fail_terminal_receipt_once(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == store_identity:
                directory_sync_count.append(True)
                if len(directory_sync_count) == 2:
                    raise OSError("synthetic terminal receipt sync failure")
            return real_fsync(fd)

        with patch("scripts.provider_project_files.os.fsync", side_effect=fail_terminal_receipt_once):
            applied_pending = reader.write(
                managed, "code.txt", "terminal receipt\n", hashlib.sha256(before_apply).hexdigest())
        self.assertEqual(applied_pending["status"], "unknown")
        self.assertEqual(len(directory_sync_count), 2)
        self.assertEqual(target.read_text(), "terminal receipt\n")
        target_identity = (target.stat().st_dev, target.stat().st_ino)
        applied = reader.resolve(managed, applied_pending["operation_id"])
        self.assertEqual((applied["status"], applied["outcome"]), ("completed", "applied"))
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), target_identity)
        self.assertEqual(target.read_text(), "terminal receipt\n")

    def test_write_rejects_same_bytes_replacement_of_closed_stage_inode(self):
        managed = self.resume_managed()
        reader = self.project_files()
        target = managed / "code.txt"
        before = target.read_bytes()
        stage_reads = []
        original_read = ProjectFiles._read_snapshot
        replacement_stage = None

        def replace_stage_before_first_named_read(checkout, relative_path):
            nonlocal replacement_stage
            if relative_path.startswith(".agent-handoff-edit-") and not stage_reads:
                stage_reads.append(relative_path)
                path = checkout / relative_path
                content = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
                held_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    path.unlink()
                    path.write_bytes(content)
                    path.chmod(mode)
                    replacement_stage = path
                finally:
                    os.close(held_fd)
            return original_read(checkout, relative_path)

        with patch.object(ProjectFiles, "_read_snapshot", side_effect=replace_stage_before_first_named_read):
            with self.assertRaises(ProjectFileError) as denied:
                reader.write(managed, "code.txt", "same bytes stage\n", hashlib.sha256(before).hexdigest())
        self.assertEqual(str(denied.exception), "project_file_denied")
        self.assertEqual(len(stage_reads), 1)
        self.assertIsNotNone(replacement_stage)
        self.assertTrue(replacement_stage.exists())
        self.assertEqual(replacement_stage.read_text(), "same bytes stage\n")
        self.assertEqual(target.read_bytes(), before)

    def test_resolve_rechecks_stage_inode_after_second_snapshot(self):
        managed = self.resume_managed()
        reader = self.project_files()
        target = managed / "code.txt"
        before = target.read_bytes()
        pending = reader.write(
            managed, "code.txt", "stage content\n", hashlib.sha256(before).hexdigest(),
            fault=lambda point: (_ for _ in ()).throw(OSError()) if point == "before_project_file_replace" else None,
        )
        stage = managed / (".agent-handoff-edit-" + pending["operation_id"])
        staged_bytes = stage.read_bytes()
        staged_mode = stat.S_IMODE(stage.stat().st_mode)
        original_read = ProjectFiles._read_snapshot
        stage_reads = []

        def replace_after_second_stage_snapshot(checkout, relative_path):
            snapshot = original_read(checkout, relative_path)
            if relative_path == stage.name:
                stage_reads.append(True)
                if len(stage_reads) == 2:
                    held_fd = os.open(stage, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        stage.unlink()
                        stage.write_bytes(staged_bytes)
                        stage.chmod(staged_mode)
                    finally:
                        os.close(held_fd)
            return snapshot

        result = self.assert_no_managed_replace(
            managed,
            lambda: self._resolve_with_snapshot_hook(reader, managed, pending["operation_id"],
                                                      replace_after_second_stage_snapshot),
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(stage_reads), 2)
        self.assertEqual(stage.read_bytes(), staged_bytes)
        self.assertEqual(target.read_bytes(), before)

    @staticmethod
    def _resolve_with_snapshot_hook(reader, managed, operation_id, hook):
        with patch.object(ProjectFiles, "_read_snapshot", side_effect=hook):
            return reader.resolve(managed, operation_id)

    def test_write_content_and_fencing_rejections_preserve_local_and_remote_state(self):
        managed = self.resume_managed()
        reader = self.project_files()
        target = managed / "code.txt"
        initial = target.read_bytes()
        digest = hashlib.sha256(initial).hexdigest()
        head = self.git(self.checkout, "rev-parse", "HEAD")
        index = (self.checkout / ".git/index").read_bytes()
        source_status = self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        ref = self.observe()
        for path, content in (
            ("../outside", "bad\n"), (".env", "bad\n"), ("not-approved.py", "bad\n"),
            ("code.txt", "-----BEGIN PRIVATE KEY-----\nsynthetic\n"),
            ("code.txt", '{"token":"' + "".join("\\u%04x" % ord(char) for char in self.context) + '"}'),
        ):
            with self.subTest(path=path), self.assertRaises(ProjectFileError):
                reader.write(managed, path, content, digest)
        self.assertEqual(target.read_bytes(), initial)
        self.assertEqual(self.observe(), ref)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), source_status)

        with Runtime(managed, self.private_root, self.authority, self.context) as owner:
            self.assertEqual(owner.release()["ownership"], "released")
        ref = self.observe()
        binding = self.runtime.store.read("checkout-binding.json")
        with self.assertRaises(ProjectFileError):
            reader.write(managed, "code.txt", "fenced\n", digest)
        self.assertEqual(target.read_bytes(), initial)
        self.assertEqual(self.observe(), ref)
        self.assertEqual(self.runtime.store.read("checkout-binding.json"), binding)

    def test_write_after_takeover_is_fenced_without_side_effects(self):
        managed = self.resume_managed()
        reader = self.project_files()
        with Runtime(self.checkout, self.root / "takeover-private", self.authority, "b" * 64) as other:
            other.initialize(self.base, mainline_ref="refs/heads/main")
            proposal = other.acquire()
            self.assertEqual(proposal["status"], "decision_required")
            other.apply_decision(proposal["decision"]["decision_id"], "takeover")
        target = managed / "code.txt"
        before = target.read_bytes()
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        ref = self.observe()
        old_binding = self.runtime.store.read("checkout-binding.json")
        with self.assertRaises(ProjectFileError):
            reader.write(managed, "code.txt", "old writer edit\n", hashlib.sha256(before).hexdigest())
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
        self.assertEqual(self.observe(), ref)
        self.assertEqual(self.runtime.store.read("checkout-binding.json"), old_binding)

    def test_corrupt_intents_are_fixed_errors_and_never_execute_saved_paths(self):
        managed = self.resume_managed()
        reader = self.project_files()
        target = managed / "code.txt"
        before = target.read_bytes()
        digest = hashlib.sha256(before).hexdigest()
        pending = reader.write(
            managed, "code.txt", "private intent desired text\n", digest,
            fault=lambda point: (_ for _ in ()).throw(OSError()) if point == "before_project_file_replace" else None,
        )
        filename = EDIT_PREFIX + pending["operation_id"] + ".json"
        record = self.runtime.store.read(filename)
        outside = self.root / "outside.txt"
        outside.write_text("must not be touched\n", encoding="utf-8")
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        remote = self.observe()
        def corrupt_path(item):
            item.update(path="../outside.txt")

        def hide_context_in_desired(item):
            import base64
            secret = self.context.encode()
            item["desired_b64"] = base64.b64encode(secret).decode()
            item["desired_sha256"] = hashlib.sha256(secret).hexdigest()
            item["stage_metadata"]["size"] = len(secret)

        for mutation in (corrupt_path, hide_context_in_desired):
            broken = dict(record)
            mutation(broken)
            self.runtime.store.write(filename, broken)
            with self.assertRaises(ProjectFileError) as denied:
                reader.resolve(managed, pending["operation_id"])
            self.assertEqual(str(denied.exception), "project_file_denied")
            self.assertNotIn(self.context, str(denied.exception))
            self.assertEqual(outside.read_text(), "must not be touched\n")
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
            self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
            self.assertEqual(self.observe(), remote)
