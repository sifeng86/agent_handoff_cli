"""Private text PEM material; local paths never become shared values."""
import base64
import binascii
import json
import os
from pathlib import Path
import re
import secrets
import stat

from ..contracts import RuntimeFailure
from ..journal import opaque_id
from .local_env import open_directory
from .manifest import parse as parse_manifest


BLOCK = re.compile(r"-----BEGIN ([A-Z0-9][A-Z0-9 -]*)-----\r?\n(.*?)\r?\n-----END \1-----", re.DOTALL)


def validate_pem(value: str):
    if not isinstance(value, str) or not value.isascii() or len(value) > 8 * 1024 * 1024:
        raise RuntimeFailure("File-backed material requires supported text PEM.", "unsupported", 8)
    position, count = 0, 0
    for block in BLOCK.finditer(value):
        if value[position:block.start()].strip():
            raise RuntimeFailure("Invalid text PEM framing.")
        lines = block[2].splitlines()
        # Traditional encrypted PEM may include metadata headers and a blank separator.
        if lines and ":" in lines[0]:
            while lines and lines[0].strip():
                if not re.fullmatch(r"[A-Za-z0-9-]+: [ -~]+", lines.pop(0)):
                    raise RuntimeFailure("Invalid text PEM header.")
            if not lines:
                raise RuntimeFailure("Invalid text PEM body.")
            lines.pop(0)
        encoded = "".join(lines)
        try:
            if not encoded or not base64.b64decode(encoded, validate=True):
                raise ValueError()
        except (ValueError, binascii.Error):
            raise RuntimeFailure("Invalid text PEM body.") from None
        position, count = block.end(), count + 1
    if not count or value[position:].strip():
        raise RuntimeFailure("Invalid text PEM framing.")


class FileMaterial:
    def __init__(self, checkout: Path, mapping: str, store):
        parse_manifest(json.dumps({"schema_version": 1, "keys": {
            "MATERIAL": {"classification": "file-backed", "required": True, "file": mapping}}}))
        self.checkout, self.mapping, self.store = checkout, mapping, store
        if Path(mapping).suffix.lower() in {".pfx", ".p12"}:
            raise RuntimeFailure("Binary file-backed material is unsupported.", "unsupported", 8)

    def parent(self, *, create=False):
        fd = open_directory(self.checkout)
        try:
            for component in self.mapping.split("/")[:-1]:
                if create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except OSError:
            os.close(fd)
            raise

    def read(self):
        try:
            parent = self.parent()
        except FileNotFoundError:
            return None
        except OSError:
            raise RuntimeFailure("Unsafe file-backed directory.") from None
        try:
            try:
                fd = os.open(Path(self.mapping).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                        info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 8 * 1024 * 1024):
                    raise RuntimeFailure("File-backed material requires owner-private regular files.")
                value = stream.read(8 * 1024 * 1024 + 1).decode("ascii")
            validate_pem(value)
            return value
        except (OSError, UnicodeError):
            raise RuntimeFailure("File-backed material is unavailable or unsafe.") from None
        finally:
            os.close(parent)

    def apply(self, value: str | None, *, expected: str | None, fault=None):
        if value is not None:
            validate_pem(value)
        if self.read() != expected:
            raise RuntimeFailure("File-backed observation changed.", "decision_required", 3)
        handle = opaque_id()
        self.store.write("secret-file-backup-" + handle + ".json",
                         {"schema_version": 1, "mapping": self.mapping, "raw": expected})
        try:
            parent = self.parent(create=value is not None)
        except FileNotFoundError:
            return handle
        except OSError:
            raise RuntimeFailure("Unsafe file-backed directory.") from None
        temporary = ".agent-handoff-material-" + secrets.token_hex(16)
        try:
            if value is not None:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(value.encode("ascii"))
                    stream.flush()
                    os.fsync(stream.fileno())
            if fault:
                fault("before_file_material_rename")
            if self.read() != expected:
                raise RuntimeFailure("File-backed material changed during replacement.", "rejected", 4)
            if value is None:
                try:
                    os.unlink(Path(self.mapping).name, dir_fd=parent)
                except FileNotFoundError:
                    pass
            else:
                os.replace(temporary, Path(self.mapping).name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            if self.read() != value:
                raise RuntimeFailure("File-backed verification failed.", "unknown", 5)
            return handle
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)
