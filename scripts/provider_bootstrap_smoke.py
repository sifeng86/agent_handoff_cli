"""Fresh provider bootstrap acceptance using a private local Git authority fixture."""
import argparse
import ast
import fcntl
import hashlib
import inspect
import importlib.util
import marshal
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import stat
import tomllib
import time
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_handoff.authority import Authority
from agent_handoff.git_backend import GitBackend, git_environment
from agent_handoff.paths import PrivateStore
from agent_handoff.publication import Runtime


class EvidenceError(ValueError):
    """A required provider behavior could not be proven from private evidence."""


def git(*args, timeout=30, strip_output=True):
    try:
        process = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], env=git_environment(),
                                 capture_output=True, timeout=timeout, umask=0o077)
    except (OSError, subprocess.TimeoutExpired):
        raise EvidenceError("git_unavailable") from None
    if process.returncode:
        raise EvidenceError("git_operation_failed")
    output = process.stdout.decode("utf-8", errors="strict")
    return output.strip() if strip_output else output


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_context(private_root, checkout):
    with PrivateStore(private_root, checkout) as store:
        value = store.read("context.json")
    token = value.get("context")
    if not isinstance(token, str) or len(token) < 32:
        raise EvidenceError("private_context_invalid")
    return token


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class retained_temp:
    """Retain private evidence for independent review after either outcome."""
    def __init__(self):
        self.path = Path(tempfile.mkdtemp(prefix="agent-handoff-provider-bootstrap-"))
        self.path.chmod(0o700)
        self.failed = False
    def __enter__(self):
        return self.path
    def __exit__(self, exc_type, exc, tb):
        self.failed = exc_type is not None
        return False


def read_jsonl(path):
    try:
        raw = path.read_bytes()
    except OSError:
        raise EvidenceError("provider_trace_missing") from None
    if len(raw) > 32 * 1024 * 1024:
        raise EvidenceError("provider_trace_too_large")
    events = []
    try:
        for line in raw.splitlines():
            if line.strip():
                events.append(json.loads(line))
    except (ValueError, UnicodeError):
        raise EvidenceError("provider_trace_invalid") from None
    return raw, events


def shell_body(command):
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if len(tokens) == 3 and Path(tokens[0]).name in ("bash", "sh") and tokens[1] in ("-c", "-lc"):
        return tokens[2]
    return command


def cli_phase(arguments):
    options = ("--authority", "--publication-ref", "--private-root", "--checkout", "--context-file", "--mainline-ref")
    if any(argument in ("--help", "-h") for argument in arguments):
        return "help"
    if any(sum(argument == option or argument.startswith(option + "=") for argument in arguments) > 1
           for option in options):
        return None
    positional, index = [], 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in options:
            index += 2
        elif any(argument.startswith(option + "=") for option in options):
            index += 1
        elif argument.startswith("-"):
            return None
        else:
            positional.append(argument)
            index += 1
    if positional == ["context", "create"]:
        return "context create"
    if positional == ["context", "init"]:
        return "context"
    return positional[0] if len(positional) == 1 and positional[0] in ("inspect", "status", "resume", "context") else None


def cli_name(command):
    command = re.sub(r"(?<!\S)2>&1(?=$|[\s;&|])", " ", shell_body(command))
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None, None
    for index, token in enumerate(tokens):
        if Path(token).name == "agentctl":
            end = next((offset for offset in range(index + 1, len(tokens))
                        if tokens[offset] in ("&&", ";", "||", "|")), len(tokens))
            label = cli_phase(tokens[index + 1:end])
            return label, label
    return None, None


