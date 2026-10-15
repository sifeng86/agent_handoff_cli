"""Descriptor-relative, owner-private, durable Linux state storage."""
import json
import os
from pathlib import Path
import stat
import secrets

from .contracts import RuntimeFailure, _unique_pairs


class PrivateStore:
    def __init__(self, root: Path, repository: Path):
        self.root = root.absolute()
        repository = repository.resolve()
        if self.root.resolve().is_relative_to(repository):
            raise RuntimeFailure("Private state must be outside the repository.")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in self.root.parts[1:]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            self._check_fd(fd, directory=True)
            self.fd = fd
        except OSError:
            os.close(fd)
            raise RuntimeFailure("Unsafe or inaccessible private state path.") from None
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _check_fd(fd: int, *, directory: bool = False):
        info = os.fstat(fd)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeFailure("Unsafe private state ownership, type, or permissions.")
        if not directory and info.st_nlink != 1:
            raise RuntimeFailure("Private records cannot have multiple hard links.")

    @staticmethod
    def _name(name: str):
        if not name or Path(name).name != name or name in (".", "..") or "\x00" in name:
            raise RuntimeFailure("Private record name must be a single path component.")

    def open_file(self, name: str, *, create: bool = False) -> int:
        self._name(name)
        self._check_fd(self.fd, directory=True)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT
        try:
            fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        except FileNotFoundError:
            raise
        except OSError:
            raise RuntimeFailure("Unsafe or inaccessible private record.") from None
        try:
            self._check_fd(fd)
            return fd
        except Exception:
            os.close(fd)
            raise

    def read(self, name: str, *, missing: bool = False) -> dict | None:
        try:
            fd = self.open_file(name)
        except FileNotFoundError:
            if missing:
                return None
            raise RuntimeFailure("Required private record is missing.") from None
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                value = json.load(stream, object_pairs_hook=_unique_pairs,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            raise RuntimeFailure("Invalid private record.") from None
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise RuntimeFailure("Invalid private record version.")
        return value

    def write(self, name: str, value: dict):
        self._name(name)
        try:
            existing = self.open_file(name)
        except FileNotFoundError:
            pass
        else:
            os.close(existing)
        encoded = json.dumps(value, allow_nan=False, sort_keys=True).encode("utf-8")
        temporary = ".record-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def names(self) -> list[str]:
        return sorted(os.listdir(self.fd))

    def close(self):
        if getattr(self, "fd", None) is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        self.close()
