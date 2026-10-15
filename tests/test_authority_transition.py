"""Explicit transition consent and remote/local safety."""
from unittest.mock import patch
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from agent_handoff.authority import Authority
from agent_handoff.authority_transition import AuthorityTransition
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.publication import Runtime
from agent_handoff.cli import main
from agent_handoff.paths import PrivateStore
from agent_handoff.secrets.adapter import destination_identity
from test_publication import RepositoryFixture


class TransitionTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.target = Runtime(self.checkout, self.root / "private",
                              Authority(str(self.remote), "refs/heads/next-publication"), self.context)
        self.addCleanup(self.target.close)
        self.transition = AuthorityTransition(self.runtime, self.target)

    def test_absent_target_requires_consent_and_independent_acquisition(self):
        source_head = self.observe()
        index = (self.checkout / ".git/index").read_bytes()
        (self.checkout / "code.txt").write_text("retain editor work\n")
        proposal = self.transition.propose()
        self.assertEqual(proposal["decision"]["category"], "authority_binding")
        self.assertIn(self.target.authority.publication_ref, proposal["decision"]["options"][0]["effect"])
        with patch.object(self.runtime.backend, "push") as source_push, patch.object(self.target.backend, "push") as target_push:
            self.assertEqual(self.transition.apply(proposal["decision"]["decision_id"], "switch")["transition_outcome"], "selected")
            source_push.assert_not_called()
            target_push.assert_not_called()
        self.assertEqual(self.observe(), source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual((self.checkout / "code.txt").read_text(), "retain editor work\n")
        self.assertIsNone(self.target.sessions.current())
        self.assertIsNone(self.target.store.read("secret-binding.json", missing=True))
        with self.assertRaises(RuntimeFailure):
            self.runtime.acquire()
        self.target.initialize(self.base, mainline_ref="refs/heads/main")
        self.assertEqual(self.target.acquire()["ownership"], "owned")

    def test_existing_unbound_target_has_no_prejournal_binding_or_trust_effects(self):
        with Runtime(self.checkout, self.root / "bootstrap", self.target.authority, "b" * 64) as bootstrap:
            bootstrap.initialize(self.base, mainline_ref="refs/heads/main")
        target_head = self.target.backend.observe()
        proposal = self.transition.propose()
        with patch.object(self.target.store, "write", side_effect=AssertionError("Transition cannot bootstrap target records")) as write:
            self.transition.apply(proposal["decision"]["decision_id"], "switch")
            write.assert_not_called()
        self.assertIsNone(self.target.store.read("bindings.json", missing=True))
        self.assertIsNone(self.target.store.read("integrity.json", missing=True))
        self.assertIsNone(self.target.sessions.current())
        with self.assertRaises(RuntimeFailure):
            self.target.acquire()
        self.assertEqual(self.target.initialize()["code_publication"], "not_attempted")
        self.assertEqual(self.target.backend.observe(), target_head)
        self.assertEqual(self.target.acquire()["ownership"], "owned")

    def test_target_changed_or_private_evidence_corrupt_does_not_switch(self):
        proposal = self.transition.propose()
        decision_id = proposal["decision"]["decision_id"]
        name = "authority-decision-" + decision_id + ".json"
        original = self.runtime.store.read(name)
        self.runtime.store.write(name, dict(original, target_identity="f" * 64))
        with patch.object(self.target.backend, "observe") as observe:
            with self.assertRaises(RuntimeFailure):
                self.transition.apply(decision_id, "switch")
            observe.assert_not_called()
        self.runtime.store.write(name, original)
        self.target.store.write("secret-binding.json", {"schema_version": 1,
            "repository_identity": self.target.authority.identity, "destination_identity": destination_identity("local-env", None, None),
            "mode": "local-env", "project": None, "config": None, "upload_authorized": False,
            "manifest": {"schema_version": 1, "keys": {}}})
        with self.assertRaises(RuntimeFailure) as caught:
            self.transition.apply(decision_id, "switch")
        self.assertEqual(caught.exception.status, "decision_required")
        self.assertEqual(self.runtime.selection.read()["repository_identity"], self.authority.identity)
        self.assertEqual(self.runtime.store.read("decision-state-" + decision_id + ".json")["status"], "stale")

    def test_active_source_capability_requires_release(self):
        self.runtime.acquire()
        with self.assertRaises(RuntimeFailure) as caught:
            self.transition.propose()
        self.assertEqual(caught.exception.status, "rejected")
        self.runtime.release()
        proposal = self.transition.propose()
        self.transition.apply(proposal["decision"]["decision_id"], "abort")
        self.runtime.selection.require(self.authority.identity)

    def test_existing_target_head_change_invalidates_review(self):
        with Runtime(self.checkout, self.root / "private", self.target.authority, "b" * 64) as bootstrap:
            bootstrap.initialize(self.base, mainline_ref="refs/heads/main")
        proposal = self.transition.propose()
        head = self.target.backend.observe()
        changed, _ = self.target.backend.prepare(head, {"code.txt": b"target progressed\n"}, "External target progress\n")
        self.assertEqual(self.target.backend.push(changed, head), "published")
        with self.assertRaises(RuntimeFailure) as caught:
            self.transition.apply(proposal["decision"]["decision_id"], "switch")
        self.assertEqual(caught.exception.status, "decision_required")
        self.runtime.selection.require(self.authority.identity)
        self.assertEqual(self.target.backend.observe(), changed)

    def test_cli_propose_and_apply_explicit_target(self):
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
        common = ["--checkout", str(self.checkout), "--private-root", str(self.root / "private"),
                  "--context-file", str(self.root / "private/context.json"), "--authority", str(self.remote),
                  "--publication-ref", self.authority.publication_ref, "--target-authority", str(self.remote),
                  "--target-publication-ref", self.target.authority.publication_ref]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["authority", "propose", *common]), 3)
        proposal = json.loads(output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["authority", "apply", "--decision-id", proposal["decision"]["decision_id"],
                                   "--option-id", "switch", *common]), 0)
        self.assertEqual(json.loads(output.getvalue())["selected_authority_identity"], self.target.authority.identity)

    def test_process_crash_after_selection_requires_linked_lifecycle_recovery(self):
        self._process_crash_recovery("after_selection_commit", "pending")

    def test_process_crash_after_lifecycle_write_resolves_idempotently(self):
        self._process_crash_recovery("after_transition_finalize", "applied")

    def _process_crash_recovery(self, boundary, initial_status):
        proposal = self.transition.propose()
        decision_id = proposal["decision"]["decision_id"]
        code = """
import os, sys
from pathlib import Path
from agent_handoff.authority import Authority
from agent_handoff.publication import Runtime
from agent_handoff.authority_transition import AuthorityTransition
checkout, private, remote, decision, boundary = sys.argv[1:]
with Runtime(Path(checkout), Path(private), Authority(remote, 'refs/heads/publication'), 'a' * 64) as source:
    with Runtime(Path(checkout), Path(private), Authority(remote, 'refs/heads/next-publication'), 'a' * 64) as target:
        source.fault = lambda point: os._exit(77) if point == boundary else None
        AuthorityTransition(source, target).apply(decision, 'switch')
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.checkout), str(self.root / "private"),
                                str(self.remote), decision_id, boundary], capture_output=True, timeout=15,
                               env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")))
        self.assertEqual(child.returncode, 77)
        lifecycle_name = "decision-state-" + decision_id + ".json"
        self.assertEqual(self.runtime.store.read(lifecycle_name)["status"], initial_status)
        with self.assertRaises(RuntimeFailure) as caught:
            self.target.acquire()
        self.assertEqual(caught.exception.status, "unknown")
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
        args = ["authority", "resolve", "--checkout", str(self.checkout), "--private-root", str(self.root / "private"),
                "--context-file", str(self.root / "private/context.json")]
        private_name = "authority-decision-" + decision_id + ".json"
        original = self.runtime.store.read(private_name)
        self.runtime.store.write(private_name, dict(original, target_identity="f" * 64))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 2)
        self.assertEqual(self.runtime.store.read(lifecycle_name)["status"], initial_status)
        with self.assertRaises(RuntimeFailure):
            self.target.acquire()
        self.runtime.store.write(private_name, original)
        for _ in range(2):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch("agent_handoff.publication.Runtime") as runtime:
                self.assertEqual(main(args), 0)
                runtime.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["transition_outcome"], "selected")
        self.assertEqual(self.runtime.store.read(lifecycle_name)["status"], "applied")
        self.assertIsNone(self.target.sessions.current())
