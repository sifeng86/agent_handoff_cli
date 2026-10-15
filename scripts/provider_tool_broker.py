"""Bounded stdio MCP broker for approved reads and scoped runtime operations."""
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts import landlock_tool_child
from scripts.provider_auth_smoke import AGENT_PHASES, _parse_agent_call
from agent_handoff.public_results import validate_public_result
from scripts.provider_readiness_smoke import EXIT_CODES


MAX_FRAME = 65536
MAX_OUTPUT = 65536
READ_TIMEOUT = 10
ENGINE_TIMEOUT = 30
PROTOCOL_VERSION = "2025-06-18"
READER = Path(__file__).with_name("landlock_tool_child.py").resolve()
SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
PUBLIC_MIRROR_PATHS = {
    "AGENTS.md", "CLAUDE.md", ".agent/instructions.md", ".agent/protocol.json",
    ".agent/owner.json", ".agent/state.json", ".agent/config.yaml", ".agent/secrets.example.yaml",
}
MIRROR_MUST_MATCH = {"AGENTS.md", "CLAUDE.md", ".agent/instructions.md"}
MAX_PUBLIC_FILE = MAX_OUTPUT


class BrokerError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))


def _owned_file(path, *, executable=False):
    if (not isinstance(path, str) or "\x00" in path or
            any(ord(char) < 32 for char in path) or ".." in Path(path).parts):
        raise BrokerError("configuration_rejected")
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.resolve(strict=True) != candidate:
        raise BrokerError("configuration_rejected")
    info = candidate.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
            info.st_mode & 0o077 or (executable and not info.st_mode & 0o100)):
        raise BrokerError("configuration_rejected")
    return candidate


def load_config(config_file):
    try:
        path = _owned_file(config_file)
        if path.stat().st_size > MAX_FRAME:
            raise ValueError("config size")
        config = _json(path.read_bytes())
        if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] not in (1, 2):
            raise ValueError("shape")
        expected_fields = {"version", "policy_file", "engine_proxy", "cwd"}
        if config["version"] == 2:
            expected_fields.update({"managed_root", "source_read_paths"})
        if set(config) != expected_fields:
            raise ValueError("shape")
        cwd = Path(config["cwd"])
        if (not cwd.is_absolute() or cwd.resolve(strict=True) != cwd or not cwd.is_dir()):
            raise ValueError("cwd")
        policy = _owned_file(config["policy_file"])
        engine = _owned_file(config["engine_proxy"], executable=True)
        private = policy.parent
        private_info = private.lstat()
        if (not stat.S_ISDIR(private_info.st_mode) or private_info.st_uid != os.getuid() or
                private_info.st_mode & 0o077 or engine.parent != private or path.parent != private):
            raise ValueError("private directory")
        if policy.parent == cwd or policy.parent in cwd.parents or cwd in policy.parent.parents:
            raise ValueError("policy scope")
        loaded = landlock_tool_child._load_policy(str(policy))
        if loaded[1] != cwd:
            raise ValueError("policy cwd")
        policy_data = _json(policy.read_bytes())
        if config["version"] == 2:
            managed_root = _managed_root(config["managed_root"], cwd, private)
            relative_paths = _source_relpaths(config["source_read_paths"])
            source_files = _validated_source_grants(cwd, relative_paths)
            active_files, policy_template = _load_managed_registry(
                policy, policy_data, cwd, private, managed_root, relative_paths, source_files)
        else:
            managed_root = None
            relative_paths = []
            source_files = {str(p): p for p, access in loaded[3] if access == landlock_tool_child._READ_FILE}
            active_files = {}
            policy_template = policy_data
        source_bytes = {name: path.read_bytes() for name, path in source_files.items()} if config["version"] == 2 else {}
        effective_files = dict(source_files)
        effective_files.update(active_files)
        registry_file = _registry_path(policy)
        return {"version": config["version"], "cwd": cwd, "policy": policy, "engine": engine,
                "read_files": effective_files, "source_read_files": dict(source_files),
                "source_relpaths": relative_paths, "source_bytes": source_bytes,
                "policy_template": policy_template, "managed_root": managed_root,
                "active_managed_files": active_files, "active_policy_bytes": policy.read_bytes(),
                "active_registry_bytes": registry_file.read_bytes() if os.path.lexists(registry_file) else None}
    except BrokerError:
        raise
    except Exception:
        raise BrokerError("configuration_rejected") from None