def read_only_command(command):
    """Accept only shell commands whose lack of file mutations is evident."""
    command = shell_body(command)
    command = re.sub(r"(?<!\S)2>&1(?=$|[\s;&|])", " ", command)
    loop = re.fullmatch(r'for ([A-Za-z_][A-Za-z0-9_]*) in ([A-Za-z0-9_./ -]+); do (.+); done(?:; (.+))?', command.strip())
    if loop:
        variable, values, body, suffix = loop.groups()
        if not all(read_only_command(body.replace('"$' + variable + '"', shlex.quote(value))) for value in values.split()):
            return False
        return suffix is None or read_only_command(suffix)
    command = re.sub(r"(?<!\S)(?:[12])?>\s*/dev/null(?=$|[\s;&|])", " ", command)
    if any(marker in command for marker in (">", "<", "`", "$(", "${", "\x00")):
        return False
    segments = re.split(r"\s*(?:&&|\|\||\||;)\s*", command.strip())
    if not segments or any(not segment.strip() for segment in segments):
        return False
    allowed = {"pwd", "ls", "cat", "head", "tail", "sed", "rg", "grep", "find", "file", "stat", "readlink", "sort", "which", "printf", "echo", "test"}
    git_read = {"status", "rev-parse", "show", "log", "diff", "ls-files"}
    for segment in segments:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        if not tokens:
            return False
        executable = Path(tokens[0]).name
        if executable == "cd" and len(tokens) == 2:
            continue
        if executable == "command" and len(tokens) == 3 and tokens[1] in ("-v", "-V"):
            continue
        if executable in allowed:
            if executable == "sort" and any(token.startswith("-o") or token.split("=", 1)[0] in ("--output", "--compress-program") for token in tokens[1:]):
                return False
            if executable == "sed" and not (len(tokens) >= 4 and tokens[1] == "-n" and
                                            re.fullmatch(r"(?:\d+(?:,\d+)?|\$)?p", tokens[2])):
                return False
            if executable == "find" and any(token in ("-exec", "-execdir", "-delete", "-ok", "-okdir",
                                                       "-fprint", "-fprint0", "-fprintf", "-fls") for token in tokens):
                return False
            if executable == "rg" and any(token.split("=", 1)[0] in ("--pre", "--hostname-bin") for token in tokens):
                return False
            if executable == "file" and any(token in ("-C", "--compile") for token in tokens):
                return False
            continue
        if executable == "git" and len(tokens) > 1:
            subcommand = tokens[1]
            arguments = tokens[2:]
            if subcommand == "-C" and len(tokens) > 3:
                subcommand = tokens[3]
                arguments = tokens[4:]
            if subcommand == "remote" and tuple(arguments) in ((), ("-v",), ("--verbose",)):
                continue
            if subcommand in git_read and not any(token.split("=", 1)[0] in
                                                  ("--output", "--ext-diff", "--textconv") for token in tokens):
                continue
        name, _ = cli_name(segment)
        if name in ("help", "inspect", "status", "context create", "context"):
            continue
        return False
    return True


def _item(event):
    item = event.get("item")
    return item if isinstance(item, dict) else {}


def _completed_command(event):
    item = _item(event)
    if event.get("type") != "item.completed" or item.get("type") != "command_execution":
        return None
    command = item.get("command")
    if not isinstance(command, str):
        return None
    return command, item.get("exit_code")


def _changed_paths(event):
    item = _item(event)
    if event.get("type") not in ("item.file_change", "item.started", "item.completed", "item.updated"):
        return []
    if item.get("type") != "file_change":
        return []
    changes = item.get("changes", [])
    if not isinstance(changes, list):
        return [None]
    paths = []
    for change in changes:
        path = change.get("path") if isinstance(change, dict) else None
        paths.append(path if isinstance(path, str) else None)
    return paths


