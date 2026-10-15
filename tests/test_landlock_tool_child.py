import json
import os
from pathlib import Path
import fcntl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import landlock_tool_child


RUNNER = Path(landlock_tool_child.__file__).resolve()


class LandlockToolChildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.public = self.work / "public"
        self.private = self.root / "private"
        self.work.mkdir()
        self.public.mkdir()
        self.private.mkdir(mode=0o700)
        self.public_file = self.public / "approved.txt"
        self.public_file.write_text("PUBLIC_MARKER_A63\n")
        self.secret = "SYNTHETIC_PRIVATE_VALUE_9e18604"
        self.context = self.private / "context.json"
        self.context.write_text(self.secret)
        self.context.chmod(0o600)
        self.alias = self.public / "alias.json"
        self.alias.symlink_to(self.context)
        self.env_file = self.public / ".env"
        self.env_file.write_text(self.secret)
        self.policy_file = self.private / "policy.json"
        self.write_policy()

    def write_policy(self, **overrides):
        value = {
            "version": 1,
            "cwd": str(self.work),
            "read_files": [str(self.public_file)],
            "list_dirs": [str(self.work), str(self.public)],
            "protected_paths": [str(self.private)],
        }
        value.update(overrides)
        raw = json.dumps(value, separators=(",", ":")).encode()
        fd = os.open(self.policy_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)

    def invoke(self, *command, env=None, pass_fds=()):
        child_env = dict(os.environ if env is None else env)
        return subprocess.run([sys.executable, str(RUNNER), "--policy-file", str(self.policy_file), "--",
                               *map(str, command)], cwd=self.root, env=child_env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                              close_fds=True, pass_fds=tuple(pass_fds), check=False)

    def test_approved_file_reads_and_environment_is_replaced(self):
        code = "import os,sys; print(open(sys.argv[1]).read().strip()); print(os.getenv('LL_PRIVATE_ENV','')); print(os.environ['HOME']); print(os.environ['TMPDIR'])"
        env = dict(os.environ, LL_PRIVATE_ENV=self.secret)
        result = self.invoke(sys.executable, "-c", code, self.public_file, env=env)
        self.assertEqual(result.returncode, 0)
        lines = result.stdout.decode().splitlines()
        self.assertEqual(lines[:2], ["PUBLIC_MARKER_A63", ""])
        self.assertEqual(lines[2:], [str(self.work), str(self.work)])
        self.assertNotIn(self.secret.encode(), result.stdout + result.stderr)

    def test_context_file_shell_symlink_and_listed_directory_do_not_grant_file_reads(self):
        results = [
            self.invoke("/bin/sh", "-c", 'cat "$1"', "sh", self.context),
            self.invoke("/bin/sh", "-c", 'cat "$1"', "sh", self.policy_file),
            self.invoke("/bin/sh", "-c", 'cat "$1"', "sh", self.alias),
            self.invoke("/bin/sh", "-c", 'cat "$1"', "sh", self.env_file),
        ]
        for result in results:
            with self.subTest(returncode=result.returncode):
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(self.secret.encode(), result.stdout + result.stderr)

    def test_denied_writes_rename_and_hardlinks_leave_public_directory_unchanged(self):
        code = (
            "import os,sys; d=sys.argv[0]; f=sys.argv[1]; "
            "ops=[lambda:open(d+'/new','w').write('x'),lambda:os.rename(f,d+'/moved'),"
            "lambda:os.link(f,d+'/linked')]; "
            "names=['write','rename','hardlink'];\n"
            "for n,op in zip(names,ops):\n"
            " try: op(); print('ALLOWED-'+n)\n"
            " except OSError: print('BLOCKED-'+n)"
        )
        result = self.invoke(sys.executable, "-c", code, self.public, self.public_file)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode().splitlines(),
                         ["BLOCKED-write", "BLOCKED-rename", "BLOCKED-hardlink"])
        self.assertFalse((self.public / "new").exists())
        self.assertFalse((self.public / "moved").exists())
        self.assertFalse((self.public / "linked").exists())

    def test_proc_parent_environment_and_passed_private_fd_are_not_reachable(self):
        fd = os.open(self.context, os.O_RDONLY)
        try:
            code = (
                "import os,sys; "
                "paths=[f'/proc/{os.getppid()}/environ',f'/proc/self/fd/{sys.argv[0]}'];\n"
                "for p in paths:\n"
                " try: data=open(p,'rb').read()\n"
                " except OSError: print('BLOCKED')\n"
                " else: print(data.decode(errors='ignore'))"
            )
            env = dict(os.environ, LL_PRIVATE_ENV=self.secret)
            result = self.invoke(sys.executable, "-c", code, str(fd), env=env, pass_fds=(fd,))
        finally:
            os.close(fd)
        self.assertEqual(result.stdout.decode().splitlines(), ["BLOCKED", "BLOCKED"])
        self.assertNotIn(self.secret.encode(), result.stdout + result.stderr)
        self.assertNotIn(self.context.read_bytes(), result.stdout + result.stderr)

    def test_high_inherited_fd_is_closed_after_child_lowers_nofile_limit(self):
        try:
            fcntl.fcntl(64, fcntl.F_GETFD)
        except OSError:
            pass
        else:
            self.skipTest("descriptor 64 is already occupied in the test process")
        source_fd = os.open(self.context, os.O_RDONLY)
        try:
            os.dup2(source_fd, 64, inheritable=True)
            target = (
                "import errno,os\ntry:\n os.read(64,4096); print('FD_LEAK')\n"
                "except OSError as e:\n print('FD_CLOSED' if e.errno==errno.EBADF else 'FD_ERROR')"
            )
            bootstrap = (
                f"import resource,runpy,sys; resource.setrlimit(resource.RLIMIT_NOFILE,(16,16)); "
                f"runner,policy=sys.argv[1:3]; sys.argv=[runner,'--policy-file',policy,'--',"
                f"'/usr/bin/python3','-c',{target!r}]; runpy.run_path(runner,run_name='__main__')"
            )
            child_env = dict(os.environ, LL_PRIVATE_ENV=self.secret)
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", bootstrap, str(RUNNER), str(self.policy_file)],
                cwd=self.root, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=20, close_fds=True, pass_fds=(64,), check=False,
            )
        finally:
            try:
                os.close(64)
            except OSError:
                pass
            os.close(source_fd)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout.decode().strip(), "FD_CLOSED")
        self.assertNotIn(self.secret.encode(), result.stdout + result.stderr)

    def test_restrictions_are_inherited_by_grandchild_exec(self):
        nested = "import sys; print('LEAK' if __import__('pathlib').Path(sys.argv[1]).read_text() else 'EMPTY')"
        outer = (
            "import subprocess,sys; p=subprocess.run([sys.executable,'-c',sys.argv[0],sys.argv[1]],"
            "capture_output=True,text=True); print('BLOCKED' if p.returncode else p.stdout.strip())"
        )
        result = self.invoke(sys.executable, "-c", outer, nested, self.context)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode().strip(), "BLOCKED")
        self.assertNotIn(self.secret.encode(), result.stdout + result.stderr)

    def test_policy_rejects_duplicate_unknown_alias_and_protected_ancestor(self):
        sentinel = self.root / "should-not-run"
        code = "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('ran')"
        invalid_policies = [
            b'{"version":1,"version":1}',
            None,
        ]
        for raw in invalid_policies:
            if raw is None:
                value = {"version": 1, "cwd": str(self.work), "read_files": [], "list_dirs": [],
                         "protected_paths": [str(self.private)], "extra": True}
                raw = json.dumps(value).encode()
            self.policy_file.write_bytes(raw)
            self.policy_file.chmod(0o600)
            result = self.invoke(sys.executable, "-c", code, sentinel)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(sentinel.exists())

        self.write_policy(read_files=[str(self.alias)])
        result = self.invoke(sys.executable, "-c", code, sentinel)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(sentinel.exists())

        self.write_policy(list_dirs=[str(self.root)])
        result = self.invoke(sys.executable, "-c", code, sentinel)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(sentinel.exists())

    def test_policy_restriction_failure_never_executes_target(self):
        sentinel = self.root / "sentinel"
        code = "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('ran')"
        with patch.object(landlock_tool_child, "_apply_landlock", side_effect=OSError("simulated")), \
                patch.object(landlock_tool_child.os, "chdir"), \
                patch.object(landlock_tool_child.os, "execvpe") as execute:
            with self.assertRaises(OSError):
                landlock_tool_child.run_child(str(self.policy_file), [sys.executable, "-c", code, str(sentinel)])
        execute.assert_not_called()
        self.assertFalse(sentinel.exists())

    def test_non_linux_platform_fails_closed_before_exec(self):
        with patch.object(landlock_tool_child.sys, "platform", "darwin"), \
                patch.object(landlock_tool_child.os, "chdir"), \
                patch.object(landlock_tool_child.os, "execvpe") as execute:
            with self.assertRaises(OSError):
                landlock_tool_child.run_child(str(self.policy_file), [sys.executable, "-c", "pass"])
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
