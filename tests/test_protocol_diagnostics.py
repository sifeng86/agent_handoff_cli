"""Future protocols remain diagnosable without authorizing interpretation or writes."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json

from agent_handoff.cli import main
from agent_handoff.paths import PrivateStore
from agent_handoff.public_results import validate_public_result
from test_publication import RepositoryFixture
from test_record_schemas import validator


class ProtocolDiagnosticsTests(RepositoryFixture):
    def test_A60_incompatible_protocol_readonly_diagnostic_and_mutation_refusal(self):
        self.runtime.acquire()
        before = self.observe()
        protocol = self.runtime.backend.records(before)["protocol"]
        future, _ = self.runtime.backend.prepare(before, {
            ".agent/protocol.json": json.dumps(dict(protocol, protocol_version="99.0")).encode()
        }, "External future protocol\n")
        self.assertEqual(self.runtime.backend.push(future, before), "published")
        (self.checkout / "code.txt").write_bytes(b"retained local edit\n")
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        source_index = (self.checkout / ".git/index").read_bytes()
        trust = self.runtime.store.read("integrity.json")
        context_root = self.root / "cli-context"
        with PrivateStore(context_root, self.checkout) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
        options = ["--authority", str(self.remote), "--publication-ref", self.authority.publication_ref,
                   "--private-root", str(self.root / "private"), "--checkout", str(self.checkout),
                   "--context-file", str(context_root / "context.json")]
        for command, family in (("inspect", "inspect"), ("release", "git")):
            with self.subTest(command=command):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main([*options, command])
                self.assertEqual(code, 8)
                report = json.loads(stdout.getvalue())
                self.assertEqual(report["status"], "unsupported")
                self.assertIn("protocol", report["next_action"].lower())
                self.assertEqual(stderr.getvalue(), "")
                self.assertNotIn(self.context, stdout.getvalue())
                validate_public_result(report, expected_family=family)
                validator("result").validate(report)
                self.assertEqual(self.observe(), future)
                self.assertEqual(self.runtime.store.read("integrity.json"), trust)
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
                self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index)
                self.assertEqual((self.checkout / "code.txt").read_bytes(), b"retained local edit\n")