def _managed_root(value, source, private):
    try:
        if not isinstance(value, str) or ".." in Path(value).parts or "\x00" in value or any(ord(c) < 32 for c in value):
            raise ValueError("path")
        root = Path(value)
        resolved = root.resolve(strict=True)
        info = root.lstat()
        if (not root.is_absolute() or resolved != root or not stat.S_ISDIR(info.st_mode) or
                info.st_uid != os.getuid() or root == source or root in source.parents or source in root.parents or
                root == private or root in private.parents or private in root.parents):
            raise ValueError("scope")
        return root
    except Exception:
        raise BrokerError("configuration_rejected") from None


def _source_relpaths(value):
    if not isinstance(value, list) or not value or len(set(value)) != len(value):
        raise ValueError("source paths")
    if any(not isinstance(item, str) or item not in PUBLIC_MIRROR_PATHS or
           Path(item).is_absolute() or ".." in Path(item).parts or Path(item).as_posix() != item for item in value):
        raise ValueError("source path whitelist")
    if "AGENTS.md" not in value:
        raise ValueError("AGENTS required")
    return sorted(value)


def _validated_source_grants(cwd, relative_paths):
    files = {}
    for relative in relative_paths:
        path = cwd / relative
        if path.resolve(strict=True) != path or cwd not in path.parents:
            raise ValueError("source grant alias")
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
                info.st_size > MAX_PUBLIC_FILE):
            raise ValueError("source grant file")
        files[str(path)] = path
    return files


def _registry_path(policy):
    return policy.with_name(policy.name + ".managed.json")


def _managed_checkout(value, root, source, private):
    if not isinstance(value, str) or ".." in Path(value).parts or "\x00" in value or any(ord(c) < 32 for c in value):
        raise ValueError("managed checkout")
    path = Path(value)
    info = path.lstat()
    if (not path.is_absolute() or path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.getuid() or path == root or root not in path.parents or
            path == source or path in source.parents or source in path.parents or
            path == private or path in private.parents or private in path.parents):
        raise ValueError("managed checkout scope")
    return path


def _validate_managed_files(managed, cwd, private, protected_paths, relative_paths, source_files, source_bytes):
    protected = [Path(item) for item in protected_paths] + [private]
    result = {}
    for relative in relative_paths:
        candidate = managed / relative
        if candidate.resolve(strict=True) != candidate or managed not in candidate.parents:
            raise ValueError("managed file alias")
        info = candidate.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or info.st_size > MAX_PUBLIC_FILE):
            raise ValueError("managed file type")
        if any(item == candidate or item in candidate.parents or candidate in item.parents for item in protected):
            raise ValueError("protected managed file")
        source_path = cwd / relative
        data = candidate.read_bytes()
        if relative in MIRROR_MUST_MATCH and (source_path.read_bytes() != source_bytes[str(source_path)] or
                                               data != source_bytes[str(source_path)]):
            raise ValueError("canonical source mismatch")
        result[str(candidate)] = candidate
    return result


