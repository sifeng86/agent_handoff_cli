from unittest.mock import patch

from agent_handoff.secrets.service import SecretService
from test_publication import RepositoryFixture
from test_secret_service import VerifiedFake


class PartialHandoffTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        self.runtime.acquire()
        self.adapter = VerifiedFake(self.runtime.sessions.current()["session_id"])
        self.service = SecretService(self.runtime, {"schema_version": 1, "keys": {"A": {"classification": "shared", "required": True}}}, self.adapter)
        proposal = self.service.propose_binding("doppler", project=self.adapter.project, config=self.adapter.config)
        self.service.apply_decision(proposal["decision"]["decision_id"], "approve-secret-binding")

    def test_A61_code_handoff_published_despite_secret_read_failure(self):
        with patch.object(self.adapter, "read", side_effect=OSError("sentinel-private-error")):
            published = self.runtime.publish_snapshot("handoff", ["code.txt"], summary="Code saved", secret_service=self.service)
        self.assertEqual(published["code_publication"], "published")
        self.assertEqual(published["ownership"], "released")
        self.assertIs(published["secrets_shared"], False)
        self.assertNotIn("sentinel-private-error", str(published))

    def test_A62_secret_convergence_success_git_handoff_unknown_separate_results(self):
        self.adapter.values = {"A": "private-remote"}
        with patch.object(self.runtime.backend, "push", return_value="publication_unknown"):
            unknown = self.runtime.publish_snapshot("handoff", ["code.txt"], summary="Code saved", secret_service=self.service)
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(unknown["code_publication"], "publication_unknown")
        self.assertIs(unknown["secrets_shared"], True)
        self.assertEqual(unknown["ownership"], "ownership_pending")

    def test_snapshot_is_fixed_before_secret_side_effect_editor_change_retained(self):
        self.adapter.values = {"A": "private-remote"}
        def editor(point):
            if point == "before_baseline_write":
                (self.checkout / "code.txt").write_text("editor change after snapshot")
        self.runtime.fault = editor
        published = self.runtime.publish_snapshot("handoff", ["code.txt"], summary="Snapshot saved", secret_service=self.service)
        head = published["published_revision"]
        self.assertEqual(self.git(self.remote, "show", head + ":code.txt"), "original")
        self.assertEqual((self.checkout / "code.txt").read_text(), "editor change after snapshot")
