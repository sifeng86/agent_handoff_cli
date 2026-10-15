import json
import unittest
from pathlib import Path
import tempfile

from agent_handoff.contracts import RuntimeFailure
from agent_handoff.secrets.dotenv import parse, patch
from agent_handoff.secrets.manifest import parse as parse_manifest, shared_scope
from agent_handoff.secrets.reconcile import reconcile
from agent_handoff.secrets.local_env import LocalEnvironment
from agent_handoff.paths import PrivateStore


class DotenvTests(unittest.TestCase):
    def test_quotes_multiline_empty_and_crlf_round_trip(self):
        raw = '# keep\r\nexport EMPTY=\r\nSINGLE=\'one\ntwo\'\r\nDOUBLE="line\\nnext\\tend" # comment\r\nLOCAL=value # keep local\r\n'
        document = parse(raw)
        self.assertEqual(document.values["EMPTY"], "")
        self.assertEqual(document.values["SINGLE"], "one\ntwo")
        self.assertEqual(document.values["DOUBLE"], "line\nnext\tend")
        updated = patch(document, {"DOUBLE": "new\nvalue", "ADD": "a # b"})
        self.assertIn("# keep\r\n", updated)
        self.assertIn("LOCAL=value # keep local\r\n", updated)
        self.assertIn(" # comment\r\n", updated)
        self.assertEqual(parse(updated).values["DOUBLE"], "new\nvalue")
        self.assertEqual(parse(updated).values["ADD"], "a # b")

    def test_duplicate_expansion_and_unsupported_escape_rejected(self):
        for raw in ('A=one\nA=two\n', 'A=${OTHER}\n', 'A=$(command)\n', 'A="bad\\q"\n', 'A="unterminated', 'invalid shell command\n'):
            with self.subTest(case=raw), self.assertRaises(RuntimeFailure):
                parse(raw)

    def test_patch_only_selected_key_or_approved_deletion(self):
        document = parse("# comment\nA=value\nLOCAL=private\n")
        updated = patch(document, {"A": None})
        self.assertEqual(updated, "# comment\nLOCAL=private\n")
        self.assertNotIn("private", repr(document))

    def test_empty_value_with_comment_is_distinct_from_absent(self):
        document = parse("EMPTY= # retain comment\nHASH=#literal\n")
        self.assertEqual(document.values, {"EMPTY": "", "HASH": "#literal"})
        updated = patch(document, {"EMPTY": "new"})
        self.assertIn("# retain comment", updated)
        self.assertEqual(parse(updated).values["EMPTY"], "new")


class ReconciliationTests(unittest.TestCase):
    def reconcile(self, baseline, local, remote, scope=None, allow=True):
        return reconcile(baseline, local, remote, scope or {"A", "B"}, allow_upload=allow)

    def test_A41_A42_bootstrap_download_or_converge(self):
        plan = self.reconcile(None, {}, {"A": "remote"})
        self.assertEqual(plan.downloads, {"A": "remote"})
        self.assertEqual(plan.baseline, {"A": "remote"})
        self.assertFalse(self.reconcile(None, {"A": "same"}, {"A": "same"}).conflicts)

    def test_A43_initial_conflict_never_mutates(self):
        plan = self.reconcile(None, {"A": "left", "B": "new"}, {"A": "right"})
        self.assertEqual(plan.conflicts, {"A": "secret_conflict"})
        self.assertEqual((plan.uploads, plan.downloads, plan.baseline), ({}, {}, {}))

    def test_A44_A45_A46_different_keys_merge(self):
        plan = self.reconcile({"A": "base", "B": "base"}, {"A": "local", "B": "base"}, {"A": "base", "B": "remote"})
        self.assertEqual(plan.uploads, {"A": "local"})
        self.assertEqual(plan.downloads, {"B": "remote"})
        self.assertEqual(plan.baseline, {"A": "local", "B": "remote"})

    def test_A47_conflict_public_report_contains_no_values_or_hashes(self):
        sentinel = "private-sentinel-never-public"
        plan = self.reconcile({"A": "base"}, {"A": sentinel}, {"A": "remote"})
        self.assertNotIn(sentinel, json.dumps(plan.public()))
        self.assertNotIn(sentinel, repr(plan))
        self.assertEqual(plan.public()["status"], "decision_required")

    def test_A48_A49_deletion_requires_decision_on_either_side(self):
        for local, remote in (({}, {"A": "base"}), ({"A": "base"}, {}), ({}, {})):
            plan = self.reconcile({"A": "base"}, local, remote)
            self.assertEqual(plan.conflicts, {"A": "secret_deletion"})
            self.assertFalse(plan.uploads or plan.downloads or plan.baseline)

    def test_unknown_local_only_and_ungranted_upload_excluded(self):
        plan = self.reconcile(None, {"A": "new", "UNKNOWN": "private"}, {}, allow=False)
        self.assertEqual(plan.conflicts, {"A": "scope_grant_required"})
        self.assertNotIn("UNKNOWN", json.dumps(plan.public()))
        manifest = parse_manifest('{"schema_version":1,"keys":{"A":{"classification":"shared","required":true},"LOCAL":{"classification":"local-only","required":false}}}')
        self.assertEqual(shared_scope(manifest), {"A"})


