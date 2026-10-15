"""Verified single-file Linux recovery; private bytes, conditional replacement."""
import base64
import hashlib
import os
from pathlib import Path
import stat
import tempfile

from .contracts import RuntimeFailure
from .host_guard import metadata
from .journal import opaque_id, validate_id
from .locks import lock
from .record_checks import shape, match, IDENTITY
from .secrets.local_env import open_directory


LIMIT = 16 * 1024 * 1024


def validate_snapshot(evidence):
    if not isinstance(evidence, dict) or set(evidence) != {"metadata", "sha256"}:
        raise RuntimeFailure("Invalid host recovery observation.")
    info = evidence["metadata"]
    if (not isinstance(info, list) or len(info) != 8 or any(type(item) is not int for item in info) or
            any(info[index] < 0 for index in range(6)) or not stat.S_ISREG(info[2]) or info[2] & 0o7000 or
            info[3] != os.getuid() or info[4] not in {os.getgid(), *os.getgroups()} or
            not 0 <= info[5] <= LIMIT or not match(evidence["sha256"], IDENTITY)):
        raise RuntimeFailure("Invalid host recovery metadata or digest.")
    return info


def restored_matches(observed, original):
    return (observed["sha256"] == original["sha256"] and
            all(observed["metadata"][i] == original["metadata"][i] for i in (2, 3, 4, 5, 6)))


def canonical_path(path):
    if not isinstance(path, (str, Path)):
        raise RuntimeFailure("Host recovery requires an absolute file path.")
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or not path.name or any(ord(char) < 32 for char in str(path)):
        raise RuntimeFailure("Host recovery requires a canonical absolute file path.")
    return path


