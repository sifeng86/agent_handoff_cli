import contextlib
import io
import json
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.paths import PrivateStore
from test_publication import RepositoryFixture


class SecretCLITests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.runtime.acquire()
        self.manifest = self.root / "manifest.yaml"
        self.manifest.write_text('{"schema_version":1,"keys":{"A":{"classification":"shared","required":true}}}')
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("cli-context.json", {"schema_version": 1, "context": self.context})
        self.common = ["--checkout", str(self.checkout), "--private-root", str(self.root / "private"),
            "--authority", str(self.remote), "--publication-ref", self.authority.publication_ref,
            "--context-file", str(self.root / "private/cli-context.json"), "--manifest-file", str(self.manifest)]

    def invoke(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main([*args, *self.common])
        self.assertEqual(error.getvalue(), "")
        return code, json.loads(output.getvalue())

    def test_local_env_configure_decision_private_patch_verify_flow(self):
        code, proposal = self.invoke("secrets", "configure", "--secrets-mode", "local-env")
        self.assertEqual(code, 3)
        self.assertEqual(self.invoke("decision", "apply", "--decision-id", proposal["decision"]["decision_id"],
            "--option-id", "approve-secret-binding")[0], 0)
        self.assertEqual(self.invoke("secrets", "verify")[0], 6)
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("patch.json", {"schema_version": 1, "expected": {}, "changes": {"A": "sentinel-private"}, "approved_deletions": []})
        code, applied = self.invoke("secrets", "apply", "--patch-file", str(self.root / "private/patch.json"))
        self.assertEqual(code, 0)
        self.assertNotIn("sentinel-private", json.dumps(applied))
        self.assertEqual(self.invoke("secrets", "verify")[0], 0)

    def test_A63_resume_requires_local_material_in_new_managed_checkout(self):
        _, proposal = self.invoke("secrets", "configure", "--secrets-mode", "local-env")
        self.invoke("decision", "apply", "--decision-id", proposal["decision"]["decision_id"], "--option-id", "approve-secret-binding")
        resumed = self.runtime.resume()
        self.assertEqual(resumed["status"], "not_ready")
        self.assertEqual(resumed["secret_availability"]["required_missing"], ["A"])

    def test_local_pem_private_patch_and_explicit_deletion(self):
        self.manifest.write_text(json.dumps({"schema_version": 1, "keys": {
            "CERT": {"classification": "file-backed", "required": True, "file": "keys/cert.pem"}}}))
        _, proposal = self.invoke("secrets", "configure", "--secrets-mode", "local-env")
        self.assertEqual(self.invoke("decision", "apply", "--decision-id", proposal["decision"]["decision_id"],
            "--option-id", "approve-secret-binding")[0], 0)
        pem = "-----BEGIN CERTIFICATE-----\ncHJpdmF0ZS10ZXN0\n-----END CERTIFICATE-----\n"
        private_patch = {"schema_version": 1, "expected": {}, "changes": {}, "approved_deletions": [],
                         "expected_files": {"CERT": None}, "file_changes": {"CERT": pem}}
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("patch.json", private_patch)
        code, applied = self.invoke("secrets", "apply", "--patch-file", str(self.root / "private/patch.json"))
        self.assertEqual(code, 0)
        self.assertFalse(applied["secrets_shared"])
        self.assertNotIn(pem, json.dumps(applied))
        material = self.checkout / "keys/cert.pem"
        self.assertEqual(material.read_text(), pem)
        self.assertEqual(material.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("cHJpdmF0ZS10ZXN0", (self.checkout / ".env").read_text())
        self.assertEqual(self.invoke("secrets", "verify")[0], 0)
        private_patch.update(expected={"CERT": "keys/cert.pem"}, expected_files={"CERT": pem}, file_changes={"CERT": None})
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("patch.json", private_patch)
        self.assertEqual(self.invoke("secrets", "apply", "--patch-file", str(self.root / "private/patch.json"))[0], 3)
        self.assertEqual(material.read_text(), pem)
        private_patch["approved_deletions"] = ["CERT"]
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("patch.json", private_patch)
        self.assertEqual(self.invoke("secrets", "apply", "--patch-file", str(self.root / "private/patch.json"))[0], 0)
        self.assertFalse(material.exists())
        self.assertEqual(self.invoke("secrets", "verify")[0], 6)

    def test_corrupt_private_decision_rejected_before_secret_service_construction(self):
        _, proposal = self.invoke("secrets", "configure", "--secrets-mode", "local-env")
        decision_id = proposal["decision"]["decision_id"]
        name = "secret-decision-" + decision_id + ".json"
        original = self.runtime.store.read(name)
        self.runtime.store.write(name, dict(original, kind="private-sentinel"))
        with patch("agent_handoff.cli.secret_service") as service:
            code, rejected = self.invoke("decision", "apply", "--decision-id", decision_id, "--option-id", "approve-secret-binding")
            service.assert_not_called()
        self.assertEqual(code, 2)
        self.assertNotIn("private-sentinel", json.dumps(rejected))
        self.runtime.store.write(name, original)
        self.assertEqual(self.invoke("decision", "apply", "--decision-id", decision_id, "--option-id", "approve-secret-binding")[0], 0)

    def test_invalid_private_patch_errors_hide_values_and_tracebacks(self):
        _, proposal = self.invoke("secrets", "configure", "--secrets-mode", "local-env")
        self.invoke("decision", "apply", "--decision-id", proposal["decision"]["decision_id"], "--option-id", "approve-secret-binding")
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("patch.json", {"schema_version": 1, "expected": {}, "changes": {"UNKNOWN": "sentinel-never-public"}, "approved_deletions": []})
        code, rejected = self.invoke("secrets", "apply", "--patch-file", str(self.root / "private/patch.json"))
        self.assertEqual(code, 3)
        self.assertNotIn("sentinel-never-public", json.dumps(rejected))
