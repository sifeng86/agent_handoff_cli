import multiprocessing
import os
from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.publication import Runtime
from test_publication import RepositoryFixture


class FaultTests(RepositoryFixture):
    def test_A20_release_accepted_process_crash_resolves_without_replay_or_writer_revival(self):
        self.runtime.acquire()
        session = self.runtime.sessions.current()
        before = self.observe()
        original_state = self.git(self.remote, "show", before + ":.agent/state.json")
        index = (self.checkout / ".git/index").read_bytes()
        def child():
            def crash(point):
                if point == "after_server_accept_before_client_reply":
                    os._exit(77)
            with Runtime(self.checkout, self.root / "private", self.authority, self.context, fault=crash) as runtime:
                runtime.release()
        process = multiprocessing.get_context("fork").Process(target=child)
        process.start()
        process.join(timeout=15)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail("Release crash harness did not terminate within its deadline.")
        self.assertEqual(process.exitcode, 77)
        accepted = self.observe()
        self.assertNotEqual(accepted, before)
        pending, = self.runtime.journal.pending()
        self.assertEqual(pending["operation_type"], "release")
        with self.new_runtime("private", self.context) as restarted:
            with patch.object(restarted.backend, "push", side_effect=AssertionError("Must not replay")) as push:
                with self.assertRaises(RuntimeFailure):
                    restarted.recover()
                resolved = restarted.resolve(pending["operation_id"])
                self.assertEqual(resolved["ownership"], "released")
                self.assertEqual(restarted.resolve(pending["operation_id"])["ownership"], "released")
                with self.assertRaises(RuntimeFailure):
                    restarted.recover()
                push.assert_not_called()
            self.assertTrue(restarted.store.read("session-" + session["session_id"] + ".json")["revoked"])
        self.assertEqual(self.observe(), accepted)
        self.assertEqual(self.git(self.remote, "show", accepted + ":.agent/state.json"), original_state)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)

    def test_prepared_before_send_is_proven_local_only_after_restart(self):
        def crash(point):
            if point == "after_journal_fsync":
                raise SystemExit(77)
        self.runtime.fault = crash
        before = self.observe()
        with self.assertRaises(SystemExit):
            self.runtime.acquire()
        pending = self.runtime.journal.pending()[0]
        self.runtime.fault = lambda point: None
        self.assertEqual(self.runtime.resolve(pending["operation_id"])["code_publication"], "local_only")
        self.assertEqual(self.observe(), before)
        self.assertFalse(self.runtime.journal.pending())
        self.assertEqual(self.runtime.acquire()["ownership"], "owned")

    def test_A16_process_crash_after_server_accept_resolves_after_restart(self):
        def child():
            def crash(point):
                if point == "after_server_accept_before_client_reply":
                    os._exit(77)
            with Runtime(self.checkout, self.root / "private", self.authority, self.context, fault=crash) as runtime:
                runtime.acquire()
        process = multiprocessing.get_context("fork").Process(target=child)
        process.start()
        process.join(timeout=15)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail("Fault harness did not terminate within its deadline.")
        self.assertEqual(process.exitcode, 77)
        pending = self.runtime.journal.pending()
        self.assertEqual(len(pending), 1)
        before = self.observe()
        with self.new_runtime("private", self.context) as restarted:
            published = restarted.resolve(pending[0]["operation_id"])
            self.assertEqual(published["code_publication"], "published")
            self.assertEqual(restarted.recover()["ownership"], "owned")
        self.assertEqual(self.observe(), before)

    def test_crash_before_capability_write_reconstructs_private_proof(self):
        def crash(point):
            if point == "before_capability_write":
                raise SystemExit(77)
        self.runtime.fault = crash
        with self.assertRaises(SystemExit):
            self.runtime.acquire()
        pending = self.runtime.journal.pending()[0]
        self.runtime.fault = lambda point: None
        self.assertEqual(self.runtime.resolve(pending["operation_id"])["ownership"], "owned")
        self.assertEqual(self.runtime.recover()["ownership"], "owned")
