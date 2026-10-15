"""Owned, bounded project-file access with conditional edits and recovery."""
import hashlib
import base64
from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import re
import stat

from agent_handoff.authority import Authority
from agent_handoff.contracts import RuntimeFailure
from agent_handoff.preservation import SECRET_CONTENT, safe_path
from agent_handoff.publication import Runtime
from agent_handoff.journal import opaque_id, validate_id
from agent_handoff.secrets.binding import validate as validate_binding
from scripts.provider_bootstrap_smoke import read_context


MAX_FILE_SIZE = 65_536
MAX_APPROVED_PATHS = 64
EDIT_PREFIX = "project-file-edit-"
_PROTECTED_NAMES = {
    "agents.md", "claude.md", "context.json", "credentials.json", "tokens.json",
    ".env", ".env.local", ".env.production",
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ProjectFileError(RuntimeError):
    """Fixed, non-sensitive failure for an unproven project-file operation."""

    code = "project_file_denied"
    _allowed_codes = {"project_file_denied", "project_file_stale"}

    def __init__(self, code="project_file_denied"):
        self.code = code if code in self._allowed_codes else "project_file_denied"
        super().__init__(self.code)


def _deny():
    raise ProjectFileError() from None


def _canonical_directory(path: Path, *, owned: bool) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        _deny()
    try:
        resolved = path.resolve(strict=True)
        if resolved != path or not resolved.is_dir():
            _deny()
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in resolved.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or (owned and info.st_uid != os.getuid()):
                _deny()
        finally:
            os.close(fd)
        return resolved
    except ProjectFileError:
        raise
    except (OSError, RuntimeFailure, ValueError):
        _deny()


def _validated_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8", "surrogatepass")) > 4096:
        _deny()
    try:
        safe_path(value)
    except (RuntimeFailure, UnicodeError):
        _deny()
    parts = value.split("/")
    if any(part.startswith(".") or part.casefold() in _PROTECTED_NAMES for part in parts):
        _deny()
    return value


def _open_directory(path: Path) -> int:
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        _deny()


def _metadata(info):
    return {"dev": info.st_dev, "ino": info.st_ino, "mode": info.st_mode, "uid": info.st_uid,
            "gid": info.st_gid, "nlink": info.st_nlink, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _read_bounded(fd: int) -> bytes:
    chunks = []
    total = 0
    while total <= MAX_FILE_SIZE:
        chunk = os.read(fd, min(16_384, MAX_FILE_SIZE + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _decode_b64(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > (MAX_FILE_SIZE * 4 // 3 + 8):
        _deny()
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        _deny()
    if len(raw) > MAX_FILE_SIZE or base64.b64encode(raw).decode("ascii") != value:
        _deny()
    return raw


def _valid_metadata(value: dict) -> bool:
    keys = {"dev", "ino", "mode", "uid", "gid", "nlink", "size", "mtime_ns", "ctime_ns"}
    return (isinstance(value, dict) and set(value) == keys and
            all(type(value[key]) is int and value[key] >= 0 for key in keys) and
            stat.S_ISREG(value["mode"]) and value["uid"] == os.getuid() and value["nlink"] == 1 and
            not value["mode"] & 0o7000 and value["size"] <= MAX_FILE_SIZE)


def _same_object(left: dict, right: dict, *, ctime=True) -> bool:
    fields = ("dev", "ino", "mode", "uid", "gid", "nlink", "size", "mtime_ns")
    if ctime:
        fields += ("ctime_ns",)
    return all(left[field] == right[field] for field in fields)


def _validate_edit_record(record: dict, operation_id: str, filename: str) -> dict:
    required = {"schema_version", "operation_id", "authority_identity", "publication_ref", "session_id",
                "checkout_identity", "managed_checkout", "path", "before_b64", "before_sha256",
                "before_metadata", "desired_b64", "desired_sha256", "stage_name", "stage_metadata",
                "phase", "outcome"}
    if (not isinstance(record, dict) or set(record) != required or
            type(record["schema_version"]) is not int or record["schema_version"] != 1):
        _deny()
    try:
        validate_id(record["operation_id"])
        path = _validated_relative_path(record["path"])
        managed = Path(record["managed_checkout"])
        if (record["operation_id"] != operation_id or filename != EDIT_PREFIX + operation_id + ".json" or
                record["stage_name"] != _stage_path(path, operation_id) or
                not managed.is_absolute() or str(managed) != record["managed_checkout"] or ".." in managed.parts or
                not isinstance(record["authority_identity"], str) or not _SHA256.fullmatch(record["authority_identity"]) or
                not isinstance(record["checkout_identity"], str) or not _SHA256.fullmatch(record["checkout_identity"]) or
                not isinstance(record["session_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", record["session_id"]) or
                not isinstance(record["publication_ref"], str) or not record["publication_ref"].startswith("refs/heads/") or
                record["phase"] not in ("prepared", "resolved") or
                record["outcome"] not in (None, "applied", "not_applied", "unchanged") or
                (record["phase"] == "prepared") != (record["outcome"] is None)):
            _deny()
        before = _decode_b64(record["before_b64"])
        desired = _decode_b64(record["desired_b64"])
        if (not _valid_metadata(record["before_metadata"]) or not _valid_metadata(record["stage_metadata"]) or
                len(before) != record["before_metadata"]["size"] or
                len(desired) != record["stage_metadata"]["size"] or
                record["before_sha256"] != hashlib.sha256(before).hexdigest() or
                record["desired_sha256"] != hashlib.sha256(desired).hexdigest() or
                record["stage_metadata"]["uid"] != record["before_metadata"]["uid"] or
                record["stage_metadata"]["gid"] != record["before_metadata"]["gid"] or
                record["stage_metadata"]["mode"] != record["before_metadata"]["mode"] or
                record["stage_metadata"]["nlink"] != 1):
            _deny()
        return record
    except (TypeError, ValueError, KeyError):
        _deny()


def _stage_path(relative_path: str, operation_id: str) -> str:
    parent = relative_path.rpartition("/")[0]
    leaf = ".agent-handoff-edit-" + operation_id
    return parent + "/" + leaf if parent else leaf


def _open_parent(checkout: Path, relative_path: str) -> tuple[int, str]:
    parts = relative_path.split("/")
    directory = _open_directory(checkout)
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    _deny()
            except BaseException:
                os.close(child)
                raise
            os.close(directory)
            directory = child
        return directory, parts[-1]
    except ProjectFileError:
        os.close(directory)
        raise
    except OSError:
        os.close(directory)
        _deny()


def _write_all(fd: int, raw: bytes):
    view = memoryview(raw)
    offset = 0
    while offset < len(view):
        written = os.write(fd, view[offset:])
        if written <= 0:
            raise OSError("Project staging write made no progress.")
        offset += written


class ProjectFiles:
    def __init__(self, *, source: Path, private_root: Path, authority: Authority,
                 context_file: Path, managed_root: Path, approved_paths: list[str]):
        try:
            self.source = _canonical_directory(source, owned=True)
            self.private_root = _canonical_directory(private_root, owned=True)
            if self.private_root == self.source or self.private_root.is_relative_to(self.source) or self.source.is_relative_to(self.private_root):
                _deny()
            self.managed_root = _canonical_directory(managed_root, owned=True)
            repos_root = self.private_root / "repos"
            if (self.managed_root != repos_root and not self.managed_root.is_relative_to(repos_root)) or self.managed_root.is_relative_to(self.source) or self.source.is_relative_to(self.managed_root):
                _deny()
            expected_context = self.private_root / "context.json"
            if not isinstance(context_file, Path) or context_file != expected_context:
                _deny()
            _validate_private_context_file(context_file)
            if not isinstance(authority, Authority):
                _deny()
            if not isinstance(approved_paths, list) or not 1 <= len(approved_paths) <= MAX_APPROVED_PATHS:
                _deny()
            paths = [_validated_relative_path(path) for path in approved_paths]
            if len(paths) != len(set(paths)):
                _deny()
            self.approved_paths = frozenset(paths)
            self.authority = authority
            # The capability is read only from the existing private context record and is never returned or logged.
            self._context = read_context(self.private_root, self.source)
        except ProjectFileError:
            raise
        except Exception:
            _deny()

    def _validated_checkout(self, managed_checkout: Path, relative_path: str) -> Path:
        relative_path = _validated_relative_path(relative_path)
        if relative_path not in self.approved_paths:
            _deny()
        checkout = _canonical_directory(managed_checkout, owned=True)
        if checkout == self.managed_root or not checkout.is_relative_to(self.managed_root):
            _deny()
        return checkout

    @contextmanager
    def _guarded_runtime(self, managed_checkout: Path):
        checkout = _canonical_directory(managed_checkout, owned=True)
        if checkout == self.managed_root or not checkout.is_relative_to(self.managed_root):
            _deny()
        with Runtime(checkout, self.private_root, self.authority, self._context) as runtime:
            with runtime.locked(), runtime.checkout_lock():
                runtime.journal.require_clear()
                oid, records = runtime._observe()
                session = runtime.sessions.prove(runtime.backend, oid, records)
                yield runtime, checkout, oid, session

    @contextmanager
    def _guarded_checkout(self, managed_checkout: Path, relative_path: str):
        relative_path = _validated_relative_path(relative_path)
        if relative_path not in self.approved_paths:
            _deny()
        with self._guarded_runtime(managed_checkout) as guarded:
            self._reject_file_mapping(guarded[0], relative_path)
            yield guarded

    def read(self, managed_checkout: Path, relative_path: str) -> dict:
        try:
            with self._guarded_checkout(managed_checkout, relative_path) as (_runtime, checkout, _oid, _session):
                raw, _snapshot = self._read_snapshot(checkout, relative_path)
            if SECRET_CONTENT.search(raw) or self._contains_context(raw):
                _deny()
            try:
                content = raw.decode("utf-8", errors="strict")
            except UnicodeError:
                _deny()
            return {"path": relative_path, "content": content, "sha256": hashlib.sha256(raw).hexdigest()}
        except ProjectFileError:
            raise
        except Exception:
            _deny()

    def write(self, managed_checkout: Path, relative_path: str, content: str, expected_sha256: str, *, fault=None) -> dict:
        if not isinstance(content, str) or not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
            _deny()
        try:
            desired = content.encode("utf-8", errors="strict")
        except UnicodeError:
            _deny()
        if len(desired) > MAX_FILE_SIZE or SECRET_CONTENT.search(desired):
            _deny()
        requested_path = relative_path
        relative_path = None
        operation_id = None
        stage_path = None
        stage_created = False
        stage_meta = None
        journal_persisted = False
        try:
            relative_path = _validated_relative_path(requested_path)
            if relative_path not in self.approved_paths or self._contains_context(desired):
                _deny()
            checkout = self._validated_checkout(managed_checkout, relative_path)
            operation_id = opaque_id()
            stage_path = _stage_path(relative_path, operation_id)
            with self._guarded_checkout(checkout, relative_path) as (runtime, checkout, _oid, session):
                self._require_no_pending(runtime, checkout, relative_path)
                before_raw, before_meta = self._read_snapshot(checkout, relative_path)
                if SECRET_CONTENT.search(before_raw) or self._contains_context(before_raw):
                    _deny()
                if not hmac.compare_digest(hashlib.sha256(before_raw).hexdigest(), expected_sha256):
                    raise ProjectFileError("project_file_stale")
                if before_meta["gid"] not in {os.getgid(), *os.getgroups()}:
                    _deny()
                stage_leaf = stage_path.rpartition("/")[2]
                parent_fd, target_leaf = _open_parent(checkout, relative_path)
                try:
                    stage_fd = os.open(stage_leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                       0o600, dir_fd=parent_fd)
                    stage_created = True
                    try:
                        _write_all(stage_fd, desired)
                        os.fchown(stage_fd, before_meta["uid"], before_meta["gid"])
                        os.fchmod(stage_fd, stat.S_IMODE(before_meta["mode"]))
                        os.fsync(stage_fd)
                        stage_meta = _metadata(os.fstat(stage_fd))
                    finally:
                        os.close(stage_fd)
                    stage_raw, named_stage_meta = self._read_snapshot(checkout, stage_path)
                    if (stage_raw != desired or named_stage_meta != stage_meta or
                            stage_meta["mode"] != before_meta["mode"]):
                        _deny()
                    record = {
                        "schema_version": 1, "operation_id": operation_id,
                        "authority_identity": self.authority.identity,
                        "publication_ref": self.authority.publication_ref,
                        "session_id": session["session_id"],
                        "checkout_identity": runtime.sessions.checkout_id,
                        "managed_checkout": str(checkout), "path": relative_path,
                        "before_b64": base64.b64encode(before_raw).decode("ascii"),
                        "before_sha256": hashlib.sha256(before_raw).hexdigest(),
                        "before_metadata": before_meta,
                        "desired_b64": base64.b64encode(desired).decode("ascii"),
                        "desired_sha256": hashlib.sha256(desired).hexdigest(),
                        "stage_name": stage_path, "stage_metadata": stage_meta,
                        "phase": "prepared", "outcome": None,
                    }
                    record = _validate_edit_record(record, operation_id, EDIT_PREFIX + operation_id + ".json")
                    try:
                        runtime.store.write(EDIT_PREFIX + operation_id + ".json", record)
                    except Exception:
                        # A failed directory sync may follow a successful rename.
                        # Retain the stage if the named intent was published.
                        journal_persisted = runtime.store.read(
                            EDIT_PREFIX + operation_id + ".json", missing=True) == record
                        raise
                    journal_persisted = True
                    if fault is not None:
                        fault("before_project_file_replace")
                    oid, records = runtime._observe()
                    session = runtime.sessions.prove(runtime.backend, oid, records)
                    if session["session_id"] != record["session_id"]:
                        _deny()
                    current_raw, current_meta = self._read_snapshot(checkout, relative_path)
                    if current_raw != before_raw or current_meta != before_meta:
                        _deny()
                    current_stage, current_stage_meta = self._read_snapshot(checkout, stage_path)
                    if current_stage != desired or current_stage_meta != stage_meta:
                        _deny()
                    if (_metadata(os.stat(target_leaf, dir_fd=parent_fd, follow_symlinks=False)) != before_meta or
                            _metadata(os.stat(stage_leaf, dir_fd=parent_fd, follow_symlinks=False)) != stage_meta):
                        _deny()
                    # The filesystem has no compare-and-swap rename against a
                    # non-cooperating editor; the adjacent checks bound that race.
                    os.replace(stage_leaf, target_leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    if fault is not None:
                        fault("after_project_file_replace")
                    self._fsync_target(checkout, relative_path, stage_meta)
                    applied_raw, applied_meta = self._read_snapshot(checkout, relative_path)
                    if applied_raw != desired or not _same_object(applied_meta, stage_meta, ctime=False):
                        _deny()
                    latest, latest_records = runtime._observe()
                    final_session = runtime.sessions.prove(runtime.backend, latest, latest_records)
                    if final_session["session_id"] != record["session_id"]:
                        _deny()
                    record.update(phase="resolved", outcome="applied")
                    runtime.store.write(EDIT_PREFIX + operation_id + ".json", record)
                    return {"status": "completed", "path": relative_path, "sha256": record["desired_sha256"],
                            "operation_id": operation_id}
                finally:
                    os.close(parent_fd)
            _deny()
        except ProjectFileError as error:
            if error.code == "project_file_stale":
                raise
            if journal_persisted:
                return {"status": "unknown", "path": relative_path, "sha256": hashlib.sha256(desired).hexdigest(),
                        "operation_id": operation_id}
            if stage_created:
                self._cleanup_unjournaled_stage(managed_checkout, stage_path, desired, stage_meta)
            raise ProjectFileError() from None
        except Exception:
            if journal_persisted:
                return {"status": "unknown", "path": relative_path, "sha256": hashlib.sha256(desired).hexdigest(),
                        "operation_id": operation_id}
            if stage_created:
                self._cleanup_unjournaled_stage(managed_checkout, stage_path, desired, stage_meta)
            _deny()

    def resolve(self, managed_checkout: Path, operation_id: str) -> dict:
        try:
            validate_id(operation_id)
            checkout = _canonical_directory(managed_checkout, owned=True)
            if checkout == self.managed_root or not checkout.is_relative_to(self.managed_root):
                _deny()
            filename = EDIT_PREFIX + operation_id + ".json"
            with Runtime(checkout, self.private_root, self.authority, self._context) as initial:
                record = _validate_edit_record(initial.store.read(filename), operation_id, filename)
            for field in ("before_b64", "desired_b64"):
                raw = _decode_b64(record[field])
                if SECRET_CONTENT.search(raw) or self._contains_context(raw):
                    _deny()
            path = record["path"]
            if (path not in self.approved_paths or record["authority_identity"] != self.authority.identity or
                    record["publication_ref"] != self.authority.publication_ref or
                    record["managed_checkout"] != str(checkout)):
                _deny()
            try:
                with self._guarded_checkout(checkout, path) as (runtime, checkout, _oid, session):
                    current_record = _validate_edit_record(runtime.store.read(filename), operation_id, filename)
                    if current_record != record:
                        return self._unknown_result(record)
                    if record["phase"] == "resolved":
                        # Refresh receipt durability after a prior private-store
                        # directory sync failed following the receipt rename.
                        runtime.store.write(filename, record)
                        return self._resolved_result(record)
                    if record["session_id"] != session["session_id"] or record["checkout_identity"] != runtime.sessions.checkout_id:
                        return self._unknown_result(record)
                    before = _decode_b64(record["before_b64"])
                    desired = _decode_b64(record["desired_b64"])
                    current_raw, current_meta = self._read_snapshot(checkout, path)
                    stage_state = self._stage_state(checkout, record["stage_name"], record["stage_metadata"], desired)
                    if current_raw == before and current_meta == record["before_metadata"] and stage_state in (True, False):
                        parent_fd, _leaf = _open_parent(checkout, record["path"])
                        try:
                            self._prove_same_session(runtime, record)
                            verified_raw, verified_meta = self._read_snapshot(checkout, path)
                            if verified_raw != before or verified_meta != record["before_metadata"]:
                                return self._unknown_result(record)
                            stage_leaf = record["stage_name"].rpartition("/")[2]
                            if stage_state:
                                stage_raw, stage_meta = self._read_snapshot(checkout, record["stage_name"])
                                if (stage_raw != desired or stage_meta != record["stage_metadata"] or
                                        _metadata(os.stat(stage_leaf, dir_fd=parent_fd, follow_symlinks=False)) != stage_meta):
                                    return self._unknown_result(record)
                                os.unlink(stage_leaf, dir_fd=parent_fd)
                            else:
                                try:
                                    os.stat(stage_leaf, dir_fd=parent_fd, follow_symlinks=False)
                                except FileNotFoundError:
                                    pass
                                else:
                                    return self._unknown_result(record)
                            os.fsync(parent_fd)
                        finally:
                            os.close(parent_fd)
                        self._prove_same_session(runtime, record)
                        record.update(phase="resolved", outcome="not_applied")
                        runtime.store.write(filename, record)
                        return self._resolved_result(record)
                    if (current_raw == desired and _same_object(current_meta, record["stage_metadata"], ctime=False)
                            and stage_state is False):
                        self._fsync_target(checkout, path, record["stage_metadata"])
                        verified_raw, verified_meta = self._read_snapshot(checkout, path)
                        if verified_raw != desired or not _same_object(verified_meta, record["stage_metadata"], ctime=False):
                            return self._unknown_result(record)
                        self._prove_same_session(runtime, record)
                        record.update(phase="resolved", outcome="applied")
                        runtime.store.write(filename, record)
                        return self._resolved_result(record)
                    return self._unknown_result(record)
            except (RuntimeFailure, ProjectFileError, OSError):
                return self._unknown_result(record)
        except ProjectFileError:
            raise
        except Exception:
            _deny()

    @staticmethod
    def _unknown_result(record):
        return {"status": "unknown", "path": record["path"], "sha256": record["desired_sha256"],
                "operation_id": record["operation_id"]}

    @staticmethod
    def _resolved_result(record):
        return {"status": "completed", "path": record["path"], "sha256": record["desired_sha256"],
                "operation_id": record["operation_id"], "outcome": record["outcome"]}

    @staticmethod
    def _stage_state(checkout, stage_path, expected_meta, desired):
        try:
            parent, leaf = _open_parent(checkout, stage_path)
            try:
                os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            finally:
                os.close(parent)
        except FileNotFoundError:
            return False
        except Exception:
            return None
        try:
            raw, metadata_value = ProjectFiles._read_snapshot(checkout, stage_path)
        except ProjectFileError:
            return None
        return True if raw == desired and metadata_value == expected_meta else None

    @staticmethod
    def _fsync_target(checkout, relative_path, expected):
        parent, leaf = _open_parent(checkout, relative_path)
        try:
            named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if not _same_object(_metadata(named), expected, ctime=False):
                _deny()
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                opened = os.fstat(fd)
                if _metadata(opened) != _metadata(named):
                    _deny()
                os.fsync(fd)
                if (_metadata(os.fstat(fd)) != _metadata(opened) or
                        _metadata(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != _metadata(opened)):
                    _deny()
            finally:
                os.close(fd)
            os.fsync(parent)
        except OSError:
            _deny()
        finally:
            os.close(parent)

    @staticmethod
    def _prove_same_session(runtime, record):
        oid, records = runtime._observe()
        session = runtime.sessions.prove(runtime.backend, oid, records)
        if session["session_id"] != record["session_id"]:
            _deny()

    @staticmethod
    def _cleanup_unjournaled_stage(checkout, stage_path, desired, expected_meta):
        if expected_meta is None:
            return
        try:
            raw, observed = ProjectFiles._read_snapshot(checkout, stage_path)
            if raw != desired or observed != expected_meta:
                return
            parent, leaf = _open_parent(checkout, stage_path)
            try:
                if _metadata(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != expected_meta:
                    return
                os.unlink(leaf, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
        except Exception:
            return

    @staticmethod
    def _require_no_pending(runtime, checkout, relative_path):
        for filename in runtime.store.names():
            if not filename.startswith(EDIT_PREFIX):
                continue
            operation_id = filename[len(EDIT_PREFIX):-5] if filename.endswith(".json") else ""
            try:
                validate_id(operation_id)
                record = _validate_edit_record(runtime.store.read(filename), operation_id, filename)
            except Exception:
                _deny()
            if (record["phase"] == "prepared" and record["managed_checkout"] == str(checkout) and
                    record["path"] == relative_path):
                _deny()

    def _contains_context(self, raw: bytes) -> bool:
        token = self._context.encode("ascii")
        candidates = {token, base64.b64encode(token), base64.urlsafe_b64encode(token)}
        try:
            decoded = bytes.fromhex(self._context)
        except ValueError:
            decoded = b""
        if decoded:
            candidates.update((decoded, base64.b64encode(decoded), base64.urlsafe_b64encode(decoded),
                               decoded.hex().encode("ascii")))
        if any(candidate and candidate in raw for candidate in candidates):
            return True
        # Preserve duplicate JSON fields while scanning escaped credential text.
        for document in (raw, *raw.splitlines()):
            try:
                value = json.loads(document, object_pairs_hook=lambda pairs: pairs)
            except (ValueError, UnicodeError, RecursionError):
                continue
            pending = [value]
            while pending:
                value = pending.pop()
                if isinstance(value, str):
                    encoded = value.encode("utf-8", "surrogatepass")
                    if any(candidate and candidate in encoded for candidate in candidates):
                        return True
                elif isinstance(value, (list, tuple)):
                    pending.extend(value)
        return False

    @staticmethod
    def _reject_file_mapping(runtime, relative_path):
        binding = runtime.store.read("secret-binding.json", missing=True)
        if binding is None:
            return
        try:
            validate_binding(binding)
            mappings = [declaration.get("file", "") for declaration in binding["manifest"]["keys"].values()
                        if declaration["classification"] == "file-backed"]
        except (RuntimeFailure, KeyError, TypeError):
            _deny()
        if any(path.casefold() == relative_path.casefold() for path in mappings):
            _deny()

    @staticmethod
    def _read_file(checkout: Path, relative_path: str) -> bytes:
        return ProjectFiles._read_snapshot(checkout, relative_path)[0]

    @staticmethod
    def _read_snapshot(checkout: Path, relative_path: str) -> tuple[bytes, dict]:
        directory, name = _open_parent(checkout, relative_path)
        try:
            before_name = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(before_name.st_mode):
                _deny()
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before_fd = os.fstat(fd)
                if (_metadata(before_name) != _metadata(before_fd) or
                        not stat.S_ISREG(before_fd.st_mode) or before_fd.st_uid != os.getuid() or
                        before_fd.st_nlink != 1 or before_fd.st_mode & 0o7000 or
                        before_fd.st_size < 0 or before_fd.st_size > MAX_FILE_SIZE or os.listxattr(fd)):
                    _deny()
                raw = _read_bounded(fd)
                after_fd = os.fstat(fd)
                after_name = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (_metadata(before_fd) != _metadata(after_fd) or _metadata(after_fd) != _metadata(after_name) or
                        len(raw) != after_fd.st_size or len(raw) > MAX_FILE_SIZE):
                    _deny()
                return raw, _metadata(after_fd)
            finally:
                os.close(fd)
        except ProjectFileError:
            raise
        except OSError:
            _deny()
        finally:
            os.close(directory)


def _validate_private_context_file(path: Path):
    parent = _open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
                    stat.S_IMODE(info.st_mode) != 0o600):
                _deny()
        finally:
            os.close(fd)
    except ProjectFileError:
        raise
    except OSError:
        _deny()
    finally:
        os.close(parent)
