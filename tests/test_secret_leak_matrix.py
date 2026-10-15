"""Focused A54 checks across CLI, public results, Git metadata, and diagnostics."""
import contextlib
import io
import json
import subprocess
import traceback
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.git_backend import GitBackend
from agent_handoff.paths import PrivateStore
from agent_handoff.public_results import validate_public_result
from agent_handoff.secrets.service import SecretService
from test_secret_service import VerifiedFake
from test_publication import RepositoryFixture


class SecretLeakMatrixTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.runtime.acquire()
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"schema_version": 1, "keys": {
            "A": {"classification": "shared", "required": True},
            "LOCAL": {"classification": "local-only", "required": False}}}))
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("cli-context.json", {"schema_version": 1, "context": self.context})
        self.common = ["--checkout", str(self.checkout), "--private-root", str(self.root / "private"),
            "--authority", str(self.remote), "--publication-ref", self.authority.publication_ref,
            "--context-file", str(self.root / "private/cli-context.json"), "--manifest-file", str(self.manifest)]

    def invoke_capture(self, *args):
        stdout, stderr, uncaught = io.StringIO(), io.StringIO(), ""
        code = None
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main([*args, *self.common])
        except BaseException:
            uncaught = traceback.format_exc()
        rendered = stdout.getvalue()
        value = json.loads(rendered) if rendered else None
        if value is not None:
            validate_public_result(value)
        return {"code": code, "stdout": rendered, "stderr": stderr.getvalue(),
                "traceback": uncaught, "result": value}

    def approve_mode(self, mode):
        destination = ["--doppler-project", "fixture-project", "--doppler-config", "dev_fixture"] if mode == "doppler" else []
        proposal = self.invoke_capture("secrets", "configure", "--secrets-mode", mode, *destination)
        self.assertEqual(proposal["code"], 3)
        decision_id = proposal["result"]["decision"]["decision_id"]
        args = ["decision", "apply", "--decision-id", decision_id,
                "--option-id", "approve-secret-binding"]
        args += destination
        applied = self.invoke_capture(*args)
        self.assertEqual(applied["code"], 0)
        return applied

    @staticmethod
    def assert_channels_clean(test, channels, sentinel):
        for name, value in channels.items():
            with test.subTest(channel=name):
                test.assertNotIn(sentinel, value)

    @staticmethod
    def diagnostic_fields(value):
        found = []
        diagnostic_names = {"error", "errors", "diagnostic", "exception", "message", "traceback",
                            "provider_error", "raw_error", "stderr", "cause", "reason"}
        def visit(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if isinstance(key, str) and key.casefold() in diagnostic_names:
                        found.append(json.dumps(child, ensure_ascii=False))
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)
        visit(value)
        return "\n".join(found)

    def test_local_success_keeps_secret_out_of_public_result_git_and_diagnostics(self):
        marker = "A54_LOCAL_SENTINEL_8d72e1"
        self.approve_mode("local-env")
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("patch.json", {"schema_version": 1, "expected": {},
                "changes": {"A": marker, "LOCAL": "local-only-value"}, "approved_deletions": []})
        head_before = self.runtime.backend.remote_head()
        applied = self.invoke_capture("secrets", "apply", "--patch-file", str(self.root / "private/patch.json"))
        checked = self.invoke_capture("secrets", "verify")
        self.assertEqual(applied["code"], 0)
        self.assertEqual(checked["code"], 0)
        self.assertFalse(applied["result"]["secrets_shared"])
        self.assertEqual(self.runtime.backend.remote_head(), head_before)

        operation_id = applied["result"]["secret_operation_id"]
        journal = self.runtime.store.read("secret-operation-" + operation_id + ".json")
        self.assertEqual(journal["result"], "local_only")
        # This is an authorized private value store; only its diagnostic fields are disclosure channels.
        self.assertEqual(journal["evidence"]["target_env"]["A"], marker)
        public_records = self.runtime.backend.records(head_before)
        history = self.runtime.backend.run("log", "--all", "--format=%B", "--name-only")
        self.assertEqual(history.returncode, 0)
        public_history = json.dumps(public_records, sort_keys=True) + history.stdout.decode("utf-8")
        channels = {
            "apply stdout": applied["stdout"], "apply stderr": applied["stderr"], "apply traceback": applied["traceback"],
            "verify stdout": checked["stdout"], "verify stderr": checked["stderr"], "verify traceback": checked["traceback"],
            "public result objects": json.dumps([applied["result"], checked["result"]]),
            "public Git records and history": public_history,
            "private journal diagnostics": self.diagnostic_fields(journal),
        }
        self.assert_channels_clean(self, channels, marker)

    def test_corrupt_decision_invalid_patch_and_unexpected_exception_are_closed(self):
        marker = "A54_PRIVATE_FAILURE_SENTINEL_b91f"
        proposal = self.invoke_capture("secrets", "configure", "--secrets-mode", "local-env")
        decision_id = proposal["result"]["decision"]["decision_id"]
        private_name = "secret-decision-" + decision_id + ".json"
        private_decision = self.runtime.store.read(private_name)
        self.runtime.store.write(private_name, dict(private_decision, extra=marker))
        rejected_decision = self.invoke_capture("decision", "apply", "--decision-id", decision_id,
                                                "--option-id", "approve-secret-binding")
        self.runtime.store.write(private_name, private_decision)
        self.assertEqual(self.invoke_capture("decision", "apply", "--decision-id", decision_id,
            "--option-id", "approve-secret-binding")["code"], 0)
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("bad-patch.json", {"schema_version": 1, "expected": {},
                "changes": {"UNDECLARED": marker}, "approved_deletions": []})
        rejected_patch = self.invoke_capture("secrets", "apply", "--patch-file", str(self.root / "private/bad-patch.json"))
        with patch("agent_handoff.cli.secret_service", side_effect=RuntimeError(marker)):
            unexpected = self.invoke_capture("secrets", "inspect")

        for observed in (rejected_decision, rejected_patch, unexpected):
            self.assertIsNotNone(observed["result"])
            self.assert_channels_clean(self, {"stdout": observed["stdout"], "stderr": observed["stderr"],
                "traceback": observed["traceback"], "result": json.dumps(observed["result"])}, marker)
        self.assertEqual(unexpected["stderr"], "")
        self.assertEqual(unexpected["traceback"], "")

    def test_doppler_private_failures_never_reach_cli_or_public_json(self):
        marker = "A54_PROVIDER_RAW_SENTINEL_51ca"
        self.approve_mode("doppler")
        version = subprocess.CompletedProcess([], 0, b"Doppler CLI 3.77.0", b"")
        base = {"DOPPLER_PROJECT": "fixture-project", "DOPPLER_CONFIG": "dev_fixture"}
        cases = [
            ("missing-token", subprocess.CompletedProcess([], 1, b"", b'{"error":"you must provide a token"}'),
             "authentication_required"),
            ("malformed", subprocess.CompletedProcess([], 0, b'{"A": ' + marker.encode(), b""), None),
            ("oversize", subprocess.CompletedProcess([], 0, b"x" * (8 * 1024 * 1024 + 1) + marker.encode(), b""), None),
            ("extra-key", subprocess.CompletedProcess([], 0, json.dumps({**base, "A": marker, "EXTRA": marker}).encode(), b""), None),
            ("restricted", subprocess.CompletedProcess([], 0, json.dumps({**base, "A": {"computed": None,
                "private_detail": marker}}).encode(), b""), None),
            ("wrong-destination", subprocess.CompletedProcess([], 0, json.dumps({"DOPPLER_PROJECT": "other",
                "DOPPLER_CONFIG": "dev_fixture", "A": marker}).encode(), b""), None),
            ("permission-error", subprocess.CompletedProcess([], 1, marker.encode(), ("permission denied " + marker).encode()), None),
            ("network-error", subprocess.CompletedProcess([], 1, marker.encode(), ("network unavailable " + marker).encode()), None),
            ("timeout", subprocess.TimeoutExpired("doppler", 15, output=marker.encode(), stderr=marker.encode()), None),
        ]
        for label, response, expected_failure in cases:
            with self.subTest(case=label), patch("agent_handoff.secrets.doppler.subprocess.run",
                    side_effect=[version, response]):
                before = self.runtime.backend.remote_head()
                observed = self.invoke_capture("secrets", "inspect")
                self.assertIsNotNone(observed["result"])
                self.assertIn(observed["code"], (0, 6, 7, 8))
                self.assert_channels_clean(self, {"stdout": observed["stdout"], "stderr": observed["stderr"],
                    "traceback": observed["traceback"], "public JSON": json.dumps(observed["result"])}, marker)
                if expected_failure is not None:
                    self.assertEqual(observed["result"]["shared_read_failure"], expected_failure)
                self.assertEqual(self.runtime.backend.remote_head(), before)


