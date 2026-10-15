"""Read explicit authority through structured Git argv; never use checkout config."""
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from urllib.parse import urlsplit

from .contracts import RuntimeFailure, parse_record, _unique_pairs
from .record_checks import config as validate_config
import json


class _GitCommandTimeout(RuntimeFailure):
    """Private timeout receipt; stdout is retained only for strict push parsing."""

    def __init__(self, stdout: bytes | None):
        super().__init__("Git operation unavailable or timed out.", "not_ready", 6)
        self.stdout = stdout if isinstance(stdout, bytes) and len(stdout) <= 65536 else b""


def _terminal_rejection(stdout: bytes, oid: str, ref: str) -> bool:
    """Accept only a complete, single-ref porcelain rejection receipt."""
    if (not isinstance(stdout, bytes) or len(stdout) > 65536 or not stdout.endswith(b"\n") or
            re.fullmatch(r"[0-9a-f]{40}", oid) is None or not ref.startswith("refs/heads/")):
        return False
    try:
        lines = stdout.decode("utf-8", "strict").splitlines()
    except UnicodeError:
        return False
    status_rows = []
    headers = 0
    footers = 0
    for line in lines:
        if line.startswith(("!\t", "=\t", " \t", "*\t")):
            status_rows.append(line)
        elif line.startswith("To "):
            headers += 1
        elif line == "Done":
            footers += 1
        else:
            return False
    if len(status_rows) != 1 or headers > 1 or footers > 1:
        return False
    fields = status_rows[0].split("\t")
    if len(fields) != 3 or fields[0] != "!" or fields[1] != f"{oid}:{ref}":
        return False
    summary = fields[2]
    return (summary == "[remote rejected]" or
            (summary.startswith("[remote rejected] (") and summary.endswith(")") and
             len(summary) > len("[remote rejected] ()")) or
            summary == "[rejected] (stale info)")


def git_environment() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file:https:ssh",
               GIT_OPTIONAL_LOCKS="0",
               GIT_AUTHOR_NAME="Agent Handoff Runtime", GIT_AUTHOR_EMAIL="runtime@example.invalid",
               GIT_COMMITTER_NAME="Agent Handoff Runtime", GIT_COMMITTER_EMAIL="runtime@example.invalid")
    return env


def validate_destination(destination: str) -> str:
    if not destination or any(c.isspace() or ord(c) < 32 for c in destination):
        raise RuntimeFailure("Invalid authority destination.")
    if destination.startswith("/"):
        path = Path(destination)
        if not path.is_dir():
            raise RuntimeFailure("Local authority must be an existing directory.")
        return str(path.resolve())
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*:[A-Za-z0-9][A-Za-z0-9_./-]*", destination):
        # Keep the explicitly approved SCP syntax as its own identity. Do not
        # infer equivalence to an SSH/HTTPS URL or rewrite relative SSH paths.
        remote_path = destination.split(":", 1)[1]
        if any(part in (".", "..", "") for part in remote_path.split("/")):
            raise RuntimeFailure("Invalid SSH authority path.")
        return destination
    try:
        parsed = urlsplit(destination)
        parsed.port
    except ValueError:
        raise RuntimeFailure("Invalid authority URL.") from None
    if parsed.scheme not in ("https", "ssh") or not parsed.hostname or not parsed.path or parsed.query or parsed.fragment:
        raise RuntimeFailure("Use an absolute local path, HTTPS URL, or SSH URL.")
    if parsed.password is not None or (parsed.scheme == "https" and parsed.username is not None):
        raise RuntimeFailure("Embedded credentials are prohibited.")
    return destination