def _load_managed_registry(policy, policy_data, cwd, private, managed_root, relative_paths, source_files):
    sidecar = _registry_path(policy)
    if not os.path.lexists(sidecar):
        if set(policy_data["read_files"]) != set(source_files):
            raise ValueError("managed grants lack receipt")
        template = dict(policy_data)
        template["read_files"] = sorted(source_files)
        return {}, template
    registry_file = _owned_file(str(sidecar))
    info = registry_file.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_FRAME:
        raise ValueError("registry permissions")
    registry = _json(registry_file.read_bytes())
    expected_keys = {"version", "source_cwd", "managed_root", "source_paths", "active_managed_checkout", "managed_paths"}
    if (not isinstance(registry, dict) or set(registry) != expected_keys or registry.get("version") != 1 or
            type(registry.get("version")) is not int or registry["source_cwd"] != str(cwd) or
            registry["managed_root"] != str(managed_root) or registry["source_paths"] != relative_paths):
        raise ValueError("registry identity")
    managed = _managed_checkout(registry["active_managed_checkout"], managed_root, cwd, private)
    managed_files = _validate_managed_files(managed, cwd, private, policy_data["protected_paths"],
                                             relative_paths, source_files, {key: path.read_bytes() for key, path in source_files.items()})
    expected_managed = sorted(managed_files)
    if registry["managed_paths"] != expected_managed or set(policy_data["read_files"]) != set(source_files) | set(managed_files):
        raise ValueError("registry grants mismatch")
    template = dict(policy_data)
    template["read_files"] = sorted(source_files)
    return managed_files, template


def _clean_env(cwd, *, wrapper_dir=None):
    env = dict(SAFE_ENV)
    env["HOME"] = str(cwd)
    env["TMPDIR"] = str(cwd)
    if wrapper_dir is not None:
        env["PATH"] = str(wrapper_dir) + ":/usr/bin:/bin"
    return env


def _run(argv, *, cwd, env, timeout, output_limit=MAX_OUTPUT):
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               close_fds=True, start_new_session=True)
    selector = None
    outputs = {process.stdout: bytearray(), process.stderr: bytearray()}
    succeeded = False
    try:
        selector = selectors.DefaultSelector()
        for stream in outputs:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrokerError("tool_timeout")
            for key, _ in selector.select(min(remaining, 0.2)):
                data = os.read(key.fd, 8192)
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buf = outputs[key.fileobj]
                if sum(map(len, outputs.values())) + len(data) > output_limit:
                    raise BrokerError("tool_output_rejected")
                buf.extend(data)
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        succeeded = True
        return code, bytes(outputs[process.stdout]), bytes(outputs[process.stderr])
    except BaseException:
        raise
    finally:
        try:
            if not succeeded:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    process.wait(timeout=1)
                except BaseException:
                    pass
        finally:
            if selector is not None:
                try:
                    selector.close()
                except Exception:
                    pass
            for stream in outputs:
                if not stream.closed:
                    try:
                        stream.close()
                    except Exception:
                        pass


def _arguments(request, expected):
    if not isinstance(request, dict) or set(request) != expected:
        raise BrokerError("invalid_arguments")
    return request


