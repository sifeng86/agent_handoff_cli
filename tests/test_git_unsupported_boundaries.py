"""Real local Git repositories exercise explicitly unsupported input formats."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_handoff.authority import Authority
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.git_backend import GitBackend, git_environment, inspect
from agent_handoff.preservation import preserve
from agent_handoff.publication import Runtime


class UnsupportedGitBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.remote.mkdir()
        self.git(self.remote, "init", "--bare", "-q")
        self.checkout = self.root / "source"
        self.checkout.mkdir()
        self.git(self.checkout, "init", "-q", "-b", "main")
        (self.checkout / "code.txt").write_bytes(b"base source\n")
        self.git(self.checkout, "add", "code.txt")
        self.git(self.checkout, "commit", "-qm", "base")
        self.base = self.git(self.checkout, "rev-parse", "HEAD")
        self.git(self.checkout, "push", "-q", str(self.remote), "main")
        self.authority = Authority(str(self.remote), "refs/heads/publication")
        self.runtime = Runtime(self.checkout, self.root / "private", self.authority, "a" * 64)
        self.addCleanup(self.runtime.close)
        initialized = self.runtime.initialize(self.base, mainline_ref="refs/heads/main")
        self.assertEqual(initialized["code_publication"], "published")
        self.initial_publication = self.runtime.backend.observe()
        self.assertIsNone(self.runtime.backend.records(self.initial_publication)["owner"]["owner"])
        self._dirty_source()
        self._capture_local_state()

    @staticmethod
    def git(directory, *args):
        return subprocess.check_output(["git", "-C", str(directory), *args], env=git_environment(),
                                       stderr=subprocess.PIPE, text=True).strip()

    def _dirty_source(self):
        tracked = self.checkout / "code.txt"
        tracked.write_bytes(b"staged local edit\n")
        self.git(self.checkout, "add", "code.txt")
        tracked.write_bytes(b"unstaged local edit\n")
        (self.checkout / "local.bin").write_bytes(b"\x00\xfflocal data")

    def _capture_local_state(self):
        self.source_head = self.git(self.checkout, "rev-parse", "HEAD")
        self.source_index = (self.checkout / ".git/index").read_bytes()
        self.source_status = self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        self.source_files = {name: (self.checkout / name).read_bytes() for name in ("code.txt", "local.bin")}
        self.private_trust = self.runtime.store.read("integrity.json")

    def _prepare_and_publish(self, changes, message):
        candidate, _ = self.runtime.backend.prepare(self.initial_publication, changes, message)
        self.assertEqual(self.runtime.backend.push(candidate, self.initial_publication), "published")
        return candidate

    def _assert_rejection_preserved_state(self, expected_remote_head):
        self.assertEqual(self.runtime.backend.remote_head(), expected_remote_head)
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/main"), self.base)
        self.assertEqual(self.runtime.backend.text("show", f"{expected_remote_head}:.agent/owner.json"),
                         self.runtime.backend.text(f"show", f"{self.initial_publication}:.agent/owner.json"))
        self.assertEqual(self.runtime.sessions.current(), None)
        self.assertEqual(self.runtime.store.read("integrity.json"), self.private_trust)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), self.source_index)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
                         self.source_status)
        self.assertEqual({name: (self.checkout / name).read_bytes() for name in self.source_files}, self.source_files)

    def _assert_unsupported_inspection(self, expected_remote_head):
        with self.assertRaises(RuntimeFailure) as failure:
            inspect(str(self.remote), self.authority.publication_ref)
        self.assertEqual((failure.exception.status, failure.exception.code), ("unsupported", 8))
        self._assert_rejection_preserved_state(expected_remote_head)

    def test_sha256_bare_authority_fails_closed_before_fetch(self):
        sha_remote = self.root / "sha256-remote.git"
        sha_remote.mkdir()
        self.git(sha_remote, "init", "--bare", "--object-format=sha256", "-q")
        sha_checkout = self.root / "sha256-source"
        sha_checkout.mkdir()
        self.git(sha_checkout, "init", "--object-format=sha256", "-q", "-b", "main")
        (sha_checkout / "file.txt").write_text("sha256 fixture\n", encoding="utf-8")
        self.git(sha_checkout, "add", "file.txt")
        self.git(sha_checkout, "commit", "-qm", "sha256 fixture")
        self.git(sha_checkout, "push", "-q", str(sha_remote), "HEAD:refs/heads/publication")
        sha_tip = self.git(sha_remote, "rev-parse", "refs/heads/publication")
        sha_head = self.git(sha_checkout, "rev-parse", "HEAD")
        sha_index = (sha_checkout / ".git/index").read_bytes()

        cache = self.root / "sha256-cache.git"
        backend = GitBackend(cache, str(sha_remote), "refs/heads/publication")
        with self.assertRaises(RuntimeFailure) as failure:
            backend.observe()
        self.assertEqual((failure.exception.status, failure.exception.code), ("unsupported", 8))
        self.assertFalse((cache / "FETCH_HEAD").exists())
        with self.assertRaises(RuntimeFailure) as inspection:
            inspect(str(sha_remote), "refs/heads/publication")
        self.assertEqual((inspection.exception.status, inspection.exception.code), ("unsupported", 8))
        self.assertEqual(self.git(sha_remote, "rev-parse", "refs/heads/publication"), sha_tip)
        self.assertEqual(self.git(sha_checkout, "rev-parse", "HEAD"), sha_head)
        self.assertEqual((sha_checkout / ".git/index").read_bytes(), sha_index)
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/publication"), self.initial_publication)
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/main"), self.base)
        self.assertEqual(self.runtime.sessions.current(), None)
        self.assertEqual(self.runtime.store.read("integrity.json"), self.private_trust)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), self.source_index)
        self._assert_rejection_preserved_state(self.initial_publication)

    def test_gitlink_tree_entry_is_rejected_by_readonly_inspection(self):
        empty_tree = self.runtime.backend.text("mktree", data=b"")
        linked_commit = self.runtime.backend.text("commit-tree", empty_tree, data=b"synthetic gitlink target\n")
        with tempfile.TemporaryDirectory(prefix="gitlink-index-", dir=self.runtime.backend.directory) as temporary:
            index = str(Path(temporary) / "index")
            self.runtime.backend.text("read-tree", self.initial_publication, index=index)
            self.runtime.backend.text("update-index", "--add", "--cacheinfo", "160000", linked_commit,
                                     "vendor", index=index)
            tree = self.runtime.backend.text("write-tree", index=index)
            candidate = self.runtime.backend.text("commit-tree", tree, "-p", self.initial_publication,
                                                  data=b"unsupported gitlink fixture\n")
        self.assertEqual(self.runtime.backend.push(candidate, self.initial_publication), "published")
        self._assert_unsupported_inspection(candidate)

    def test_root_gitattributes_lfs_filter_is_rejected(self):
        candidate = self._prepare_and_publish(
            {".gitattributes": b"*.bin filter=lfs diff=lfs merge=lfs -text\n"}, "root LFS fixture")
        self._assert_unsupported_inspection(candidate)

    def test_nested_gitattributes_lfs_filter_is_rejected(self):
        candidate = self._prepare_and_publish(
            {"assets/.gitattributes": b"*.bin filter=lfs diff=lfs merge=lfs -text\n"}, "nested LFS fixture")
        self._assert_unsupported_inspection(candidate)

    def test_git_backend_ignores_configured_pre_push_hook(self):
        hooks = self.root / "client-hooks"
        hooks.mkdir()
        marker = self.root / "pre-push-ran"
        hook = hooks / "pre-push"
        hook.write_text("#!" + sys.executable + "\nfrom pathlib import Path\n"
                        f"Path({str(marker)!r}).write_text('triggered')\nraise SystemExit(1)\n", encoding="utf-8")
        hook.chmod(0o700)
        self.git(self.runtime.backend.directory, "config", "core.hooksPath", str(hooks))
        candidate, _ = self.runtime.backend.prepare(self.initial_publication, {"hook-test.txt": b"candidate\n"},
                                                    "pre-push hook isolation")

        control = subprocess.run(["git", "--git-dir", str(self.runtime.backend.directory), "push", "--porcelain",
                                  f"--force-with-lease={self.authority.publication_ref}:{self.initial_publication}",
                                  "--", str(self.remote), f"{candidate}:{self.authority.publication_ref}"],
                                 env=git_environment(), capture_output=True, timeout=20)
        self.assertNotEqual(control.returncode, 0)
        self.assertTrue(marker.exists())
        self.assertEqual(self.runtime.backend.remote_head(), self.initial_publication)
        marker.unlink()

        self.assertEqual(self.runtime.backend.push(candidate, self.initial_publication), "published")
        self.assertFalse(marker.exists())
        self._assert_rejection_preserved_state(candidate)

    def test_preservation_disables_source_fsmonitor_helper(self):
        marker = self.root / "fsmonitor-ran"
        helper = self.root / "fake-fsmonitor"
        helper.write_text("#!" + sys.executable + "\nfrom pathlib import Path\nimport sys\n"
                          f"Path({str(marker)!r}).write_text('triggered')\n"
                          "sys.stdout.buffer.write(b'cookie\\x00')\n", encoding="utf-8")
        helper.chmod(0o700)
        self.git(self.checkout, "config", "core.fsmonitor", str(helper))

        control = subprocess.run(["git", "-C", str(self.checkout), "status", "--porcelain=v1"],
                                 env=git_environment(), capture_output=True, timeout=20)
        self.assertEqual(control.returncode, 0, control.stderr.decode("utf-8", "replace"))
        self.assertTrue(marker.exists())
        marker.unlink()
        # The control status may add Git's own fsmonitor index extension; that
        # post-control index is the preservation baseline for the runtime call.
        self.source_index = (self.checkout / ".git/index").read_bytes()

        record = preserve(self.checkout, self.runtime.store)
        self.assertFalse(marker.exists())
        self.assertEqual(record["HEAD"], self.source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), self.source_index)
        index_entries = bytes.fromhex(record["index_entries_hex"])
        self.assertEqual(record["index_evidence"], hashlib.sha256(index_entries).hexdigest())
        self.git(self.checkout, "config", "--unset", "core.fsmonitor")
        self._assert_rejection_preserved_state(self.initial_publication)
