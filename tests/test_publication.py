import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.authority import Authority
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.git_backend import git_environment
from agent_handoff.locks import lock
from agent_handoff.publication import Runtime


class RepositoryFixture(unittest.TestCase):
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
        (self.checkout / "code.txt").write_text("original\n")
        self.git(self.checkout, "add", "code.txt")
        self.git(self.checkout, "commit", "-qm", "base")
        self.base = self.git(self.checkout, "rev-parse", "HEAD")
        self.git(self.checkout, "push", "-q", str(self.remote), "main")
        self.authority = Authority(str(self.remote), "refs/heads/publication")
        self.context = "a" * 64
        self.runtime = self.new_runtime("private", self.context)
        self.addCleanup(self.runtime.close)
        initialized = self.runtime.initialize(self.base, mainline_ref="refs/heads/main")
        self.assertEqual(initialized["code_publication"], "published")

    @staticmethod
    def git(directory, *args):
        return subprocess.check_output(["git", "-C", str(directory), *args], env=git_environment(),
                                       stderr=subprocess.PIPE, text=True).strip()

    def new_runtime(self, private, context, checkout=None):
        return Runtime(checkout or self.checkout, self.root / private, self.authority, context)

    def observe(self):
        return self.git(self.remote, "rev-parse", self.authority.publication_ref)


