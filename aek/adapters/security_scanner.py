"""Optional fixed scanner adapters; no binary/database installation."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from aek.adapters.security_process import ProcessPort

from aek.core.security.scanners import NormalizedScan, ScannerCapability, ScannerPlan, VERSIONS, normalize_scan, plan_digest


def _digest(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("INVALID_SCANNER_DIGEST")
    return value


def _file_digest(path: Path) -> str:
    if not path.is_absolute() or not path.is_file():
        raise ValueError("SCANNER_FILE_MISSING")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("SCANNER_SYMLINK_REFUSED")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_file(root: Path, relative: str) -> Path:
    if (type(relative) is not str or not relative or "\\" in relative
            or ":" in relative or "\0" in relative):
        raise ValueError("INVALID_SCANNER_PATH")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"..", "."} for part in relative.split("/")):
        raise ValueError("INVALID_SCANNER_PATH")
    candidate = root / relative
    _file_digest(candidate)
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("INVALID_SCANNER_PATH")
    return candidate


def _material_digest(value: object) -> str:
    import json
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


class ScannerAdapter:
    def __init__(self, repository: Path, tool_id: str, executable_prefix: tuple[str, ...] = (), *, process_port=None):
        if tool_id not in VERSIONS:
            raise ValueError("UNKNOWN_SCANNER")
        self.repository = repository.resolve(strict=True)
        self.tool_id = tool_id
        self.executable_prefix = executable_prefix
        self._version_evidence: tuple[str, str] | None = None
        self.process_port = process_port or ProcessPort()

    def probe(self) -> ScannerCapability:
        if not self.executable_prefix:
            return ScannerCapability(self.tool_id, configured=False, installed="UNKNOWN",
                                     reason_codes=("SCANNER_NOT_CONFIGURED",))
        installed = all(Path(path).is_absolute() and Path(path).is_file()
                        and not Path(path).is_symlink() for path in self.executable_prefix)
        return ScannerCapability(self.tool_id, configured=True, installed=installed,
                                 reason_codes=() if installed else ("SCANNER_NOT_FOUND",))

    def normalize(self, raw: bytes, **kwargs):
        return normalize_scan(self.tool_id, raw, **kwargs)

    def plan(self, input_paths: tuple[str, ...] = (), *, identity_digest: str,
             configuration_path: str | None = None, cache_files: tuple[str, ...] = (),
             action: str = "scan", network_mode: str = "offline",
             required_boundaries: tuple[str, ...] = ("network", "filesystem"), timeout_seconds: float = 300,
             max_output_bytes: int = 2000000, max_items: int = 10000,
             canonical_scope_ref: str | None = None) -> ScannerPlan:
        _digest(identity_digest)
        if canonical_scope_ref is not None:
            _digest(canonical_scope_ref)
        if (action not in {"scan", "version_probe"} or network_mode not in {"offline", "deny"}
                or type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 300
                or type(max_output_bytes) is not int or not 0 < max_output_bytes <= 2000000
                or type(max_items) is not int or not 0 < max_items <= 10000
                or any(item not in {"network", "filesystem"} for item in required_boundaries)):
            raise ValueError("INVALID_SCANNER_PLAN")
        if not self.executable_prefix or type(self.executable_prefix) is not tuple:
            raise ValueError("SCANNER_NOT_CONFIGURED")
        executable_files = tuple((path, _file_digest(Path(path))) for path in self.executable_prefix)
        executable_digest = _material_digest(executable_files)
        inputs = []
        excluded = {".git", ".repo-memory-kit", ".agents", ".claude", ".codex", ".aws", ".ssh"}
        controls = {".semgrepignore", ".gitleaksignore", ".gitleaks.toml", ".gitignore", ".trivyignore", ".trivyignore.yaml", "trivy.yaml", "trivy.yml", ".env", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
        if type(input_paths) is not tuple or len(set(input_paths)) != len(input_paths):
            raise ValueError("INVALID_SCANNER_SCOPE")
        for relative in sorted(input_paths):
            name = PurePosixPath(relative).name
            if (set(PurePosixPath(relative).parts) & excluded or name in controls
                    or name.startswith(".env.") or relative.startswith("docs/memory/")
                    or name.endswith((".pem", ".key"))):
                raise ValueError("SCANNER_CONTROL_FILE_REFUSED")
            source = _local_file(self.repository, relative)
            inputs.append((relative, _file_digest(source)))
        resources = []
        configuration_digest = _material_digest(())
        if action == "scan":
            if not inputs or not configuration_path:
                raise ValueError("SCANNER_INPUT_OR_CONFIGURATION_MISSING")
            config = _local_file(self.repository, configuration_path)
            content = config.read_bytes()
            if len(content) > 2000000:
                raise ValueError("SCANNER_CONFIGURATION_LIMIT")
            text = content.decode("utf-8", errors="strict").lower()
            forbidden = ("http://", "https://", "validators", "autofix", "fix-regex", "allowlist", "extend", "baseline", "pro-secrets")
            # Python 3.10 has no stdlib TOML/YAML parser. Accept a deliberately
            # conservative local-config subset: Unicode escape spellings can
            # otherwise hide semantic allowlist/validator/extension keys from
            # lexical inspection. No claim of general TOML/YAML validation.
            if (any(word in text for word in forbidden) or re.search(r"\\[uUxXN]", text)
                    or re.search(r"\\\r?\n", text)):
                raise ValueError("DANGEROUS_SCANNER_CONFIGURATION")
            if self.tool_id == "trivy" and content.strip() not in {b"", b"{}"}:
                raise ValueError("DANGEROUS_SCANNER_CONFIGURATION")
            configuration_digest = _file_digest(config)
            resources.append(("config", configuration_path, configuration_digest))
            for relative in sorted(cache_files):
                source = _local_file(self.repository, relative)
                resources.append(("cache", relative, _file_digest(source)))
        elif inputs or configuration_path or cache_files:
            raise ValueError("INVALID_VERSION_PROBE_SCOPE")
        database_digest = _material_digest(tuple(item for item in resources if item[0] == "cache")) if cache_files else None
        version = self._version_evidence[1] if action == "scan" and self._version_evidence and self._version_evidence[0] == executable_digest else "UNKNOWN"
        if action == "version_probe":
            argv = self.executable_prefix + (("version",) if self.tool_id == "gitleaks" else ("--version",))
        elif self.tool_id == "trivy":
            argv = self.executable_prefix + ("fs", "--scanners", "vuln", "--format", "json", "--list-all-pkgs", "--offline-scan", "--skip-db-update", "--skip-java-db-update", "--skip-version-check", "--disable-telemetry", "--exit-code", "10", "--cache-dir", "{cache}", "--config", "{config}", "{input}")
        elif self.tool_id == "semgrep":
            argv = self.executable_prefix + ("scan", "--json", "--error", "--metrics", "off", "--disable-version-check", "--disable-nosem", "--config", "{config}", "{input}")
        else:
            argv = self.executable_prefix + ("dir", "--config", "{config}", "--report-format", "json", "--report-path", "{output}", "--redact=100", "--exit-code", "10", "--ignore-gitleaks-allow", "--no-banner", "{input}")
        boundaries = set(required_boundaries)
        if network_mode == "deny":
            boundaries.add("network")
        plan = ScannerPlan(self.tool_id, action, version, self.executable_prefix, executable_files,
                           executable_digest, str(self.repository), canonical_scope_ref or _material_digest(inputs), identity_digest,
                           tuple(inputs), tuple(resources), configuration_digest,
                           configuration_digest if self.tool_id == "semgrep" and action == "scan" else None,
                           database_digest, argv, "{input}", network_mode,
                           ("snapshot", "configuration", "cache", "output"), tuple(sorted(boundaries)),
                           timeout_seconds, max_output_bytes, max_items)
        return replace(plan, plan_digest=plan_digest(plan))

    def validate_plan_material(self, plan):
        """Read-only current-byte validation, also used for completed reuse."""
        if (not isinstance(plan, ScannerPlan) or plan.plan_digest != plan_digest(plan)
                or plan.tool_id != self.tool_id or plan.repository_ref != str(self.repository)
                or plan.executable_prefix != self.executable_prefix):
            raise ValueError("INVALID_SCANNER_PLAN")
        try:
            for path, expected in plan.executable_files:
                if _file_digest(Path(path)) != expected:
                    raise ValueError("SCANNER_PLAN_STALE")
            for path, expected in plan.input_files:
                if _file_digest(_local_file(self.repository, path)) != expected:
                    raise ValueError("SCANNER_PLAN_STALE")
            for _kind, path, expected in plan.resource_files:
                if _file_digest(_local_file(self.repository, path)) != expected:
                    raise ValueError("SCANNER_PLAN_STALE")
        except OSError:
            raise ValueError("SCANNER_PLAN_STALE") from None
        config_paths = [path for kind, path, _digest in plan.resource_files if kind == "config"]
        canonical = self.plan(tuple(path for path, _digest in plan.input_files),
            identity_digest=plan.identity_digest, canonical_scope_ref=plan.scope_ref,
            configuration_path=config_paths[0] if config_paths else None,
            cache_files=tuple(path for kind, path, _digest in plan.resource_files if kind == "cache"),
            action=plan.action, network_mode=plan.network_mode, required_boundaries=plan.required_boundaries,
            timeout_seconds=plan.timeout_seconds, max_output_bytes=plan.max_output_bytes, max_items=plan.max_items)
        # Native provenance attests the historical version probe. Checking
        # unchanged material on restart must not run --version again or depend
        # on this adapter instance's ephemeral version cache.
        canonical = replace(canonical, tool_version=plan.tool_version, plan_digest="")
        canonical = replace(canonical, plan_digest=plan_digest(canonical))
        if canonical != plan:
            raise ValueError("INVALID_SCANNER_PLAN")

    def _validate_execution(self, plan, grant, authority):
        self.validate_plan_material(plan)
        if authority is None or grant is None:
            raise ValueError("AUTHORIZATION_REQUIRED")
        authority.validate_runtime_grant(grant, **{key: getattr(plan, key) for key in (
            "plan_digest", "repository_ref", "scope_ref", "identity_digest", "tool_id",
            "executable_digest", "action", "network_mode", "write_scope", "timeout_seconds",
            "max_output_bytes", "max_items")})
        if not set(plan.required_boundaries) <= self.process_port.supported_boundaries:
            raise ValueError("BOUNDARY_UNAVAILABLE")

    @staticmethod
    def _environment(temporary: Path):
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE"}}
        environment.update({key: str(temporary) for key in ("HOME", "USERPROFILE", "TEMP", "TMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")})
        environment.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        return environment

    def version_probe(self, plan: ScannerPlan, *, grant=None, authority=None) -> ScannerCapability:
        try:
            if plan.action != "version_probe":
                raise ValueError("INVALID_VERSION_PROBE_PLAN")
            self._validate_execution(plan, grant, authority)
            with tempfile.TemporaryDirectory(prefix="aek-version-") as directory:
                root = Path(directory)
                process = self.process_port.run(plan.argv, root, timeout_seconds=plan.timeout_seconds,
                    max_output_bytes=plan.max_output_bytes, environment=self._environment(root))
            if process.reason_code != "PROCESS_COMPLETED" or process.exit_code != 0 or not process.cleanup_complete:
                return ScannerCapability(self.tool_id, True, True, reason_codes=(process.reason_code if process.reason_code != "PROCESS_COMPLETED" else "VERSION_PROBE_EXIT_ERROR",))
            text = process.stdout.decode("utf-8", errors="strict").strip()
            patterns = {"trivy": r"(?:Version:\s*)?(\d+\.\d+\.\d+)(?:\n.*)?",
                        "semgrep": r"(\d+\.\d+\.\d+)",
                        "gitleaks": r"(?:gitleaks version\s+)?(\d+\.\d+\.\d+)"}
            match = re.fullmatch(patterns[self.tool_id], text, flags=re.DOTALL)
            if match is None:
                raise ValueError("INVALID_VERSION_PROBE_OUTPUT")
            version = match.group(1)
            self._version_evidence = (plan.executable_digest, version)
            compatible = version == VERSIONS[self.tool_id]
            return ScannerCapability(self.tool_id, True, True, version, compatible,
                                     compatible, True, "UNKNOWN", () if compatible else ("UNSUPPORTED_VERSION",))
        except (ValueError, OSError, UnicodeError) as error:
            allowed = {"INVALID_SCANNER_PLAN", "SCANNER_PLAN_STALE", "AUTHORIZATION_REQUIRED", "BOUNDARY_UNAVAILABLE",
                       "SECURITY_AUTHORITY_INVALID", "AUTHORIZATION_SOURCE_UNAVAILABLE", "INVALID_VERSION_PROBE_PLAN", "INVALID_VERSION_PROBE_OUTPUT"}
            reason = str(error) if str(error) in allowed else "SCANNER_EXECUTION_REJECTED"
            return ScannerCapability(self.tool_id, reason_codes=(reason,))

    def execute(self, plan: ScannerPlan, *, grant=None, authority=None) -> NormalizedScan | ScannerCapability:
        if isinstance(plan, ScannerPlan) and plan.action == "version_probe":
            return self.version_probe(plan, grant=grant, authority=authority)
        try:
            self._validate_execution(plan, grant, authority)
            if (plan.action != "scan" or plan.tool_version != VERSIONS[self.tool_id]
                    or self._version_evidence != (plan.executable_digest, plan.tool_version)):
                raise ValueError("UNSUPPORTED_VERSION")
            with tempfile.TemporaryDirectory(prefix="aek-scan-") as directory:
                root = Path(directory)
                os.chmod(root, 0o700)
                snapshot, cache, output = root / "input", root / "cache", root / "report.json"
                snapshot.mkdir(mode=0o700)
                cache.mkdir(mode=0o700)
                config = root / ("config" + Path(next(path for kind, path, _digest in plan.resource_files if kind == "config")).suffix)
                for relative, expected in plan.input_files:
                    self._copy_checked(relative, snapshot / relative, expected, 2000000)
                for kind, relative, expected in plan.resource_files:
                    if kind == "config":
                        destination = config
                    else:
                        parts = PurePosixPath(relative).parts
                        if len(parts) < 2 or parts[-2:] not in {("db", "trivy.db"), ("db", "metadata.json"), ("java-db", "trivy-java.db"), ("java-db", "metadata.json")}:
                            raise ValueError("INVALID_SCANNER_CACHE_PATH")
                        destination = cache.joinpath(*parts[-2:])
                    self._copy_checked(relative, destination, expected, 2000000 if kind == "config" else 1000000000)
                if self.tool_id == "trivy" and (not (cache / "db/trivy.db").is_file()
                                               or (cache / "db/trivy.db").stat().st_size == 0):
                    raise ValueError("DATABASE_EVIDENCE_MISSING")
                replacements = {"{input}": str(snapshot), "{cache}": str(cache), "{config}": str(config), "{output}": str(output)}
                argv = tuple(replacements.get(arg, arg) for arg in plan.argv)
                # Snapshot construction can take time. Recheck current files,
                # live approval provenance and expiry immediately before start.
                self._validate_execution(plan, grant, authority)
                process = self.process_port.run(argv, snapshot, timeout_seconds=plan.timeout_seconds,
                    max_output_bytes=plan.max_output_bytes, environment=self._environment(root),
                    output_file=output if self.tool_id == "gitleaks" else None)
                if process.reason_code != "PROCESS_COMPLETED" or not process.cleanup_complete:
                    return NormalizedScan(self.tool_id, plan.tool_version, "ERROR", reason_codes=(process.reason_code,))
                if self.tool_id == "gitleaks":
                    if not output.is_file() or output.is_symlink():
                        raise ValueError("SCANNER_REPORT_MISSING")
                    with output.open("rb") as stream:
                        raw = stream.read(plan.max_output_bytes + 1)
                else:
                    raw = process.stdout
                return normalize_scan(self.tool_id, raw, exit_code=process.exit_code, version=plan.tool_version,
                    scope_paths=tuple(path for path, _digest in plan.input_files), database_digest=plan.database_digest,
                    max_items=plan.max_items, max_output_bytes=plan.max_output_bytes, scope_root=str(snapshot))
        except (ValueError, OSError, StopIteration) as error:
            allowed = {"INVALID_SCANNER_PLAN", "SCANNER_PLAN_STALE", "AUTHORIZATION_REQUIRED", "BOUNDARY_UNAVAILABLE",
                       "SECURITY_AUTHORITY_INVALID", "AUTHORIZATION_SOURCE_UNAVAILABLE", "UNSUPPORTED_VERSION",
                       "SNAPSHOT_INPUT_LIMIT", "DATABASE_EVIDENCE_MISSING", "INVALID_SCANNER_CACHE_PATH", "SCANNER_REPORT_MISSING"}
            reason = str(error) if str(error) in allowed else "SCANNER_EXECUTION_REJECTED"
            return NormalizedScan(self.tool_id, getattr(plan, "tool_version", "UNKNOWN"), "BLOCKED", reason_codes=(reason,))

    def _copy_checked(self, relative, destination, expected, limit):
        source = _local_file(self.repository, relative)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(source, flags)
        total, digest = 0, hashlib.sha256()
        with os.fdopen(descriptor, "rb") as stream, destination.open("xb") as target:
            os.chmod(destination, 0o600)
            for chunk in iter(lambda: stream.read(65536), b""):
                total += len(chunk)
                if total > limit:
                    raise ValueError("SNAPSHOT_INPUT_LIMIT")
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("SCANNER_PLAN_STALE")
