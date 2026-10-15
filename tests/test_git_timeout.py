"""Real OS timeout and descendant cancellation with accepted Git writes."""
import os
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.git_backend import _GitCommandTimeout, _terminal_rejection
from test_publication import RepositoryFixture


class GitTimeoutTests(RepositoryFixture):
    def wrapper(self, *, accept):
        directory = self.root / "transport-bin"
        directory.mkdir()
        pid_path = self.root / "descendant.pid"
        real_git = shutil.which("git")
        wrapper = directory / "git"
        wrapper.write_text("#!/usr/bin/python3\n" +
            "import os, subprocess, sys\n" +
            "from pathlib import Path\n" +
            "if 'push' not in sys.argv or '--porcelain' not in sys.argv:\n" +
            f"    os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n" +
            (f"subprocess.run([{real_git!r}, *sys.argv[1:]], check=True)\n" if accept else "") +
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n" +
            f"Path({str(pid_path)!r}).write_text(str(child.pid))\n" +
            "child.wait()\n")
        wrapper.chmod(0o700)
        return directory, pid_path

    def assert_descendant_terminal(self, pid_path):
        self.assertTrue(pid_path.exists())
        pid = int(pid_path.read_text())
        status = Path('/proc') / str(pid) / 'stat'
        if status.exists():
            # Reparented killed children can remain zombies until init reaps them.
            self.assertEqual(status.read_text().rsplit(')', 1)[1].split()[0], 'Z')

    def test_timeout_kills_pipe_holding_descendants_and_preserves_unknown_intent(self):
        directory, pid_path = self.wrapper(accept=False)
        before = self.observe()
        self.runtime.backend.command_timeout = 0.5
        started = time.monotonic()
        with patch.dict(os.environ, {'PATH': str(directory) + os.pathsep + os.environ['PATH']}):
            report = self.runtime.acquire()
        self.assertLess(time.monotonic() - started, 6)
        self.assert_descendant_terminal(pid_path)
        self.assertEqual(report['status'], 'unknown')
        self.assertEqual(self.observe(), before)
        self.assertIsNone(self.runtime.sessions.current())
        with self.assertRaises(RuntimeFailure):
            self.runtime.acquire()
        self.assertEqual(self.runtime.resolve(report['operation_id'])['status'], 'unknown')

    def test_accepted_write_timeout_resolves_exact_commit_without_duplicate(self):
        directory, pid_path = self.wrapper(accept=True)
        self.runtime.backend.command_timeout = 0.5
        replies = []
        push = self.runtime.backend.push
        def observed_push(*args):
            reply = push(*args)
            replies.append(reply)
            return reply
        with patch.dict(os.environ, {'PATH': str(directory) + os.pathsep + os.environ['PATH']}), \
                patch.object(self.runtime.backend, 'push', side_effect=observed_push):
            report = self.runtime.acquire()
        self.assertEqual(replies, ['publication_unknown'])
        self.assert_descendant_terminal(pid_path)
        self.assertEqual(report['code_publication'], 'published')
        self.assertEqual(report['ownership'], 'owned')
        accepted = self.observe()
        with patch.object(self.runtime.backend, 'push', side_effect=AssertionError('No replay')):
            self.runtime.resolve(report['operation_id'])
            self.assertEqual(self.runtime.recover()['ownership'], 'owned')
        self.assertEqual(self.observe(), accepted)

    def test_terminal_remote_rejection_timeout_is_resolved_and_fresh_acquire_is_new(self):
        directory = self.root / "terminal-bin"
        directory.mkdir()
        real_git = shutil.which("git")
        client_pushes = self.root / "client-pushes"
        receive_calls = self.root / "receive-calls"
        hooks = self.remote / "fixture-hooks"
        hooks.mkdir()
        hook = hooks / "pre-receive"
        hook.write_text(
            "#!/usr/bin/python3\n"
            "import pathlib, sys\n"
            f"counter = pathlib.Path({str(receive_calls)!r})\n"
            "counter.write_text(counter.read_text() + '1\\n' if counter.exists() else '1\\n')\n"
            "sys.stdin.read()\n"
            "raise SystemExit(1)\n"
        )
        hook.chmod(0o700)
        receive_shim = directory / "git-receive-pack"
        receive_shim.write_text(
            "#!/usr/bin/python3\n"
            "import os, sys\n"
            f"os.execv({real_git!r}, [{real_git!r}, '-c', {('core.hooksPath=' + str(hooks))!r}, "
            "'receive-pack', *sys.argv[1:]])\n"
        )
        receive_shim.chmod(0o700)
        git_wrapper = directory / "git"
        git_wrapper.write_text(
            "#!/usr/bin/python3\n"
            "import os, subprocess, sys\n"
            f"real = {real_git!r}\n"
            "if 'push' not in sys.argv or '--porcelain' not in sys.argv:\n"
            "    os.execv(real, [real, *sys.argv[1:]])\n"
            f"with open({str(client_pushes)!r}, 'a', encoding='ascii') as count: count.write('push\\n')\n"
            "args = list(sys.argv[1:])\n"
            "args.insert(args.index('push') + 1, "
            f"'--receive-pack={receive_shim}')\n"
            "subprocess.run([real, *args], check=False)\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "child.wait()\n"
        )
        git_wrapper.chmod(0o700)

        before = self.observe()
        self.runtime.backend.command_timeout = 0.5
        with patch.dict(os.environ, {'PATH': str(directory) + os.pathsep + os.environ['PATH']}):
            rejected = self.runtime.acquire()
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["code_publication"], "rejected")
        self.assertEqual(self.observe(), before)
        self.assertIsNone(self.runtime.sessions.current())
        record = self.runtime.journal.read(rejected["operation_id"])
        self.assertEqual((record["phase"], record["result"]), ("resolved", "rejected"))
        self.assertEqual(record["operation_type"], "acquire")
        candidate_id = record["candidate"]["session_id"]
        self.assertIsNone(self.runtime.store.read("session-" + candidate_id + ".json", missing=True))
        self.assertEqual(receive_calls.read_text(), "1\n")
        self.assertEqual(client_pushes.read_text(), "push\n")
        reachable = self.git(self.remote, "rev-list", "--all").splitlines()
        self.assertNotIn(record["prepared_commit_oid"], reachable)

        hook.unlink()
        with patch.dict(os.environ, {'PATH': str(directory) + os.pathsep + os.environ['PATH']}):
            acquired = self.runtime.acquire()
        self.assertEqual(acquired["status"], "completed")
        self.assertEqual(acquired["ownership"], "owned")
        self.assertNotEqual(acquired["operation_id"], rejected["operation_id"])
        fresh = self.runtime.journal.read(acquired["operation_id"])
        self.assertNotEqual(fresh["prepared_commit_oid"], record["prepared_commit_oid"])
        self.assertEqual(fresh["result"], "published")
        self.assertEqual(self.observe(), fresh["prepared_commit_oid"])
        self.assertEqual(client_pushes.read_text(), "push\npush\n")
        published_since = self.git(self.remote, "rev-list", before + ".." + self.observe()).splitlines()
        self.assertEqual(published_since, [fresh["prepared_commit_oid"]])
        self.assertNotIn(record["prepared_commit_oid"], self.git(self.remote, "rev-list", "--all").splitlines())
        owner = self.runtime.backend.records(self.observe())["owner"]["owner"]
        self.assertEqual(owner["session_id"], fresh["candidate"]["session_id"])
        self.assertNotEqual(owner["session_id"], candidate_id)


