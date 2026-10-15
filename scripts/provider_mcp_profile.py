"""Claude MCP-only launch profile and strict native stream-json adapter."""
import json
import os
from pathlib import Path
import shlex
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff.public_results import validate_public_result
from scripts.provider_auth_smoke import AGENT_PHASES, _parse_agent_call
from scripts.provider_readiness_smoke import EXIT_CODES
from scripts.provider_tool_broker import _json, load_config
from scripts.provider_bootstrap_smoke import EvidenceError


MODEL = "claude-haiku-5-5"
SERVER = "handoff"
TOOL_NAMES = {"mcp__handoff__read_file", "mcp__handoff__agentctl"}
MCP_CONFIG_NAME = "handoff-mcp.json"


def _private_directory(path):
    try:
        directory = Path(path)
        canonical = directory.resolve(strict=True)
        info = directory.lstat()
    except Exception:
        raise EvidenceError("mcp_private_directory_invalid")
    if (not directory.is_absolute() or canonical != directory or not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise EvidenceError("mcp_private_directory_invalid")
    return directory


def _broker_config_path(path):
    try:
        value = Path(path)
        if not value.is_absolute() or value.resolve(strict=True) != value:
            raise ValueError("path")
        info = value.lstat()
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("mode")
        load_config(str(value))
    except Exception:
        raise EvidenceError("mcp_broker_config_invalid")
    return value


def _mcp_value(cwd, broker_config, program="provider_tool_broker.py"):
    args = ["-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "PYTHONDONTWRITEBYTECODE=1",
            "HOME=" + str(cwd), "TMPDIR=" + str(cwd), sys.executable, "-I", "-S", "-B",
            str(ROOT / "scripts" / program), "--config-file", str(broker_config)]
    return {"mcpServers": {SERVER: {"command": "/usr/bin/env", "args": args}}}


def _write_profile_config(destination, broker, loaded, program):
    dest = Path(destination)
    if not dest.is_absolute() or dest.parent.resolve(strict=True) != dest.parent:
        raise EvidenceError("mcp_config_destination_invalid")
    _private_directory(dest.parent)
    if dest.exists() or os.path.lexists(dest):
        raise EvidenceError("mcp_config_destination_exists")
    value = _mcp_value(loaded["cwd"], broker, program)
    raw = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(dest, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            dest.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    return dest


def write_mcp_config(destination, broker_config):
    broker = _broker_config_path(broker_config)
    loaded = load_config(str(broker))
    return _write_profile_config(destination, broker, loaded, "provider_tool_broker.py")


def _validate_profile_config(mcp_config, *, broker_validator, profile_builder):
    try:
        path = Path(mcp_config)
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ValueError("path")
        info = path.lstat()
    except Exception:
        raise EvidenceError("mcp_config_untrusted")
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
            stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
        raise EvidenceError("mcp_config_untrusted")
    _private_directory(path.parent)
    try:
        value = _json(path.read_bytes())
        servers = value["mcpServers"]
        if not isinstance(value, dict) or set(value) != {"mcpServers"} or set(servers) != {SERVER}:
            raise ValueError("shape")
        server = servers[SERVER]
        if not isinstance(server, dict) or set(server) != {"command", "args"}:
            raise ValueError("server")
        args = server["args"]
        if (not isinstance(args, list) or any(not isinstance(item, str) for item in args) or
                server["command"] != "/usr/bin/env" or len(args) < 8 or args[-2:] != ["--config-file", args[-1]]):
            raise ValueError("args")
        broker, loaded = broker_validator(args[-1])
        if value != profile_builder(loaded["cwd"], broker):
            raise ValueError("profile mismatch")
        return path
    except EvidenceError:
        raise
    except Exception:
        raise EvidenceError("mcp_config_untrusted") from None


def _validate_mcp_config(mcp_config):
    def validate(path):
        broker = _broker_config_path(path)
        return broker, load_config(str(broker))
    return _validate_profile_config(
        mcp_config, broker_validator=validate,
        profile_builder=lambda cwd, broker: _mcp_value(cwd, broker, "provider_tool_broker.py"))


def _canonical_executable(path):
    executable = Path(path)
    if not executable.is_absolute() or executable.resolve(strict=True) != executable:
        raise EvidenceError("claude_executable_invalid")
    info = executable.lstat()
    if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
        raise EvidenceError("claude_executable_invalid")
    return executable


def _claude_profile_command(claude, mcp_config, model, validator, allowed_tools):
    if model != MODEL:
        raise EvidenceError("claude_model_not_approved")
    executable = _canonical_executable(claude)
    config = validator(mcp_config)
    return [str(executable), "-p", "--model", MODEL, "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--setting-sources", "project", "--settings", '{"disableAllHooks":true}',
            "--strict-mcp-config", "--mcp-config", str(config), "--disable-slash-commands", "--tools", "",
            "--allowedTools", allowed_tools, "--permission-mode", "dontAsk"]


def claude_mcp_command(claude, mcp_config, model=MODEL):
    return _claude_profile_command(claude, mcp_config, model, _validate_mcp_config,
                                   "mcp__handoff__read_file,mcp__handoff__agentctl")


def _event_error(code):
    raise EvidenceError(code)


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        _event_error("mcp_tool_output_invalid")
    chunks = []
    for part in content:
        if not isinstance(part, dict) or set(part) != {"type", "text"} or part["type"] != "text" or not isinstance(part["text"], str):
            _event_error("mcp_tool_output_invalid")
        chunks.append(part["text"])
    return "\n".join(chunks)


def _normalize_result(name, arguments, result_block):
    failed = result_block.get("is_error", False)
    if type(failed) is not bool:
        _event_error("mcp_tool_output_invalid")
    text = _content_text(result_block.get("content", ""))
    if name == "mcp__handoff__read_file":
        if set(arguments) != {"path"} or not isinstance(arguments["path"], str):
            _event_error("mcp_read_arguments_invalid")
        raw_path = arguments["path"]
        path = _read_event_path(raw_path)
    elif name == "mcp__handoff__agentctl":
        argv, phase = _validated_agent_argv(arguments)
    else:
        _event_error("mcp_tool_unverified")
    if failed:
        return 1, text
    if name == "mcp__handoff__read_file":
        if not path.is_absolute():
            _event_error("mcp_read_path_invalid")
        _canonical_path(str(path), "mcp_read_path_invalid")
        return 0, text
    try:
        public = validate_public_result(_json(text.encode("utf-8")))
    except Exception:
        _event_error("mcp_agent_result_invalid")
    if not isinstance(public, dict) or public.get("command") != phase or public.get("status") not in EXIT_CODES:
        _event_error("mcp_agent_result_invalid")
    return EXIT_CODES[public["status"]], text


def _canonical_path(raw, code):
    try:
        path = Path(raw)
        if (not isinstance(raw, str) or not path.is_absolute() or ".." in path.parts or
                any(ord(char) < 32 for char in raw) or "\x00" in raw or str(path) != raw):
            raise ValueError("path")
        return path
    except Exception:
        _event_error(code)


def _read_event_path(raw):
    try:
        if (not isinstance(raw, str) or not raw or "\x00" in raw or
                any(ord(char) < 32 for char in raw)):
            raise ValueError("path")
        path = Path(raw)
        if ".." in path.parts or str(path) != raw:
            raise ValueError("path")
        if path.is_absolute():
            return _canonical_path(raw, "mcp_read_path_invalid")
        return path
    except Exception:
        _event_error("mcp_read_path_invalid")


def _validated_agent_argv(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"argv"}:
        _event_error("mcp_agent_arguments_invalid")
    argv = arguments["argv"]
    if (not isinstance(argv, list) or not argv or len(argv) > 64 or
            any(not isinstance(item, str) or not item or "\x00" in item or
                any(ord(char) < 32 for char in item) for item in argv) or
            sum(len(item.encode("utf-8")) for item in argv) > 8192):
        _event_error("mcp_agent_arguments_invalid")
    phase, _extra = _parse_agent_call(argv)
    if phase not in AGENT_PHASES or phase == "help":
        _event_error("mcp_agent_phase_invalid")
    return argv, phase


def _normalize_auth_item(name, arguments, block, identity, use_event, result_event):
    if name == "mcp__handoff__read_file":
        if set(arguments) != {"path"} or not isinstance(arguments["path"], str):
            _event_error("mcp_read_arguments_invalid")
        path = _read_event_path(arguments["path"])
        command = "cat " + shlex.quote(str(path))
    elif name == "mcp__handoff__agentctl":
        argv, _phase = _validated_agent_argv(arguments)
        command = shlex.join(["agentctl", *argv])
    elif name == "EndConversation":
        return None
    else:
        _event_error("claude_mcp_tool_unverified")
    exit_code, output = _normalize_result(name, arguments, block)
    return {"type": "item.completed", "item": {
        "type": "command_execution", "command": command,
        "exit_code": exit_code, "aggregated_output": output,
    }}


def claude_mcp_trace(events, model=MODEL):
    return _trace_native(events, model, TOOL_NAMES, _normalize_auth_item)


def _trace_native(events, model, tool_names, normalize_item):
    if model != MODEL or not isinstance(events, list) or not events or any(not isinstance(event, dict) for event in events):
        _event_error("claude_mcp_trace_invalid")
    init_events = [event for event in events if event.get("type") == "system" and event.get("subtype") == "init"]
    telemetry = [event for event in events if event.get("type") == "rate_limit_event" or
                 (event.get("type") == "system" and event.get("subtype") == "thinking_tokens")]
    if (len(init_events) != 1 or events[0] is not init_events[0] or
            any(event.get("subtype") not in {"init", "thinking_tokens"}
                for event in events if event.get("type") == "system")):
        _event_error("claude_mcp_init_unproven")
    if (any(event.get("type") not in {"system", "assistant", "user", "rate_limit_event", "result"}
            for event in events) or any("message" in event or "content" in event for event in telemetry)):
        _event_error("claude_mcp_trace_invalid")
    init = init_events[0]
    if init.get("model") != model:
        _event_error("claude_mcp_model_unproven")
    tools = init.get("tools")
    if not isinstance(tools, list) or any(not isinstance(item, str) for item in tools) or len(set(tools)) != len(tools):
        _event_error("claude_mcp_registry_invalid")
    if set(tools) - (tool_names | {"EndConversation"}) or not tool_names <= set(tools):
        _event_error("claude_mcp_registry_invalid")
    servers = init.get("mcp_servers")
    if (not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], dict) or
            set(servers[0]) not in ({"name", "status"}, {"name", "status", "source"}) or
            servers[0].get("name") != SERVER or servers[0].get("status") != "connected" or
            servers[0].get("source", "dynamic") != "dynamic"):
        _event_error("claude_mcp_server_unproven")
    results = [event for event in events if event.get("type") == "result"]
    if (len(results) != 1 or events[-1] is not results[0] or results[0].get("is_error") is not False or
            results[0].get("subtype") != "success"):
        _event_error("claude_mcp_success_unproven")
    usage = results[0].get("modelUsage")
    if not isinstance(usage, dict) or set(usage) != {model} or not isinstance(usage.get(model), dict):
        _event_error("claude_mcp_model_unproven")
    if "canonicalModel" in usage[model] and usage[model]["canonicalModel"] != model:
        _event_error("claude_mcp_model_unproven")

    pending, seen = {}, set()
    normalized = []
    for event in events[1:-1]:
        if event.get("type") == "rate_limit_event" or (event.get("type") == "system" and event.get("subtype") == "thinking_tokens"):
            continue
        if event.get("type") == "system":
            _event_error("claude_mcp_init_unproven")
        message = event.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                _event_error("claude_mcp_block_invalid")
            kind = block.get("type")
            if kind == "tool_use":
                identity, name, arguments = block.get("id"), block.get("name"), block.get("input")
                if (event.get("type") != "assistant" or not isinstance(identity, str) or not identity or identity in seen or
                        name not in tools or not isinstance(arguments, dict)):
                    _event_error("claude_mcp_tool_invalid")
                seen.add(identity)
                pending[identity] = (name, arguments, event)
            elif kind == "tool_result":
                if event.get("type") != "user":
                    _event_error("claude_mcp_result_invalid")
                identity = block.get("tool_use_id")
                if not isinstance(identity, str) or identity not in pending:
                    _event_error("claude_mcp_result_uncorrelated")
                name, arguments, use_event = pending.pop(identity)
                normalized_item = normalize_item(name, arguments, block, identity, use_event, event)
                if normalized_item is not None:
                    normalized.append(normalized_item)
            elif kind in {"text", "thinking", "tool_use", "tool_result"}:
                continue
            else:
                _event_error("claude_mcp_tool_unverified")
    if pending:
        _event_error("claude_mcp_completion_unproven")
    return normalized