class Broker:
    def __init__(self, config):
        self.config = load_config(config) if isinstance(config, (str, Path)) else config

    def tool_descriptions(self):
        return [
            {"name": "read_file", "description": "Read one approved public file using its absolute canonical path; writes are unavailable.",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                             "required": ["path"], "additionalProperties": False}},
            {"name": "agentctl", "description": "Run approved discovery, resume, and secret diagnostics. Complete inspect or status before resume, as required by the canonical instructions.",
             "inputSchema": {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
                             "required": ["argv"], "additionalProperties": False}},
        ]

    def call_tool(self, name, arguments):
        if name == "read_file":
            return self.read_file(arguments)
        if name == "agentctl":
            return self.agentctl(arguments)
        raise BrokerError("tool_not_found")

    def read_file(self, arguments):
        args = _arguments(arguments, {"path"})
        raw_path = args["path"]
        if not isinstance(raw_path, str) or "\x00" in raw_path or any(ord(c) < 32 for c in raw_path):
            raise BrokerError("read_denied")
        try:
            path = Path(raw_path)
            if not path.is_absolute() or path.resolve(strict=True) != path:
                raise ValueError("path")
            approved = self.config["read_files"].get(str(path))
            if approved is None:
                raise ValueError("not approved")
            info = approved.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_OUTPUT:
                raise ValueError("file")
            command = [sys.executable, "-I", "-S", "-B", str(READER), "--policy-file",
                       str(self.config["policy"]), "--", "/usr/bin/cat", "--", str(approved)]
            code, stdout, stderr = _run(command, cwd=self.config["cwd"],
                                        env=_clean_env(self.config["cwd"]), timeout=READ_TIMEOUT)
            if code != 0 or stderr:
                raise BrokerError("read_denied")
            return stdout.decode("utf-8", "strict")
        except BrokerError:
            raise
        except Exception:
            raise BrokerError("read_denied") from None

    def agentctl(self, arguments):
        args = _arguments(arguments, {"argv"})
        argv = args["argv"]
        if (not isinstance(argv, list) or not argv or len(argv) > 64 or
                any(not isinstance(item, str) or not item or "\x00" in item or
                    any(ord(char) < 32 for char in item) for item in argv) or
                sum(len(item.encode("utf-8")) for item in argv) > 8192):
            raise BrokerError("engine_rejected")
        phase, _extra = _parse_agent_call(argv)
        if phase not in AGENT_PHASES or phase == "help":
            raise BrokerError("engine_rejected")
        try:
            code, stdout, stderr = _run([sys.executable, "-I", "-S", "-B", str(self.config["engine"]), *argv],
                                        cwd=self.config["cwd"], env=_clean_env(self.config["cwd"], wrapper_dir=self.config["engine"].parent),
                                        timeout=ENGINE_TIMEOUT)
            if stderr:
                raise BrokerError("engine_rejected")
            result = _json(stdout.strip())
            result = validate_public_result(result)
            if (not isinstance(result, dict) or result.get("status") not in EXIT_CODES or
                    result.get("command") != phase or code != EXIT_CODES[result["status"]]):
                raise BrokerError("engine_rejected")
            if (phase == "resume" and self.config["version"] == 2 and
                    result.get("status") == "ready" and result.get("ownership") == "owned"):
                self._refresh_managed_grants(result)
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except BrokerError:
            raise
        except Exception:
            raise BrokerError("engine_rejected") from None

    def _refresh_managed_grants(self, result):
        try:
            managed = _managed_checkout(result.get("managed_checkout"), self.config["managed_root"],
                                        self.config["cwd"], self.config["policy"].parent)
            policy_template = self.config["policy_template"]
            refreshed = dict(self.config["source_read_files"])
            managed_files = _validate_managed_files(managed, self.config["cwd"],
                self.config["policy"].parent, policy_template["protected_paths"],
                self.config["source_relpaths"], self.config["source_read_files"], self.config["source_bytes"])
            refreshed.update(managed_files)
            new_policy = dict(policy_template)
            new_policy["read_files"] = sorted(refreshed)
            registry = {"version": 1, "source_cwd": str(self.config["cwd"]),
                        "managed_root": str(self.config["managed_root"]),
                        "source_paths": self.config["source_relpaths"],
                        "active_managed_checkout": str(managed),
                        "managed_paths": sorted(managed_files)}
            policy_bytes, registry_bytes = self._write_policy_atomically(new_policy, registry)
            self.config["read_files"] = refreshed
            self.config["active_managed_files"] = managed_files
            self.config["active_policy_bytes"] = policy_bytes
            self.config["active_registry_bytes"] = registry_bytes
        except Exception:
            raise BrokerError("public_grant_refresh_failed_after_resume") from None

    def _write_policy_atomically(self, value, registry):
        policy = self.config["policy"]
        parent = policy.parent
        temp = parent / (".broker-policy-" + uuid.uuid4().hex)
        sidecar = _registry_path(policy)
        registry_temp = parent / (".broker-registry-" + uuid.uuid4().hex)
        raw = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        registry_raw = (json.dumps(registry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        fds = []
        try:
            if policy.read_bytes() != self.config["active_policy_bytes"]:
                raise ValueError("policy changed outside broker")
            current_registry = sidecar.read_bytes() if os.path.lexists(sidecar) else None
            if current_registry != self.config["active_registry_bytes"]:
                raise ValueError("registry changed outside broker")
            current = landlock_tool_child._load_policy(str(policy))
            if current[1] != self.config["cwd"]:
                raise ValueError("policy changed")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            for target, content in ((temp, raw), (registry_temp, registry_raw)):
                fd = os.open(target, flags, 0o600)
                fds.append(fd)
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.close(fd)
                fds.remove(fd)
            validated = landlock_tool_child._load_policy(str(temp))
            if validated[1] != self.config["cwd"]:
                raise ValueError("candidate policy")
            candidate_policy = _json(temp.read_bytes())
            candidate_registry = _json(registry_temp.read_bytes())
            expected_paths = {str(self.config["cwd"] / item) for item in registry["source_paths"]}
            expected_paths.update(registry["managed_paths"])
            if (set(candidate_policy["read_files"]) != expected_paths or candidate_registry != registry or
                    stat.S_IMODE(registry_temp.stat().st_mode) != 0o600):
                raise ValueError("candidate policy grants")
            os.replace(temp, policy)
            self._fsync_private_dir(parent)
            os.replace(registry_temp, sidecar)
            self._fsync_private_dir(parent)
            return raw, registry_raw
        except Exception:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        finally:
            for target in (temp, registry_temp):
                try:
                    target.unlink()
                except OSError:
                    pass

    @staticmethod
    def _fsync_private_dir(path):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _response(identifier, result=None, error=None):
    value = {"jsonrpc": "2.0", "id": identifier}
    if error is not None:
        value["error"] = error
    else:
        value["result"] = result
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _params_with_meta(params, required):
    if not isinstance(params, dict) or not required <= set(params) or set(params) - required - {"_meta"}:
        return False
    return "_meta" not in params or isinstance(params["_meta"], dict)


def serve(config, source=None, sink=None, *, broker_factory=None):
    source = sys.stdin.buffer if source is None else source
    sink = sys.stdout.buffer if sink is None else sink
    broker = Broker(config) if broker_factory is None else broker_factory(config)
    initialize_response_sent = False
    initialized_notification_seen = False
    while True:
        line = source.readline(MAX_FRAME + 1)
        if not line:
            return 0
        if len(line) > MAX_FRAME or not line.endswith(b"\n"):
            return 1
        identifier = None
        try:
            request = _json(line[:-1])
            if (not isinstance(request, dict) or set(request) - {"jsonrpc", "id", "method", "params"} or
                    request.get("jsonrpc") != "2.0" or "method" not in request):
                raise ValueError("request")
            identifier = request.get("id")
            if identifier is not None and (isinstance(identifier, bool) or not isinstance(identifier, (str, int))):
                raise ValueError("id")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params")
            if identifier is None and method.startswith("notifications/"):
                if method == "notifications/initialized" and initialize_response_sent:
                    initialized_notification_seen = True
                continue
            if method == "initialize" and identifier is not None:
                initialized_notification_seen = False
                result = {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "agent-handoff-broker", "version": "1"}}
                if result["protocolVersion"] not in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}:
                    result["protocolVersion"] = PROTOCOL_VERSION
                sink.write(_response(identifier, result)); sink.flush()
                initialize_response_sent = True
                continue
            if not initialize_response_sent or not initialized_notification_seen:
                raise ValueError("not initialized")
            if method == "tools/list" and identifier is not None and _params_with_meta(params, set()):
                sink.write(_response(identifier, {"tools": broker.tool_descriptions()})); sink.flush()
                continue
            if (method == "tools/call" and identifier is not None and
                    _params_with_meta(params, {"name", "arguments"}) and isinstance(params.get("name"), str)):
                try:
                    text = broker.call_tool(params["name"], params["arguments"])
                    sink.write(_response(identifier, {"content": [{"type": "text", "text": text}], "isError": False}))
                except BrokerError as exc:
                    sink.write(_response(identifier, {"content": [{"type": "text", "text": exc.code}], "isError": True}))
                sink.flush()
                continue
            raise ValueError("method")
        except Exception:
            if identifier is not None:
                sink.write(_response(identifier, error={"code": -32600, "message": "invalid request"}))
                sink.flush()
            else:
                return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "--config-file":
        sys.stderr.write("provider_tool_broker: invalid invocation\n")
        return 2
    try:
        return serve(argv[1])
    except BrokerError as exc:
        sys.stderr.write("provider_tool_broker: " + exc.code + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