def verify_trace(events, cli_calls, source, managed, context_token):
    serialized = json.dumps(events, ensure_ascii=False, separators=(",", ":"))
    if context_token and context_token in serialized:
        raise EvidenceError("context_token_in_provider_trace")
    calls = [dict(call, command="inspect", actual_command="status") if call.get("command") == "status" else call
             for call in cli_calls if call.get("command") != "help"]
    inspect_calls = [i for i, call in enumerate(calls) if call.get("command") == "inspect"]
    context_calls = [i for i, call in enumerate(calls) if call.get("command") == "context create"]
    resume_calls = [i for i, call in enumerate(calls) if call.get("command") == "resume"]
    if not inspect_calls or len(context_calls) != 1 or not resume_calls:
        raise EvidenceError("required_wrapper_phases_unproven")
    first_resume = resume_calls[0]
    inspect_call, context_call = calls[inspect_calls[0]], calls[context_calls[0]]
    resume_call = calls[first_resume]
    if not (inspect_calls[0] < context_calls[0] < first_resume and
            inspect_call.get("status") == "completed" and inspect_call.get("exit_code") == 0 and
            context_call.get("status") == "completed" and context_call.get("exit_code") == 0 and
            resume_call.get("status") == "ready" and resume_call.get("ownership") == "owned" and
            resume_call.get("exit_code") == 0):
        raise EvidenceError("wrapper_phase_order_or_status_invalid")
    managed = managed.resolve()
    commands = []
    mutation_indices = []
    change_paths = []
    cli_command_indices = {"inspect": [], "context create": [], "resume": []}
    for index, event in enumerate(events):
        completed = _completed_command(event)
        if completed is not None:
            command, code = completed
            commands.append((index, command, code))
            name, _ = cli_name(command)
            if name == "status":
                name = "inspect"
            if name in cli_command_indices:
                cli_command_indices[name].append(index)
            if name == "resume":
                mutation_indices.append(index)
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = []
            if any(token in ("push", "update-ref", "reset", "clean", "checkout", "commit", "merge", "rebase")
                   for token in tokens if token in ("push", "update-ref", "reset", "clean", "checkout", "commit", "merge", "rebase")):
                raise EvidenceError("direct_git_mutation_unverified")
        paths = _changed_paths(event)
        if paths:
            mutation_indices.append(index)
            change_paths.extend(paths)
            for path in paths:
                if path is None or Path(path).resolve() != (managed / "app.py").resolve():
                    raise EvidenceError("unexpected_or_unlocated_provider_file_change")
    if any(not cli_command_indices[name] for name in cli_command_indices):
        raise EvidenceError("provider_cli_trace_missing")
    inspect_event = cli_command_indices["inspect"][0]
    context_event = cli_command_indices["context create"][0]
    resume_event = cli_command_indices["resume"][0]
    if not inspect_event < context_event < resume_event:
        raise EvidenceError("provider_cli_order_invalid")
    canonical = (Path(__file__).resolve().parents[1] / "template/.agent/instructions.md").read_text().strip()
    instruction_reads = [index for index, command, code in commands
                         if code == 0 and ".agent/instructions.md" in command and
                         canonical in _item(events[index]).get("aggregated_output", "")]
    if not instruction_reads or min(instruction_reads) >= inspect_event:
        raise EvidenceError("canonical_instruction_read_unproven")
    for name in cli_command_indices:
        if not any(code == 0 and index == cli_command_indices[name][0] for index, _, code in commands):
            raise EvidenceError("provider_cli_completion_unproven")
    for index, command, code in commands:
        name, _ = cli_name(command)
        if name == "context" and code != 7:
            raise EvidenceError("context_discovery_effects_unproven")
        # Owned managed work may run tests; final-state verification checks its scope.
        if index < resume_event:
            if name == "resume":
                continue
            if not read_only_command(command):
                raise EvidenceError("ambiguous_pre_resume_command")
            if code != 0 and name in ("inspect", "context create", "help"):
                raise EvidenceError("pre_resume_cli_phase_failed")
    if not change_paths or not mutation_indices or min(mutation_indices) < resume_event:
        raise EvidenceError("feature_mutation_order_unproven")
    # All file changes are after a completed ready/owned resume, and the actual diff is checked separately.
    if not any(index > resume_event for index in range(len(events)) if _changed_paths(events[index])):
        raise EvidenceError("feature_change_missing_after_resume")
    agents_read = any(code == 0 and "AGENTS.md" in command for _, command, code in commands)
    return {"instructions_read_before_inspect": True, "agents_read_observed": agents_read,
            "inspect_before_resume": True,
            "resume_ready_owned_before_feature_change": True, "only_managed_app_change_event": True}


