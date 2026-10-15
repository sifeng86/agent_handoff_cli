"""Read-only host configuration observations; no host repair or policy mutation."""
import hashlib
import os
from pathlib import Path
import stat

from .contracts import RuntimeFailure
from .journal import opaque_id, validate_id
from .record_checks import shape, match, IDENTITY
from .secrets.local_env import open_directory


def default_roots():
    home = Path.home()
    return {
        "ssh": [home / ".ssh", Path("/etc/ssh")],
        "git": [home / ".gitconfig", home / ".config/git"],
        "provider": [home / ".codex/config.toml", home / ".claude/settings.json", home / ".doppler"],
        "shell": [home / ".profile", home / ".bashrc", home / ".zshrc", Path("/etc/profile.d")],
        "network": [Path("/etc/netplan"), Path("/etc/NetworkManager/system-connections"), Path("/etc/resolv.conf")],
        "vpn": [Path("/etc/openvpn"), Path("/etc/wireguard")],
        "firewall": [Path("/etc/ufw"), Path("/etc/nftables.conf")],
        "users_security": [Path("/etc/passwd"), Path("/etc/group"), Path("/etc/sudoers"), Path("/etc/security")],
        "package_sources": [Path("/etc/apt/sources.list"), Path("/etc/apt/sources.list.d")],
        "services": [Path("/etc/systemd/system")],
    }


def metadata(info):
    # Reading a file may change atime; it does not change host configuration.
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def require_fresh_prefix(path):
    if os.path.lexists(path):
        raise RuntimeFailure("Existing installation is retained; select a fresh isolated prefix.", "rejected", 4)


def tree(parent, name, budget, depth=0):
    budget[0] += 1
    if budget[0] > 10000 or depth > 64:
        raise RuntimeFailure("Host observation exceeds its bounded scope.")
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode):
        value = hashlib.sha256(os.readlink(name, dir_fd=parent).encode()).hexdigest()
    elif stat.S_ISDIR(before.st_mode):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if metadata(os.fstat(fd)) != metadata(before):
                raise RuntimeFailure("Host directory changed during observation.")
            value = {child: tree(fd, child, budget, depth + 1) for child in sorted(os.listdir(fd))}
        finally:
            os.close(fd)
    elif stat.S_ISREG(before.st_mode):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if metadata(os.fstat(fd)) != metadata(before):
                raise RuntimeFailure("Host file changed during observation.")
            digest, count = hashlib.sha256(), 0
            while block := os.read(fd, 65536):
                count += len(block)
                if count > 16 * 1024 * 1024:
                    raise RuntimeFailure("Host file exceeds bounded observation size.")
                digest.update(block)
            if metadata(os.fstat(fd)) != metadata(before):
                raise RuntimeFailure("Host file changed during observation.")
            value = digest.hexdigest()
        finally:
            os.close(fd)
    else:
        raise RuntimeFailure("Host special file cannot be verified safely.")
    if metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)) != metadata(before):
        raise RuntimeFailure("Host entry changed during observation.")
    return {"metadata": metadata(before), "value": value}


def observe(roots):
    observations = {}
    for category, paths in roots.items():
        entries = {}
        for path in paths:
            path = Path(path).absolute()
            try:
                parent = open_directory(path.parent, allow_missing=True)
                if parent is None:
                    entries[str(path)] = {"status": "absent"}
                    continue
                try:
                    try:
                        initial = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        entries[str(path)] = {"status": "absent"}
                        continue
                    observation = tree(parent, path.name, [0])
                    if observation["metadata"] != metadata(initial):
                        raise RuntimeFailure("Host root changed during observation.")
                    entries[str(path)] = {"status": "observed", "tree": observation}
                finally:
                    os.close(parent)
            except (OSError, RuntimeFailure, UnicodeError):
                entries[str(path)] = {"status": "unverifiable"}
        observations[category] = entries
    return observations


