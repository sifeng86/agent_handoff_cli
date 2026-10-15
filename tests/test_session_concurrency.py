"""Independent-process mutation exclusion for one session and two checkouts."""
import multiprocessing
from queue import Empty

from agent_handoff.authority import Authority
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.publication import Runtime
from test_publication import RepositoryFixture


def checkpoint_in_process(checkout, private, remote, publication_ref, context, ready, release, output):
    try:
        def hold(point):
            if ready is not None and point == "before_remote_send":
                ready.set()
                if not release.wait(15):
                    raise RuntimeError("Session concurrency fixture deadline exceeded.")
        with Runtime(checkout, private, Authority(remote, publication_ref), context, fault=hold) as runtime:
            report = runtime.publish_snapshot("checkpoint", ["code.txt"], summary="Bound writer snapshot")
            output.put((0, report["ownership"], report["published_revision"]))
    except RuntimeFailure as error:
        reason = {"Another runtime command holds the mutation lock.": "lock",
                  "Another checkout or process context holds the session capability.": "binding"}.get(str(error), "other")
        output.put((error.code, error.status, reason))
    except Exception:
        output.put((99, "harness_error", None))


class SessionConcurrencyTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.mp = multiprocessing.get_context("spawn")
        self.processes = []
        self.addCleanup(self._cleanup_processes)

    def _cleanup_processes(self):
        for process in self.processes:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)

    def _start(self, checkout, ready=None, release=None):
        output = self.mp.Queue()
        self.addCleanup(output.close)
        process = self.mp.Process(target=checkpoint_in_process, args=(checkout, self.root / "private",
            str(self.remote), self.authority.publication_ref, self.context, ready, release, output))
        self.processes.append(process)
        process.start()
        return process, output

    def _result(self, process, output):
        process.join(timeout=15)
        self.assertFalse(process.is_alive(), "Mutation process exceeded its fixture deadline.")
        self.assertEqual(process.exitcode, 0)
        try:
            return output.get(timeout=5)
        except Empty:
            self.fail("Mutation process did not return a safe result.")

    def test_A29_same_session_two_checkouts_have_only_one_process_writer(self):
        self.runtime.acquire()
        original_session = self.runtime.sessions.current()
        original_binding = self.runtime.store.read("checkout-binding.json")
        before = self.observe()
        other = self.root / "other-checkout"
        self.git(self.root, "clone", "--quiet", "--branch", "main", "--", str(self.remote), str(other))
        snapshots = {}
        for checkout, label in ((self.checkout, "bound"), (other, "unbound")):
            (checkout / "code.txt").write_text(label + " staged\n")
            self.git(checkout, "add", "--", "code.txt")
            (checkout / "code.txt").write_text(label + " retained edit\n")
            status = self.git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
            snapshots[checkout] = (self.git(checkout, "rev-parse", "HEAD"),
                (checkout / ".git/index").read_bytes(), (checkout / "code.txt").read_bytes(), status)
        ready, release = self.mp.Event(), self.mp.Event()
        winner, winner_output = self._start(self.checkout, ready, release)
        self.assertTrue(ready.wait(15), "Bound writer did not reach the locked send boundary.")
        loser, loser_output = self._start(other)
        self.assertEqual(self._result(loser, loser_output), (4, "rejected", "lock"))
        self.assertEqual(self.observe(), before)
        self.assertEqual(self.runtime.store.read("checkout-binding.json"), original_binding)
        release.set()
        code, ownership, revision = self._result(winner, winner_output)
        self.assertEqual((code, ownership), (0, "owned"))
        self.assertEqual(self.observe(), revision)
        self.assertEqual(self.git(self.remote, "rev-list", "--parents", "-n", "1", revision).split(), [revision, before])
        self.assertEqual(self.git(self.remote, "show", revision + ":code.txt"), "bound retained edit")
        proved = self.runtime.sessions.prove(self.runtime.backend, revision, self.runtime.backend.records(revision))
        self.assertEqual(proved["session_id"], original_session["session_id"])
        retry, retry_output = self._start(other)
        self.assertEqual(self._result(retry, retry_output), (4, "rejected", "binding"))
        self.assertEqual(self.observe(), revision)
        self.assertEqual(self.runtime.store.read("checkout-binding.json"), original_binding)
        for checkout, snapshot in snapshots.items():
            head, index, contents, status = snapshot
            self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), head)
            self.assertEqual((checkout / ".git/index").read_bytes(), index)
            self.assertEqual((checkout / "code.txt").read_bytes(), contents)
            self.assertEqual(self.git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"), status)