def make_wrapper(wrapper_path, config_path):
    source = '''#!@PYTHON@\nimport fcntl, json, os, subprocess, sys, time\nfrom pathlib import Path\nCONFIG = @CONFIG@\n\ndef load():\n    with open(CONFIG, encoding="utf-8") as stream: return json.load(stream)\ndef save(value):\n    temp = CONFIG + ".new"\n    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)\n    with os.fdopen(fd, "w", encoding="utf-8") as stream: json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())\n    os.replace(temp, CONFIG)\ndef option(args, name):\n    if sum(token == name or token.startswith(name + "=") for token in args) != 1: return None\n    for i, token in enumerate(args):\n        if token == name and i + 1 < len(args): return args[i + 1]\n        if token.startswith(name + "="): return token.split("=", 1)[1]\n    return None\ndef reject(message="bootstrap wrapper rejected command"):\n    print(message, file=sys.stderr); return 7\ndef main():\n    args = sys.argv[1:]\n    cfg = load()\n    started = time.monotonic()\n    if cli_phase(args) == "help":\n        try: proc = subprocess.run([cfg["real"], "--help"], capture_output=True, timeout=30)\n        except Exception: return reject()\n        with open(cfg["log"], "a", encoding="utf-8") as log: log.write(json.dumps({"command":"help","status":"help","exit_code":proc.returncode,"elapsed_ms":int((time.monotonic()-started)*1000)})+"\\n")\n        sys.stdout.buffer.write(proc.stdout); sys.stderr.buffer.write(proc.stderr); return proc.returncode\n    label = cli_phase(args)\n    allowed = {"inspect", "status", "context create", "resume"}\n    if label not in allowed: return reject()\n    private = option(args, "--private-root")\n    checkout = option(args, "--checkout") or os.getcwd()\n    authority = option(args, "--authority")\n    pubref = option(args, "--publication-ref")\n    context = option(args, "--context-file")\n    if private is None or os.path.realpath(private) != cfg["private"] or checkout is None: return reject()\n    if label == "context create":\n        if cfg["inspect_ok"] is not True: return reject()\n    else:\n        if authority is None or authority != cfg["authority"] or pubref != cfg["ref"] or checkout is None: return reject()\n    allowed_checkouts = {cfg["source"]}\n    if cfg.get("managed"): allowed_checkouts.add(cfg["managed"])\n    actual_checkout = os.path.realpath(checkout)\n    if actual_checkout not in allowed_checkouts: return reject()\n    if label in ("inspect", "status") and actual_checkout != cfg["source"]: return reject()\n    if label == "context create" and actual_checkout != cfg["source"]: return reject()\n    if label == "resume":\n        if not cfg["inspect_ok"] or not cfg["context_ok"] or context is None or os.path.realpath(context) != os.path.join(cfg["private"], "context.json") or cfg.get("resume_count", 0): return reject()\n    try: proc = subprocess.run([cfg["real"], *args], capture_output=True, timeout=180)\n    except Exception: return reject()\n    try:\n        value = json.loads(proc.stdout.decode("utf-8"))\n        statuses = {"completed":0,"ready":0,"not_ready":6,"decision_required":3,"rejected":4,"unknown":5,"invalid":7,"unsupported":8}\n        if not isinstance(value, dict) or value.get("command") != label or value.get("status") not in statuses or proc.returncode != statuses[value["status"]]: raise ValueError()\n        if label in ("inspect", "status") and (value["status"] != "completed" or not isinstance(value.get("inspection"), dict)): raise ValueError()\n        if label == "context create" and (value["status"] != "completed" or not isinstance(value.get("context_file"), str) or os.path.realpath(value["context_file"]) != os.path.join(cfg["private"], "context.json")): raise ValueError()\n        if label == "resume":\n            if value["status"] != "ready" or value.get("ownership") != "owned": raise ValueError()\n            managed = os.path.realpath(value.get("managed_checkout", ""))\n            if os.path.commonpath([os.path.join(cfg["private"], "repos"), managed]) != os.path.join(cfg["private"], "repos") or managed == cfg["source"]: raise ValueError()\n            cfg["managed"] = managed; cfg["resume_count"] = cfg.get("resume_count", 0) + 1; cfg["resume_ok"] = True; cfg["resume_revision"] = value.get("observed_revision")\n    except Exception:\n        value = None\n    code = proc.returncode if value is not None else 7\n    if value is None: return reject("agentctl returned an invalid bootstrap result")\n    if label in ("inspect", "status"): cfg["inspect_ok"] = True\n    if label == "context create": cfg["context_ok"] = True\n    lockfd = os.open(CONFIG + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)\n    try:\n        fcntl.flock(lockfd, fcntl.LOCK_EX); save(cfg)\n        record = {"command":label,"status":value["status"],"exit_code":code,"elapsed_ms":int((time.monotonic()-started)*1000),"checkout_kind":"managed" if actual_checkout == cfg.get("managed") else "source"}\n        for key in ("observed_revision","published_revision","ownership","code_publication","managed_checkout"):\n            if key in value: record[key] = value[key]\n        with open(cfg["log"], "a", encoding="utf-8") as log: log.write(json.dumps(record, separators=(",",":"))+"\\n")\n    finally: os.close(lockfd)\n    sys.stdout.buffer.write(json.dumps(value, separators=(",",":" )).encode()+b"\\n")\n    sys.stderr.buffer.write(proc.stderr)\n    return code\nif __name__ == "__main__": raise SystemExit(main())\n'''
    source = source.replace("@PYTHON@", sys.executable).replace("@CONFIG@", repr(str(config_path)))
    source = source.replace("CONFIG = ", inspect.getsource(cli_phase) + "\nCONFIG = ", 1)
    write_private(wrapper_path, source.encode())
    wrapper_path.chmod(0o700)