def inspect(destination: str, publication_ref: str) -> dict:
    from .plans import validate as validate_plans
    destination = validate_destination(destination)
    if not publication_ref.startswith("refs/heads/"):
        raise RuntimeFailure("Publication ref must be a full branch ref.")
    with tempfile.TemporaryDirectory(prefix="agent-handoff-inspect-") as directory:
        backend = GitBackend(Path(directory) / "objects.git", destination, publication_ref)
        oid = backend.observe()
        if oid is None:
            raise RuntimeFailure("Publication branch is absent; initialization requires explicit approval.", "not_ready", 6)
        records = backend.records(oid)
        validate_plans(backend, oid, records["state"])
        # Return public control facts only; repository text never enters diagnostics.
        return {"observed_revision": oid, "owner_present": records["owner"]["owner"] is not None,
                "runtime_status": records["state"]["status"], "protocol_version": "1.2"}


class GitBackend:
    """Private bare object cache with one explicit conditional publication ref."""

    def __init__(self, directory: Path, destination: str, publication_ref: str):
        self.directory = directory
        self.destination = validate_destination(destination)
        self.ref = publication_ref
        self.command_timeout = 30
        if directory.is_symlink():
            raise RuntimeFailure("Git cache must not be a symlink.")
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeFailure("Unsafe Git cache permissions.")
        if not (directory / "HEAD").exists():
            self.text("init", "--bare", "--quiet")
        self.text("check-ref-format", publication_ref)
        if not publication_ref.startswith("refs/heads/"):
            raise RuntimeFailure("Publication ref must be a branch ref.")

    def run(self, *args: str, data: bytes | None = None, index: str | None = None):
        env = git_environment()
        if index:
            env["GIT_INDEX_FILE"] = index
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                   "-C", str(self.directory), *args]
        process = None
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True, umask=0o077)
            try:
                stdout, stderr = process.communicate(data, timeout=self.command_timeout)
            except subprocess.TimeoutExpired as timeout_error:
                partial_stdout = timeout_error.output if isinstance(timeout_error.output, bytes) else b""
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # An escaped descendant must not hold the API indefinitely.
                try:
                    complete_stdout, _ = process.communicate(timeout=5)
                    if isinstance(complete_stdout, bytes):
                        partial_stdout = complete_stdout
                except subprocess.TimeoutExpired as drain_error:
                    if isinstance(drain_error.output, bytes):
                        partial_stdout = drain_error.output
                raise _GitCommandTimeout(partial_stdout) from None
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except OSError:
            raise RuntimeFailure("Git operation unavailable or timed out.", "not_ready", 6) from None
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def text(self, *args: str, data: bytes | None = None, index: str | None = None) -> str:
        result = self.run(*args, data=data, index=index)
        if result.returncode:
            raise RuntimeFailure("Git operation failed.", "not_ready", 6)
        try:
            return result.stdout.decode("utf-8").strip()
        except UnicodeError:
            raise RuntimeFailure("Git returned invalid control text.") from None

    def remote_head(self) -> str | None:
        lines = self.text("ls-remote", "--refs", "--", self.destination, self.ref).splitlines()
        if not lines:
            return None
        if len(lines) != 1:
            raise RuntimeFailure("Ambiguous authority response.")
        row = lines[0].split()
        if len(row) != 2 or row[1] != self.ref or not re.fullmatch(r"[0-9a-f]{40}", row[0]):
            raise RuntimeFailure("Unsupported or invalid authority object format.", "unsupported", 8)
        return row[0]

    def observe(self) -> str | None:
        oid = self.remote_head()
        if oid is None:
            return None
        self.text("fetch", "--quiet", "--no-tags", "--", self.destination, self.ref)
        if self.text("rev-parse", "FETCH_HEAD") != oid:
            raise RuntimeFailure("Authority changed while fetching; refresh the operation.", "rejected", 4)
        self.text("cat-file", "-e", oid + "^{commit}")
        return oid

    def ancestor(self, older: str, newer: str) -> bool:
        for oid in (older, newer):
            self.text("cat-file", "-e", oid + "^{commit}")
        result = self.run("merge-base", "--is-ancestor", older, newer)
        if result.returncode not in (0, 1):
            raise RuntimeFailure("Authority history cannot be verified.", "unknown", 5)
        return result.returncode == 0

    def records(self, oid: str) -> dict:
        from .preservation import safe_path
        listing = self.run("ls-tree", "-r", "-z", oid)
        if listing.returncode:
            raise RuntimeFailure("Publication tree cannot be validated.")
        for entry in listing.stdout.split(b"\x00"):
            if not entry:
                continue
            header, raw_path = entry.split(b"\t", 1)
            if header.startswith(b"160000 "):
                raise RuntimeFailure("Submodules are unsupported.", "unsupported", 8)
            try:
                path = raw_path.decode("utf-8")
            except UnicodeError:
                raise RuntimeFailure("Publication paths must be UTF-8.", "unsupported", 8) from None
            safe_path(path, controls=True)
            if Path(path).name == ".gitattributes":
                attributes = self.run("show", f"{oid}:{path}")
                if attributes.returncode or b"filter=lfs" in attributes.stdout:
                    raise RuntimeFailure("Git LFS is unsupported.", "unsupported", 8)
        records = {}
        for kind in ("protocol", "owner", "state"):
            entry = self.text("ls-tree", oid, "--", f".agent/{kind}.json")
            if not entry.startswith("100644 blob "):
                raise RuntimeFailure("Control record must be a regular tracked file.")
            records[kind] = parse_record(self.text("show", f"{oid}:.agent/{kind}.json"), kind)
        if not self.text("ls-tree", oid, "--", ".agent/config.yaml").startswith("100644 blob "):
            raise RuntimeFailure("Config must be a regular tracked file.")
        try:
            config = json.loads(self.text("show", f"{oid}:.agent/config.yaml"), object_pairs_hook=_unique_pairs)
        except (ValueError, RecursionError):
            raise RuntimeFailure("Config requires the supported JSON-compatible YAML syntax.") from None
        validate_config(config)
        if config["authority"] != self.destination or config["publication_ref"] != self.ref:
            raise RuntimeFailure("Repository config differs from the approved authority.", "invalid", 7)
        self.text("check-ref-format", config["mainline_ref"])
        records["config"] = config
        return records

    def prepare(self, parent: str, changes: dict[str, bytes | None], message: str) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="index-", dir=self.directory) as temporary:
            index = str(Path(temporary) / "index")
            self.text("read-tree", parent, index=index)
            entries = bytearray()
            for path, content in sorted(changes.items()):
                if content is None:
                    entries.extend(b"0 " + b"0" * 40 + b"\t" + path.encode() + b"\x00")
                else:
                    mode = "100644"
                    if isinstance(content, tuple):
                        mode, content = content
                    blob = self.text("hash-object", "-w", "--stdin", data=content)
                    entries.extend(f"{mode} {blob}\t".encode() + path.encode() + b"\x00")
            self.text("update-index", "-z", "--index-info", data=bytes(entries), index=index)
            tree = self.text("write-tree", index=index)
            final_message = message(tree) if callable(message) else message
            oid = self.text("commit-tree", tree, "-p", parent, data=final_message.encode())
        if self.text("rev-list", "--parents", "-n", "1", oid).split() != [oid, parent]:
            raise RuntimeFailure("Prepared publication must have exactly the expected parent.")
        return oid, tree

    def pin(self, operation_id: str, oid: str):
        self.text("update-ref", "refs/agent-handoff/recovery/" + operation_id, oid)

    def push(self, oid: str, expected: str | None) -> str:
        try:
            result = self.run("push", "--porcelain", "--no-verify",
                              f"--force-with-lease={self.ref}:{expected or ''}",
                              "--", self.destination, f"{oid}:{self.ref}")
        except _GitCommandTimeout as timeout:
            return "rejected" if _terminal_rejection(timeout.stdout, oid, self.ref) else "publication_unknown"
        except RuntimeFailure:
            return "publication_unknown"
        if result.returncode == 0:
            return "published"
        # Only explicit porcelain rejection is terminal. Transport errors are ambiguous.
        if _terminal_rejection(result.stdout, oid, self.ref):
            return "rejected"
        return "publication_unknown"