def unverifiable(entries):
    def linked(value):
        if stat.S_ISLNK(value["metadata"][2]):
            return True
        return isinstance(value["value"], dict) and any(linked(child) for child in value["value"].values())
    return any(entry["status"] == "unverifiable" or
               (entry["status"] == "observed" and linked(entry["tree"])) for entry in entries.values())


def validate_record(record):
    shape(record, {"schema_version", "handle_id", "observations"})
    validate_id(record["handle_id"])
    if not isinstance(record["observations"], dict):
        raise RuntimeFailure("Invalid private host observations.")
    budget = [0]
    def checked_tree(value, depth=0):
        budget[0] += 1
        if budget[0] > 10000 or depth > 64 or not isinstance(value, dict) or set(value) != {"metadata", "value"}:
            raise RuntimeFailure("Invalid private host observation tree.")
        info = value["metadata"]
        if not isinstance(info, list) or len(info) != 8 or any(type(item) is not int for item in info) or any(item < 0 for item in info[:6]):
            raise RuntimeFailure("Invalid private host metadata.")
        if stat.S_ISDIR(info[2]):
            if not isinstance(value["value"], dict):
                raise RuntimeFailure("Invalid private host directory observation.")
            for name, child in value["value"].items():
                if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\x00" in name:
                    raise RuntimeFailure("Invalid private host entry name.")
                checked_tree(child, depth + 1)
        elif not (stat.S_ISREG(info[2]) or stat.S_ISLNK(info[2])) or not match(value["value"], IDENTITY):
            raise RuntimeFailure("Invalid private host file observation.")
    for category, entries in record["observations"].items():
        if not isinstance(category, str) or not category or not isinstance(entries, dict):
            raise RuntimeFailure("Invalid private host inspection scope.")
        for path, entry in entries.items():
            if not isinstance(path, str) or not path.startswith("/") or not isinstance(entry, dict):
                raise RuntimeFailure("Invalid private host inspection path.")
            if entry.get("status") == "observed":
                if set(entry) != {"status", "tree"}:
                    raise RuntimeFailure("Invalid private host observation fields.")
                checked_tree(entry["tree"])
            elif entry.get("status") not in ("absent", "unverifiable") or set(entry) != {"status"}:
                raise RuntimeFailure("Invalid private host observation status.")
    return record


class HostGuard:
    def __init__(self, store, roots=None):
        self.store = store
        self.roots = default_roots() if roots is None else roots

    def inspect(self):
        handle = opaque_id()
        observations = observe(self.roots)
        self.store.write("host-preservation-" + handle + ".json",
                         {"schema_version": 1, "handle_id": handle, "observations": observations})
        unavailable = sorted(category for category, entries in observations.items()
                             if unverifiable(entries))
        return {"preservation_handle": handle, "inspected_categories": sorted(observations),
                "unverifiable_categories": unavailable, "host_changes_attempted": False,
                "scope": "selected_linux_configuration_entries"}

    def verify(self, handle):
        validate_id(handle)
        record = validate_record(self.store.read("host-preservation-" + handle + ".json"))
        if record["handle_id"] != handle:
            raise RuntimeFailure("Invalid host observation identity.")
        current = observe(self.roots)
        # Compare the entire original record, including paths and nested evidence.
        if set(record["observations"]) != set(current):
            raise RuntimeFailure("Host inspection scope changed.")
        changed, preserved, unavailable = [], [], []
        for category, entries in current.items():
            previous = record["observations"][category]
            if not isinstance(previous, dict) or set(previous) != set(entries):
                raise RuntimeFailure("Host inspection paths changed.")
            unknown = unverifiable(entries) or unverifiable(previous)
            if unknown:
                unavailable.append(category)
            if previous != entries and not any(entry["status"] == "unverifiable" for entry in [*entries.values(), *previous.values()]):
                changed.append(category)
            elif not unknown:
                preserved.append(category)
        return {"preservation_handle": handle, "preserved_categories": sorted(preserved),
                "changed_categories": sorted(changed), "unverifiable_categories": sorted(unavailable),
                "host_changes_attempted": False, "scope": "selected_linux_configuration_entries",
                "status": "completed" if not changed and not unavailable else "not_ready"}
