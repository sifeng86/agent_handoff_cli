"""Real Git wire traffic over loopback TCP; SSH shim is not authentication."""
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import time
from unittest.mock import patch

from agent_handoff.authority import Authority
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.git_backend import git_environment
from agent_handoff.publication import Runtime
from test_publication import RepositoryFixture


def read_exact(stream, size):
    result = b""
    while len(result) < size:
        block = stream.read(size - len(result))
        if not block:
            raise EOFError("Fixture Git advertisement ended early.")
        result += block
    return result


class GitTCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        process = None
        try:
            self.request.settimeout(10)
            operation = b""
            while not operation.endswith(b"\n") and len(operation) < 32:
                byte = self.request.recv(1)
                if not byte:
                    return
                operation += byte
            if operation not in (b"upload\n", b"receive\n"):
                return
            receive = operation == b"receive\n"
            if receive:
                self.server.push_requests += 1
            process = subprocess.Popen([self.server.git, "receive-pack" if receive else "upload-pack",
                str(self.server.remote)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=git_environment(), bufsize=0, start_new_session=True)
            self.server.processes.append(process)
            def input_bridge():
                try:
                    while block := self.request.recv(65536):
                        remaining = memoryview(block)
                        while remaining:
                            count = process.stdin.write(remaining)
                            remaining = remaining[count:]
                except OSError:
                    pass
                finally:
                    process.stdin.close()
            bridge = threading.Thread(target=input_bridge, daemon=True)
            bridge.start()
            if receive:
                # Advertisement must reach the client before withholding result frames.
                while True:
                    header = read_exact(process.stdout, 4)
                    size = int(header, 16)
                    self.request.sendall(header)
                    if size == 0:
                        break
                    self.request.sendall(read_exact(process.stdout, size - 4))
            while block := os.read(process.stdout.fileno(), 65536):
                if receive and self.server.hold_reply:
                    self.server.reply_waiting.set()
                    if not self.server.reply_gate.wait(10):
                        raise TimeoutError("Fixture reply gate deadline exceeded.")
                try:
                    self.request.sendall(block)
                except (BrokenPipeError, ConnectionResetError):
                    # The real server may finish after the timed-out client has gone.
                    pass
            process.wait(timeout=10)
            bridge.join(timeout=1)
        except (OSError, EOFError, ValueError, subprocess.TimeoutExpired):
            self.server.errors.append("transport_fixture_error")
        finally:
            if process is not None:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                process.stdout.close()


class GitTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, remote):
        self.remote, self.git = remote, shutil.which("git")
        self.processes, self.errors = [], []
        self.push_requests = 0
        self.hold_reply = False
        self.reply_gate, self.reply_waiting = threading.Event(), threading.Event()
        super().__init__(("127.0.0.1", 0), GitTCPHandler)
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def stop(self):
        self.reply_gate.set()
        self.shutdown()
        self.server_close()
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        self.thread.join(timeout=2)


class GitTCPTimeoutTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.server = GitTCPServer(self.remote)
        self.addCleanup(self.server.stop)
        directory = self.root / "ssh-shim"
        directory.mkdir()
        shim = directory / "ssh"
        shim.write_text("#!/usr/bin/python3\n" +
            "import os, socket, sys, threading\n" +
            "if '-G' in sys.argv: raise SystemExit(0)\n" +
            f"connection = socket.create_connection(('127.0.0.1', {self.server.server_address[1]}), timeout=10)\n" +
            "connection.settimeout(None)\n" +
            "connection.sendall(b'receive\\n' if 'git-receive-pack' in sys.argv[-1] else b'upload\\n')\n" +
            "def send():\n" +
            "    try:\n" +
            "        while block := os.read(0, 65536): connection.sendall(block)\n" +
            "        connection.shutdown(socket.SHUT_WR)\n" +
            "    except OSError: pass\n" +
            "threading.Thread(target=send, daemon=True).start()\n" +
            "while block := connection.recv(65536):\n" +
            "    remaining = memoryview(block)\n" +
            "    while remaining: remaining = remaining[os.write(1, remaining):]\n")
        shim.chmod(0o700)
        self.environment = patch.dict(os.environ, {"PATH": str(directory) + os.pathsep + os.environ["PATH"]})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.authority_tcp = Authority("git@127.0.0.1:fixture", "refs/heads/tcp-publication")
        self.network_runtime = Runtime(self.checkout, self.root / "tcp-private", self.authority_tcp, self.context)
        self.addCleanup(self.network_runtime.close)
        self.network_runtime.initialize(self.base, mainline_ref="refs/heads/main")
        self.network_runtime.backend.command_timeout = 1
        self.pushes_before = self.server.push_requests

    def test_accepted_server_reply_held_over_tcp_times_out_without_replay(self):
        self.server.hold_reply = True
        replies = []
        push = self.network_runtime.backend.push
        def capture(*args):
            reply = push(*args)
            replies.append(reply)
            return reply
        with patch.object(self.network_runtime.backend, "push", side_effect=capture):
            report = self.network_runtime.acquire()
        self.assertTrue(self.server.reply_waiting.is_set())
        self.assertEqual(replies, ["publication_unknown"])
        self.assertEqual(report["code_publication"], "published")
        self.assertEqual(report["ownership"], "owned")
        accepted = self.network_runtime.backend.observe()
        self.server.reply_gate.set()
        with patch.object(self.network_runtime.backend, "push", side_effect=AssertionError("No network replay")):
            self.network_runtime.resolve(report["operation_id"])
            self.assertEqual(self.network_runtime.recover()["ownership"], "owned")
        self.assertEqual(self.network_runtime.backend.observe(), accepted)
        self.assertEqual(self.server.push_requests, self.pushes_before + 1)

    def test_server_executes_after_client_timeout_unknown_barrier_then_exact_resolution(self):
        entered, gate = self.root / "hook-entered", self.root / "server-release"
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/usr/bin/python3\n" +
            "import sys, time\nfrom pathlib import Path\n" +
            "sys.stdin.read()\n" + f"Path({str(entered)!r}).touch()\n" +
            "deadline = time.monotonic() + 10\n" +
            f"while not Path({str(gate)!r}).exists():\n" +
            "    if time.monotonic() > deadline: raise SystemExit(1)\n" + "    time.sleep(0.01)\n")
        hook.chmod(0o700)
        before = self.network_runtime.backend.observe()
        report = self.network_runtime.acquire()
        self.assertTrue(entered.exists())
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(self.network_runtime.backend.observe(), before)
        self.assertIsNone(self.network_runtime.sessions.current())
        with self.assertRaises(RuntimeFailure):
            self.network_runtime.acquire()
        self.assertEqual(self.network_runtime.resolve(report["operation_id"])["status"], "unknown")
        gate.touch()
        deadline = time.monotonic() + 5
        while self.git(self.remote, "rev-parse", self.authority_tcp.publication_ref) == before:
            if time.monotonic() > deadline:
                self.fail("Delayed receive-pack did not finish after its gate opened.")
            time.sleep(0.01)
        with patch.object(self.network_runtime.backend, "push", side_effect=AssertionError("No delayed replay")):
            resolved = self.network_runtime.resolve(report["operation_id"])
            self.assertEqual(resolved["code_publication"], "published")
            self.assertEqual(self.network_runtime.recover()["ownership"], "owned")
        self.assertEqual(self.server.push_requests, self.pushes_before + 1)
