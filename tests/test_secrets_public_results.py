"""Public secret reports never expose values or publication capabilities."""
import contextlib
import io
import json
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.paths import PrivateStore
from agent_handoff.public_results import validate_secrets_result
from agent_handoff.public_results import PublicResultFailure
from agent_handoff.output import result
from agent_handoff.secrets.service import SecretService
from test_publication import RepositoryFixture
from test_record_schemas import validator


class SecretsPublicTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.runtime.acquire()
        self.manifest = {"schema_version": 1, "keys": {"A": {"classification": "shared", "required": True}}}
        self.service = SecretService(self.runtime, self.manifest)

    def configure(self):
        proposal = self.service.propose_binding("local-env")
        applied = self.service.apply_decision(proposal["decision"]["decision_id"], "approve-secret-binding")
        return proposal, applied

    def test_real_local_flow_matches_schemas_and_rejects_private_fields(self):
        proposal, configured = self.configure()
        missing = self.service.inspect()
        applied = self.service.apply_local({"schema_version": 1, "expected": {}, "changes": {"A": "private-sentinel"}, "approved_deletions": []})
        verified = self.service.verify()
        for value in (proposal, configured, missing, applied, verified):
            validate_secrets_result(value)
            validator("secrets-result").validate(value)
            validator("result").validate(value)
            for changes in ({"values": {"A": "private-sentinel"}}, {"ownership": "owned"}, {"code_publication": "published"}):
                invalid = dict(value, **changes)
                self.assertFalse(validator("secrets-result").is_valid(invalid))
                with self.assertRaises(RuntimeFailure):
                    validate_secrets_result(invalid)

    def test_inspection_cannot_claim_shared_or_ready_with_missing_material(self):
        self.configure()
        value = self.service.inspect()
        for changes in ({"secrets_shared": True}, {"status": "completed"}):
            invalid = dict(value, **changes)
            self.assertFalse(validator("secrets-result").is_valid(invalid))
            with self.assertRaises(RuntimeFailure):
                validate_secrets_result(invalid)

    def test_structured_shared_read_failure_is_scoped_and_backward_compatible(self):
        self.configure()
        report = self.service.inspect()
        report.update(secrets_mode="doppler", destination_identity="a" * 64,
                      shared_read="unavailable", shared_read_failure="authentication_required",
                      secrets_shared=False)
        validate_secrets_result(report)
        validator("secrets-result").validate(report)
        verify = dict(report, command="secrets verify", status="not_ready", convergence="unverified")
        validate_secrets_result(verify)
        validator("secrets-result").validate(verify)
        missing_shared_read = dict(verify)
        missing_shared_read.pop("shared_read")
        missing_mode = dict(verify)
        missing_mode.pop("secrets_mode")
        for invalid in (missing_shared_read, missing_mode):
            self.assertFalse(validator("secrets-result").is_valid(invalid))
            with self.assertRaises(RuntimeFailure):
                validate_secrets_result(invalid)
        for invalid in (
            dict(report, shared_read_failure="private provider diagnostic"),
            dict(report, secrets_mode="local-env"),
            dict(report, shared_read="available"),
            dict(report, secrets_shared=None),
        ):
            self.assertFalse(validator("secrets-result").is_valid(invalid))
            with self.assertRaises(RuntimeFailure):
                validate_secrets_result(invalid)
        legacy = dict(report)
        legacy.pop("shared_read_failure")
        validate_secrets_result(legacy)
        validator("secrets-result").validate(legacy)

    def test_post_effect_secret_payload_mutation_keeps_operation_address(self):
        self.manifest["keys"]["CERT"] = {"classification": "file-backed", "required": False, "file": "keys/cert.pem"}
        self.service = SecretService(self.runtime, self.manifest)
        self.configure()
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
            store.write("patch.json", {"schema_version": 1, "expected": {}, "changes": {"A": "private-sentinel"}, "approved_deletions": [],
                "expected_files": {"CERT": None}, "file_changes": {"CERT": "-----BEGIN CERTIFICATE-----\ncHJpdmF0ZS1maXh0dXJl\n-----END CERTIFICATE-----\n"}})
        original = SecretService.apply_local
        def unsafe(service, patch_data):
            value = original(service, patch_data)
            value.update(command="inspect", values={"A": "private-sentinel"})
            return value
        output = io.StringIO()
        with patch.object(SecretService, "apply_local", unsafe), contextlib.redirect_stdout(output):
            code = main(["secrets", "apply", "--checkout", str(self.checkout), "--private-root", str(self.root / "private"),
                "--authority", str(self.remote), "--publication-ref", self.authority.publication_ref,
                "--context-file", str(self.root / "private/context.json"), "--patch-file", str(self.root / "private/patch.json")])
        self.assertEqual(code, 7)
        self.assertNotIn("private-sentinel", output.getvalue())
        value = json.loads(output.getvalue())
        self.assertEqual(value["secrets_shared"], "unknown")
        validator("secrets-result").validate(value)
        self.assertIsNotNone(value["secret_operation_id"])
        self.assertFalse(self.service.resolve(value["secret_operation_id"])["secrets_shared"])

    def test_output_factory_failure_does_not_corrupt_terminal_local_journal(self):
        self.manifest["keys"]["CERT"] = {"classification": "file-backed", "required": False, "file": "keys/cert.pem"}
        self.service = SecretService(self.runtime, self.manifest)
        self.configure()
        patch_data = {"schema_version": 1, "expected": {}, "changes": {"A": "private-sentinel"}, "approved_deletions": [],
            "expected_files": {"CERT": None}, "file_changes": {"CERT": "-----BEGIN CERTIFICATE-----\ncHJpdmF0ZS1maXh0dXJl\n-----END CERTIFICATE-----\n"}}
        def poison(command, **fields):
            if command == "secrets apply":
                fields["values"] = {"A": "private-sentinel"}
            return result(command, **fields)
        with patch("agent_handoff.secrets.service.result", side_effect=poison), self.assertRaises(PublicResultFailure) as caught:
            self.service.apply_local(patch_data)
        operation_id = caught.exception.secret_operation_id
        journal = self.service.journal.read(operation_id)
        self.assertEqual(journal["phase"], "resolved")
        self.assertEqual(journal["result"], "local_only")
        self.assertFalse(self.service.resolve(operation_id)["secrets_shared"])