class PublicationTests(RepositoryFixture):
    def test_A23_readonly_inspection_rejects_wrong_authority_config_and_invalid_plan(self):
        from agent_handoff.git_backend import inspect
        head = self.observe()
        config = self.runtime.backend.records(head)["config"]
        trust = self.runtime.store.read("integrity.json")
        index = (self.checkout / ".git/index").read_bytes()
        (self.checkout / "code.txt").write_text("retain local edit\n")
        cases = [
            {".agent/config.yaml": json.dumps(dict(config, authority="https://example.invalid/other.git")).encode()},
            {".agent/config.yaml": None},
            {".agent/config.yaml": ("120000", json.dumps(config).encode())},
            {".agent/state.json": json.dumps({"schema_version": 1, "status": "working", "active_plan": "P-MISSING",
                "current_step": "S001", "blocker": None, "summary": "private-looking-repository-text"}).encode()},
        ]
        for changes in cases:
            with self.subTest(paths=list(changes)):
                invalid, _ = self.runtime.backend.prepare(head, changes, "hand edited invalid config or plan")
                current = self.observe()
                self.assertEqual(self.runtime.backend.push(invalid, current), "published")
                with self.assertRaises(RuntimeFailure) as caught:
                    inspect(str(self.remote), self.authority.publication_ref)
                self.assertNotIn("private-looking-repository-text", str(caught.exception))
                self.assertEqual(self.observe(), invalid)
                self.assertEqual(self.runtime.store.read("integrity.json"), trust)
                self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
                self.assertEqual((self.checkout / "code.txt").read_text(), "retain local edit\n")

    def test_A01_A04_A09_acquire_recover_release(self):
        original = self.git(self.checkout, "status", "--porcelain")
        acquired = self.runtime.acquire()
        self.assertEqual(acquired["ownership"], "owned")
        head = self.observe()
        self.assertEqual(self.runtime.recover()["ownership"], "owned")
        self.assertEqual(self.observe(), head)
        state = self.git(self.remote, "show", head + ":.agent/state.json")
        released = self.runtime.release()
        self.assertEqual(released["ownership"], "released")
        self.assertEqual(self.git(self.remote, "show", self.observe() + ":.agent/state.json"), state)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), original)

    def test_A03_A06_A08_takeover_fences_previous_release(self):
        self.runtime.acquire()
        old_session = self.runtime.sessions.current()
        old_head = self.observe()
        with self.new_runtime("other-private", "b" * 64) as other:
            other.initialize()
            proposal = other.acquire()
            self.assertEqual(proposal["status"], "decision_required")
            self.assertEqual(proposal["decision"]["category"], "takeover")
            self.assertEqual(self.observe(), old_head)
            takeover = other.apply_decision(proposal["decision"]["decision_id"], "takeover")
            self.assertEqual(takeover["ownership"], "owned")
            takeover_head = self.observe()
            self.assertEqual(takeover["published_revision"], takeover_head)
            self.assertEqual(self.git(self.remote, "rev-list", "--parents", "-n", "1", takeover_head).split(),
                             [takeover_head, old_head])
            new_session = other.sessions.prove(other.backend, takeover_head, other.backend.records(takeover_head))
            self.assertNotEqual(new_session["session_id"], old_session["session_id"])
            owner = other.backend.records(takeover_head)["owner"]["owner"]
            self.assertEqual(owner["session_id"], new_session["session_id"])
            self.assertEqual(owner["acquisition_operation_id"], new_session["acquisition_operation_id"])
        head = self.observe()
        with self.assertRaises(RuntimeFailure) as failure:
            self.runtime.release()
        self.assertEqual(failure.exception.code, 4)
        self.assertEqual(self.observe(), head)

    def test_A07_old_owner_checkpoint_after_takeover_cannot_publish(self):
        self.runtime.acquire()
        (self.checkout / "code.txt").write_text("old owner retained edit\n")
        with self.new_runtime("takeover-private", "b" * 64) as other:
            other.initialize()
            proposal = other.acquire()
            other.apply_decision(proposal["decision"]["decision_id"], "takeover")
            before = self.observe()
            index = (self.checkout / ".git/index").read_bytes()
            with self.assertRaises(RuntimeFailure) as fenced:
                self.runtime.publish_snapshot("checkpoint", ["code.txt"], summary="Old owner edit")
            self.assertEqual(fenced.exception.code, 4)
            self.assertEqual(self.observe(), before)
            self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
            self.assertEqual((self.checkout / "code.txt").read_text(), "old owner retained edit\n")
            self.assertEqual(other.recover()["ownership"], "owned")

    def test_A23_invalid_hand_edited_control_metadata_never_authorizes_mutation(self):
        import json
        self.runtime.acquire()
        before = self.observe()
        (self.checkout / "code.txt").write_text("retained local edit\n")
        edits = [(".agent/owner.json", {"schema_version": 1, "owner": {"session_id": "invalid"}}),
                 (".agent/protocol.json", {"schema_version": 1, "protocol_version": "future", "required_features": []})]
        for path, value in edits:
            with self.subTest(control=path):
                self.git(self.remote, "update-ref", self.authority.publication_ref, before)
                corrupt, _tree = self.runtime.backend.prepare(before, {path: json.dumps(value).encode()}, "Hand-edited control\n")
                self.assertEqual(self.runtime.backend.push(corrupt, before), "published")
                with self.assertRaises(RuntimeFailure) as invalid:
                    self.runtime.publish_snapshot("checkpoint", ["code.txt"], summary="Invalid authority")
                self.assertIn(invalid.exception.code, (7, 8))
                self.assertEqual(self.observe(), corrupt)
                self.assertEqual(self.runtime.store.read("integrity.json")["last_trusted_oid"], before)
                self.assertEqual((self.checkout / "code.txt").read_text(), "retained local edit\n")

    def test_A20_unknown_release_does_not_claim_or_clear_owner(self):
        self.runtime.acquire()
        before = self.observe()
        owner = self.runtime.backend.records(before)["owner"]
        with patch.object(self.runtime.backend, "push", return_value="publication_unknown") as send:
            report = self.runtime.release()
            self.assertEqual(report["status"], "unknown")
            self.assertEqual(report["code_publication"], "publication_unknown")
            self.assertNotEqual(report["ownership"], "released")
            self.assertEqual(self.runtime.resolve(report["operation_id"])["status"], "unknown")
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.observe(), before)
        self.assertEqual(self.runtime.backend.records(before)["owner"], owner)
        with self.assertRaises(RuntimeFailure) as pending:
            self.runtime.publish_snapshot("checkpoint", ["code.txt"], summary="Pending release")
        self.assertEqual(pending.exception.code, 5)

    def test_A18_unknown_send_with_advanced_head_never_overwrites_competing_revision(self):
        self.runtime.acquire()
        before = self.observe()
        (self.checkout / "code.txt").write_text("retained original intent\n")
        push = self.runtime.backend.push
        competing = []
        def advance(_, expected):
            commit, _tree = self.runtime.backend.prepare(expected, {"other.txt": b"competing publication\n"}, "Competing writer\n")
            self.assertEqual(push(commit, expected), "published")
            competing.append(commit)
            return "publication_unknown"
        with patch.object(self.runtime.backend, "push", side_effect=advance) as send:
            report = self.runtime.publish_snapshot("checkpoint", ["code.txt"], summary="Original intent")
            self.assertEqual(send.call_count, 1)
        self.assertEqual(report["code_publication"], "publication_unknown")
        self.assertEqual(self.observe(), competing[0])
        self.assertEqual(self.git(self.remote, "rev-list", "--parents", "-n", "1", competing[0]).split(), [competing[0], before])
        self.assertEqual(self.git(self.remote, "show", competing[0] + ":code.txt"), "original")
        self.assertEqual((self.checkout / "code.txt").read_text(), "retained original intent\n")

    def test_A05_remote_id_without_local_capability_cannot_recover(self):
        self.runtime.acquire()
        original_session = self.runtime.sessions.current()
        original_binding = self.runtime.store.read("checkout-binding.json")
        original_head = self.observe()
        original_owner = self.runtime.backend.records(original_head)["owner"]["owner"]
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        source_status = self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        source_file = (self.checkout / "code.txt").read_bytes()

        with self.new_runtime("no-proof", "b" * 64) as other:
            other.initialize()
            with self.assertRaises(RuntimeFailure) as failure:
                other.recover()
            self.assertEqual(failure.exception.code, 4)
            self.assertEqual(failure.exception.status, "rejected")
            self.assertEqual(self.observe(), original_head)
            self.assertEqual(self.runtime.backend.records(original_head)["owner"]["owner"], original_owner)

        os.unlink("session-" + original_session["session_id"] + ".json", dir_fd=self.runtime.store.fd)
        self.assertEqual(self.runtime.store.read("checkout-binding.json"), original_binding)
        self.assertEqual(original_binding["session_id"], original_session["session_id"])
        with self.assertRaises(RuntimeFailure) as missing:
            self.runtime.recover()
        self.assertEqual(missing.exception.code, 4)
        self.assertEqual(missing.exception.status, "rejected")
        self.assertEqual(self.observe(), original_head)
        self.assertEqual(self.runtime.backend.records(original_head)["owner"]["owner"], original_owner)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), source_status)
        self.assertEqual((self.checkout / "code.txt").read_bytes(), source_file)

        proposal = self.runtime.acquire()
        self.assertEqual(proposal["status"], "decision_required")
        self.assertEqual(proposal["decision"]["category"], "takeover")
        self.assertEqual(self.observe(), original_head)
        self.assertEqual(self.runtime.backend.records(original_head)["owner"]["owner"], original_owner)
        taken = self.runtime.apply_decision(proposal["decision"]["decision_id"], "takeover")
        self.assertEqual(taken["ownership"], "owned")
        takeover_head = self.observe()
        self.assertEqual(self.git(self.remote, "rev-list", "--parents", "-n", "1", takeover_head).split(),
                         [takeover_head, original_head])
        new_session = self.runtime.sessions.prove(self.runtime.backend, takeover_head,
                                                  self.runtime.backend.records(takeover_head))
        self.assertNotEqual(new_session["session_id"], original_session["session_id"])
        self.assertEqual(self.runtime.backend.records(takeover_head)["owner"]["owner"]["session_id"],
                         new_session["session_id"])

    def test_stale_decision(self):
        self.runtime.acquire()
        source = self.checkout / "code.txt"
        source.write_bytes(b"staged before stale check\n")
        self.git(self.checkout, "add", "code.txt")
        source.write_bytes(b"unstaged before stale check\n")
        untracked = self.checkout / "stale-check.bin"
        untracked.write_bytes(b"\x00\xfflocal")
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        source_status = self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        source_files = {"code.txt": source.read_bytes(), "stale-check.bin": untracked.read_bytes()}
        with self.new_runtime("other-private", "b" * 64) as other:
            other.initialize()
            proposal = other.acquire()
            decision_id = proposal["decision"]["decision_id"]
            private_trust = other.store.read("integrity.json")
            released = self.runtime.release()
            current_ref = other.backend.observe()
            current_owner = other.backend.records(current_ref)["owner"]["owner"]
            self.assertEqual(released["published_revision"], current_ref)
            self.assertIsNone(current_owner)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
            self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), source_status)
            self.assertEqual({name: (self.checkout / name).read_bytes() for name in source_files}, source_files)
            with patch.object(other.backend, "push") as push, self.assertRaises(RuntimeFailure) as failure:
                other.apply_decision(proposal["decision"]["decision_id"], "takeover")
            self.assertEqual(failure.exception.code, 3)
            self.assertEqual(failure.exception.status, "decision_required")
            self.assertEqual(other.store.read("decision-state-" + decision_id + ".json")["status"], "stale")
            push.assert_not_called()
            self.assertEqual(other.backend.remote_head(), current_ref)
            self.assertEqual(other.backend.records(current_ref)["owner"]["owner"], current_owner)
            self.assertEqual(other.store.read("integrity.json"), private_trust)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
            self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), source_status)
            self.assertEqual({name: (self.checkout / name).read_bytes() for name in source_files}, source_files)

    def test_reply_loss_resolves_exact_commit(self):
        real_push = self.runtime.backend.push
        def lost_reply(oid, expected):
            self.assertEqual(real_push(oid, expected), "published")
            return "publication_unknown"
        with patch.object(self.runtime.backend, "push", side_effect=lost_reply):
            published = self.runtime.acquire()
        self.assertEqual(published["code_publication"], "published")
        count = self.git(self.remote, "rev-list", "--count", self.authority.publication_ref)
        self.assertEqual(self.runtime.resolve(published["operation_id"])["code_publication"], "published")
        self.assertEqual(self.git(self.remote, "rev-list", "--count", self.authority.publication_ref), count)

    def test_unknown_blocks_mutation_without_retry(self):
        with patch.object(self.runtime.backend, "push", return_value="publication_unknown"):
            unknown = self.runtime.acquire()
        self.assertEqual(unknown["status"], "unknown")
        with self.assertRaises(RuntimeFailure) as failure:
            self.runtime.acquire()
        self.assertEqual(failure.exception.code, 5)
        self.assertEqual(self.runtime.resolve(unknown["operation_id"])["status"], "unknown")

    def test_checkout_binding_and_context_not_implicitly_shared(self):
        self.runtime.acquire()
        with self.new_runtime("private", "b" * 64) as other:
            with self.assertRaises(RuntimeFailure):
                other.recover()

    def test_kernel_lock_rejects_second_writer(self):
        with lock(self.runtime.store, "repository.lock"):
            with self.assertRaises(RuntimeFailure) as failure:
                self.runtime.acquire()
            self.assertEqual(failure.exception.code, 4)

    def test_registered_ref_delete_cannot_reinitialize(self):
        staged = self.checkout / "code.txt"
        staged.write_bytes(b"staged local edit\n")
        self.git(self.checkout, "add", "code.txt")
        staged.write_bytes(b"unstaged local edit\n")
        untracked = self.checkout / "local.bin"
        untracked.write_bytes(b"\x00\xfflocal")
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        source_status = self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        source_files = {"code.txt": staged.read_bytes(), "local.bin": untracked.read_bytes()}
        trust = self.runtime.store.read("integrity.json")
        mainline = self.git(self.remote, "rev-parse", "refs/heads/main")
        self.git(self.remote, "update-ref", "-d", self.authority.publication_ref)
        with patch.object(self.runtime.backend, "push") as push, self.assertRaises(RuntimeFailure) as failure:
            self.runtime.initialize(self.base)
        self.assertEqual(failure.exception.code, 7)
        push.assert_not_called()
        self.assertIsNone(self.runtime.backend.remote_head())
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/main"), mainline)
        self.assertEqual(self.runtime.store.read("integrity.json"), trust)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), source_status)
        self.assertEqual({name: (self.checkout / name).read_bytes() for name in source_files}, source_files)

    def test_direct_two_prepared_cas_writers(self):
        expected = self.observe()
        one, _ = self.runtime.backend.prepare(expected, {"first.txt": b"one"}, "first")
        two, _ = self.runtime.backend.prepare(expected, {"second.txt": b"two"}, "second")
        self.assertEqual(self.runtime.backend.push(one, expected), "published")
        self.assertEqual(self.runtime.backend.push(two, expected), "rejected")
        self.assertEqual(self.observe(), one)

    def test_rewritten_history_stops_mutation(self):
        initial = self.observe()
        self.runtime.acquire()
        tracked = self.checkout / "code.txt"
        tracked.write_bytes(b"staged local edit\n")
        self.git(self.checkout, "add", "code.txt")
        tracked.write_bytes(b"unstaged local edit\n")
        untracked = self.checkout / "local.bin"
        untracked.write_bytes(b"\x00\xfflocal")
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        source_status = self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        source_files = {"code.txt": tracked.read_bytes(), "local.bin": untracked.read_bytes()}
        trust = self.runtime.store.read("integrity.json")
        mainline = self.git(self.remote, "rev-parse", "refs/heads/main")
        self.git(self.remote, "update-ref", self.authority.publication_ref, initial)
        rewritten_owner = subprocess.check_output(["git", "-C", str(self.remote), "show", f"{initial}:.agent/owner.json"],
                                                   env=git_environment(), stderr=subprocess.PIPE)
        with patch.object(self.runtime.backend, "push") as push, self.assertRaises(RuntimeFailure) as failure:
            self.runtime.release()
        self.assertEqual(failure.exception.code, 7)
        push.assert_not_called()
        self.assertEqual(self.runtime.backend.remote_head(), initial)
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.remote), "show", f"{initial}:.agent/owner.json"],
                                                  env=git_environment(), stderr=subprocess.PIPE), rewritten_owner)
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/main"), mainline)
        self.assertEqual(self.runtime.store.read("integrity.json"), trust)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), source_status)
        self.assertEqual({name: (self.checkout / name).read_bytes() for name in source_files}, source_files)


if __name__ == "__main__":
    unittest.main()
