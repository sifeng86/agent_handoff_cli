import json
import multiprocessing
import os
import subprocess
from unittest.mock import patch

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.secrets.adapter import Adapter, AuthenticationRequired, Capabilities
from agent_handoff.secrets.doppler import Doppler
from agent_handoff.secrets.service import SecretService
from test_publication import RepositoryFixture
from test_record_schemas import validator


class VerifiedFake(Adapter):
    """Deterministic contract fixture; never evidence of live Doppler fencing."""
    capabilities = Capabilities(True, True, True, True)

    def __init__(self, owner):
        super().__init__("fixture-project", "dev_fixture")
        self.owner = owner
        self.values = {}
        self.operations = {}
        self.write_keys = []
        self.timeout_key = None
        self.reject_key = None
        self.write_calls = []

    def read(self, scope):
        return {key: self.values[key] for key in scope if key in self.values}

    def lease(self, **facts):
        if facts["session_id"] != self.owner:
            raise RuntimeFailure("Fenced fixture lease.", "rejected", 4)
        return facts["session_id"]

    def write_key(self, key, value, *, expected, lease, operation_id):
        self.write_calls.append(key)
        if lease != self.owner or self.values.get(key) != expected or key == self.reject_key:
            self.operations[operation_id] = "rejected"
            raise RuntimeFailure("Fenced or conflicting fixture write.", "rejected", 4)
        if key == self.timeout_key:
            self.operations[operation_id] = "unknown"
            raise RuntimeFailure("Fixture timeout.", "unknown", 5)
        if value is None:
            self.values.pop(key, None)
        else:
            self.values[key] = value
        self.operations[operation_id] = "published"
        self.write_keys.append(key)

    def operation_status(self, operation_id):
        return self.operations.get(operation_id, "unknown")


class BasicFake(VerifiedFake):
    """Basic confirmation fixture; provider receipts can disappear on restart."""
    basic_writes = True


class SecretServiceTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.runtime.acquire()
        self.adapter = VerifiedFake(self.runtime.sessions.current()["session_id"])
        self.manifest = {"schema_version": 1, "keys": {"A": {"classification": "shared", "required": True},
            "B": {"classification": "shared", "required": False}, "LOCAL": {"classification": "local-only", "required": False}}}
        self.service = SecretService(self.runtime, self.manifest, self.adapter)

    def configure(self, *, mode="doppler", upload=False):
        proposal = self.service.propose_binding(mode, project=self.adapter.project if mode == "doppler" else None,
            config=self.adapter.config if mode == "doppler" else None, upload_authorized=upload)
        return self.service.apply_decision(proposal["decision"]["decision_id"], "approve-secret-binding")

    def test_corrupt_private_binding_rejected_before_provider_read(self):
        self.configure(mode="local-env")
        original = self.runtime.store.read("secret-binding.json")
        schema = validator("secret-binding")
        schema.validate(original)
        for changes in [{"schema_version": 2}, {"schema_version": True}, {"extra_grant": True},
                        {"upload_authorized": True}, {"destination_identity": "public-label"}]:
            with self.subTest(changes=changes):
                self.runtime.store.write("secret-binding.json", dict(original, **changes))
                self.assertFalse(schema.is_valid(dict(original, **changes)))
                with patch.object(self.adapter, "read", side_effect=AssertionError("Must not read provider")):
                    with self.assertRaises(RuntimeFailure):
                        self.service.inspect()
        self.runtime.store.write("secret-binding.json", original)

    def test_corrupt_nested_operation_evidence_stops_before_external_resolution(self):
        self.configure()
        self.adapter.values = {"A": "private-remote-fixture"}
        def fail(point):
            if point == "before_baseline_write":
                raise RuntimeError("Private fault")
        self.runtime.fault = fail
        pending = self.sync()
        self.assertEqual(pending["status"], "unknown")
        operation_id = pending["secret_operation_id"]
        original = self.service.journal.read(operation_id)
        validator("secret-journal").validate(original)
        before = (self.checkout / ".env").read_bytes()
        for field, value in (("observed_head", "public-label"), ("binding", {}),
                             ("local_values", {"A": "different-private-fixture"}),
                             ("target", {"UNKNOWN": "different-private-fixture"})):
            corrupt = json.loads(json.dumps(original))
            corrupt["evidence"][field] = value
            self.runtime.store.write("secret-operation-" + operation_id + ".json", corrupt)
            with self.subTest(field=field), patch.object(self.adapter, "read") as read, patch.object(self.runtime.backend, "observe") as observe:
                with self.assertRaises(RuntimeFailure):
                    self.service.resolve(operation_id)
                read.assert_not_called()
                observe.assert_not_called()
            self.assertEqual((self.checkout / ".env").read_bytes(), before)
            self.assertIsNone(self.service._baseline(self.service._binding()).read())
        self.runtime.store.write("secret-operation-" + operation_id + ".json", original)
        self.runtime.fault = lambda point: None
        self.assertTrue(self.service.resolve(operation_id)["secrets_shared"])

    def test_corrupt_plan_cannot_replace_source_derived_target_or_trigger_io(self):
        self.configure(upload=True)
        self.local({"A": "same-private-fixture"})
        self.adapter.values = {"A": "same-private-fixture"}
        proposal = self.service.reconcile()
        plan_id = proposal["plan_id"]
        original = self.service._read_plan(plan_id)
        validator("secret-plan").validate(original)
        before = (self.checkout / ".env").read_bytes()
        for changes in ({"schema_version": 2}, {"plan_id": "f" * 32}, {"extra": True},
                        {"target": {"A": "injected-private-fixture"}}, {"conflicts": {"A": "secret_deletion"}},
                        {"decision_id": "f" * 32}):
            corrupt = dict(original, **changes)
            self.runtime.store.write("secret-plan-" + plan_id + ".json", corrupt)
            with self.subTest(fields=list(changes)), patch.object(self.adapter, "read") as read, patch.object(self.adapter, "write_key") as write:
                with self.assertRaises(RuntimeFailure):
                    self.service.apply(plan_id)
                read.assert_not_called()
                write.assert_not_called()
            self.assertEqual((self.checkout / ".env").read_bytes(), before)
            self.assertIsNone(self.service._baseline(self.service._binding()).read())
        self.runtime.store.write("secret-plan-" + plan_id + ".json", original)
        self.assertTrue(self.service.apply(plan_id)["secrets_shared"])

    def test_recovery_checks_original_plan_before_provider_or_baseline_update(self):
        self.configure()
        self.adapter.values = {"A": "private-remote-fixture"}
        def fail(point):
            if point == "before_baseline_write":
                raise RuntimeError("Private fault")
        self.runtime.fault = fail
        pending = self.sync()
        operation_id = pending["secret_operation_id"]
        evidence = self.service.journal.read(operation_id)["evidence"]
        plan_id = evidence["plan_id"]
        original = self.service._read_plan(plan_id)
        self.runtime.store.write("secret-plan-" + plan_id + ".json", dict(original, plan_id="f" * 32))
        with patch.object(self.adapter, "read") as read:
            with self.assertRaises(RuntimeFailure):
                self.service.resolve(operation_id)
            read.assert_not_called()
        self.assertIsNone(self.service._baseline(self.service._binding()).read())
        self.runtime.store.write("secret-plan-" + plan_id + ".json", original)
        self.runtime.fault = lambda point: None
        self.assertTrue(self.service.resolve(operation_id)["secrets_shared"])

    def test_private_decision_corruption_rejected_before_external_observation(self):
        self.configure()
        proposal = self.service.propose_binding("doppler", project=self.adapter.project, config=self.adapter.config)
        decision_id = proposal["decision"]["decision_id"]
        name = "secret-decision-" + decision_id + ".json"
        original = self.runtime.store.read(name)
        validator("secret-decision").validate(original)
        binding = self.runtime.store.read("secret-binding.json")
        corruptions = [dict(original, kind="takeover"), dict(original, schema_version=2), dict(original, extra="private-sentinel"),
                       dict(original, decision_id="f" * 32), dict(original, candidate={}), dict(original, previous_binding={}),
                       {key: value for key, value in original.items() if key != "decision_id"}]
        for corrupt in corruptions:
            self.runtime.store.write(name, corrupt)
            with self.subTest(fields=list(corrupt)), patch.object(self.runtime.backend, "observe") as observe, patch.object(self.adapter, "read") as read:
                with self.assertRaises(RuntimeFailure) as caught:
                    self.service.apply_decision(decision_id, "approve-secret-binding")
                self.assertNotIn("private-sentinel", str(caught.exception))
                observe.assert_not_called()
                read.assert_not_called()
            self.assertEqual(self.runtime.store.read("secret-binding.json"), binding)
            self.assertEqual(self.runtime.decisions.require_pending(decision_id)["status"], "pending")
        public_name = "decision-" + decision_id + ".json"
        public = self.runtime.store.read(public_name)
        self.runtime.store.write(name, original)
        self.runtime.store.write(public_name, dict(public, category="takeover"))
        with patch.object(self.runtime.backend, "observe") as observe:
            with self.assertRaises(RuntimeFailure):
                self.service.apply_decision(decision_id, "approve-secret-binding")
            observe.assert_not_called()
        self.runtime.store.write(public_name, public)
        self.assertEqual(self.service.apply_decision(decision_id, "approve-secret-binding")["status"], "completed")

    def test_reconcile_private_decision_kind_cannot_route_to_binding_executor(self):
        self.configure()
        self.local({"A": "private-left"})
        self.adapter.values = {"A": "private-right"}
        proposal = self.service.reconcile()
        decision_id = proposal["decision"]["decision_id"]
        name = "secret-decision-" + decision_id + ".json"
        original = self.runtime.store.read(name)
        validator("secret-decision").validate(original)
        swapped = {"schema_version": 1, "decision_id": decision_id, "kind": "binding",
                   "candidate": self.service._binding(), "previous_binding": self.service._binding()}
        validator("secret-decision").validate(swapped)
        self.runtime.store.write(name, swapped)
        with patch.object(self.runtime.backend, "observe") as observe, patch.object(self.adapter, "read") as read:
            with self.assertRaises(RuntimeFailure):
                self.service.apply_decision(decision_id, "use-remote")
            observe.assert_not_called()
            read.assert_not_called()
        self.assertEqual(self.service.environment.read().values["A"], "private-left")
        self.runtime.store.write(name, original)
        self.assertTrue(self.service.apply_decision(decision_id, "use-remote")["secrets_shared"])

    def test_invalid_local_patch_never_changes_material(self):
        self.configure(mode="local-env")
        self.local({"A": "retain"})
        patches = [None, {"schema_version": 1, "expected": {"A": "retain"}, "changes": {"A": 123}, "approved_deletions": []},
                   {"schema_version": 1, "expected": {"A": "retain"}, "changes": {"A": "new"}, "approved_deletions": ["A"]},
                   {"schema_version": 1, "expected": {"A": "retain"}, "changes": {"A": None}, "approved_deletions": ["A", "A"]}]
        before = (self.checkout / ".env").read_bytes()
        for private_patch in patches:
            with self.subTest(patch=private_patch), self.assertRaises(RuntimeFailure):
                self.service.apply_local(private_patch)
            self.assertEqual((self.checkout / ".env").read_bytes(), before)

    def test_ordinary_local_patch_is_journaled_and_resolved_without_rewrite(self):
        self.configure(mode="local-env")
        self.local({"A": "private-old", "LOCAL": "retain"})
        private_patch = {"schema_version": 1, "expected": {"A": "private-old", "LOCAL": "retain"},
                         "changes": {"A": "private-new"}, "approved_deletions": []}
        applied = self.service.apply_local(private_patch)
        operation_id = applied["secret_operation_id"]
        journal = self.service.journal.read(operation_id)
        validator("secret-journal").validate(journal)
        self.assertEqual(journal["phase"], "resolved")
        self.assertEqual(journal["result"], "local_only")
        self.assertEqual(journal["evidence"]["target_files"], {})
        self.assertEqual(journal["evidence"]["original_files"], {})
        file_after_apply = (self.checkout / ".env").read_bytes()
        with patch.object(self.service.journal, "write", side_effect=AssertionError("Resolve must not rewrite")):
            resolved = self.service.resolve(operation_id)
        self.assertFalse(resolved["secrets_shared"])
        self.assertEqual((self.checkout / ".env").read_bytes(), file_after_apply)

    def test_ordinary_local_patch_unknown_before_rename_blocks_until_readonly_resolve(self):
        self.configure(mode="local-env")
        self.local({"A": "before"})
        private_patch = {"schema_version": 1, "expected": {"A": "before"},
                         "changes": {"A": "after"}, "approved_deletions": []}
        observed_phases = []
        def fail_before_rename(point):
            if point == "before_local_rename":
                observed_phases.append(self.service.journal.pending()[0]["phase"])
                raise RuntimeError("Private fault")
        self.runtime.fault = fail_before_rename
        pending = self.service.apply_local(private_patch)
        self.assertEqual(pending["status"], "unknown")
        self.assertEqual(observed_phases, ["applying_local"])
        self.assertEqual((self.checkout / ".env").read_text(), '# retained\nA="before"\n')
        with self.assertRaises(RuntimeFailure) as blocked:
            self.service.apply_local(private_patch)
        self.assertEqual(blocked.exception.code, 5)
        self.runtime.fault = lambda _: None
        with patch.object(self.service.environment, "apply", side_effect=AssertionError("Resolve must not write")):
            resolved = self.service.resolve(pending["secret_operation_id"])
        self.assertFalse(resolved["local_converged"])
        self.assertFalse(self.service.journal.pending())
        retried = self.service.apply_local(private_patch)
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(self.service.environment.read().values["A"], "after")

    def test_ordinary_local_patch_after_rename_unknown_preserves_editor_change(self):
        self.configure(mode="local-env")
        self.local({"A": "before"})
        private_patch = {"schema_version": 1, "expected": {"A": "before"},
                         "changes": {"A": "after"}, "approved_deletions": []}
        def fail_after_rename(point):
            if point == "after_local_rename":
                raise RuntimeError("Private fault")
        self.runtime.fault = fail_after_rename
        pending = self.service.apply_local(private_patch)
        self.assertEqual(pending["status"], "unknown")
        self.assertEqual(self.service.environment.read().values, {"A": "after"})
        self.local({"A": "editor"})
        self.runtime.fault = lambda _: None
        with patch.object(self.service.environment, "apply", side_effect=AssertionError("Resolve must not replay")):
            resolved = self.service.resolve(pending["secret_operation_id"])
        self.assertFalse(resolved["local_converged"])
        self.assertEqual(self.service.environment.read().values, {"A": "editor"})
        self.assertEqual(self.service.journal.read(pending["secret_operation_id"])["result"], "partial")

    def test_ordinary_local_patch_process_crash_after_rename_resolves_without_replay(self):
        self.configure(mode="local-env")
        self.local({"A": "before"})
        private_patch = {"schema_version": 1, "expected": {"A": "before"},
                         "changes": {"A": "after"}, "approved_deletions": []}
        head = self.observe()
        source_head = self.git(self.checkout, "rev-parse", "HEAD")
        index = (self.checkout / ".git/index").read_bytes()
        def crash_child():
            from agent_handoff.publication import Runtime
            child_runtime = Runtime(self.checkout, self.root / "private", self.authority, self.context)
            child_runtime.fault = lambda point: os._exit(77) if point == "after_local_rename" else None
            child_service = SecretService(child_runtime, self.manifest, self.adapter)
            with patch.object(child_service.adapter, "write_key", side_effect=AssertionError("Local patch must not upload")):
                child_service.apply_local(private_patch)
        process = multiprocessing.get_context("fork").Process(target=crash_child)
        process.start()
        process.join(timeout=15)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail("Ordinary dotenv crash harness did not terminate within its deadline.")
        self.assertEqual(process.exitcode, 77)
        with self.new_runtime("private", self.context) as restarted:
            restarted_service = SecretService(restarted, self.manifest)
            pending, = restarted_service.journal.pending()
            self.assertEqual(pending["evidence"]["kind"], "local_patch")
            self.assertEqual(pending["evidence"]["target_files"], {})
            operation_id = pending["operation_id"]
            after_crash = (self.checkout / ".env").read_bytes()
            self.assertEqual(restarted_service.environment.read().values, {"A": "after"})
            with patch.object(restarted_service.environment, "apply", side_effect=AssertionError("Resolve must not replay")):
                resolved = restarted_service.resolve(operation_id)
            self.assertTrue(resolved["local_converged"])
            self.assertEqual((self.checkout / ".env").read_bytes(), after_crash)
            self.assertFalse(restarted_service.journal.pending())
        self.assertEqual(self.observe(), head)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), index)
        self.assertEqual(self.adapter.write_keys, [])

    def test_local_patch_journal_rejects_out_of_scope_target_env_change(self):
        self.configure(mode="local-env")
        self.local({"A": "before", "EXTRA": "retain"})
        applied = self.service.apply_local({"schema_version": 1,
            "expected": {"A": "before", "EXTRA": "retain"}, "changes": {"A": "after"},
            "approved_deletions": []})
        journal = self.service.journal.read(applied["secret_operation_id"])
        schema = validator("secret-journal")
        schema.validate(journal)
        corrupt = json.loads(json.dumps(journal))
        corrupt["evidence"]["target_env"]["UNDECLARED"] = "private"
        schema.validate(corrupt)
        with self.assertRaises(RuntimeFailure):
            self.service.journal.write(corrupt)

    def test_local_pem_partial_resolution_preserves_editor_and_never_replays(self):
        manifest = {"schema_version": 1, "keys": {
            "CERT": {"classification": "file-backed", "required": True, "file": "keys/cert.pem"}}}
        self.service = SecretService(self.runtime, manifest)
        self.configure(mode="local-env")
        pem = "-----BEGIN CERTIFICATE-----\nZHVtbXk=\n-----END CERTIFICATE-----\n"
        private_patch = {"schema_version": 1, "expected": {}, "changes": {}, "approved_deletions": [],
                         "expected_files": {"CERT": None}, "file_changes": {"CERT": pem}}
        def fail(point):
            if point == "after_local_material_apply":
                raise RuntimeError("Private fault")
        self.runtime.fault = fail
        applied = self.service.apply_local(private_patch)
        self.assertEqual(applied["status"], "unknown")
        self.assertFalse((self.checkout / ".env").exists())
        self.assertEqual((self.checkout / "keys/cert.pem").read_text(), pem)
        with self.assertRaises(RuntimeFailure):
            self.service.apply_local(private_patch)
        pending = self.service.journal.read(applied["secret_operation_id"])
        validator("secret-journal").validate(pending)
        corrupt = json.loads(json.dumps(pending))
        corrupt["evidence"]["target_env"]["CERT"] = "other.pem"
        name = "secret-operation-" + applied["secret_operation_id"] + ".json"
        self.runtime.store.write(name, corrupt)
        with self.assertRaises(RuntimeFailure):
            self.service.journal.require_clear()
        self.runtime.store.write(name, pending)
        self.local({"EDITOR": "retain"})
        with patch.object(self.service.environment, "apply", side_effect=AssertionError("Must not replay")):
            resolved = self.service.resolve(applied["secret_operation_id"])
        self.assertFalse(resolved["local_converged"])
        self.assertFalse(resolved["secrets_shared"])
        self.assertEqual(self.service.environment.read().values, {"EDITOR": "retain"})
        self.assertEqual(self.service.journal.pending(), [])
        journal = self.service.journal.read(applied["secret_operation_id"])
        validator("secret-journal").validate(journal)
        self.assertEqual(journal["result"], "partial")
        self.assertEqual(journal["evidence"]["original_files"], {"CERT": None})
        self.runtime.fault = lambda point: None
        private_patch.update(expected={"EDITOR": "retain"}, expected_files={"CERT": pem})
        self.assertEqual(self.service.apply_local(private_patch)["status"], "completed")
        self.assertEqual(self.service.environment.read().values, {"EDITOR": "retain", "CERT": "keys/cert.pem"})

    def test_local_pem_preflights_all_material_and_observations(self):
        manifest = {"schema_version": 1, "keys": {
            key: {"classification": "file-backed", "required": False, "file": "keys/" + key + ".pem"}
            for key in ("A", "B")}}
        self.service = SecretService(self.runtime, manifest)
        self.configure(mode="local-env")
        pem = "-----BEGIN CERTIFICATE-----\nZHVtbXk=\n-----END CERTIFICATE-----\n"
        private_patch = {"schema_version": 1, "expected": {}, "changes": {}, "approved_deletions": [],
                         "expected_files": {"A": None, "B": None}, "file_changes": {"A": pem, "B": "invalid-private"}}
        with self.assertRaises(RuntimeFailure):
            self.service.apply_local(private_patch)
        self.assertFalse((self.checkout / "keys").exists())
        self.assertEqual(self.service.journal.pending(), [])
        private_patch["file_changes"]["B"] = pem
        private_patch["expected_files"]["B"] = pem
        with self.assertRaises(RuntimeFailure):
            self.service.apply_local(private_patch)
        self.assertFalse((self.checkout / "keys").exists())
        self.assertFalse((self.checkout / ".env").exists())

    def local(self, values):
        path = self.checkout / ".env"
        path.write_text("# retained\n" + "".join(key + "=" + json.dumps(value) + "\n" for key, value in values.items()))
        path.chmod(0o600)

    def pem_service(self, mapping="keys/cert.pem", upload=False):
        self.manifest = {"schema_version": 1, "keys": {
            "CERT": {"classification": "file-backed", "required": True, "file": mapping}}}
        self.service = SecretService(self.runtime, self.manifest, self.adapter)
        self.configure(upload=upload)
        return "-----BEGIN CERTIFICATE-----\nYQ==\n-----END CERTIFICATE-----\n"

    def test_file_backed_download_keeps_content_out_of_dotenv_and_public_results(self):
        pem = self.pem_service()
        self.adapter.values = {"CERT": pem}
        report = self.sync()
        material = self.checkout / "keys/cert.pem"
        self.assertEqual(material.read_text(), pem)
        self.assertEqual(material.stat().st_mode & 0o777, 0o600)
        self.assertEqual(material.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.service.environment.read().values, {"CERT": "keys/cert.pem"})
        self.assertEqual(self.service._baseline(self.service._binding()).read(), {"CERT": pem})
        self.assertTrue(self.service.verify()["secrets_shared"])
        self.assertNotIn(pem, json.dumps(report))

    def test_file_backed_upload_sends_content_and_adds_missing_local_pointer(self):
        pem = self.pem_service(upload=True)
        material = self.checkout / "keys/cert.pem"
        material.parent.mkdir(mode=0o700)
        material.write_text(pem)
        material.chmod(0o600)
        report = self.sync()
        self.assertTrue(report["secrets_shared"])
        self.assertEqual(self.adapter.values, {"CERT": pem})
        self.assertEqual(self.service.environment.read().values, {"CERT": "keys/cert.pem"})

    def test_file_backed_editor_change_invalidates_observation(self):
        pem = self.pem_service()
        self.adapter.values = {"CERT": pem}
        self.sync()
        planned = self.service.reconcile()
        (self.checkout / "keys/cert.pem").write_text(pem.replace("YQ==", "Yg=="))
        with self.assertRaises(RuntimeFailure) as stale:
            self.service.apply(planned["plan_id"])
        self.assertEqual(stale.exception.status, "decision_required")
        self.assertEqual(self.service._baseline(self.service._binding()).read(), {"CERT": pem})

    def test_file_backed_deletion_requires_decision_and_removes_only_approved_material(self):
        pem = self.pem_service()
        self.adapter.values = {"CERT": pem}
        self.sync()
        self.adapter.values = {}
        planned = self.service.reconcile()
        self.assertEqual(planned["status"], "decision_required")
        self.assertTrue((self.checkout / "keys/cert.pem").exists())
        report = self.service.apply_decision(planned["decision"]["decision_id"], "use-remote")
        self.assertTrue(report["secrets_shared"])
        self.assertFalse((self.checkout / "keys/cert.pem").exists())
        self.assertNotIn("CERT", self.service.environment.read().values)

    def test_file_backed_tracked_source_and_binary_are_rejected(self):
        for mapping in ("code.txt", "keys/cert.p12"):
            with self.subTest(mapping=mapping):
                with self.assertRaises(RuntimeFailure):
                    pem = self.pem_service(mapping)
                    self.adapter.values = {"CERT": pem}
                    self.service.reconcile()
        self.assertEqual((self.checkout / "code.txt").read_text(), "original\n")

    def test_file_backed_crash_before_baseline_resolves_without_replay(self):
        pem = self.pem_service()
        self.adapter.values = {"CERT": pem}
        planned = self.service.reconcile()
        def crash(point):
            if point == "before_baseline_write":
                raise SystemExit(77)
        self.runtime.fault = crash
        with self.assertRaises(SystemExit):
            self.service.apply(planned["plan_id"])
        pending = self.service.journal.pending()[0]
        self.runtime.fault = lambda _: None
        self.assertTrue(self.service.resolve(pending["operation_id"])["secrets_shared"])
        self.assertEqual(self.adapter.write_keys, [])

    def test_integrity_adoption_cannot_bypass_mapped_secret_prohibition(self):
        pem = self.pem_service(mapping="keys/cert.txt")
        before = self.runtime.store.read("integrity.json")
        parent = self.observe()
        invalid, _ = self.runtime.backend.prepare(parent, {"keys/cert.txt": pem.encode()}, "hand edited mapped material")
        self.assertEqual(self.runtime.backend.push(invalid, parent), "published")
        proposal = self.runtime.propose_integrity()
        self.assertNotIn("adopt", [option["option_id"] for option in proposal["decision"]["options"]])
        with self.assertRaises(RuntimeFailure):
            self.runtime.apply_decision(proposal["decision"]["decision_id"], "adopt")
        self.assertEqual(self.runtime.store.read("integrity.json"), before)
        self.runtime.apply_decision(proposal["decision"]["decision_id"], "abort")
        self.assertEqual(self.observe(), invalid)

    def test_file_backed_nonsecret_suffix_cannot_enter_git_snapshot(self):
        pem = self.pem_service("keys/cert.txt")
        self.adapter.values = {"CERT": pem}
        self.sync()
        before = self.observe()
        with self.assertRaises(RuntimeFailure):
            self.runtime.publish_snapshot("checkpoint", ["keys/cert.txt"], summary="Selected snapshot")
        self.assertEqual(self.observe(), before)
        self.assertFalse(self.runtime.journal.pending())

    def sync(self):
        planned = self.service.reconcile()
        self.assertEqual(planned["status"], "completed")
        return self.service.apply(planned["plan_id"])

    def test_repeated_binding_consent_cannot_change_terminal_lifecycle(self):
        proposal = self.service.propose_binding("local-env")
        decision_id = proposal["decision"]["decision_id"]
        self.service.apply_decision(decision_id, "approve-secret-binding")
        with self.assertRaises(RuntimeFailure) as rejected:
            self.service.apply_decision(decision_id, "approve-secret-binding")
        self.assertEqual(rejected.exception.status, "rejected")
        lifecycle = self.runtime.store.read("decision-state-" + decision_id + ".json")
        self.assertEqual(lifecycle["status"], "applied")
        with self.assertRaises(RuntimeFailure):
            self.runtime.decisions.finish(decision_id, "stale")
        self.assertEqual(self.runtime.store.read("decision-state-" + decision_id + ".json"), lifecycle)

    def test_A41_download_new_machine_private_local_material_and_baseline(self):
        self.configure()
        self.adapter.values = {"A": "private-remote", "UNKNOWN": "never-download"}
        synced = self.sync()
        self.assertEqual(synced["secrets_shared"], True)
        self.assertEqual(self.service.environment.read().values, {"A": "private-remote"})
        self.assertEqual(self.service._baseline(self.service._binding()).read(), {"A": "private-remote"})
        self.assertNotIn("private-remote", json.dumps(synced))

    def test_A53_network_failure_keeps_required_local_secrets_available(self):
        self.configure()
        self.local({"A": "dummy-local-retained", "LOCAL": "dummy-local-only"})
        before = (self.checkout / ".env").read_bytes()
        with patch.object(self.adapter, "read", side_effect=OSError("dummy-private-provider-error")):
            report = self.service.inspect()
        self.assertIs(report["secrets_shared"], False)
        self.assertTrue(report["local_work_available"])
        self.assertEqual(report["availability"]["required_missing"], [])
        self.assertEqual((self.checkout / ".env").read_bytes(), before)
        self.assertIsNone(self.service._baseline(self.service._binding()).read())
        self.assertNotIn("dummy-local-retained", json.dumps(report))
        self.assertNotIn("dummy-private-provider-error", json.dumps(report))

    def test_A44_A45_A46_merge_per_key_and_preserve_local_only(self):
        self.configure(upload=True)
        self.local({"A": "base", "B": "base", "LOCAL": "never-upload"})
        self.adapter.values = {"A": "base", "B": "base", "UNRELATED": "preserve"}
        self.sync()
        self.local({"A": "local-new", "B": "base", "LOCAL": "never-upload"})
        self.adapter.values["B"] = "remote-new"
        synced = self.sync()
        self.assertEqual(synced["status"], "completed")
        self.assertEqual(self.adapter.write_keys, ["A"])
        self.assertEqual(self.adapter.values["UNRELATED"], "preserve")
        self.assertNotIn("LOCAL", self.adapter.values)
        self.assertEqual(self.service.environment.read().values, {"A": "local-new", "B": "remote-new", "LOCAL": "never-upload"})

    def test_A45_remote_only_single_key_delta_applies_locally_then_advances_baseline(self):
        self.configure()
        self.local({"A": "stable", "B": "base", "LOCAL": "keep-local"})
        self.adapter.values = {"A": "stable", "B": "base", "UNRELATED": "preserve"}
        self.sync()

        binding = self.service._binding()
        baseline = self.service._baseline(binding)
        baseline_record_before = self.runtime.store.read(baseline.name)
        baseline_before = baseline.read()
        self.assertEqual(baseline_before, {"A": "stable", "B": "base"})
        self.assertEqual(self.service.environment.read().values["B"], baseline_before["B"])
        self.adapter.values["B"] = "remote-new"

        publication_before = self.observe()
        owner_before = self.runtime.backend.records(publication_before)["owner"]["owner"]
        source_head_before = self.git(self.checkout, "rev-parse", "HEAD")
        source_index_before = (self.checkout / ".git/index").read_bytes()
        planned = self.service.reconcile()
        self.assertEqual(planned["status"], "completed")
        synced = self.service.apply(planned["plan_id"])

        self.assertEqual(synced["status"], "completed")
        self.assertTrue(synced["secrets_shared"])
        self.assertEqual(self.service.environment.read().values,
                         {"A": "stable", "B": "remote-new", "LOCAL": "keep-local"})
        baseline_record_after = self.runtime.store.read(baseline.name)
        baseline_after = baseline.read()
        self.assertEqual(baseline_after, {"A": "stable", "B": "remote-new"})
        self.assertNotEqual(baseline_record_after, baseline_record_before)
        self.assertEqual(baseline_record_after["verified_sync_id"], synced["secret_operation_id"])
        self.assertEqual(self.adapter.values["UNRELATED"], "preserve")
        self.assertEqual(self.adapter.write_calls, [])
        self.assertEqual(self.observe(), publication_before)
        self.assertEqual(self.runtime.backend.records(publication_before)["owner"]["owner"], owner_before)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), source_head_before)
        self.assertEqual((self.checkout / ".git/index").read_bytes(), source_index_before)
        self.assertEqual(self.git(self.remote, "rev-parse", "refs/heads/main"), self.base)

    def test_A45_remote_only_delta_fault_before_baseline_keeps_previous_baseline(self):
        self.configure()
        self.local({"A": "stable", "B": "base", "LOCAL": "keep-local"})
        self.adapter.values = {"A": "stable", "B": "base"}
        self.sync()
        binding = self.service._binding()
        baseline = self.service._baseline(binding)
        baseline_record_before = self.runtime.store.read(baseline.name)
        baseline_before = baseline.read()
        self.adapter.values["B"] = "remote-new"
        planned = self.service.reconcile()

        def interrupt_baseline_persistence(point):
            if point == "before_baseline_write":
                raise RuntimeError("fixture baseline persistence interruption")

        self.runtime.fault = interrupt_baseline_persistence
        report = self.service.apply(planned["plan_id"])
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(self.runtime.store.read(baseline.name), baseline_record_before)
        self.assertEqual(baseline.read(), baseline_before)
        self.assertEqual(self.adapter.write_calls, [])

    def test_A43_A47_conflict_decision_contains_no_values_or_hashes(self):
        self.configure(upload=True)
        self.local({"A": "sentinel-local-value", "B": "initial-new"})
        self.adapter.values = {"A": "sentinel-remote-value"}
        before = (self.checkout / ".env").read_bytes()
        proposal = self.service.reconcile()
        public = json.dumps(proposal)
        self.assertNotIn("sentinel-local-value", public)
        self.assertNotIn("sentinel-remote-value", public)
        self.assertEqual(self.adapter.write_keys, [])
        self.assertEqual((self.checkout / ".env").read_bytes(), before)
        adopted = self.service.apply_decision(proposal["decision"]["decision_id"], "use-remote")
        self.assertEqual(adopted["status"], "completed")
        self.assertEqual(self.service.environment.read().values["A"], "sentinel-remote-value")

    def test_A48_A49_deletion_is_explicit_and_adoption_does_not_rebuild_remote(self):
        self.configure(upload=True)
        self.local({"A": "base"})
        self.adapter.values = {"A": "base"}
        self.sync()
        self.adapter.values.clear()
        proposal = self.service.reconcile()
        self.assertEqual(self.service.environment.read().values, {"A": "base"})
        adopted = self.service.apply_decision(proposal["decision"]["decision_id"], "use-remote")
        self.assertEqual(adopted["status"], "completed")
        self.assertEqual(self.adapter.write_keys, [])
        self.assertEqual(self.service.environment.read().values, {})
        self.assertEqual(self.service.inspect()["availability"]["required_missing"], ["A"])

    def test_A50_secret_decision_stale_when_remote_changes(self):
        self.configure(upload=True)
        self.local({"A": "local"})
        self.adapter.values = {"A": "remote"}
        proposal = self.service.reconcile()
        self.adapter.values["A"] = "new-remote"
        with self.assertRaises(RuntimeFailure) as failure:
            self.service.apply_decision(proposal["decision"]["decision_id"], "keep-local")
        self.assertEqual(failure.exception.code, 3)
        self.assertEqual(self.adapter.write_keys, [])

    def test_A51_old_session_fenced_before_secret_write(self):
        self.configure(upload=True)
        self.local({"A": "local"})
        proposal = self.service.reconcile()
        with self.new_runtime("other-private", "b" * 64) as other:
            other.initialize()
            takeover = other.acquire()
            other.apply_decision(takeover["decision"]["decision_id"], "takeover")
        with self.assertRaises(RuntimeFailure) as failure:
            self.service.apply(proposal["plan_id"])
        self.assertEqual(failure.exception.code, 4)
        self.assertEqual(self.adapter.write_keys, [])

    def test_takeover_after_owner_check_provider_gate_rejects_suspended_writer(self):
        self.configure(upload=True)
        self.local({"A": "local"})
        proposal = self.service.reconcile()
        def takeover(point):
            if point == "before_secret_key_write":
                with self.new_runtime("other-private", "b" * 64) as other:
                    other.initialize()
                    decision = other.acquire()
                    other.apply_decision(decision["decision"]["decision_id"], "takeover")
                    self.adapter.owner = other.sessions.current()["session_id"]
        self.runtime.fault = takeover
        rejected = self.service.apply(proposal["plan_id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.adapter.write_keys, [])
        self.assertEqual(self.adapter.values, {})

    def test_A52_partial_timeout_keeps_old_baseline_and_pending_evidence(self):
        self.configure(upload=True)
        self.local({"A": "base", "B": "base"})
        self.adapter.values = {"A": "base", "B": "base"}
        self.sync()
        baseline = self.service._baseline(self.service._binding()).read()
        self.local({"A": "new", "B": "new"})
        self.adapter.timeout_key = "B"
        partial = self.sync()
        self.assertEqual(partial["status"], "unknown")
        self.assertEqual(partial["secrets_shared"], "partial")
        self.assertEqual(self.service._baseline(self.service._binding()).read(), baseline)
        self.assertEqual(self.service.resolve(partial["secret_operation_id"])["status"], "unknown")
        with self.assertRaises(RuntimeFailure):
            self.service.reconcile()
        session_before = self.runtime.sessions.current()
        with self.assertRaises(RuntimeFailure) as blocked:
            self.runtime.resume()
        self.assertEqual(blocked.exception.status, "unknown")
        self.assertEqual(self.runtime.sessions.current(), session_before)

    def test_terminal_partial_resolution_retains_baseline_and_never_replays(self):
        self.configure(upload=True)
        self.local({"A": "base", "B": "base"})
        self.adapter.values = {"A": "base", "B": "base"}
        self.sync()
        old_baseline = self.service._baseline(self.service._binding()).read()
        self.local({"A": "new", "B": "new"})
        self.adapter.timeout_key = "B"
        partial = self.sync()
        pending = self.service.journal.read(partial["secret_operation_id"])
        self.adapter.operations[pending["keys"]["B"]["operation_id"]] = "rejected"
        writes = list(self.adapter.write_keys)
        resolved = self.service.resolve(partial["secret_operation_id"])
        self.assertEqual(resolved["status"], "completed")
        self.assertEqual(resolved["secrets_shared"], "partial")
        self.assertTrue(resolved["baseline_retained"])
        self.assertFalse(self.service.journal.pending())
        self.assertEqual(self.adapter.write_keys, writes)
        self.assertEqual(self.service._baseline(self.service._binding()).read(), old_baseline)
        self.assertEqual(self.service.reconcile()["status"], "completed")

    def test_resolved_operation_cannot_overwrite_newer_baseline(self):
        self.configure()
        self.adapter.values = {"A": "first"}
        first = self.sync()
        self.adapter.values = {"A": "second"}
        second = self.sync()
        binding = self.service._binding()
        before = self.runtime.store.read(self.service._baseline(binding).name)
        self.service.resolve(first["secret_operation_id"])
        self.assertEqual(self.runtime.store.read(self.service._baseline(binding).name), before)
        self.assertEqual(before["verified_sync_id"], second["secret_operation_id"])

    def test_partial_local_pem_resolution_allows_fresh_pointer_repair(self):
        pem = self.pem_service()
        self.adapter.values = {"CERT": pem}
        planned = self.service.reconcile()
        def crash(point):
            if point == "before_local_rename":
                raise SystemExit(77)
        self.runtime.fault = crash
        with self.assertRaises(SystemExit):
            self.service.apply(planned["plan_id"])
        self.assertEqual((self.checkout / "keys/cert.pem").read_text(), pem)
        self.assertNotIn("CERT", self.service.environment.read().values)
        pending = self.service.journal.pending()[0]
        self.runtime.fault = lambda _: None
        resolved = self.service.resolve(pending["operation_id"])
        self.assertTrue(resolved["baseline_retained"])
        self.assertIsNone(self.service._baseline(self.service._binding()).read())
        self.assertTrue(self.sync()["secrets_shared"])
        self.assertEqual(self.service.environment.read().values, {"CERT": "keys/cert.pem"})

    def test_historical_resolution_after_release_never_restores_writer(self):
        self.configure(upload=True)
        self.local({"A": "base", "B": "base"})
        self.adapter.values = {"A": "base", "B": "base"}
        self.sync()
        self.local({"A": "new", "B": "new"})
        self.adapter.timeout_key = "B"
        partial = self.sync()
        pending = self.service.journal.read(partial["secret_operation_id"])
        self.adapter.operations[pending["keys"]["B"]["operation_id"]] = "rejected"
        self.runtime.release()
        before = self.observe()
        resolved = self.service.resolve(partial["secret_operation_id"])
        self.assertEqual(resolved["secrets_shared"], "partial")
        self.assertEqual(self.observe(), before)
        self.assertIsNone(self.runtime.backend.records(before)["owner"]["owner"])
        with self.assertRaises(RuntimeFailure):
            self.runtime.recover()

    def test_historical_resolution_rejects_other_context_before_provider_read(self):
        self.configure()
        self.adapter.values = {"A": "remote"}
        planned = self.service.reconcile()
        def crash(point):
            if point == "before_baseline_write":
                raise SystemExit(77)
        self.runtime.fault = crash
        with self.assertRaises(SystemExit):
            self.service.apply(planned["plan_id"])
        pending = self.service.journal.pending()[0]
        with self.new_runtime("private", "b" * 64) as other:
            service = SecretService(other, self.manifest, self.adapter)
            with patch.object(self.adapter, "read", side_effect=AssertionError("Must not read provider")):
                with self.assertRaises(RuntimeFailure) as rejected:
                    service.resolve(pending["operation_id"])
            self.assertEqual(rejected.exception.status, "rejected")
        self.assertTrue(self.service.journal.pending())

    def test_crash_before_baseline_resolves_without_rewriting_keys(self):
        self.configure()
        self.adapter.values = {"A": "remote"}
        planned = self.service.reconcile()
        def crash(point):
            if point == "before_baseline_write":
                raise SystemExit(77)
        self.runtime.fault = crash
        with self.assertRaises(SystemExit):
            self.service.apply(planned["plan_id"])
        pending = self.service.journal.pending()[0]
        self.runtime.fault = lambda _: None
        self.assertEqual(self.service.resolve(pending["operation_id"])["secrets_shared"], True)
        self.assertEqual(self.adapter.write_keys, [])

    def test_manifest_scope_expansion_requires_new_decision(self):
        self.configure()
        changed = json.loads(json.dumps(self.manifest))
        changed["keys"]["NEW"] = {"classification": "shared", "required": False}
        service = SecretService(self.runtime, changed, self.adapter)
        with self.assertRaises(RuntimeFailure) as failure:
            service.inspect()
        self.assertEqual(failure.exception.code, 3)

    def test_local_env_mode_is_explicit_and_patch_is_private(self):
        self.configure(mode="local-env")
        self.local({"A": "private-old", "LOCAL": "retain"})
        applied = self.service.apply_local({"schema_version": 1, "expected": {"A": "private-old", "LOCAL": "retain"},
            "changes": {"A": "private-new"}, "approved_deletions": []})
        self.assertEqual(applied["secrets_shared"], False)
        self.assertNotIn("private-new", json.dumps(applied))
        with self.assertRaises(RuntimeFailure) as failure:
            self.service.reconcile()
        self.assertEqual(failure.exception.code, 8)

    def test_unimplemented_adapter_write_gate_cannot_be_enabled_by_grant(self):
        service = SecretService(self.runtime, self.manifest, Adapter("test", "dev"))
        with self.assertRaises(RuntimeFailure) as failure:
            service.propose_binding("doppler", project="test", config="dev", upload_authorized=True)
        self.assertEqual(failure.exception.code, 8)

    def test_basic_doppler_binding_can_request_explicit_upload_grant(self):
        service = SecretService(self.runtime, self.manifest, Doppler("test", "dev", self.checkout))
        proposal = service.propose_binding("doppler", project="test", config="dev", upload_authorized=True)
        self.assertEqual(proposal["status"], "decision_required")

    def _use_basic_adapter(self):
        self.adapter = BasicFake(self.runtime.sessions.current()["session_id"])
        self.service = SecretService(self.runtime, self.manifest, self.adapter)
        self.configure(upload=True)
        self.local({"A": "base", "B": "base", "LOCAL": "keep-local"})
        self.adapter.values = {"A": "base", "B": "base", "UNRELATED": "keep-remote"}
        self.sync()

    def test_basic_write_merges_different_keys_and_updates_baseline(self):
        self._use_basic_adapter()
        self.local({"A": "local-new", "B": "base", "LOCAL": "keep-local"})
        self.adapter.values["B"] = "remote-new"
        applied = self.sync()
        self.assertTrue(applied["secrets_shared"])
        self.assertEqual(self.adapter.write_keys, ["A"])
        self.assertEqual(self.adapter.values["UNRELATED"], "keep-remote")
        self.assertEqual(self.service.environment.read().values["LOCAL"], "keep-local")
        self.assertEqual(self.service._baseline(self.service._binding()).read(),
                         {"A": "local-new", "B": "remote-new"})

    def test_basic_timeout_resolves_from_current_values_without_replay(self):
        self._use_basic_adapter()
        self.local({"A": "new", "B": "base"})
        self.adapter.timeout_key = "A"
        pending = self.sync()
        self.assertEqual(pending["status"], "unknown")
        self.assertEqual(self.service._baseline(self.service._binding()).read()["A"], "base")
        # Simulate a completed request and loss of in-memory receipts after restart.
        self.adapter.values["A"] = "new"
        self.adapter.operations.clear()
        writes = list(self.adapter.write_calls)
        confirmed = self.service.resolve(pending["secret_operation_id"])
        self.assertTrue(confirmed["secrets_shared"])
        self.assertEqual(self.adapter.write_calls, writes)
        self.assertEqual(self.service._baseline(self.service._binding()).read()["A"], "new")

    def test_basic_timeout_mismatch_retains_baseline_and_allows_fresh_plan(self):
        self._use_basic_adapter()
        self.local({"A": "new", "B": "base"})
        self.adapter.timeout_key = "A"
        pending = self.sync()
        writes = list(self.adapter.write_calls)
        confirmed = self.service.resolve(pending["secret_operation_id"])
        self.assertFalse(confirmed["secrets_shared"])
        self.assertTrue(confirmed["baseline_retained"])
        self.assertEqual(self.service._baseline(self.service._binding()).read()["A"], "base")
        self.assertEqual(self.adapter.write_calls, writes)
        self.assertFalse(self.service.journal.pending())
        self.assertEqual(self.service.reconcile()["status"], "completed")


    def _preservation_snapshot(self):
        return (self.runtime.backend.remote_head(), self.git(self.checkout, "rev-parse", "HEAD"),
                (self.checkout / ".git/index").read_bytes(), (self.checkout / ".env").read_bytes())

    def test_typed_auth_and_second_read_result_are_safe_and_preserve_local_state(self):
        self.configure()
        self.local({"A": "private-local-value"})
        before = self._preservation_snapshot()
        with patch.object(self.adapter, "read", side_effect=AuthenticationRequired()):
            inspected = self.service.inspect()
        self.assertEqual(inspected["shared_read"], "unavailable")
        self.assertEqual(inspected["shared_read_failure"], "authentication_required")
        self.assertTrue(inspected["local_work_available"])

        with patch.object(self.adapter, "read", side_effect=[AuthenticationRequired(),
                RuntimeFailure("private provider network sentinel", "not_ready", 6)]):
            failed_verify = self.service.verify()
        self.assertEqual(failed_verify["status"], "not_ready")
        self.assertEqual(failed_verify["shared_read"], "unavailable")
        self.assertEqual(failed_verify["shared_read_failure"], "unavailable")
        self.assertNotIn("private provider", json.dumps(failed_verify))
        self.assertEqual(self._preservation_snapshot(), before)

        with patch.object(self.adapter, "read", side_effect=[AuthenticationRequired(), {}]):
            recovered_verify = self.service.verify()
        self.assertEqual(recovered_verify["shared_read"], "available")
        self.assertNotIn("shared_read_failure", recovered_verify)
        self.assertEqual(recovered_verify["shared_available_keys"], [])
        self.assertEqual(self._preservation_snapshot(), before)

    def test_local_baseline_failure_does_not_relabel_successful_remote_read(self):
        self.configure()
        self.local({"A": "private-local-value"})
        with patch.object(self.service, "_baseline", side_effect=RuntimeFailure("private baseline sentinel")):
            report = self.service.verify()
        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(report["shared_read"], "available")
        self.assertNotIn("shared_read_failure", report)


class DopplerBoundaryTests(RepositoryFixture):
    def test_scoped_response_hidden_and_identity_checked(self):
        adapter = Doppler("test", "dev", self.checkout)
        responses = [subprocess.CompletedProcess([], 0, b"v3.58.0", b""), subprocess.CompletedProcess([], 0,
            json.dumps({"A": {"computed": "sentinel-private"}, "DOPPLER_PROJECT": "test", "DOPPLER_CONFIG": "dev"}).encode(), b"")]
        with patch("agent_handoff.secrets.doppler.subprocess.run", side_effect=responses) as execute:
            self.assertEqual(adapter.read({"A"}), {"A": "sentinel-private"})
            argv = execute.call_args.args[0]
            self.assertIn("--no-read-env", argv)
            self.assertIn("--no-exit-on-missing-secret", argv)
            self.assertNotIn("sentinel-private", json.dumps(argv))
        wrong = subprocess.CompletedProcess([], 0, b'{"DOPPLER_PROJECT":"other","DOPPLER_CONFIG":"dev"}', b"private-error")
        with patch("agent_handoff.secrets.doppler.subprocess.run", return_value=wrong), self.assertRaises(RuntimeFailure):
            adapter.read({"A"})

    def test_restricted_or_old_cli_values_never_become_empty_baseline(self):
        adapter = Doppler("test", "dev", self.checkout)
        with patch("agent_handoff.secrets.doppler.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"v3.57.1", b"")):
            with self.assertRaises(RuntimeFailure) as failure:
                adapter.read({"A"})
            self.assertEqual(failure.exception.code, 8)

    def test_only_exact_empty_stdout_missing_token_json_is_typed_authentication_failure(self):
        adapter = Doppler("test", "dev", self.checkout)
        arguments = ["--no-check-version", "--no-read-env", "--attempts", "1", "--timeout", "10s",
                     "secrets", "get", "A", "DOPPLER_PROJECT", "DOPPLER_CONFIG", "--json",
                     "--no-exit-on-missing-secret", "--project", "test", "--config", "dev"]
        exact = subprocess.CompletedProcess([], 1, b"", b'{"error":"you must provide a token"}')
        with patch("agent_handoff.secrets.doppler.subprocess.run", return_value=exact):
            with self.assertRaises(AuthenticationRequired) as caught:
                adapter._run(arguments)
        self.assertEqual((caught.exception.status, caught.exception.code), ("not_ready", 6))
        self.assertNotIn("token", str(caught.exception).casefold())

        unsafe = (
            subprocess.CompletedProcess([], 1, b"private-sentinel", b'{"error":"you must provide a token"}'),
            subprocess.CompletedProcess([], 1, b"", b'{"error":"you must provide a token; private-sentinel"}'),
            subprocess.CompletedProcess([], 1, b"", b'{"error":"you must provide a token","error":"private-sentinel"}'),
            subprocess.CompletedProcess([], 1, b"", b'{"error":"permission denied"}'),
            subprocess.CompletedProcess([], 1, b"", b'{"error":"network unavailable"}'),
        )
        for response in unsafe:
            with self.subTest(stderr_category=type(response.stderr).__name__), patch(
                    "agent_handoff.secrets.doppler.subprocess.run", return_value=response):
                with self.assertRaises(RuntimeFailure) as failure:
                    adapter._run(arguments)
                self.assertNotIsInstance(failure.exception, AuthenticationRequired)
                self.assertNotIn("private-sentinel", str(failure.exception))
        version_failure = subprocess.CompletedProcess([], 1, b"", b'{"error":"you must provide a token"}')
        with patch("agent_handoff.secrets.doppler.subprocess.run", return_value=version_failure):
            with self.assertRaises(RuntimeFailure) as failure:
                adapter._run(["--version"])
        self.assertNotIsInstance(failure.exception, AuthenticationRequired)
        with patch("agent_handoff.secrets.doppler.subprocess.run", side_effect=subprocess.TimeoutExpired("doppler", 15)):
            with self.assertRaises(RuntimeFailure) as failure:
                adapter._run(arguments)
        self.assertNotIsInstance(failure.exception, AuthenticationRequired)