class ManifestMappingTests(unittest.TestCase):
    def manifest(self, *paths):
        return json.dumps({"schema_version": 1, "keys": {
            "KEY_" + str(index): {"classification": "file-backed", "required": True, "file": path}
            for index, path in enumerate(paths)}})

    def test_reject_noncanonical_and_protected_file_mappings(self):
        for path in ["/absolute.pem", "../outside.pem", "keys/./cert.pem", "keys//cert.pem", "keys/",
                     "C:/cert.pem", "keys\\cert.pem", "keys/\x00cert.pem", ".git/config",
                     ".GiT/hooks/pre-commit", ".agent/state.json", ".env", "keys/.env"]:
            with self.subTest(path=path), self.assertRaises(RuntimeFailure):
                parse_manifest(self.manifest(path))

    def test_reject_alias_and_file_directory_overlap(self):
        for paths in [("keys/cert.pem", "keys/cert.pem"), ("keys/cert.pem", "KEYS/CERT.PEM"),
                      ("keys/cert", "keys/cert/private.pem")]:
            with self.subTest(paths=paths), self.assertRaises(RuntimeFailure):
                parse_manifest(self.manifest(*paths))
        self.assertEqual(shared_scope(parse_manifest(self.manifest("keys/cert.pem", "keys/key.pem"))),
                         {"KEY_0", "KEY_1"})


class LocalEnvironmentTests(unittest.TestCase):
    def test_file_backed_availability_rejects_hardlink_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "repo"
            checkout.mkdir()
            env = checkout / ".env"
            env.write_text('CERT="cert.pem"\n')
            env.chmod(0o600)
            certificate = checkout / "cert.pem"
            certificate.write_text("-----BEGIN CERTIFICATE-----\nYQ==\n-----END CERTIFICATE-----\n")
            certificate.chmod(0o600)
            manifest = parse_manifest('{"schema_version":1,"keys":{"CERT":{"classification":"file-backed","required":true,"file":"cert.pem"}}}')
            with PrivateStore(root / "private", checkout) as store:
                environment = LocalEnvironment(env, store)
                self.assertEqual(environment.inspect(manifest)["available_keys"], ["CERT"])
                (checkout / "alias.pem").hardlink_to(certificate)
                report = environment.inspect(manifest)
                self.assertEqual(report["required_missing"], ["CERT"])
                self.assertNotIn("private-test-material", json.dumps(report))

    def test_private_patch_preserves_comments_local_only_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "repo"
            checkout.mkdir()
            env = checkout / ".env"
            env.write_text("# retain\nA=old\nLOCAL=private\n")
            env.chmod(0o600)
            with PrivateStore(root / "private", checkout) as store:
                environment = LocalEnvironment(env, store)
                actual = environment.apply({"A": "new"}, expected={"A": "old", "LOCAL": "private"}, scope={"A"})
                self.assertEqual(actual["secrets_shared"], False)
                self.assertIn("LOCAL=private\n", env.read_text())
                self.assertEqual(env.stat().st_mode & 0o777, 0o600)
                backup = store.read("secret-backup-" + actual["preservation_handle"] + ".json")
                self.assertEqual(backup["raw"], "# retain\nA=old\nLOCAL=private\n")

    def test_stale_private_patch_and_editor_write_cannot_replace_observed_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "repo"
            checkout.mkdir()
            env = checkout / ".env"
            env.write_text("A=old\n")
            env.chmod(0o600)
            with PrivateStore(root / "private", checkout) as store:
                environment = LocalEnvironment(env, store)
                with self.assertRaises(RuntimeFailure):
                    environment.apply({"A": "new"}, expected={"A": "stale"}, scope={"A"})
                def edited(_):
                    env.write_text("A=editor\n")
                with self.assertRaises(RuntimeFailure):
                    environment.apply({"A": "new"}, expected={"A": "old"}, scope={"A"}, fault=edited)
                self.assertEqual(env.read_text(), "A=editor\n")

    def test_required_missing_report_never_contains_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "repo"
            checkout.mkdir()
            env = checkout / ".env"
            env.write_text("A=sentinel-value\n")
            env.chmod(0o600)
            manifest = parse_manifest('{"schema_version":1,"keys":{"A":{"classification":"shared","required":true},"B":{"classification":"shared","required":true}}}')
            with PrivateStore(root / "private", checkout) as store:
                report = LocalEnvironment(env, store).inspect(manifest)
                self.assertEqual(report["required_missing"], ["B"])
                self.assertNotIn("sentinel-value", json.dumps(report))
