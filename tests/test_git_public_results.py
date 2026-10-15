"""Publication output evidence and safe serializer failure after real effects."""
import contextlib
import io
import json
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.paths import PrivateStore
from agent_handoff.public_results import validate_git_result
from agent_handoff.publication import Runtime
from test_publication import RepositoryFixture
from test_record_schemas import validator


class GitPublicTests(RepositoryFixture):
    def test_real_lifecycle_reports_match_contract_and_proof_requirements(self):
        acquired = self.runtime.acquire()
        recovered = self.runtime.recover()
        checkpoint = self.runtime.publish_snapshot("checkpoint", ["code.txt"], summary="Fixture report")
        released = self.runtime.release()
        for value in (acquired, recovered, checkpoint, released):
            validate_git_result(value)
            validator("git-result").validate(value)
            validator("result").validate(value)
            for changes in ({"raw": "private-sentinel"}, {"schema_version": True}):
                invalid = dict(value, **changes)
                self.assertFalse(validator("git-result").is_valid(invalid))
                with self.assertRaises(RuntimeFailure):
                    validate_git_result(invalid)
        for changes in ({"operation_id": None}, {"published_revision": None}):
            invalid = dict(acquired, **changes)
            self.assertFalse(validator("git-result").is_valid(invalid))
            with self.assertRaises(RuntimeFailure):
                validate_git_result(invalid)
        with self.assertRaises(RuntimeFailure):
            validate_git_result(dict(checkpoint, test_status="passed"))
        self.assertFalse(validator("git-result").is_valid(dict(checkpoint, test_status="passed")))
        with self.assertRaises(RuntimeFailure):
            validate_git_result(dict(checkpoint, secrets_shared=True))
        self.assertFalse(validator("git-result").is_valid(dict(checkpoint, secrets_shared=True)))
        invalid_recover = dict(recovered, code_publication="published", published_revision="a" * 40, operation_id="b" * 32)
        with self.assertRaises(RuntimeFailure):
            validate_git_result(invalid_recover)
        self.assertFalse(validator("git-result").is_valid(invalid_recover))
        incomplete = dict(acquired, code_publication="not_attempted", operation_id=None, published_revision=None, ownership="unknown")
        self.assertFalse(validator("git-result").is_valid(incomplete))
        with self.assertRaises(RuntimeFailure):
            validate_git_result(incomplete)

    def test_handoff_secret_summary_excludes_values(self):
        self.runtime.acquire()
        value = self.runtime.publish_snapshot("handoff", ["code.txt"], summary="Fixture handoff")
        value["secrets_result"] = {"status": "unknown", "next_action": "Resolve separate secret evidence."}
        validator("git-result").validate(value)
        validate_git_result(value)
        value["secrets_result"]["raw_base64"] = "private-sentinel"
        self.assertFalse(validator("git-result").is_valid(value))
        with self.assertRaises(RuntimeFailure):
            validate_git_result(value)

    def test_post_effect_payload_mutation_returns_publication_unknown(self):
        with PrivateStore(self.root / "private", self.checkout) as store:
            store.write("context.json", {"schema_version": 1, "context": self.context})
        original = Runtime.acquire
        def unsafe(runtime):
            value = original(runtime)
            value.update(raw_base64="private-sentinel", command="inspect")
            return value
        output = io.StringIO()
        with patch.object(Runtime, "acquire", unsafe), contextlib.redirect_stdout(output):
            code = main(["acquire", "--checkout", str(self.checkout), "--private-root", str(self.root / "private"),
                "--authority", str(self.remote), "--publication-ref", self.authority.publication_ref,
                "--context-file", str(self.root / "private/context.json")])
        self.assertEqual(code, 7)
        self.assertNotIn("private-sentinel", output.getvalue())
        value = json.loads(output.getvalue())
        self.assertEqual(value["code_publication"], "publication_unknown")
        self.assertEqual(value["ownership"], "ownership_pending")
        validator("git-result").validate(value)
        self.assertEqual(self.runtime.resolve(value["operation_id"])["code_publication"], "published")
