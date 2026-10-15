import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_handoff.cli import main
from agent_handoff.contracts import RuntimeFailure, parse_record
from agent_handoff.git_backend import GitBackend, inspect, validate_destination
from agent_handoff.paths import PrivateStore


class ContractTests(unittest.TestCase):
    def test_terminal_git_rejection_is_distinct_from_transport_failure(self):
        backend = object.__new__(GitBackend)
        backend.ref = "refs/heads/publication"
        backend.destination = "/unused.git"
        oid = "a" * 40
        for stdout, expected in [
            (f"!\t{oid}:refs/heads/publication\t[remote rejected] (failed to update ref)\n".encode(), "rejected"),
            (f"!\t{oid}:refs/heads/publication\t[rejected] (stale info)\n".encode(), "rejected"),
            (b"!\tcommit:refs/heads/publication\t[remote rejected] (hook declined)\n", "publication_unknown"),
            (b"", "publication_unknown"),
        ]:
            with self.subTest(expected=expected, response=stdout):
                response = subprocess.CompletedProcess([], 1, stdout, b"private transport response")
                with patch.object(backend, "run", return_value=response):
                    self.assertEqual(backend.push(oid, "b" * 40), expected)

    def test_explicit_scp_authority_is_preserved_without_alias_inference(self):
        destination = "git@github.com:sifeng86/agent_handoff_cli.git"
        self.assertEqual(validate_destination(destination), destination)
        with self.assertRaises(RuntimeFailure):
            validate_destination("git:password@github.com:owner/repo.git")

    def test_template_records(self):
        for kind in ("protocol", "owner", "state"):
            parse_record(Path(f"template/.agent/{kind}.json").read_text(), kind)

    def test_invalid_records(self):
        cases = [('{"schema_version":1,"schema_version":1,"owner":null}', "owner"),
                 ('{"schema_version":true,"owner":null}', "owner"),
                 ('{"schema_version":1,"owner":null,"extra":0}', "owner"),
                 ('{"schema_version":1,"owner":{}}', "owner"),
                 ('{"schema_version":1,"owner":NaN}', "owner")]
        for raw, kind in cases:
            with self.subTest(raw=raw), self.assertRaises(RuntimeFailure):
                parse_record(raw, kind)

    def test_credentials_and_helpers_rejected(self):
        for url in ("https://token@example.com/repo", "ssh://git:password@example.com/repo",
                    "ext::dangerous", "-option", "https://example.com/repo?token=private",
                    "https://[invalid/repo", "https://example.com:invalid/repo"):
            with self.subTest(url=url), self.assertRaises(RuntimeFailure):
                validate_destination(url)

    def test_schema_assets_and_required_fields_match(self):
        for kind in ("protocol", "owner", "state"):
            schema = json.loads(Path(f"schemas/{kind}.schema.json").read_text())
            fixture = json.loads(Path(f"template/.agent/{kind}.json").read_text())
            self.assertEqual(set(schema["required"]), set(fixture))
            for field in schema["required"]:
                invalid = dict(fixture)
                del invalid[field]
                with self.subTest(kind=kind, field=field), self.assertRaises(RuntimeFailure):
                    parse_record(json.dumps(invalid), kind)

    def test_mutation_requires_explicit_authority_and_is_json_only(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["acquire"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["code_publication"], "not_attempted")


class PrivateTests(unittest.TestCase):
    def test_durable_private_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = PrivateStore(base / "private", base / "repo")
            store.write("journal.json", {"schema_version": 1})
            self.assertEqual((base / "private/journal.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((base / "private/journal.json").read_text()), {"schema_version": 1})
            with self.assertRaises(RuntimeFailure):
                store.write("../escape", {})

    def test_symlink_and_repository_root_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            with self.assertRaises(RuntimeFailure):
                PrivateStore(repo / "private", repo)
            (base / "link").symlink_to(repo, target_is_directory=True)
            with self.assertRaises(RuntimeFailure):
                PrivateStore(base / "link/private", base / "other")

    def test_insecure_root_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            private = base / "private"
            private.mkdir(mode=0o755)
            private.chmod(0o755)
            with self.assertRaises(RuntimeFailure):
                PrivateStore(private, base / "repo")


class GitTests(unittest.TestCase):
    def test_real_git_pinned_inspection_preserves_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            def git(*args):
                env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
                env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
                return subprocess.check_output(["git", "-C", str(repo), *args], env=env, stderr=subprocess.PIPE, text=True).strip()
            git("init", "-q", "-b", "publication")
            (repo / ".agent").mkdir()
            for kind in ("protocol", "owner", "state"):
                (repo / f".agent/{kind}.json").write_text(Path(f"template/.agent/{kind}.json").read_text())
            (repo / ".agent/config.yaml").write_text(json.dumps({"schema_version": 1, "authority": str(repo),
                "publication_ref": "refs/heads/publication", "mainline_ref": "refs/heads/main", "plan_directory": "plans"}))
            git("add", ".agent")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
            oid = git("rev-parse", "HEAD")
            (repo / "dirty.txt").write_text("keep me")
            before = git("status", "--porcelain")
            result = inspect(str(repo), "refs/heads/publication")
            self.assertEqual(result["observed_revision"], oid)
            self.assertFalse(result["owner_present"])
            self.assertEqual(git("status", "--porcelain"), before)
            with self.assertRaises(RuntimeFailure):
                inspect(str(repo), "refs/heads/missing")
            (repo / ".agent/owner.json").write_text('{"schema_version":1,"owner":{}}')
            git("add", ".agent/owner.json")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "invalid owner")
            with self.assertRaises(RuntimeFailure):
                inspect(str(repo), "refs/heads/publication")


if __name__ == "__main__":
    unittest.main()
