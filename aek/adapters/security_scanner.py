"""Optional fixed scanner adapters; no binary/database installation."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from aek.adapters.security_process import ProcessPort

from aek.core.security.scanners import NormalizedScan, ScannerCapability, ScannerPlan, VERSIONS, normalize_scan, plan_digest
from aek.core.security.scanners import ScanEvidencePolicy, ScanEvidenceView, database_currentness, reconcile_occurrences
from aek.core.security.model import decode_security_json
from aek.core.security.authority import timestamp


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


@dataclass(frozen=True)
class _ScannerCompletion:
    raw: bytes
    exit_code: int
    cleanup_complete: bool
    reason_code: str
    scope_root: str
    plan_digest: str


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

    def normalize_v2(self, raw: bytes, *, exit_code, version, scope_paths,
                     source_port, resolved_inventory_ref, scan_evidence_ref,
                     database_evidence_refs, identity_digest, scope_ref,
                     policy: ScanEvidencePolicy, clock, plan=None,
                     max_items=10000, max_output_bytes=2000000, scope_root=None):
        """Read-only explicit v2 view; imported reports cannot mint provenance.

        Source, policy and clock are injected by the original trusted workflow.
        Missing provenance stays inconclusive; v1 normalize/execute are unchanged.
        """
        if (self.tool_id != "trivy" or type(policy) is not ScanEvidencePolicy or not callable(clock)
                or type(database_evidence_refs) is not tuple or len(database_evidence_refs) > 2
                or any(type(ref) is not str or not ref for ref in database_evidence_refs)
                or len(set(database_evidence_refs)) != len(database_evidence_refs)):
            raise ValueError("INVALID_SCAN_V2_INPUT")
        _digest(identity_digest)
        if type(scope_ref) is not str or not scope_ref:
            raise ValueError("INVALID_SCAN_V2_INPUT")
        now = clock()
        # Also validates the original controlled clock before any evidence IO.
        database_currentness({}, policy=policy, now=now)
        def bounded(value, name, ceiling):
            # Do not coerce invalid inputs (notably bool) into valid integers.
            # The v1 normalizer retains its exact INVALID_SCANNER_INPUT result.
            if type(value) is not int or not 0 < value <= ceiling or type(plan) is not ScannerPlan:
                return value
            limit = getattr(plan, name)
            if type(limit) is not int or not 0 < limit <= ceiling:
                return limit
            return min(value, limit)
        scan = normalize_scan(self.tool_id, raw, exit_code=exit_code, version=version,
            scope_paths=scope_paths, database_digest=None,
            max_items=bounded(max_items, "max_items", 10000),
            max_output_bytes=bounded(max_output_bytes, "max_output_bytes", 2000000), scope_root=scope_root)
        resolved = {"status": "UNKNOWN", "reason_code": "RESOLVED_SOURCE_UNAVAILABLE"}
        covered = {"status": "INCONCLUSIVE", "missing_instance_ids": [],
                   "matched_instance_ids": [], "unexpected_packages": len(scan.packages)}
        execution = {"status": "UNVERIFIED", "reason_code": "SCANNER_SOURCE_UNAVAILABLE"}
        databases = {"status": "UNKNOWN", "items": [], "policy_ref": policy.policy_ref,
                     "checked_at": now.isoformat()}
        gaps = []
        observations, validated_databases = {}, []
        def lookup(ref, kind):
            if type(ref) is not str or not ref or len(ref) > 2048 or source_port is None:
                raise ValueError("SOURCE_UNAVAILABLE")
            observation = source_port.lookup(ref)
            if observation is None:
                raise ValueError("SOURCE_UNAVAILABLE")
            metadata, facts = observation.metadata, observation.facts
            if (metadata.get("status") != "VERIFIED" or metadata.get("kind") != kind
                    or metadata.get("scope_ref") != scope_ref
                    or metadata.get("identity", {}).get("envelope_digest") != identity_digest
                    or timestamp(metadata["checked_at"]) > now
                    or metadata.get("coverage") != "COMPLETE_FOR_DECLARED_SCOPE"
                    or metadata.get("coverage_gaps")):
                raise ValueError("SOURCE_NOT_CURRENT_OR_COMPLETE")
            observations[ref] = (metadata, facts, observation.provenance_ref)
            return facts
        try:
            facts = lookup(resolved_inventory_ref, "inventory")
            inventory = facts["inventory"]
            if (facts.get("schema_version") != 2 or inventory.get("kind") != "RESOLVED"
                    or inventory.get("scope_ref") != scope_ref
                    or inventory.get("identity_digest") != identity_digest
                    or inventory.get("coverage") != "COMPLETE_FOR_DECLARED_SCOPE"
                    or inventory.get("coverage_gaps") or type(facts.get("occurrences")) is not list):
                raise ValueError("RESOLVED_SOURCE_INCOMPLETE")
            covered = reconcile_occurrences(scan.packages, facts["occurrences"])
            resolved = {"status": "COMPLETE", "source_ref": resolved_inventory_ref,
                        "instance_count": len(facts["occurrences"])}
        except (ValueError, TypeError, KeyError, AttributeError):
            gaps.append("RESOLVED_SOURCE_UNAVAILABLE_OR_INCOMPLETE")
        if covered["status"] != "COMPLETE":
            gaps.append("SCANNER_OCCURRENCE_COVERAGE_INCOMPLETE")
        try:
            facts = lookup(scan_evidence_ref, "scanner-run")
            if (facts.get("schema_version") != 2 or facts.get("tool_id") != self.tool_id
                    or facts.get("tool_version") != version or facts.get("output_digest") != scan.output_digest
                    or type(facts.get("exit_code")) is not int or facts["exit_code"] != exit_code
                    or facts.get("resolved_inventory_ref") != resolved_inventory_ref
                    or facts.get("database_evidence_refs") != list(database_evidence_refs)
                    or facts.get("scope_root") != scope_root
                    or type(facts.get("requires_java_db")) is not bool
                    or type(plan) is not ScannerPlan or facts.get("plan_digest") != plan.plan_digest
                    or plan.identity_digest != identity_digest or plan.scope_ref != scope_ref
                    or tuple(path for path, _sha in plan.input_files) != scope_paths
                    or plan.tool_version != version or plan.action != "scan"):
                raise ValueError("SCANNER_SOURCE_BINDING_MISMATCH")
            self.validate_plan_material(plan)
            execution = {"status": "VERIFIED", "source_ref": scan_evidence_ref,
                         "plan_digest": plan.plan_digest}
            required = {"vulnerability", "java"} if facts["requires_java_db"] else {"vulnerability"}
            kinds = set()
            for ref in database_evidence_refs:
                db_facts = lookup(ref, "database")
                if db_facts.get("schema_version") != 2:
                    raise ValueError("DATABASE_SOURCE_SCHEMA_UNSUPPORTED")
                db = db_facts["database"]
                view = database_currentness(db, policy=policy, now=now)
                if timestamp(db["updated_at"]) > timestamp(observations[ref][0]["checked_at"]):
                    raise ValueError("DATABASE_POSTDATES_OBSERVATION")
                kinds.add(db.get("kind"))
                view.update({"source_ref": ref, "kind": db.get("kind")})
                # Current bytes, metadata and scan-plan resources must all agree.
                self._validate_database(db, plan)
                validated_databases.append((ref, db))
                databases["items"].append(view)
            if (kinds != required or len(kinds) != len(database_evidence_refs)
                    or any(row["status"] != "CURRENT" for row in databases["items"])):
                raise ValueError("DATABASE_CURRENTNESS_INCOMPLETE")
            databases["status"] = "CURRENT"
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            gaps.append("SCANNER_PROVENANCE_OR_DATABASE_CURRENTNESS_INCOMPLETE")
        # Preserve candidates even if independent completeness evidence is absent.
        if scan.status not in {"ERROR", "BLOCKED"} and self.tool_id == "trivy":
            payload = decode_security_json(raw)
            candidates = [item for row in payload["Results"] for item in row.get("Vulnerabilities", []) or []]
            findings = []
            for finding, item in zip(scan.findings, candidates):
                finding = dict(finding)
                for source, target in (("FixedVersion", "fixed_versions"), ("PrimaryURL", "source_url")):
                    value = item.get(source)
                    if type(value) is str and 0 < len(value) <= 2048:
                        finding[target] = [part.strip() for part in value.split(",") if part.strip()] if source == "FixedVersion" else value
                aliases = item.get("VendorIDs")
                if type(aliases) is list and len(aliases) <= 100 and all(type(alias) is str and 0 < len(alias) <= 2048 for alias in aliases):
                    finding["aliases"] = list(aliases)
                finding["raw_digest"] = scan.output_digest
                findings.append(finding)
            scan = replace(scan, findings=tuple(findings))
        # Re-read all original provenance plus all current material after IO.
        try:
            if execution["status"] == "VERIFIED":
                self.validate_plan_material(plan)
            for _ref, database in validated_databases:
                self._validate_database(database, plan)
            final_now = clock()
            database_currentness({}, policy=policy, now=final_now)
            if final_now < now:
                raise ValueError("DATABASE_CLOCK_MOVED_BACKWARDS")
            databases["checked_at"] = final_now.isoformat()
            for view, (_ref, database) in zip(databases["items"], validated_databases):
                view.update(database_currentness(database, policy=policy, now=final_now))
            if any(view["status"] != "CURRENT" for view in databases["items"]):
                databases["status"] = "UNKNOWN"
                gaps.append("DATABASE_NOT_CURRENT_AT_PUBLICATION")
            # Last: expiry/revocation can occur during byte hashing or even the
            # final clock read. Do not fence provenance before those operations.
            for ref, expected in observations.items():
                current = source_port.lookup(ref)
                if current is None or (current.metadata, current.facts, current.provenance_ref) != expected:
                    raise ValueError("SOURCE_CHANGED")
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            gaps.append("SCAN_EVIDENCE_CHANGED_DURING_VALIDATION")
            execution = {"status": "UNVERIFIED", "reason_code": "SCAN_EVIDENCE_CHANGED"}
            resolved = {"status": "UNKNOWN", "reason_code": "SCAN_EVIDENCE_CHANGED"}
            covered["status"] = "INCONCLUSIVE"
            databases["status"] = "UNKNOWN"
        discharged = set()
        if databases["status"] == "CURRENT":
            discharged.add("DATABASE_EVIDENCE_MISSING")
        if resolved.get("status") == "COMPLETE" and resolved.get("instance_count") == 0:
            discharged.add("EMPTY_DEPENDENCY_INVENTORY")
            if execution["status"] == "VERIFIED" and covered["status"] == "COMPLETE":
                discharged.add("SCOPE_COVERAGE_INCOMPLETE")
        remaining = tuple(gap for gap in scan.coverage_gaps if gap not in discharged)
        if gaps:
            scan = replace(scan, status=scan.status if scan.status in {"ERROR", "BLOCKED"} else "INCONCLUSIVE",
                coverage_gaps=tuple(sorted(set((*remaining, *gaps)))))
        elif scan.status not in {"ERROR", "BLOCKED"}:
            # A proven empty inventory is distinct from an unexecuted unknown.
            scan = replace(scan, coverage_gaps=remaining,
                status="INCONCLUSIVE" if remaining else "FAILED" if scan.findings else "PASSED")
        return ScanEvidenceView(scan, execution, resolved, covered, databases)

    def _validate_database(self, database, plan):
        """Stream actual DB bytes via the existing cross-platform handle boundary."""
        from contextlib import ExitStack
        import stat
        from aek.adapters import ui_filesystem as fs
        def read(relative, limit, *, content=False):
            if type(relative) is not str or len(relative) > 2048:
                raise ValueError("INVALID_DATABASE_PATH")
            parts = relative.split("/")
            with ExitStack() as stack:
                directory = fs.open_absolute_dir(self.repository)
                stack.callback(fs.close_dir, directory)
                for part in parts[:-1]:
                    directory = fs.open_child_dir(directory, part)
                    stack.callback(fs.close_dir, directory)
                fd = fs.open_file(directory, parts[-1], os.O_RDONLY)
                stack.callback(os.close, fd)
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
                    raise ValueError("DATABASE_SIZE_INVALID")
                digest, data, total = hashlib.sha256(), bytearray(), 0
                while chunk := os.read(fd, min(1024 * 1024, limit + 1 - total)):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("DATABASE_SIZE_INVALID")
                    digest.update(chunk)
                    if content:
                        data.extend(chunk)
                after = os.fstat(fd)
                if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        or total != before.st_size):
                    raise ValueError("DATABASE_CHANGED")
                return digest.hexdigest(), bytes(data)
        db_sha, _ = read(database["path"], 1000000000)
        metadata_sha, raw = read(database["metadata_path"], 65536, content=True)
        if (db_sha != database["sha256"] or metadata_sha != database["metadata_sha256"]
                or ("cache", database["path"], db_sha) not in plan.resource_files
                or ("cache", database["metadata_path"], metadata_sha) not in plan.resource_files):
            raise ValueError("DATABASE_CONTENT_BINDING_MISMATCH")
        metadata = decode_security_json(raw)
        if (type(metadata) is not dict or type(metadata.get("Version")) is not int
                or metadata["Version"] != database["schema"]
                or metadata.get("UpdatedAt") != database["updated_at"]):
            raise ValueError("DATABASE_METADATA_BINDING_MISMATCH")

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

    def execute(self, plan: ScannerPlan, *, grant=None, authority=None, completion_sink=None) -> NormalizedScan | ScannerCapability:
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
                normalized = normalize_scan(self.tool_id, raw, exit_code=process.exit_code, version=plan.tool_version,
                    scope_paths=tuple(path for path, _digest in plan.input_files), database_digest=plan.database_digest,
                    max_items=plan.max_items, max_output_bytes=plan.max_output_bytes, scope_root=str(snapshot))
            # The internal host observer receives only this original execution,
            # after temporary cleanup and a final grant/current-material check.
            # Default callers retain the original return value and behavior.
            if completion_sink is not None and normalized.status not in {"ERROR", "BLOCKED"}:
                self._validate_execution(plan, grant, authority)
                completion_sink(_ScannerCompletion(raw, process.exit_code, process.cleanup_complete,
                    process.reason_code, str(snapshot), plan.plan_digest))
            return normalized
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
