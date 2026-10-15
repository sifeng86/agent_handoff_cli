"""Shared selection barrier blocks cross-authority runtime mutations."""
from unittest.mock import patch
import contextlib
import io
import json

from agent_handoff.authority import Authority
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.publication import Runtime
from agent_handoff.secrets.service import SecretService
from agent_handoff.cli import main
from agent_handoff.paths import PrivateStore
from agent_handoff.authority_transition import AuthorityTransition
from test_publication import RepositoryFixture


class RuntimeSelectionTests(RepositoryFixture):
    def test_cli_resolve_pending_transition_without_backend_or_capability(self):
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
        selection = self.runtime.selection
        with Runtime(self.checkout, self.root / "private",
                     Authority(str(self.remote), "refs/heads/next-publication"), self.context) as target:
            transition = AuthorityTransition(self.runtime, target)
            proposal = transition.propose()
            decision_id = proposal["decision"]["decision_id"]
            self.runtime.fault = lambda _: (_ for _ in ()).throw(SystemExit(77))
            with self.assertRaises(SystemExit):
                transition.apply(decision_id, "switch")
            self.runtime.fault = lambda _: None
        args = ["authority", "resolve", "--checkout", str(self.checkout), "--private-root", str(self.root / "private"),
                "--context-file", str(self.root / "private/context.json")]
        for _ in range(2):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch("agent_handoff.publication.Runtime") as runtime:
                self.assertEqual(main(args), 0)
                runtime.assert_not_called()
            report = json.loads(output.getvalue())
            self.assertEqual(report["transition_outcome"], "not_selected")
            self.assertEqual(report["selected_authority_identity"], self.authority.identity)
            self.assertEqual(report["code_publication"], "not_attempted")
        self.assertIsNone(self.runtime.sessions.current())
        self.assertEqual(self.runtime.store.read("decision-state-" + decision_id + ".json")["status"], "stale")
        self.assertEqual(self.runtime.acquire()["ownership"], "owned")

    def test_changed_cli_authority_cannot_initialize_or_acquire(self):
        other = Authority(str(self.remote), "refs/heads/other-publication")
        with Runtime(self.checkout, self.root / "private", other, self.context) as runtime:
            with patch.object(runtime.backend, "observe") as observe:
                for action in (lambda: runtime.initialize(self.base, mainline_ref="refs/heads/main"),
                               runtime.acquire, runtime.recover, runtime.release):
                    with self.assertRaises(RuntimeFailure) as caught:
                        action()
                    self.assertEqual(caught.exception.status, "decision_required")
                observe.assert_not_called()
        self.assertEqual(self.runtime.selection.read()["repository_identity"], self.authority.identity)
        self.assertEqual(self.git(self.remote, "branch", "--list", "other-publication"), "")

    def test_pending_transition_blocks_runtime_before_remote_observation(self):
        selection = self.runtime.selection
        def crash(_):
            raise SystemExit(77)
        with selection.locked():
            with self.assertRaises(SystemExit):
                selection.switch(self.authority.identity, "d" * 64, "e" * 32, fault=crash)
        with patch.object(self.runtime.backend, "observe") as observe:
            with self.assertRaises(RuntimeFailure) as caught:
                self.runtime.acquire()
            self.assertEqual(caught.exception.status, "unknown")
            observe.assert_not_called()
            service = SecretService(self.runtime, {"schema_version": 1, "keys": {}})
            with self.assertRaises(RuntimeFailure) as secret_error:
                service.propose_binding("local-env")
            self.assertEqual(secret_error.exception.status, "unknown")
            observe.assert_not_called()
        with selection.locked():
            self.assertEqual(selection.resolve()["outcome"], "not_selected")
        self.assertEqual(self.runtime.acquire()["ownership"], "owned")