def snapshot(path):
    parent = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
                    info.st_gid not in {os.getgid(), *os.getgroups()} or info.st_mode & 0o7000 or
                    os.listxattr(fd)):
                raise RuntimeFailure("Host recovery requires owned regular files without links, special modes or extended attributes.")
            if info.st_size > LIMIT:
                raise RuntimeFailure("Host recovery file exceeds bounded size.")
            chunks, size = [], 0
            while data := os.read(fd, 65536):
                size += len(data)
                if size > LIMIT:
                    raise RuntimeFailure("Host recovery file exceeds bounded size.")
                chunks.append(data)
            raw = b"".join(chunks)
            if (metadata(os.fstat(fd)) != metadata(info) or
                    metadata(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != metadata(info)):
                raise RuntimeFailure("Host recovery observation changed during read.", "decision_required", 3)
            return {"metadata": metadata(info), "sha256": hashlib.sha256(raw).hexdigest()}, raw
        finally:
            os.close(fd)
    finally:
        os.close(parent)


class HostFileRecovery:
    def __init__(self, store):
        self.store = store

    def read(self, handle):
        value = self.store.read("host-backup-" + validate_id(handle) + ".json")
        shape(value, {"schema_version", "handle_id", "path", "snapshot", "raw_base64", "verification"})
        if value["handle_id"] != handle or value["verification"] != "private_restore_bytes_verified":
            raise RuntimeFailure("Invalid host backup identity or verification.")
        canonical_path(value["path"])
        evidence = value["snapshot"]
        info = validate_snapshot(evidence)
        if not isinstance(value["raw_base64"], str) or len(value["raw_base64"]) > (LIMIT + 2) // 3 * 4:
            raise RuntimeFailure("Invalid host backup metadata or bounded material.")
        try:
            raw = base64.b64decode(value["raw_base64"], validate=True)
        except ValueError:
            raise RuntimeFailure("Invalid private host backup encoding.") from None
        if (base64.b64encode(raw).decode() != value["raw_base64"] or len(raw) != info[5] or
                hashlib.sha256(raw).hexdigest() != evidence["sha256"]):
            raise RuntimeFailure("Host backup bytes do not match verified evidence.")
        return value, raw

    def backup(self, path):
        path = canonical_path(path)
        with lock(self.store, "host-recovery.lock"):
            observed, raw = snapshot(path)
            # Exercise byte restoration in a private staging directory before issuing a handle.
            with tempfile.TemporaryDirectory(prefix="restore-check-", dir=self.store.root) as root:
                restored = Path(root) / "file"
                fd = os.open(restored, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if restored.read_bytes() != raw:
                    raise RuntimeFailure("Host backup restore verification failed.")
            if snapshot(path)[0] != observed:
                raise RuntimeFailure("Host file changed before backup commit.", "decision_required", 3)
            handle = opaque_id()
            self.store.write("host-backup-" + handle + ".json", {"schema_version": 1, "handle_id": handle,
                "path": str(path), "snapshot": observed, "raw_base64": base64.b64encode(raw).decode(),
                "verification": "private_restore_bytes_verified"})
            self.read(handle)
            return {"status": "completed", "backup_handle": handle, "backup_verified": True}

    def restore(self, handle, expected_handle, *, fault=lambda _: None):
        with lock(self.store, "host-recovery.lock"):
            for name in self.store.names():
                if name.startswith("host-recovery-op-") and name.endswith(".json"):
                    pending_id = name[len("host-recovery-op-"):-len(".json")]
                    pending = self.store.read(name)
                    self._validate_operation(pending_id, pending)
                    if pending["phase"] != "resolved":
                        raise RuntimeFailure("Resolve pending host recovery before replacement.", "unknown", 5)
            original, raw = self.read(handle)
            expected, _ = self.read(expected_handle)
            if original["path"] != expected["path"]:
                raise RuntimeFailure("Host restore handles refer to different files.")
            path = canonical_path(original["path"])
            if snapshot(path)[0] != expected["snapshot"]:
                raise RuntimeFailure("Host file changed; preserve and review the current state.", "decision_required", 3)
            operation_id = opaque_id()
            journal = {"schema_version": 1, "operation_id": operation_id, "backup_handle": handle,
                       "expected_handle": expected_handle, "phase": "prepared", "outcome": None, "staging": None}
            self.store.write("host-recovery-op-" + operation_id + ".json", journal)
            parent = open_directory(path.parent)
            temporary = ".agent-handoff-host-" + operation_id
            try:
                fault("after_host_recovery_journal")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fchown(stream.fileno(), original["snapshot"]["metadata"][3], original["snapshot"]["metadata"][4])
                    os.fchmod(stream.fileno(), stat.S_IMODE(original["snapshot"]["metadata"][2]))
                    mtime = original["snapshot"]["metadata"][6]
                    os.utime(stream.fileno(), ns=(mtime, mtime))
                    os.fsync(stream.fileno())
                staged = snapshot(path.parent / temporary)[0]
                if not restored_matches(staged, original["snapshot"]):
                    raise RuntimeFailure("Host staged material differs from verified backup.", "unknown", 5)
                journal["staging"] = staged
                self.store.write("host-recovery-op-" + operation_id + ".json", journal)
                fault("before_host_recovery_rename")
                if snapshot(path)[0] != expected["snapshot"]:
                    raise RuntimeFailure("Host editor changed the reviewed file.", "decision_required", 3)
                if snapshot(path.parent / temporary)[0] != journal["staging"]:
                    raise RuntimeFailure("Host staging file changed; retain evidence.", "unknown", 5)
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
                fault("after_host_recovery_rename")
                return self._resolve(operation_id, journal)
            except (OSError, RuntimeFailure):
                return {"status": "unknown", "operation_id": operation_id, "backup_handle": handle}
            finally:
                self._cleanup_staging(path, operation_id, journal["staging"])
                os.close(parent)

    def resolve(self, operation_id):
        with lock(self.store, "host-recovery.lock"):
            value = self.store.read("host-recovery-op-" + validate_id(operation_id) + ".json")
            return self._resolve(operation_id, value)

    @staticmethod
    def _validate_operation(operation_id, value):
        validate_id(operation_id)
        shape(value, {"schema_version", "operation_id", "backup_handle", "expected_handle", "phase", "outcome", "staging"})
        validate_id(value["backup_handle"])
        validate_id(value["expected_handle"])
        if value["staging"] is not None:
            validate_snapshot(value["staging"])
        if (value["operation_id"] != operation_id or value["phase"] not in ("prepared", "resolved") or
                value["outcome"] not in (None, "restored", "not_restored") or
                (value["phase"] == "prepared") != (value["outcome"] is None)):
            raise RuntimeFailure("Invalid host recovery journal.")

    @staticmethod
    def _cleanup_staging(path, operation_id, staging):
        temporary = path.parent / (".agent-handoff-host-" + validate_id(operation_id))
        try:
            observed, _ = snapshot(temporary)
        except FileNotFoundError:
            return True
        except (OSError, RuntimeFailure):
            return False
        if staging is None or observed != staging:
            return False
        try:
            parent = open_directory(path.parent)
        except (OSError, RuntimeFailure):
            return False
        try:
            # Match once more before unlink; editor changes are never cleanup authority.
            if snapshot(temporary)[0] != staging:
                return False
            os.unlink(temporary.name, dir_fd=parent)
            os.fsync(parent)
            return True
        except (OSError, RuntimeFailure):
            return False
        finally:
            os.close(parent)

    @staticmethod
    def _sync_current(path, expected):
        """Sync the exact observed target and parent before terminal bookkeeping."""
        try:
            parent = open_directory(path.parent)
        except (OSError, RuntimeFailure):
            return False
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                if metadata(os.fstat(fd)) != expected["metadata"]:
                    return False
                os.fsync(fd)
                os.fsync(parent)
            finally:
                os.close(fd)
            return snapshot(path)[0] == expected
        except (OSError, RuntimeFailure):
            return False
        finally:
            os.close(parent)

    def _resolve(self, operation_id, value):
        self._validate_operation(operation_id, value)
        original, _ = self.read(value["backup_handle"])
        expected, _ = self.read(value["expected_handle"])
        if original["path"] != expected["path"]:
            raise RuntimeFailure("Host recovery journal file scope differs.")
        path = canonical_path(original["path"])
        try:
            observed, _ = snapshot(path)
        except (OSError, RuntimeFailure):
            return {"status": "unknown", "operation_id": operation_id, "backup_handle": value["backup_handle"]}
        restored = restored_matches(observed, original["snapshot"])
        outcome = "restored" if restored else "not_restored" if observed == expected["snapshot"] else None
        if outcome is None or (value["phase"] == "resolved" and outcome != value["outcome"]):
            return {"status": "unknown", "operation_id": operation_id, "backup_handle": value["backup_handle"]}
        if (not self._cleanup_staging(path, operation_id, value["staging"]) or
                not self._sync_current(path, observed)):
            return {"status": "unknown", "operation_id": operation_id, "backup_handle": value["backup_handle"]}
        if value["phase"] != "resolved":
            self.store.write("host-recovery-op-" + operation_id + ".json", dict(value, phase="resolved", outcome=outcome))
        return {"status": "completed", "operation_id": operation_id, "backup_handle": value["backup_handle"],
                "recovery_outcome": outcome}