class SecretServiceLeakScenarios(RepositoryFixture):
    """Real local runtime/journal/Git with the deterministic VerifiedFake adapter."""
    def setUp(self):
        super().setUp()
        self.runtime.acquire()
        self.adapter = VerifiedFake(self.runtime.sessions.current()["session_id"])
        self.manifest = {"schema_version": 1, "keys": {
            "A": {"classification": "shared", "required": True},
            "B": {"classification": "shared", "required": False},
            "LOCAL": {"classification": "local-only", "required": False}}}
        self.service = SecretService(self.runtime, self.manifest, self.adapter)
        self.common = ["--checkout", str(self.checkout), "--private-root", str(self.root / "private"),
            "--authority", str(self.remote), "--publication-ref", self.authority.publication_ref,
            "--context-file", str(self.root / "private/cli-context.json"), "--manifest-file", str(self.root / "manifest.json")]
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest))
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("cli-context.json", {"schema_version": 1, "context": self.context})

    def configure(self, upload=True):
        proposal = self.service.propose_binding("doppler", project=self.adapter.project,
            config=self.adapter.config, upload_authorized=upload)
        return self.service.apply_decision(proposal["decision"]["decision_id"], "approve-secret-binding")

    def local(self, values):
        path = self.checkout / ".env"
        path.write_text("# keep\n" + "".join(key + "=" + json.dumps(value) + "\n" for key, value in values.items()))
        path.chmod(0o600)

    def invoke(self, *args):
        stdout, stderr, uncaught = io.StringIO(), io.StringIO(), ""
        code = None
        try:
            with patch("agent_handoff.cli.secret_service", return_value=self.service), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main([*args, *self.common])
        except BaseException:
            uncaught = traceback.format_exc()
        rendered = stdout.getvalue()
        result_value = json.loads(rendered) if rendered else None
        if result_value is not None:
            validate_public_result(result_value)
        return {"code": code, "stdout": rendered, "stderr": stderr.getvalue(),
                "traceback": uncaught, "result": result_value}

    def assert_private_values_absent(self, observed, *markers):
        channels = {"stdout": observed["stdout"], "stderr": observed["stderr"],
                    "traceback": observed["traceback"], "public result": json.dumps(observed["result"])}
        self.assertEqual(observed["stderr"], "")
        self.assertEqual(observed["traceback"], "")
        for marker in markers:
            SecretLeakMatrixTests.assert_channels_clean(self, channels, marker)

    def assert_public_history_clean(self, *markers):
        observer = GitBackend(self.root / "privacy-observer.git", str(self.remote),
                              self.authority.publication_ref)
        head = observer.observe()
        self.assertIsNotNone(head)
        observer.records(head)
        # Scan every reachable commit/tree/blob, including ancestor file contents.
        objects = observer.text("rev-list", "--objects", head).splitlines()
        object_ids = [row.split(" ", 1)[0] for row in objects]
        history = observer.run("cat-file", "--batch",
                               data=("\n".join(object_ids) + "\n").encode("ascii"))
        self.assertEqual(history.returncode, 0)
        for marker in markers:
            self.assertNotIn(marker.encode("utf-8"), history.stdout)

    def assert_journal_diagnostics_clean(self, operation_id, *markers):
        journal = self.service.journal.read(operation_id)
        diagnostics = SecretLeakMatrixTests.diagnostic_fields(journal)
        for marker in markers:
            self.assertNotIn(marker, diagnostics)
        return journal

    def test_conflict_abort_and_stale_decision_leave_values_private(self):
        local_secret, remote_secret = "A54_CONFLICT_LOCAL_31ef", "A54_CONFLICT_REMOTE_7a82"
        self.configure(upload=True)
        self.local({"A": local_secret})
        self.adapter.values = {"A": remote_secret}
        local_before = (self.checkout / ".env").read_bytes()
        baseline_before = self.service._baseline(self.service._binding()).read()
        proposal = self.invoke("secrets", "reconcile")
        self.assertEqual(proposal["code"], 3)
        self.assertEqual(proposal["result"]["status"], "decision_required")
        self.assert_private_values_absent(proposal, local_secret, remote_secret)
        self.assertEqual(self.adapter.write_calls, [])
        self.assertEqual((self.checkout / ".env").read_bytes(), local_before)
        self.assertEqual(self.service._baseline(self.service._binding()).read(), baseline_before)
        head = self.runtime.backend.remote_head()
        aborted = self.invoke("decision", "apply", "--decision-id", proposal["result"]["decision"]["decision_id"],
                              "--option-id", "abort")
        self.assertEqual(aborted["code"], 0)
        self.assert_private_values_absent(aborted, local_secret, remote_secret)
        self.assertEqual(self.adapter.write_calls, [])
        self.assertEqual((self.checkout / ".env").read_bytes(), local_before)
        self.assertEqual(self.service._baseline(self.service._binding()).read(), baseline_before)
        self.assertEqual(self.runtime.backend.remote_head(), head)
        self.assert_public_history_clean(local_secret, remote_secret)

        # A second independent fixture decision becomes stale after the remote value changes.
        self.adapter.values = {"A": "A54_STALE_REMOTE_13a0"}
        stale_local = "A54_STALE_LOCAL_24b1"
        self.local({"A": stale_local})
        stale = self.invoke("secrets", "reconcile")
        self.assertEqual(stale["code"], 3)
        self.adapter.values["A"] = "A54_STALE_CHANGED_35c2"
        writes = list(self.adapter.write_calls)
        baseline_before = self.service._baseline(self.service._binding()).read()
        rejected = self.invoke("decision", "apply", "--decision-id", stale["result"]["decision"]["decision_id"],
                               "--option-id", "use-remote")
        self.assertEqual(rejected["code"], 3)
        self.assert_private_values_absent(rejected, stale_local, "A54_STALE_REMOTE_13a0", "A54_STALE_CHANGED_35c2")
        self.assertEqual(self.adapter.write_calls, writes)
        self.assertEqual(self.service._baseline(self.service._binding()).read(), baseline_before)
        lifecycle = self.runtime.store.read("decision-state-" + stale["result"]["decision"]["decision_id"] + ".json")
        self.assertEqual(lifecycle["status"], "stale")
        self.assert_public_history_clean(local_secret, remote_secret, stale_local,
                                        "A54_STALE_REMOTE_13a0", "A54_STALE_CHANGED_35c2")

    def test_takeover_fences_suspended_secret_writer_without_disclosing_material(self):
        marker = "A54_TAKEOVER_LOCAL_SECRET_41c7"
        self.configure(upload=True)
        self.local({"A": marker})
        proposal = self.invoke("secrets", "reconcile")
        self.assertEqual(proposal["code"], 0)
        before = self.runtime.backend.remote_head()
        def takeover(point):
            if point == "before_secret_key_write":
                with self.new_runtime("leak-matrix-takeover", "b" * 64) as other:
                    other.initialize()
                    choice = other.acquire()
                    other.apply_decision(choice["decision"]["decision_id"], "takeover")
                    self.adapter.owner = other.sessions.current()["session_id"]
        self.runtime.fault = takeover
        rejected = self.invoke("secrets", "apply", "--plan-id", proposal["result"]["plan_id"])
        self.assertEqual(rejected["result"]["status"], "rejected")
        self.assert_private_values_absent(rejected, marker)
        self.assertEqual(self.adapter.write_keys, [])
        self.assertEqual(self.adapter.values, {})
        self.assertIsNone(self.service._baseline(self.service._binding()).read())
        self.assertNotEqual(self.runtime.backend.remote_head(), before)
        self.assert_public_history_clean(marker)

    def test_partial_unknown_and_terminal_resolution_never_replay_or_leak(self):
        marker_a, marker_b = "A54_PARTIAL_A_d4e2", "A54_PARTIAL_B_9f30"
        self.configure(upload=True)
        self.local({"A": "A54_BASE_A", "B": "A54_BASE_B"})
        self.adapter.values = {"A": "A54_BASE_A", "B": "A54_BASE_B"}
        initial_plan = self.invoke("secrets", "reconcile")
        self.assertEqual(initial_plan["code"], 0)
        initial = self.invoke("secrets", "apply", "--plan-id", initial_plan["result"]["plan_id"])
        self.assertEqual(initial["code"], 0)
        baseline = self.service._baseline(self.service._binding()).read()
        self.local({"A": marker_a, "B": marker_b})
        planned = self.invoke("secrets", "reconcile")
        self.assertEqual(planned["code"], 0)
        self.adapter.timeout_key = "B"
        pending = self.invoke("secrets", "apply", "--plan-id", planned["result"]["plan_id"])
        self.assertEqual(pending["result"]["status"], "unknown")
        self.assertEqual(pending["result"]["secrets_shared"], "partial")
        self.assert_private_values_absent(pending, marker_a, marker_b)
        operation_id = pending["result"]["secret_operation_id"]
        journal = self.assert_journal_diagnostics_clean(operation_id, marker_a, marker_b)
        self.assertEqual(journal["evidence"]["target"]["A"], marker_a)
        self.assertEqual(self.service._baseline(self.service._binding()).read(), baseline)
        writes = list(self.adapter.write_calls)
        unresolved = self.invoke("secrets", "resolve", "--operation-id", operation_id)
        self.assertEqual(unresolved["result"]["status"], "unknown")
        self.assert_private_values_absent(unresolved, marker_a, marker_b)
        self.assertEqual(self.adapter.write_calls, writes)
        self.assertEqual(self.service._baseline(self.service._binding()).read(), baseline)

        pending_record = self.service.journal.read(operation_id)
        self.adapter.operations[pending_record["keys"]["B"]["operation_id"]] = "rejected"
        terminal = self.invoke("secrets", "resolve", "--operation-id", operation_id)
        self.assertEqual(terminal["result"]["status"], "completed")
        self.assertEqual(terminal["result"]["secrets_shared"], "partial")
        self.assertTrue(terminal["result"]["baseline_retained"])
        self.assert_private_values_absent(terminal, marker_a, marker_b)
        self.assertFalse(self.service.journal.pending())
        self.assertEqual(self.adapter.write_calls, writes)
        self.assertEqual(self.service._baseline(self.service._binding()).read(), baseline)
        self.assert_public_history_clean(marker_a, marker_b)


if __name__ == "__main__":
    import unittest
    unittest.main()
