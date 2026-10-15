"""Isolated, unauthenticated, readonly Doppler CLI fixture for A35 evidence."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


PROJECT = "agent-handoff-test"
CONFIG = "dev"
IDENTITY_KEYS = {"DOPPLER_PROJECT", "DOPPLER_CONFIG"}
KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
AUTH_FAILURE = re.compile(
    r"must provide (?:a )?token|missing (?:doppler )?token|no (?:valid )?token (?:was )?provided",
    re.IGNORECASE,
)


class FixtureEvidenceError(RuntimeError):
    """Private fixture evidence did not satisfy its safety contract."""


@dataclass(frozen=True)
class Observation:
    command: str
    returncode: int
    no_credentials_classified: bool
    request_scope: tuple[str, ...]
    environment_credentials_removed: bool
    no_remote_write: bool = True

    def public(self):
        return {
            "command": self.command,
            "returncode": self.returncode,
            "no_credentials_classified": self.no_credentials_classified,
            "request_scope": list(self.request_scope),
            "environment_credentials_removed": self.environment_credentials_removed,
            "no_remote_write": self.no_remote_write,
        }


def resolve_doppler(executable=None):
    candidate = str(executable) if executable is not None else shutil.which("doppler")
    if not candidate:
        raise FixtureEvidenceError("doppler_cli_unavailable")
    path = Path(candidate).resolve(strict=True)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise FixtureEvidenceError("doppler_cli_not_executable")
    return path


def snapshot_host_config(path=None):
    """Capture ~/.doppler metadata and bytes privately for before/after equality."""
    root = Path(path) if path is not None else Path.home() / ".doppler"
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return ("missing",)
    if stat.S_ISLNK(root_info.st_mode):
        raise FixtureEvidenceError("host_config_snapshot_unsupported")
    if not stat.S_ISDIR(root_info.st_mode):
        raise FixtureEvidenceError("host_config_snapshot_unsupported")

    result = []
    pending = [(root, "", 0)]
    total_bytes = 0
    while pending:
        directory, relative, depth = pending.pop()
        if depth > 16 or len(result) > 10000:
            raise FixtureEvidenceError("host_config_snapshot_limit")
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            raise FixtureEvidenceError("host_config_snapshot_unavailable") from None
        for entry in entries:
            rel = f"{relative}/{entry.name}" if relative else entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                raise FixtureEvidenceError("host_config_snapshot_unavailable") from None
            common = (rel, info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_size,
                      info.st_mtime_ns, info.st_ctime_ns)
            if stat.S_ISLNK(info.st_mode):
                result.append(("link", common, os.readlink(entry.path)))
            elif stat.S_ISDIR(info.st_mode):
                result.append(("directory", common))
                pending.append((Path(entry.path), rel, depth + 1))
            elif stat.S_ISREG(info.st_mode):
                total_bytes += info.st_size
                if total_bytes > 32 * 1024 * 1024:
                    raise FixtureEvidenceError("host_config_snapshot_limit")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                try:
                    fd = os.open(entry.path, flags)
                    with os.fdopen(fd, "rb") as stream:
                        content = stream.read(32 * 1024 * 1024 + 1)
                except OSError:
                    raise FixtureEvidenceError("host_config_snapshot_unavailable") from None
                result.append(("file", common, content))
            else:
                result.append(("other", common))
    root_metadata = (root_info.st_dev, root_info.st_ino, root_info.st_mode, root_info.st_uid,
                     root_info.st_gid, root_info.st_size, root_info.st_mtime_ns, root_info.st_ctime_ns)
    return ("directory-root", root_metadata, tuple(result))


class DopplerAuthFixture:
    """Runs only fixed readonly commands with an isolated Doppler config dir."""

    def __init__(self, root, *, executable=None, approved_keys=(), host_config=None):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise FixtureEvidenceError("fixture_root_permissions_unsafe")
        self.executable = resolve_doppler(executable)
        keys = set(approved_keys)
        if (not keys or any(not isinstance(key, str) or not KEY_PATTERN.fullmatch(key) or
                            key.startswith("DOPPLER_") for key in keys)):
            raise FixtureEvidenceError("approved_key_scope_invalid")
        self.approved_keys = frozenset(keys)
        self.host_config = Path(host_config) if host_config is not None else Path.home() / ".doppler"
        self.host_before = snapshot_host_config(self.host_config)
        self.config_dir = Path(tempfile.mkdtemp(prefix="doppler-isolated-", dir=self.root))
        self.config_dir.chmod(0o700)
        self.log_dir = self.root / "doppler-private-logs"
        self.log_dir.mkdir(mode=0o700)
        self._sequence = 0
        self._started_empty = not any(self.config_dir.iterdir())
        if not self._started_empty:
            raise FixtureEvidenceError("isolated_config_not_empty")

    def _environment(self):
        return {key: value for key, value in os.environ.items() if not key.startswith("DOPPLER_")}

    def _snapshot_check(self):
        if snapshot_host_config(self.host_config) != self.host_before:
            raise FixtureEvidenceError("host_doppler_config_changed")

    def _run(self, tail, *, command, scope=()):
        self._snapshot_check()
        self._sequence += 1
        stdout_path = self.log_dir / f"{self._sequence:03d}-stdout.bin"
        stderr_path = self.log_dir / f"{self._sequence:03d}-stderr.bin"
        argv = [str(self.executable), "--config-dir", str(self.config_dir), "--no-read-env",
                "--no-check-version", "--attempts", "1", "--timeout", "10s", *tail]
        try:
            process = subprocess.run(argv, env=self._environment(), cwd=self.root,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     timeout=15, umask=0o077)
        except (OSError, subprocess.TimeoutExpired):
            raise FixtureEvidenceError("doppler_readonly_command_unavailable") from None
        self._write_private(stdout_path, process.stdout)
        self._write_private(stderr_path, process.stderr)
        self._snapshot_check()
        return process, process.stdout, process.stderr

    @staticmethod
    def _write_private(path, data):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)

    def _help_call(self, tail, label):
        process, _stdout, _stderr = self._run(tail, command=label)
        if process.returncode != 0:
            raise FixtureEvidenceError("doppler_help_unavailable")
        return Observation(label, process.returncode, False, (), True).public()

    def version(self):
        return self._help_call(["--version"], "version")

    def help(self, command="root"):
        choices = {
            "root": ["--help"],
            "login": ["login", "--help"],
            "secrets-get": ["secrets", "get", "--help"],
        }
        if command not in choices:
            raise FixtureEvidenceError("readonly_help_command_rejected")
        return self._help_call(choices[command], f"{command}_help")

    def probe_missing_auth(self, requested_keys):
        keys = tuple(sorted(set(requested_keys)))
        if not keys or any(key not in self.approved_keys for key in keys):
            raise FixtureEvidenceError("requested_key_scope_rejected")
        scope = tuple(sorted(set(keys) | IDENTITY_KEYS))
        tail = ["secrets", "get", *scope, "--json", "--no-exit-on-missing-secret",
                "--project", PROJECT, "--config", CONFIG]
        process, stdout, stderr = self._run(tail, command="secrets_get", scope=scope)
        diagnostic = stderr.decode("utf-8", errors="replace")
        if process.returncode == 0:
            raise FixtureEvidenceError("unexpected_authenticated_doppler")
        if stdout or not AUTH_FAILURE.search(diagnostic):
            raise FixtureEvidenceError("missing_auth_not_proven")
        return Observation("secrets_get", process.returncode, True, scope, True).public()


def create_fixture(root, *, executable=None, approved_keys=(), host_config=None):
    """Convenience constructor; does not login or contact a secret-writing API."""
    return DopplerAuthFixture(root, executable=executable, approved_keys=approved_keys,
                              host_config=host_config)
