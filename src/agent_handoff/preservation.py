"""Preserve the original checkout and collect fixed, explicitly selected blobs."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

from .contracts import RuntimeFailure
from .git_backend import git_environment
from .journal import opaque_id


SECRET_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json", "tokens.json"}
SECRET_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".keystore"}
SECRET_CONTENT = re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|(?:ghp_|github_pat_|sk_live_)[A-Za-z0-9_]{16,}")


def safe_path(path: str, *, controls: bool = False) -> str:
    parts = PurePosixPath(path).parts
    if (not path or path.startswith("/") or "\\" in path or any(ord(c) < 32 for c in path)
            or any(part in (".", "..", ".git") for part in path.split("/"))
            or str(PurePosixPath(path)) != path):
        raise RuntimeFailure("Snapshot paths must be canonical repository-relative paths.")
    if not controls and parts[0] == ".agent":
        raise RuntimeFailure("Control records require explicit runtime inputs.")
    if controls and parts[0] == ".agent" and path not in {
            ".agent/protocol.json", ".agent/config.yaml", ".agent/owner.json", ".agent/state.json",
            ".agent/instructions.md", ".agent/secrets.example.yaml"}:
        raise RuntimeFailure("Private or unrecognized runtime files cannot enter publication.")
    for part in parts:
        if (part in SECRET_NAMES or (part.startswith(".env.") and part != ".env.example") or
                Path(part).suffix.lower() in SECRET_SUFFIXES or part in (".aws", ".ssh")):
            raise RuntimeFailure("Snapshot selection contains a prohibited secret path.")
    return path


def checkout_git(checkout: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                                    "-C", str(checkout), *args], env=git_environment(), capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeFailure("Checkout Git inspection unavailable.", "not_ready", 6) from None
    if completed.returncode:
        raise RuntimeFailure("Original checkout cannot be inspected safely.", "not_ready", 6)
    return completed.stdout


def status_counts(report: bytes) -> tuple[int, int, int]:
    if report and not report.endswith(b"\x00"):
        raise RuntimeFailure("Truncated checkout status evidence.")
    entries = iter(report.split(b"\x00"))
    tracked = untracked = ignored = 0
    for entry in entries:
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            raise RuntimeFailure("Invalid checkout status evidence.")
        status = entry[:2]
        if status == b"??":
            untracked += 1
        elif status == b"!!":
            ignored += 1
        else:
            if any(character not in b" MADRCUT" for character in status):
                raise RuntimeFailure("Invalid checkout status classification.")
            tracked += 1
            if b"R" in status or b"C" in status:
                if not next(entries, b""):
                    raise RuntimeFailure("Incomplete checkout rename evidence.")
    return tracked, untracked, ignored


def validate_record(value):
    from .record_checks import shape, match, OID, IDENTITY
    from .journal import validate_id
    shape(value, {"schema_version", "handle_id", "original_checkout", "HEAD", "scope", "method", "index_evidence",
                  "index_entries_hex", "status_entries_hex", "tracked_changes", "untracked", "ignored",
                  "local_commits", "merge_or_rebase", "verification"})
    validate_id(value["handle_id"])
    path = value["original_checkout"]
    if (not isinstance(path, str) or not Path(path).is_absolute() or str(Path(path)) != path or ".." in Path(path).parts or
            not match(value["HEAD"], OID) or not match(value["index_evidence"], IDENTITY) or
            value["scope"] != "entire original checkout retained" or value["method"] != "retain-original" or
            value["verification"] != "No checkout/index writes are performed; original directory remains available." or
            type(value["merge_or_rebase"]) is not bool or
            any(type(value[field]) is not int or value[field] < 0 for field in ("tracked_changes", "untracked", "ignored"))):
        raise RuntimeFailure("Invalid private preservation observation.")
    for field in ("index_entries_hex", "status_entries_hex"):
        if not isinstance(value[field], str) or not re.fullmatch(r"(?:[0-9a-f]{2})*", value[field]):
            raise RuntimeFailure("Invalid private preservation byte evidence.")
    index, report = bytes.fromhex(value["index_entries_hex"]), bytes.fromhex(value["status_entries_hex"])
    if hashlib.sha256(index).hexdigest() != value["index_evidence"]:
        raise RuntimeFailure("Private index evidence digest mismatch.")
    if index and (not index.endswith(b"\x00") or any(not re.fullmatch(
            rb"(?:100644|100755|120000|160000) (?:[0-9a-f]{40}|[0-9a-f]{64}) [0-3]\t[^\x00]+", entry)
            for entry in index.split(b"\x00")[:-1])):
        raise RuntimeFailure("Invalid private index stage evidence.")
    if status_counts(report) != tuple(value[field] for field in ("tracked_changes", "untracked", "ignored")):
        raise RuntimeFailure("Private checkout status counts differ from observations.")
    commits = value["local_commits"]
    if not isinstance(commits, list) or any(not match(commit, OID) for commit in commits) or len(set(commits)) != len(commits):
        raise RuntimeFailure("Invalid private local commit observations.")
    return value


def read_record(store, handle, *, checkout=None):
    from .journal import validate_id
    record = validate_record(store.read("preservation-" + validate_id(handle) + ".json"))
    if record["handle_id"] != handle or (checkout is not None and record["original_checkout"] != str(checkout)):
        raise RuntimeFailure("Preservation evidence identity or checkout mismatch.")
    return record


def preserve(checkout: Path, store) -> dict:
    head = checkout_git(checkout, "rev-parse", "HEAD").decode().strip()
    report = checkout_git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
    index = checkout_git(checkout, "ls-files", "--stage", "-z")
    local_commits = checkout_git(checkout, "rev-list", "HEAD", "--not", "--remotes").decode().splitlines()
    gitdir = Path(checkout_git(checkout, "rev-parse", "--absolute-git-dir").decode().strip())
    tracked, untracked, ignored = status_counts(report)
    value = {"schema_version": 1, "handle_id": opaque_id(), "original_checkout": str(checkout), "HEAD": head,
             "scope": "entire original checkout retained", "method": "retain-original",
             "index_evidence": hashlib.sha256(index).hexdigest(), "index_entries_hex": index.hex(),
             "status_entries_hex": report.hex(), "tracked_changes": tracked,
             "untracked": untracked, "ignored": ignored,
             "local_commits": local_commits,
             "merge_or_rebase": any((gitdir / name).exists() for name in ("MERGE_HEAD", "rebase-merge", "rebase-apply")),
             "verification": "No checkout/index writes are performed; original directory remains available."}
    validate_record(value)
    store.write("preservation-" + value["handle_id"] + ".json", value)
    return value


@dataclass(frozen=True)
class Blob:
    mode: str
    data: bytes
    identity: tuple


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(checkout: Path, path: str) -> Blob | None:
    safe_path(path)
    parent = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                return None
            os.close(parent)
            parent = child
        try:
            before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(before.st_mode):
            target = os.readlink(parts[-1], dir_fd=parent)
            # Preserve links as blobs; never read their target or allow secret aliases.
            if target.startswith("/"):
                raise RuntimeFailure("Absolute snapshot symlinks require a preservation decision.", "decision_required", 3)
            resolved = (checkout / path).resolve()
            if not resolved.is_relative_to(checkout):
                raise RuntimeFailure("Snapshot symlink escapes the checkout.")
            safe_path(str(resolved.relative_to(checkout)))
            data, mode = os.fsencode(target), "120000"
        elif stat.S_ISREG(before.st_mode):
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                if _identity(os.fstat(stream.fileno())) != _identity(before):
                    raise RuntimeFailure("File changed while snapshotting.", "rejected", 4)
                data = stream.read()
                if _identity(os.fstat(stream.fileno())) != _identity(before):
                    raise RuntimeFailure("File changed while snapshotting.", "rejected", 4)
            mode = "100755" if before.st_mode & 0o111 else "100644"
        else:
            raise RuntimeFailure("Explicit snapshot selections must be files, not directories or special devices.")
        after = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if _identity(before) != _identity(after):
            raise RuntimeFailure("File changed while snapshotting.", "rejected", 4)
        if SECRET_CONTENT.search(data):
            raise RuntimeFailure("Snapshot contains prohibited credential material.")
        return Blob(mode, data, _identity(after))
    except OSError:
        raise RuntimeFailure("Snapshot path is unsafe or inaccessible.") from None
    finally:
        os.close(parent)


def snapshot(checkout: Path, included: list[str]) -> dict[str, tuple[str, bytes] | None]:
    if len(set(included)) != len(included):
        raise RuntimeFailure("Duplicate snapshot selection.")
    fixed = {safe_path(path): _read(checkout, path) for path in included}
    # Re-read every selected blob after collection; changes abort the entire intent.
    for path, blob in fixed.items():
        if _read(checkout, path) != blob:
            raise RuntimeFailure("Selected files changed during snapshot collection.", "rejected", 4)
    return {path: (blob.mode, blob.data) if blob else None for path, blob in fixed.items()}
