"""Resume readiness requires materialization and current capability evidence."""
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.public_results import validate_resume_result
from test_publication import RepositoryFixture
from test_record_schemas import validator


class ResumePublicTests(RepositoryFixture):
    def test_ready_resume_schema_and_invalid_ready_claims(self):
        value = self.runtime.resume()
        self.assertEqual(value["status"], "ready")
        validate_resume_result(value)
        validator("resume-result").validate(value)
        validator("result").validate(value)
        for changes in ({"ownership": "fenced"}, {"raw": "private-sentinel"}, {"secrets_shared": True},
                        {"managed_checkout": "relative"}, {"observed_revision": "bad"}, {"code_publication": "publication_unknown"}):
            invalid = dict(value, **changes)
            self.assertFalse(validator("resume-result").is_valid(invalid))
            with self.assertRaises(RuntimeFailure):
                validate_resume_result(invalid)
        invalid = dict(value)
        invalid.pop("preservation_handle")
        self.assertFalse(validator("resume-result").is_valid(invalid))
        with self.assertRaises(RuntimeFailure):
            validate_resume_result(invalid)
        invalid = dict(value, secret_availability={"status": "not_ready", "required_missing": ["A"]})
        self.assertFalse(validator("resume-result").is_valid(invalid))
        with self.assertRaises(RuntimeFailure):
            validate_resume_result(invalid)

    def test_local_work_preserved_not_ready_has_no_false_materialization_claim(self):
        (self.checkout / "code.txt").write_text("latest-local-work")
        value = self.runtime.resume()
        self.assertEqual(value["status"], "not_ready")
        validator("resume-result").validate(value)
        validate_resume_result(value)
        self.assertEqual((self.checkout / "code.txt").read_text(), "latest-local-work")

    def test_takeover_offer_from_resume_remains_scoped_decision(self):
        self.runtime.acquire()
        with self.new_runtime("other-private", "b" * 64) as other:
            other.initialize(self.base, mainline_ref="refs/heads/main")
            value = other.resume()
            self.assertEqual(value["status"], "decision_required")
            validator("resume-result").validate(value)
            validate_resume_result(value)
            self.assertEqual(value["decision"]["category"], "takeover")