def parse_log(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        calls = [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError, UnicodeError):
        raise EvidenceError("wrapper_log_invalid") from None
    if any(not isinstance(call, dict) for call in calls):
        raise EvidenceError("wrapper_log_invalid")
    return calls


def verify_final_state(source, managed, remote, mainline, base_revision, source_revision, expected_revision):
    if git("-C", str(source), "rev-parse", "HEAD") != source_revision:
        raise EvidenceError("source_head_changed")
    if (source / "app.py").read_text(encoding="utf-8") != "def answer():\n    return 41\n":
        raise EvidenceError("source_file_changed")
    if git("-C", str(source), "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise EvidenceError("source_checkout_not_clean")
    if git("-C", str(managed), "rev-parse", "HEAD") != expected_revision:
        raise EvidenceError("managed_head_changed")
    managed_status = git("-C", str(managed), "status", "--porcelain=v1", "-z", "--untracked-files=all",
                         strip_output=False)
    entries = []
    generated_cache = False
    cache_name = "__pycache__/app.cpython-" + str(sys.version_info.major) + str(sys.version_info.minor) + ".pyc"
    for entry in managed_status.split("\x00"):
        if not entry:
            continue
        if entry == "?? " + cache_name:
            cache_path = managed / cache_name
            info = cache_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                raise EvidenceError("unexpected_managed_verification_cache")
            cached = cache_path.read_bytes()
            compiled = (managed / "app.py").read_text(encoding="utf-8")
            expected = [compile(compiled, filename, "exec", dont_inherit=True)
                        for filename in (str(managed / "app.py"), "app.py")]
            try:
                payload = marshal.loads(cached[16:])
            except (ValueError, TypeError, EOFError):
                raise EvidenceError("unexpected_managed_verification_cache") from None
            if len(cached) < 16 or cached[:4] != importlib.util.MAGIC_NUMBER or payload not in expected:
                raise EvidenceError("unexpected_managed_verification_cache")
            generated_cache = True
        else:
            entries.append(entry)
    if len(entries) != 1 or entries[0][3:] != "app.py":
        raise EvidenceError("unexpected_managed_checkout_changes")
    code = (managed / "app.py").read_text(encoding="utf-8")
    try:
        tree = ast.parse(code)
        answer = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "answer")
        returns_42 = (len(tree.body) == 1 and not answer.args.args and not answer.args.posonlyargs and
                      not answer.args.kwonlyargs and answer.args.vararg is None and answer.args.kwarg is None and
                      not answer.decorator_list and len(answer.body) == 1 and isinstance(answer.body[0], ast.Return) and
                      isinstance(answer.body[0].value, ast.Constant) and answer.body[0].value.value == 42)
    except (SyntaxError, StopIteration):
        returns_42 = False
    if not returns_42:
        raise EvidenceError("managed_answer_not_42")
    if remote.remote_head() != expected_revision:
        raise EvidenceError("unexpected_remote_publication")
    if mainline.remote_head() != base_revision:
        raise EvidenceError("mainline_changed")
    return {"source_unchanged": True, "managed_app_returns_42": True,
            "no_feature_publication": True, "mainline_unchanged": True,
            "verified_python_cache_present": generated_cache}


def verify_owner(private_root, managed, authority, context_token, backend):
    with Runtime(managed, private_root, authority, context_token) as runtime:
        oid, records = runtime._observe()
        session = runtime.sessions.prove(runtime.backend, oid, records)
        owner = records["owner"]["owner"]
        if (owner is None or owner["session_id"] != session["session_id"] or
                owner["acquisition_operation_id"] != session["acquisition_operation_id"] or
                backend.remote_head() != oid):
            raise EvidenceError("owner_session_proof_failed")
        return oid


def restore_fixture_trust(config_file, before, source):
    """Remove only this fixture's newly added trust table; retain concurrent edits."""
    current = config_file.read_bytes() if config_file.is_file() else b""
    if current == before:
        return False
    previous_values = tomllib.loads(before.decode())
    values = tomllib.loads(current.decode())
    key = str(source)
    if key in previous_values.get("projects", {}) or values.get("projects", {}).get(key) != {"trust_level": "trusted"}:
        raise EvidenceError("provider_settings_change_outside_fixture")
    lines = current.decode().splitlines(keepends=True)
    removed = set()
    for index, line in enumerate(lines):
        if not line.strip().startswith("[projects."):
            continue
        try:
            section = tomllib.loads(line.strip() + '\ntrust_level="trusted"\n')
        except tomllib.TOMLDecodeError:
            continue
        if set(section.get("projects", {})) == {key}:
            end = next((offset for offset in range(index + 1, len(lines)) if lines[offset].lstrip().startswith("[")), len(lines))
            removed.update(range(index, end))
    trimmed = "".join(line for index, line in enumerate(lines) if index not in removed).encode()
    candidates = (trimmed, trimmed.rstrip() + b"\n", trimmed.rstrip() + b"\n\n")
    if before not in candidates:
        raise EvidenceError("provider_settings_change_outside_fixture")
    info = config_file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise EvidenceError("provider_settings_identity_unsupported")
    fd, staged = tempfile.mkstemp(prefix=".agent-handoff-trust-", dir=config_file.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        latest = config_file.lstat()
        if (latest.st_ino, latest.st_mtime_ns, latest.st_size) != (info.st_ino, info.st_mtime_ns, info.st_size) or config_file.read_bytes() != current:
            raise EvidenceError("provider_settings_changed_concurrently")
        os.replace(staged, config_file)
        directory = os.open(config_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    return True


def provider_settings_snapshot(provider="codex"):
    if provider == "claude":
        data_root = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        names = ("settings.json", ".credentials.json")
    else:
        data_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        names = ("config.toml", "auth.json")
    return {name: digest((data_root / name).read_bytes()) if (data_root / name).is_file() else None
            for name in names}


def codex_command(codex, source, private_root, remote, model, state_root):
    raise EvidenceError("legacy_provider_profile_not_isolated")


def claude_command(claude, private_root, remote, model):
    raise EvidenceError("legacy_provider_profile_not_isolated")


def claude_trace(events, model):
    """Normalize correlated native tool events for the shared bootstrap verifier."""
    normalized, pending, seen = [], {}, set()
    results = [event for event in events if event.get("type") == "result"]
    if len(results) != 1 or results[0].get("is_error") is not False or results[0].get("subtype") != "success":
        raise EvidenceError("claude_success_unproven")
    used = results[0].get("modelUsage", {})
    if not isinstance(used, dict) or set(used) != {model}:
        raise EvidenceError("claude_requested_model_unproven")
    for event in events:
        message = event.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                identity, name, arguments = block.get("id"), block.get("name"), block.get("input")
                if not isinstance(identity, str) or identity in seen or not isinstance(arguments, dict):
                    raise EvidenceError("claude_tool_identity_invalid")
                seen.add(identity)
                if name == "Bash":
                    command = arguments.get("command")
                    if not isinstance(command, str):
                        raise EvidenceError("claude_command_invalid")
                elif name == "Read":
                    path = arguments.get("file_path")
                    if not isinstance(path, str) or not Path(path).is_absolute():
                        raise EvidenceError("claude_read_path_invalid")
                    command = "cat " + shlex.quote(path)
                elif name in ("Edit", "Write"):
                    path = arguments.get("file_path")
                    if not isinstance(path, str) or not Path(path).is_absolute():
                        raise EvidenceError("claude_change_path_invalid")
                    normalized.append({"type": "item.started", "item": {"type": "file_change", "changes": [{"path": path}]}})
                    command = None
                else:
                    raise EvidenceError("claude_tool_unverified")
                pending[identity] = (name, command)
            elif block.get("type") == "tool_result":
                identity = block.get("tool_use_id")
                if identity not in pending:
                    raise EvidenceError("claude_tool_result_uncorrelated")
                name, command = pending.pop(identity)
                output = block.get("content", "")
                if isinstance(output, list):
                    output = "\n".join(part.get("text", "") for part in output if isinstance(part, dict) and part.get("type") == "text")
                if not isinstance(output, str):
                    raise EvidenceError("claude_tool_output_invalid")
                if name == "Read":
                    output = re.sub(r"(?m)^\s*\d+→", "", output)
                if command is not None:
                    normalized.append({"type": "item.completed", "item": {"type": "command_execution",
                        "command": command, "exit_code": 1 if block.get("is_error", False) else 0,
                        "aggregated_output": output}})
    if pending:
        raise EvidenceError("claude_tool_completion_unproven")
    return normalized


def run(agentctl, codex, model="gpt-6-luna", provider="codex"):
    agentctl_path, codex_path = Path(agentctl).resolve(), Path(codex).resolve()
    if not agentctl_path.is_file() or not os.access(agentctl_path, os.X_OK):
        raise ValueError("Installed agentctl executable is unavailable.")
    if not codex_path.is_file() or not os.access(codex_path, os.X_OK):
        raise ValueError("Provider executable is unavailable.")
    if provider not in ("codex", "claude"):
        raise ValueError("Unsupported provider.")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ValueError("Invalid model label.")
    failure_category = "setup"
    try:
        with retained_temp() as root:
            remote, seed, source = root / "remote.git", root / "seed", root / "source"
            private_root, wrapper_dir = root / "private", root / "bin"
            remote.mkdir(mode=0o700); seed.mkdir(mode=0o700); private_root.mkdir(mode=0o700); wrapper_dir.mkdir(mode=0o700)
            git("-C", str(remote), "init", "--bare", "--quiet")
            git("-C", str(seed), "init", "--quiet", "-b", "main")
            template_root = Path(__file__).resolve().parents[1] / "template"
            (seed / ".agent").mkdir()
            entry_files = ("AGENTS.md", ".agent/instructions.md") + (("CLAUDE.md",) if provider == "claude" else ())
            for relative in entry_files:
                target = seed / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((template_root / relative).read_bytes())
            (seed / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
            git("-C", str(seed), "add", "--", *entry_files, "app.py")
            git("-C", str(seed), "commit", "--quiet", "-m", "fresh provider fixture")
            base = git("-C", str(seed), "rev-parse", "HEAD")
            git("-C", str(seed), "push", "--quiet", str(remote), "refs/heads/main")
            mainline = GitBackend(root / "verify-main.git", str(remote), "refs/heads/main")
            publication_ref = "refs/heads/agent-handoff-provider-smoke-" + uuid.uuid4().hex
            authority = Authority(str(remote), publication_ref)
            with Runtime(seed, private_root, authority) as runtime:
                initialized = runtime.initialize(base, mainline_ref="refs/heads/main")
            if initialized.get("code_publication") != "published":
                raise EvidenceError("fixture_initialization_failed")
            init_revision = initialized["published_revision"]
            git("clone", "--quiet", "--no-checkout", "--branch", "main", "--", str(remote), str(source))
            git("-C", str(source), "fetch", "--quiet", "--no-tags", "--", str(remote), publication_ref)
            git("-C", str(source), "checkout", "--quiet", "--detach", init_revision)
            source_head = git("-C", str(source), "rev-parse", "HEAD")
            source_index = digest((source / ".git/index").read_bytes())
            write_private(root / "fixture-before.json", json.dumps({"base_revision": base, "initialized_revision": init_revision, "source_head": source_head, "source_index_sha256": source_index, "model": model}).encode())
            if git("-C", str(source), "status", "--porcelain=v1", "-z", "--untracked-files=all"):
                raise EvidenceError("fixture_source_not_clean")
            config_path, log_path = root / "wrapper-config.json", root / "agentctl-calls.jsonl"
            trace_path, stderr_path = root / "provider.jsonl", root / "provider.stderr"
            for path, data in ((log_path, b""), (trace_path, b""), (stderr_path, b"")):
                write_private(path, data)
            config = {"real": str(agentctl_path), "authority": str(remote), "ref": publication_ref,
                      "private": str(private_root), "source": str(source), "managed": None,
                      "log": str(log_path), "inspect_ok": False, "context_ok": False, "resume_ok": False}
            write_private(config_path, json.dumps(config).encode())
            wrapper_path = wrapper_dir / "agentctl"
            make_wrapper(wrapper_path, config_path)
            prompt = ("Approved setup facts: agentctl is available at " + str(wrapper_path) + " and on PATH. Approved authority: " + str(remote) +
                      ". Approved full publication ref: " + publication_ref + ". Approved private state root: " +
                      str(private_root) + ".\nTask: Change answer() in app.py to return 42.\n")
            env = git_environment()
            env["PATH"] = str(wrapper_dir) + os.pathsep + env.get("PATH", "")
            trace_fd = os.open(trace_path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            invocation = (claude_command(codex_path, private_root, remote, model) if provider == "claude" else
                          codex_command(codex_path, source, private_root, remote, model, root / "codex-state"))
            settings_before = provider_settings_snapshot(provider)
            write_private(root / "provider-settings-before.json", json.dumps(settings_before).encode())
            settings_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            config_file = settings_dir / "config.toml"
            config_before = config_file.read_bytes() if config_file.is_file() else b""
            write_private(root / "provider-config-before.bin", config_before)
            failure_category = "provider_execution"
            try:
                completed = subprocess.run(invocation, input=prompt.encode(), stdout=trace_fd, stderr=stderr_fd,
                                            env=env, cwd=source, timeout=300, umask=0o077)
                write_private(root / "provider-execution.json", json.dumps({"exit_code": completed.returncode, "model": model}).encode())
            except (OSError, subprocess.TimeoutExpired):
                raise EvidenceError("provider_timeout_or_unavailable") from None
            finally:
                os.close(trace_fd); os.close(stderr_fd)
                if provider == "codex":
                    restore_fixture_trust(config_file, config_before, source)
            raw_trace, events = read_jsonl(trace_path)
            if provider_settings_snapshot(provider) != settings_before:
                raise EvidenceError("provider_settings_changed")
            if completed.returncode != 0:
                raise EvidenceError("provider_nonzero_exit")
            if provider == "claude":
                events = claude_trace(events, model)
            calls = parse_log(log_path)
            if not any(call.get("command") == "resume" and call.get("status") == "ready" for call in calls):
                raise EvidenceError("required_wrapper_phases_unproven")
            token = read_context(private_root, source)
            if token.encode() in raw_trace or token.encode() in stderr_path.read_bytes():
                raise EvidenceError("context_token_leak")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            managed_raw = config.get("managed")
            if not isinstance(managed_raw, str):
                raise EvidenceError("managed_checkout_unproven")
            managed = Path(managed_raw).resolve()
            failure_category = "bootstrap_verification"
            trace_flags = verify_trace(events, calls, source, managed, token)
            state_flags = verify_final_state(source, managed,
                                             GitBackend(root / "verify.git", str(remote), publication_ref),
                                             mainline, base, init_revision, config.get("resume_revision"))
            owner_revision = verify_owner(private_root, managed, authority, token,
                                          GitBackend(root / "verify-owner.git", str(remote), publication_ref))
            if owner_revision != config.get("resume_revision") or source_head != git("-C", str(source), "rev-parse", "HEAD") or source_index != digest((source / ".git/index").read_bytes()):
                raise EvidenceError("final_owner_or_source_proof_failed")
            version = subprocess.run([str(codex_path), "--version"], env=env, capture_output=True, timeout=15, umask=0o077)
            provider_version = version.stdout.decode("utf-8", errors="ignore").strip().splitlines()[0][:120] if version.returncode == 0 and version.stdout else None
            template_hash = digest((template_root / "AGENTS.md").read_bytes() + b"\x00" +
                                   (template_root / ".agent/instructions.md").read_bytes())
            return {"passed": True, "evidence_scope": "one fresh " + provider + " session on an isolated local Git authority",
                    "provider": provider,
                    "model": model, "provider_version": provider_version, "agentctl_version": None,
                    "python_version": platform.python_version(), "git_version": git("--version"),
                    "provider_trace_sha256": digest(raw_trace), "template_instructions_sha256": template_hash,
                    "base_revision": base, "initialized_revision": init_revision,
                    "resume_revision": config["resume_revision"], "publication_ref": publication_ref,
                    "provider_exit_code": completed.returncode, "wrapper_calls": len(calls),
                    "provider_config_and_auth_unchanged": True,
                    "provider_settings_guard_scope": "settings.json/.credentials.json" if provider == "claude" else "config.toml/auth.json",
                    "source_unchanged": state_flags["source_unchanged"],
                    "managed_app_returns_42": state_flags["managed_app_returns_42"],
                    "no_feature_publication": state_flags["no_feature_publication"],
                    **trace_flags, "owner_session_capability_proved": True,
                    "context_token_absent_from_provider_output": True}
    except EvidenceError as error:
        return {"passed": False, "failure_category": failure_category, "evidence": str(error)}
    except Exception:
        return {"passed": False, "failure_category": failure_category, "evidence": "unexpected_private_failure"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentctl", required=True)
    executable = parser.add_mutually_exclusive_group(required=True)
    executable.add_argument("--codex")
    executable.add_argument("--claude")
    parser.add_argument("--provider", choices=("codex", "claude"))
    parser.add_argument("--model")
    args = parser.parse_args()
    provider = args.provider or ("claude" if args.claude else "codex")
    if args.claude and provider != "claude":
        parser.error("--claude requires the claude provider")
    model = args.model or ("claude-haiku-5-5" if provider == "claude" else "gpt-6-luna")
    report = run(args.agentctl, args.claude or args.codex, model, provider)
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
