"""Real independent-process authority transition serialization."""
import multiprocessing
from queue import Empty

from agent_handoff.authority import Authority
from agent_handoff.authority_transition import AuthorityTransition
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.publication import Runtime
from test_publication import RepositoryFixture


def apply_in_process(checkout, private, remote, source_ref, target_ref, context, decision_id,
                     ready, release, output):
    try:
        with Runtime(checkout, private, Authority(remote, source_ref), context) as source:
            with Runtime(checkout, private, Authority(remote, target_ref), context) as target:
                if ready is not None:
                    def hold(point):
                        if point == "after_transition_journal":
                            ready.set()
                            if not release.wait(10):
                                raise RuntimeError("Transition harness deadline exceeded.")
                    source.fault = hold
                report = AuthorityTransition(source, target).apply(decision_id, "switch")
                output.put((0, report["transition_outcome"]))
    except RuntimeFailure as error:
        output.put((error.code, error.status))
    except Exception:
        output.put((99, "harness_error"))


class TransitionConcurrencyTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.processes = []
        self.addCleanup(self._cleanup_processes)
        self.mp = multiprocessing.get_context("spawn")

    def _cleanup_processes(self):
        for process in self.processes:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)

    def _start(self, source_ref, target_ref, context, decision_id, ready=None, release=None):
        output = self.mp.Queue()
        self.addCleanup(output.close)
        process = self.mp.Process(target=apply_in_process, args=(self.checkout, self.root / "private",
            str(self.remote), source_ref, target_ref, context, decision_id, ready, release, output))
        process.start()
        self.processes.append(process)
        return process, output

    def _result(self, process, output):
        try:
            result = output.get(timeout=10)
        except Empty:
            self.fail("Independent transition process produced no terminal result.")
        process.join(timeout=5)
        self.assertFalse(process.is_alive(), "Transition process did not terminate.")
        self.assertEqual(process.exitcode, 0)
        return result

    def test_same_caller_competing_transitions_have_one_local_commit(self):
        first_ref, second_ref = "refs/heads/first-target", "refs/heads/second-target"
        head = self.observe()
        index = (self.checkout / ".git/index").read_bytes()
        with Runtime(self.checkout, self.root / "private", Authority(str(self.remote), first_ref), self.context) as first:
            with Runtime(self.checkout, self.root / "private", Authority(str(self.remote), second_ref), self.context) as second:
                first_id = AuthorityTransition(self.runtime, first).propose()["decision"]["decision_id"]
                second_id = AuthorityTransition(self.runtime, second).propose()["decision"]["decision_id"]
                ready, release = self.mp.Event(), self.mp.Event()
                winner, winner_output = self._start(self.authority.publication_ref, first_ref, self.context,
                                                    first_id, ready, release)
                self.assertTrue(ready.wait(10), "Winner never reached durable intent boundary.")
                loser, loser_output = self._start(self.authority.publication_ref, second_ref, self.context, second_id)
                self.assertEqual(self._result(loser, loser_output), (4, "rejected"))
                release.set()
                self.assertEqual(self._result(winner, winner_output), (0, "selected"))
                self.runtime.selection.require(first.authority.identity)
                with self.assertRaises(RuntimeFailure):
                    AuthorityTransition(self.runtime, second).apply(second_id, "switch")
                self.assertEqual(self.runtime.store.read("decision-state-" + first_id + ".json")["status"], "applied")
                self.assertIsNone(first.sessions.current())
                self.assertIsNone(second.sessions.current())
        self.assertEqual(self.observe(), head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual(self.git(self.remote, "branch", "--list", "first-target", "second-target"), "")

    def test_opposite_transitions_different_callers_share_repo_serialization(self):
        target_ref, other_context = "refs/heads/next-publication", "b" * 64
        target_authority = Authority(str(self.remote), target_ref)
        with Runtime(self.checkout, self.root / "private", target_authority, other_context) as target_source:
            target_source.initialize(self.base, mainline_ref="refs/heads/main")
            target_head = target_source.backend.observe()
            source_head = self.observe()
            with Runtime(self.checkout, self.root / "private", target_authority, self.context) as target:
                with Runtime(self.checkout, self.root / "private", self.authority, other_context) as reverse_target:
                    forward_id = AuthorityTransition(self.runtime, target).propose()["decision"]["decision_id"]
                    reverse_id = AuthorityTransition(target_source, reverse_target).propose()["decision"]["decision_id"]
                    ready, release = self.mp.Event(), self.mp.Event()
                    forward, output = self._start(self.authority.publication_ref, target_ref, self.context,
                                                  forward_id, ready, release)
                    self.assertTrue(ready.wait(10))
                    reverse, reverse_output = self._start(target_ref, self.authority.publication_ref,
                                                          other_context, reverse_id)
                    self.assertEqual(self._result(reverse, reverse_output), (4, "rejected"))
                    release.set()
                    self.assertEqual(self._result(forward, output), (0, "selected"))
                    self.runtime.selection.require(target_authority.identity)
                    target_source.selection.require(target_authority.identity)
                    retried, retry_output = self._start(target_ref, self.authority.publication_ref,
                                                        other_context, reverse_id)
                    self.assertEqual(self._result(retried, retry_output), (0, "selected"))
                    target_source.selection.require(self.authority.identity)
                    self.runtime.selection.require(target_authority.identity)
            self.assertEqual(target_source.backend.observe(), target_head)
            self.assertEqual(self.observe(), source_head)
