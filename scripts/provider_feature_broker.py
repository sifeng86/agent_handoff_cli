"""MCP broker with capability-checked project-file operations."""
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from agent_handoff.authority import Authority
from scripts import provider_tool_broker as transport
from scripts.provider_project_files import ProjectFileError, ProjectFiles


class FeatureBroker(transport.Broker):
    def __init__(self, config_file):
        try:
            wrapper_path = transport._owned_file(str(config_file))
            if wrapper_path.stat().st_size > transport.MAX_FRAME or stat.S_IMODE(wrapper_path.stat().st_mode) != 0o600:
                raise ValueError("wrapper permissions")
            wrapper = transport._json(wrapper_path.read_bytes())
            if (not isinstance(wrapper, dict) or set(wrapper) != {
                    "version", "broker_config", "authority", "publication_ref", "private_root",
                    "context_file", "approved_paths"} or type(wrapper.get("version")) is not int or
                    wrapper["version"] != 1):
                raise ValueError("wrapper shape")
            base_path = transport._owned_file(wrapper["broker_config"])
            base = transport.Broker(str(base_path))
            if base.config["version"] != 2 or base_path.parent != wrapper_path.parent:
                raise ValueError("nested broker configuration")
            managed = self._active_checkout(base.config)
            private = Path(wrapper["private_root"])
            context = Path(wrapper["context_file"])
            if (not isinstance(wrapper["authority"], str) or not isinstance(wrapper["publication_ref"], str) or
                    not isinstance(wrapper["approved_paths"], list)):
                raise ValueError("wrapper types")
            super().__init__(base.config)
            self.managed_checkout = managed
            self.project_files = ProjectFiles(
                source=base.config["cwd"], private_root=private,
                authority=Authority(wrapper["authority"], wrapper["publication_ref"]),
                context_file=context, managed_root=base.config["managed_root"],
                approved_paths=wrapper["approved_paths"],
            )
            self._canonical_read = False
            self._discovery_seen = False
        except transport.BrokerError:
            raise
        except Exception:
            raise transport.BrokerError("configuration_rejected") from None

    @staticmethod
    def _active_checkout(config):
        raw = config["active_registry_bytes"]
        files = config["active_managed_files"]
        if raw is None:
            if files:
                raise ValueError("registry missing")
            return None
        registry = transport._json(raw)
        managed = transport._managed_checkout(registry.get("active_managed_checkout"), config["managed_root"],
                                              config["cwd"], config["policy"].parent)
        if (not files or registry.get("managed_paths") != sorted(files) or
                any(not Path(path).is_relative_to(managed) for path in files)):
            raise ValueError("managed receipt mismatch")
        return managed

    def tool_descriptions(self):
        tools = super().tool_descriptions()
        tools[0] = dict(tools[0], description=(
            "Read one approved public file using its absolute canonical path. "
            "Use project_file for owned feature-file reads and conditional writes."))
        tools[1] = dict(tools[1], description=(
            "Run only inspect, status, resume, secrets inspect, or secrets verify; paths and authority are fixed by trusted configuration."),
            inputSchema={"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"},
                "enum": [["inspect"], ["status"], ["resume"], ["secrets", "inspect"], ["secrets", "verify"]]}},
                "required": ["argv"], "additionalProperties": False})
        tools.append({
            "name": "project_file",
            "description": "Read, conditionally write, or resolve an approved feature file in the active owned managed checkout.",
            "inputSchema": {
                "type": "object",
                "oneOf": [
                    {"type": "object", "properties": {"action": {"const": "read"}, "path": {"type": "string"}},
                     "required": ["action", "path"], "additionalProperties": False},
                    {"type": "object", "properties": {"action": {"const": "write"}, "path": {"type": "string"},
                     "content": {"type": "string"}, "expected_sha256": {"type": "string"}},
                     "required": ["action", "path", "content", "expected_sha256"], "additionalProperties": False},
                    {"type": "object", "properties": {"action": {"const": "resolve"},
                     "operation_id": {"type": "string"}},
                     "required": ["action", "operation_id"], "additionalProperties": False},
                ]
            },
        })
        return tools

    def read_file(self, arguments):
        text = super().read_file(arguments)
        path = arguments.get("path") if isinstance(arguments, dict) else None
        canonical_source = str(self.config["cwd"] / ".agent/instructions.md")
        canonical_managed = (str(self.managed_checkout / ".agent/instructions.md")
                             if self.managed_checkout is not None else None)
        if path == canonical_source or (canonical_managed is not None and path == canonical_managed):
            self._canonical_read = True
        return text

    def agentctl(self, arguments):
        try:
            args = transport._arguments(arguments, {"argv"})
        except transport.BrokerError:
            raise transport.BrokerError("engine_rejected") from None
        requested = args["argv"]
        supported = {("inspect",), ("status",), ("resume",), ("secrets", "inspect"), ("secrets", "verify")}
        if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested):
            raise transport.BrokerError("engine_rejected") from None
        phase_key = tuple(requested)
        if phase_key not in supported:
            raise transport.BrokerError("engine_rejected")
        phase = requested[0] if requested[0] != "secrets" else "secrets " + requested[1]
        if not self._canonical_read or (phase == "resume" and not self._discovery_seen):
            raise transport.BrokerError("engine_rejected")
        checkout = self.config["cwd"]
        if requested[0] == "secrets":
            checkout = self._active_checkout(self.config) or self.config["cwd"]
        fixed_argv = [*requested, "--authority", str(self.project_files.authority.destination),
                      "--publication-ref", self.project_files.authority.publication_ref,
                      "--checkout", str(checkout), "--private-root", str(self.project_files.private_root),
                      "--context-file", str(self.project_files.private_root / "context.json")]
        raw = super().agentctl({"argv": fixed_argv})
        try:
            result = transport._json(raw.encode("utf-8"))
        except Exception:
            raise transport.BrokerError("engine_rejected") from None
        if phase in {"inspect", "status"} and result.get("status") == "completed":
            self._discovery_seen = True
        if phase == "resume" and result.get("status") == "ready" and result.get("ownership") == "owned":
            try:
                self.managed_checkout = self._active_checkout(self.config)
            except Exception:
                raise transport.BrokerError("public_grant_refresh_failed_after_resume") from None
            if self.managed_checkout is None:
                raise transport.BrokerError("public_grant_refresh_failed_after_resume")
        return raw

    def call_tool(self, name, arguments):
        if name != "project_file":
            return super().call_tool(name, arguments)
        if not self._canonical_read or not self._discovery_seen:
            raise transport.BrokerError("project_file_denied")
        try:
            if not isinstance(arguments, dict):
                raise transport.BrokerError("invalid_arguments")
            managed = self._active_checkout(self.config)
            if managed is None:
                raise transport.BrokerError("project_file_denied")
            action = arguments.get("action")
            if action == "read" and set(arguments) == {"action", "path"}:
                result = self.project_files.read(managed, arguments["path"])
            elif action == "write" and set(arguments) == {"action", "path", "content", "expected_sha256"}:
                result = self.project_files.write(managed, arguments["path"],
                                                  arguments["content"], arguments["expected_sha256"])
            elif action == "resolve" and set(arguments) == {"action", "operation_id"}:
                result = self.project_files.resolve(managed, arguments["operation_id"])
            else:
                raise transport.BrokerError("invalid_arguments")
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except ProjectFileError as error:
            raise transport.BrokerError(error.code) from None
        except transport.BrokerError:
            raise
        except Exception:
            raise transport.BrokerError("project_file_denied") from None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "--config-file":
        sys.stderr.write("provider_feature_broker: invalid invocation\n")
        return 2
    try:
        return transport.serve(argv[1], broker_factory=FeatureBroker)
    except transport.BrokerError as exc:
        sys.stderr.write("provider_feature_broker: " + exc.code + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
