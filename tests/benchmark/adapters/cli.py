"""Bounded --version/--help probes, never a model invocation or sandbox proof."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .process import ProcessResult, run_process


@dataclass(frozen=True)
class ProbeEvidence:
    exit_code: int | None
    reason_code: str
    stdout_sha256: str
    stderr_sha256: str

    @classmethod
    def capture(cls, result: ProcessResult) -> ProbeEvidence:
        return cls(result.exit_code, result.reason_code, result.stdout_sha256, result.stderr_sha256)


@dataclass(frozen=True)
class CliProbe:
    agent: str
    status: str
    reason_code: str
    version: str | None
    capabilities: tuple[tuple[str, bool | None], ...]
    probes: tuple[ProbeEvidence, ...]
    command_sha256: str | None = None

    def to_dict(self) -> dict:
        return {
            "agent": self.agent, "status": self.status, "reason_code": self.reason_code,
            "version": self.version, "capabilities": dict(self.capabilities),
            "source": "cli_version_and_help", "authentication": "NOT_EVALUATED",
            "isolation": "NOT_EVALUATED", "live_ready": False,
            "command_sha256": self.command_sha256,
            "probes": [{"exit_code": item.exit_code, "reason_code": item.reason_code,
                        "stdout_sha256": item.stdout_sha256,
                        "stderr_sha256": item.stderr_sha256} for item in self.probes],
        }


def _options(text: str) -> dict[str, str]:
    """Read option declarations, not flag names mentioned in unrelated prose."""
    result: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        match = re.match(r"^\s*(?:-[a-zA-Z],\s*)?(--[a-z][a-z0-9-]*)\b", line)
        if match:
            current = match[1]
            result[current] = line
        elif line and not line[0].isspace():
            current = None
        elif current:
            result[current] += "\n" + line
    return result


def probe_cli(agent: str, command: tuple[str, ...], cwd: Path, *,
              timeout_seconds: int = 5) -> CliProbe:
    if agent not in {"codex", "claude"}:
        raise ValueError("UNSUPPORTED_AGENT")
    if (not isinstance(command, tuple) or not command
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)):
        raise ValueError("INVALID_CLI_COMMAND")
    if type(timeout_seconds) is not int or not 0 < timeout_seconds <= 30:
        raise ValueError("INVALID_PROBE_TIMEOUT")
    command_digest = hashlib.sha256(json.dumps(command, separators=(",", ":")).encode("utf-8")).hexdigest()
    flags = ({"structured_events": "--json", "explicit_model": "--model",
              "ephemeral_session": "--ephemeral", "sandbox_option": "--sandbox",
              "ignore_user_config": "--ignore-user-config", "ignore_rules": "--ignore-rules",
              "max_turns": "--max-turns"}
             if agent == "codex" else {
                 "structured_events": "--output-format", "explicit_model": "--model",
                 "ephemeral_session": "--no-session-persistence", "print_mode": "--print",
                 "verbose": "--verbose", "bare_mode": "--bare",
                 "setting_sources": "--setting-sources", "strict_mcp_config": "--strict-mcp-config",
                 "mcp_config": "--mcp-config",
                 "max_turns": "--max-turns", "max_budget_usd": "--max-budget-usd"})
    unknown = tuple((key, None) for key in flags)
    version_result = run_process((*command, "--version"), cwd,
                                 timeout_seconds=timeout_seconds)
    version_evidence = ProbeEvidence.capture(version_result)
    if version_result.reason_code != "PROCESS_COMPLETED" or version_result.exit_code != 0:
        return CliProbe(agent, "UNAVAILABLE", "CLI_VERSION_PROBE_FAILED", None,
                        unknown, (version_evidence,), command_digest)
    try:
        raw_version = version_result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        raw_version = ""
    version_pattern = (r"codex-cli (\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)"
                       if agent == "codex" else
                       r"(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?) \(Claude Code\)")
    version = re.fullmatch(version_pattern, raw_version)
    if version is None:
        return CliProbe(agent, "INCOMPATIBLE", "CLI_VERSION_UNRECOGNIZED", None,
                        unknown, (version_evidence,), command_digest)
    help_args = ("exec", "--help") if agent == "codex" else ("--help",)
    help_result = run_process((*command, *help_args), cwd, timeout_seconds=timeout_seconds)
    probes = (version_evidence, ProbeEvidence.capture(help_result))
    if help_result.reason_code != "PROCESS_COMPLETED" or help_result.exit_code != 0:
        return CliProbe(agent, "UNAVAILABLE", "CLI_HELP_PROBE_FAILED", version[1], unknown, probes, command_digest)
    try:
        options = _options(help_result.stdout.decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        return CliProbe(agent, "INCOMPATIBLE", "CLI_HELP_INVALID_ENCODING", version[1], unknown, probes, command_digest)
    capabilities = {key: flag in options for key, flag in flags.items()}
    if agent == "claude":
        capabilities["structured_events"] = (
            "--output-format" in options
            and "stream-json" in options["--output-format"])
    required = (("structured_events", "explicit_model", "ephemeral_session", "sandbox_option")
                if agent == "codex" else
                ("structured_events", "explicit_model", "ephemeral_session", "print_mode", "verbose"))
    compatible = all(capabilities[key] for key in required)
    return CliProbe(agent, "COMPATIBLE" if compatible else "INCOMPATIBLE",
                    "CLI_PROBED" if compatible else "CLI_REQUIRED_OPTIONS_MISSING", version[1],
                    tuple(capabilities.items()), probes, command_digest)


def build_invocation(probe: CliProbe, command: tuple[str, ...], *, model: str, prompt: str,
                     max_turns: int | None = None, mcp_config: str | None = None) -> tuple[str, ...]:
    """Construct only advertised arguments; the isolated controller owns launch/auth."""
    if (not isinstance(probe, CliProbe) or probe.status != "COMPATIBLE" or probe.version is None
            or probe.agent not in {"codex", "claude"} or type(command) is not tuple or not command
            or any(type(item) is not str or not item or "\0" in item for item in command)
            or type(model) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+\[\]-]{0,127}", model)
            or type(prompt) is not str or not prompt.strip() or "\0" in prompt):
        raise ValueError("CLI_INVOCATION_INPUT_INVALID")
    try:
        if len(prompt.encode("utf-8")) > 64 * 1024:
            raise ValueError("capacity")
    except (UnicodeError, ValueError) as exc:
        raise ValueError("CLI_INVOCATION_INPUT_INVALID") from exc
    if probe.command_sha256 != hashlib.sha256(json.dumps(command, separators=(",", ":")).encode("utf-8")).hexdigest():
        raise ValueError("CLI_PROBE_COMMAND_MISMATCH")
    capabilities = dict(probe.capabilities)
    if max_turns is not None:
        if type(max_turns) is not int or not 0 < max_turns <= 1000:
            raise ValueError("CLI_TURN_LIMIT_INVALID")
        if capabilities.get("max_turns") is not True:
            raise ValueError("CLI_TURN_LIMIT_UNSUPPORTED")
    turns = () if max_turns is None else ("--max-turns", str(max_turns))
    if probe.agent == "codex":
        if mcp_config is not None:
            raise ValueError("CLI_MCP_CONFIG_UNSUPPORTED")
        if not all(capabilities.get(key) is True for key in ("structured_events", "explicit_model", "ephemeral_session",
                                                            "sandbox_option", "ignore_user_config", "ignore_rules")):
            raise ValueError("CLI_CONFIG_CONTROL_UNSUPPORTED")
        return (*command, "exec", "--json", "--ephemeral", "--model", model, "--sandbox", "workspace-write",
                "--ignore-user-config", "--ignore-rules", *turns, "--", prompt)
    if not all(capabilities.get(key) is True for key in ("structured_events", "explicit_model", "ephemeral_session",
                                                        "print_mode", "verbose", "setting_sources", "strict_mcp_config", "mcp_config")):
        raise ValueError("CLI_CONFIG_CONTROL_UNSUPPORTED")
    if type(mcp_config) is not str or not re.fullmatch(r"/run/[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.json", mcp_config):
        raise ValueError("CLI_MCP_CONFIG_INVALID")
    return (*command, "--print", "--verbose", "--output-format", "stream-json", "--model", model,
            "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config", "--mcp-config", mcp_config,
            *turns, "--", prompt)