class TerminalRejectionParserTests(unittest.TestCase):
    def setUp(self):
        self.oid = "a" * 40
        self.ref = "refs/heads/publication"
        self.row = f"To /fixture.git\n!\t{self.oid}:{self.ref}\t[remote rejected] (pre-receive hook declined)\nDone\n".encode()

    def test_accepts_only_complete_single_ref_terminal_rejections(self):
        self.assertTrue(_terminal_rejection(self.row, self.oid, self.ref))
        stale = f"!\t{self.oid}:{self.ref}\t[rejected] (stale info)\n".encode()
        self.assertTrue(_terminal_rejection(stale, self.oid, self.ref))

    def test_rejects_ambiguous_truncated_or_wrong_identity_receipts(self):
        cases = [
            self.row[:-1],
            self.row + b"\xff\n",
            self.row + b"x" * 65536,
            self.row.replace(self.oid.encode(), b"b" * 40),
            self.row.replace(self.ref.encode(), b"refs/heads/other"),
            self.row.replace(b"[remote rejected]", b"[remote rejected]bad"),
            self.row + f"!\t{self.oid}:{self.ref}\t[remote rejected] duplicate\n".encode(),
            self.row.replace(b"!\t", b"=\t"),
            self.row.replace(b"[remote rejected] (pre-receive hook declined)", b"transport failure"),
            self.row.replace(b"[remote rejected] (pre-receive hook declined)", b"[remote rejected] (partial"),
        ]
        for data in cases:
            with self.subTest(length=len(data)):
                self.assertFalse(_terminal_rejection(data, self.oid, self.ref))
        self.assertFalse(_terminal_rejection(self.row, "b" * 40, self.ref))
        self.assertFalse(_terminal_rejection(self.row, self.oid, "refs/heads/other"))

    def test_timeout_exception_keeps_only_bounded_private_stdout(self):
        marker = b"private-stdout-marker"
        failure = _GitCommandTimeout(marker)
        self.assertNotIn(marker.decode(), str(failure))
        self.assertNotIn(marker.decode(), repr(failure))
        self.assertNotIn(marker.decode(), repr(failure.args))
        self.assertFalse(hasattr(failure, "stderr"))
        oversized = _GitCommandTimeout(b"x" * 65537)
        self.assertEqual(oversized.stdout, b"")
