"""Private local dotenv IO, preserve-first replacement, non-secret availability."""
import os
from pathlib import Path
import secrets
import stat

from ..contracts import RuntimeFailure
from ..journal import opaque_id
from .dotenv import parse, patch


def open_directory(path: Path, *, allow_missing=False) -> int | None:
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.absolute().parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as error:
        os.close(fd)
        if allow_missing and isinstance(error, FileNotFoundError):
            return None
        raise RuntimeFailure("Unsafe local secret directory.") from None


class LocalEnvironment:
    def __init__(self, path: Path, store):
        self.path = path
        self.store = store

    def read(self):
        parent = open_directory(self.path.parent)
        try:
            try:
                fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                return parse("")
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                    raise RuntimeFailure("Local secrets require owner-private regular files.")
                content = stream.read()
            return parse(content.decode("utf-8"))
        except (OSError, UnicodeError):
            raise RuntimeFailure("Local secret material is unavailable or unsafe.") from None
        finally:
            os.close(parent)

    def inspect(self, manifest: dict) -> dict:
        document = self.read()
        missing, available, unsupported = [], [], []
        for key, declaration in manifest["keys"].items():
            present = key in document.values
            if declaration["classification"] == "file-backed":
                if Path(declaration["file"]).suffix.lower() in (".pfx", ".p12"):
                    unsupported.append(key)
                    present = False
                else:
                    # A dotenv path is local metadata, never a shared secret value.
                    present = present and document.values[key] == declaration["file"]
                    if present:
                        try:
                            from .file_material import FileMaterial
                            present = FileMaterial(self.path.parent, declaration["file"], self.store).read() is not None
                        except (RuntimeFailure, OSError):
                            present = False
            if present:
                available.append(key)
            elif declaration["required"]:
                missing.append(key)
        return {"status": "not_ready" if missing else "completed", "available_keys": sorted(available),
                "required_missing": sorted(missing), "unsupported_keys": sorted(unsupported), "secrets_shared": False}

    def apply(self, changes: dict[str, str | None], *, expected: dict[str, str], scope: set[str],
              approved_deletions: set[str] = frozenset(), fault=None) -> dict:
        if not set(changes) <= scope or any(value is None and key not in approved_deletions for key, value in changes.items()):
            raise RuntimeFailure("Secret patch exceeds approved scope or deletion intent.", "decision_required", 3)
        original = self.read()
        if original.values != expected:
            raise RuntimeFailure("Local secret observation changed; rebuild the private patch.", "decision_required", 3)
        updated = patch(original, changes)
        handle = opaque_id()
        self.store.write("secret-backup-" + handle + ".json",
                         {"schema_version": 1, "path": str(self.path), "raw": original.raw})
        parent = open_directory(self.path.parent)
        temporary = ".agent-handoff-env-" + secrets.token_hex(16)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(updated.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if fault:
                fault("before_local_rename")
            if self.read().raw != original.raw:
                raise RuntimeFailure("Local file changed during replacement; original edits are retained.", "rejected", 4)
            os.replace(temporary, self.path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            if fault:
                fault("after_local_rename")
            if self.read().values != parse(updated).values:
                raise RuntimeFailure("Local secret verification failed; preserve private evidence.", "unknown", 5)
            return {"status": "completed", "updated_keys": sorted(changes), "secrets_shared": False,
                    "preservation_handle": handle}
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)
