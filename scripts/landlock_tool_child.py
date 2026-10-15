"""Run one tool child under a fail-closed Landlock filesystem policy (ABI 1).

This restricts filesystem access only. ABI 1 does not mediate network access,
truncate, or complete metadata changes such as chmod/chown/utime. It is not a
read-only or file-integrity sandbox and is not sufficient for unrestricted
shell/Python tools or provider credentials; callers must expose only bounded,
trusted tools and synthetic/private-free fixture data. A trusted spawner must
scrub the environment before starting this Python bootstrap and invoke Python
with -I -S; this runner can only sanitize the environment after bootstrap,
so it cannot prevent startup-time LD_PRELOAD or Python environment effects.
"""
import ctypes
import json
import os
from pathlib import Path
import platform
import stat
import sys


_SYSCALLS = {"x86_64": (444, 445, 446), "aarch64": (444, 445, 446)}
_VERSION = 1
_RULE_PATH_BENEATH = 1
_VERSION_QUERY = 1
_HANDLED = (1 << 13) - 1
_READ_SYSTEM = (1 << 0) | (1 << 2) | (1 << 3)  # EXECUTE | READ_FILE | READ_DIR
_READ_DIR = 1 << 3
_READ_FILE = 1 << 2
_WRITE_FILE = 1 << 1
_O_PATH = getattr(os, "O_PATH", 0o10000000)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _safe_path(value, *, expected_type=None, strict=True):
    if (not isinstance(value, str) or not value.startswith("/") or "\x00" in value or
            any(ord(char) < 32 for char in value)):
        raise ValueError("path")
    path = Path(value)
    if ".." in path.parts or str(path) != value:
        raise ValueError("path")
    resolved = path.resolve(strict=strict)
    if str(resolved) != value:
        raise ValueError("alias")
    info = path.lstat()
    if expected_type == "dir" and not stat.S_ISDIR(info.st_mode):
        raise ValueError("directory")
    if expected_type == "file" and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ValueError("regular file")
    if expected_type is None and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError("protected type")
    return path


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _load_policy(policy_file):
    policy_path = _safe_path(policy_file, expected_type="file")
    info = policy_path.stat()
    if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ValueError("policy mode")
    with policy_path.open("rb") as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError("policy size")
    value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    if (not isinstance(value, dict) or set(value) != {"version", "cwd", "read_files", "list_dirs", "protected_paths"} or
            type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("policy shape")

    cwd = _safe_path(value["cwd"], expected_type="dir")
    if not all(isinstance(value[name], list) for name in ("read_files", "list_dirs", "protected_paths")):
        raise ValueError("policy list")
    protected = [_safe_path(path) for path in value["protected_paths"]]
    if len(set(protected)) != len(protected):
        raise ValueError("duplicate protected path")
    # The policy's private containing directory is never visible to the tool child.
    protected.append(policy_path.parent.resolve(strict=True))

    read_files = [_safe_path(path, expected_type="file") for path in value["read_files"]]
    list_dirs = [_safe_path(path, expected_type="dir") for path in value["list_dirs"]]
    if len(set(read_files)) != len(read_files) or len(set(list_dirs)) != len(list_dirs):
        raise ValueError("duplicate allowed path")

    grants = [(cwd, _READ_DIR)] + [(path, _READ_FILE) for path in read_files] + [(path, _READ_DIR) for path in list_dirs]
    for path, _ in grants:
        if any(_overlap(path, private) for private in protected):
            raise ValueError("protected overlap")

    system_dirs = []
    for candidate in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
        if not os.path.lexists(candidate):
            continue
        actual = candidate.resolve(strict=True)
        if actual not in system_dirs:
            info = actual.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("system directory")
            if any(_overlap(actual, private) for private in protected):
                raise ValueError("system protected overlap")
            system_dirs.append(actual)
    loader_cache = Path("/etc/ld.so.cache")
    cache_info = loader_cache.lstat()
    if not stat.S_ISREG(cache_info.st_mode):
        raise ValueError("loader cache")
    if any(_overlap(loader_cache, private) for private in protected):
        raise ValueError("loader cache overlap")
    return policy_path, cwd, protected, grants, system_dirs, loader_cache


def _landlock_syscalls():
    if sys.platform != "linux":
        raise OSError("unsupported platform")
    numbers = _SYSCALLS.get(platform.machine())
    if numbers is None:
        raise OSError("unsupported architecture")
    return numbers


def _syscall(libc, number, *args):
    result = libc.syscall(number, *args)
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, "landlock syscall failed")
    return result


def _apply_landlock(grants, system_dirs, loader_cache):
    create_nr, add_nr, restrict_nr = _landlock_syscalls()
    libc = ctypes.CDLL(None, use_errno=True)
    abi = _syscall(libc, create_nr, None, ctypes.c_size_t(0), ctypes.c_uint32(_VERSION_QUERY))
    if abi < _VERSION:
        raise OSError("Landlock ABI unavailable")
    attr = _RulesetAttr(_HANDLED)
    ruleset_fd = _syscall(libc, create_nr, ctypes.byref(attr), ctypes.sizeof(attr), ctypes.c_uint32(0))
    rule_fds = []
    try:
        all_grants = list(grants)
        all_grants.extend((path, _READ_SYSTEM) for path in system_dirs)
        all_grants.append((loader_cache, _READ_FILE))
        for path, access in all_grants:
            flags = _O_PATH | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            rule_fds.append(fd)
            attr_rule = _PathBeneathAttr(access, fd)
            _syscall(libc, add_nr, ruleset_fd, ctypes.c_uint32(_RULE_PATH_BENEATH),
                     ctypes.byref(attr_rule), ctypes.c_uint32(0))
        for fd in rule_fds:
            os.close(fd)
        rule_fds.clear()
        # PR_SET_NO_NEW_PRIVS, required before restricting the current process.
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl failed")
        _syscall(libc, restrict_nr, ruleset_fd, ctypes.c_uint32(0))
    finally:
        for fd in rule_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        os.close(ruleset_fd)


def _close_extra_fds():
    libc = ctypes.CDLL(None, use_errno=True)
    close_range_nr = 436
    _syscall(libc, close_range_nr, ctypes.c_uint32(3), ctypes.c_uint32(0xFFFFFFFF), ctypes.c_uint32(0))


def run_child(policy_file, argv):
    if not argv or not argv[0]:
        raise ValueError("argv")
    policy_path, cwd, protected, grants, system_dirs, loader_cache = _load_policy(policy_file)
    # Never inherit open descriptors that could retain a pre-sandbox private file or directory.
    os.chdir(cwd)
    _apply_landlock(grants, system_dirs, loader_cache)
    _close_extra_fds()
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "HOME": str(cwd), "TMPDIR": str(cwd)}
    os.execvpe(argv[0], argv, env)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 4 or argv[0] != "--policy-file" or argv[2] != "--":
        sys.stderr.write("landlock_tool_child: invalid invocation\n")
        return 125
    try:
        run_child(argv[1], argv[3:])
    except Exception:
        sys.stderr.write("landlock_tool_child: filesystem sandbox unavailable or policy rejected\n")
        return 126
    return 126


if __name__ == "__main__":
    raise SystemExit(main())
