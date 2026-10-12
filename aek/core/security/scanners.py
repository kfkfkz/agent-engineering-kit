"""Pinned, data-only scanner contracts. Completion is not source authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
import re

from aek.core.security.model import decode_security_json

VERSIONS = {"trivy": "0.75.0", "semgrep": "1.180.0", "gitleaks": "8.30.1"}


@dataclass(frozen=True)
class ScannerCapability:
    tool_id: str
    configured: bool | str = "UNKNOWN"
    installed: bool | str = "UNKNOWN"
    version: str = "UNKNOWN"
    version_compatible: bool | str = "UNKNOWN"
    runnable: bool | str = "UNKNOWN"
    authorized: bool | str = "UNKNOWN"
    covered: bool | str = "UNKNOWN"
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScannerPlan:
    tool_id: str
    action: str
    tool_version: str
    executable_prefix: tuple[str, ...]
    executable_files: tuple[tuple[str, str], ...]
    executable_digest: str
    repository_ref: str
    scope_ref: str
    identity_digest: str
    input_files: tuple[tuple[str, str], ...]
    resource_files: tuple[tuple[str, str, str], ...]
    configuration_digest: str
    rules_digest: str | None
    database_digest: str | None
    argv: tuple[str, ...]
    cwd: str
    network_mode: str
    write_scope: tuple[str, ...]
    required_boundaries: tuple[str, ...]
    timeout_seconds: float
    max_output_bytes: int
    max_items: int
    plan_digest: str = ""


def plan_digest(plan: ScannerPlan) -> str:
    body = asdict(plan)
    body.pop("plan_digest")
    return hashlib.sha256(b"aek-scanner-plan-v1\0" + json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load_scanner_plan(value: object) -> ScannerPlan:
    """Decode an immutable material manifest; it is never an execution grant."""
    if type(value) is not dict or set(value) != {field.name for field in fields(ScannerPlan)}:
        raise ValueError("INVALID_SCANNER_PLAN")
    body = dict(value)
    for key in ("executable_prefix", "argv", "write_scope", "required_boundaries"):
        if type(body[key]) is not list or any(type(item) is not str for item in body[key]):
            raise ValueError("INVALID_SCANNER_PLAN")
        body[key] = tuple(body[key])
    for key, width in (("executable_files", 2), ("input_files", 2), ("resource_files", 3)):
        if (type(body[key]) is not list or any(type(row) is not list or len(row) != width
                or any(type(item) is not str for item in row) for row in body[key])):
            raise ValueError("INVALID_SCANNER_PLAN")
        body[key] = tuple(tuple(row) for row in body[key])
    plan = ScannerPlan(**body)
    if plan.plan_digest != plan_digest(plan):
        raise ValueError("INVALID_SCANNER_PLAN")
    return plan


@dataclass(frozen=True)
class NormalizedScan:
    tool_id: str
    version: str
    status: str
    findings: tuple[dict[str, object], ...] = ()
    packages: tuple[dict[str, object], ...] = ()
    coverage_gaps: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    output_digest: str = ""
    source_status: str = "DECLARED"


def _path(value: object, scope: tuple[str, ...], scope_root: str | None = None) -> str:
    if scope_root is not None and type(value) is str:
        root = scope_root.replace("\\", "/").rstrip("/")
        value = value.replace("\\", "/")
        absolute = value.startswith("/") or re.match(r"^[A-Za-z]:/", value)
        if absolute:
            prefix = root + "/"
            insensitive = bool(re.match(r"^[A-Za-z]:/", root)) or root.startswith("//")
            matches = value.lower().startswith(prefix.lower()) if insensitive else value.startswith(prefix)
            if not matches:
                raise ValueError("INVALID_SCANNER_PATH")
            value = value[len(prefix):]
    if type(value) is not str or "\\" in value or ":" in value or "\0" in value:
        raise ValueError("INVALID_SCANNER_PATH")
    if value.startswith("/") or ".." in value.split("/") or value not in scope:
        raise ValueError("INVALID_SCANNER_PATH")
    return value


def _text(value: object) -> str:
    if type(value) is not str or not value or len(value) > 2048:
        raise ValueError("INVALID_SCANNER_OUTPUT")
    return value


def _severity(value: object) -> str:
    return value if type(value) is str and value in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO", "UNKNOWN"} else "UNKNOWN"


def normalize_scan(tool_id: str, raw: bytes, *, exit_code: int | None, version: str,
                   scope_paths: tuple[str, ...], database_digest: str | None = None,
                   max_items: int = 10000, max_output_bytes: int = 2000000,
                   scope_root: str | None = None) -> NormalizedScan:
    digest = hashlib.sha256(raw).hexdigest() if type(raw) is bytes else ""
    def result(status, findings=(), packages=(), gaps=(), reasons=()):
        return NormalizedScan(tool_id, version, status, tuple(findings), tuple(packages),
                              tuple(sorted(set(gaps))), tuple(reasons), digest)
    if (type(max_items) is not int or not 0 < max_items <= 10000
            or type(max_output_bytes) is not int or not 0 < max_output_bytes <= 2000000
            or type(scope_paths) is not tuple or any(type(path) is not str for path in scope_paths)
            or len(set(scope_paths)) != len(scope_paths)
            or (scope_root is not None and type(scope_root) is not str)
            or (database_digest is not None and (type(database_digest) is not str
                or re.fullmatch(r"[a-f0-9]{64}", database_digest) is None))):
        return result("ERROR", reasons=("INVALID_SCANNER_INPUT",))
    if tool_id not in VERSIONS or version != VERSIONS[tool_id]:
        return result("BLOCKED", reasons=("UNSUPPORTED_VERSION",))
    if type(exit_code) is not int or exit_code not in ({0, 1} if tool_id == "semgrep" else {0, 10}):
        return result("ERROR", reasons=("SCANNER_EXIT_ERROR",))
    if type(raw) is not bytes or len(raw) > min(2000000, max_output_bytes):
        return result("ERROR", reasons=("OUTPUT_LIMIT_EXCEEDED",))
    findings, packages, gaps = [], [], []
    try:
        payload = decode_security_json(raw)
        if tool_id == "gitleaks":
            if type(payload) is not list:
                raise ValueError("INVALID_SCANNER_OUTPUT")
            if len(payload) > min(10000, max_items):
                return result("ERROR", reasons=("ITEM_LIMIT_EXCEEDED",))
            for item in payload:
                path = _path(item["File"], scope_paths, scope_root)
                if item.get("SymlinkFile"):
                    raise ValueError("INVALID_SCANNER_PATH")
                # Rule names/descriptions are untrusted free text; hashes preserve
                # stable identity without carrying echoed Secret/Match bytes.
                rule_digest = hashlib.sha256(_text(item["RuleID"]).encode()).hexdigest()
                line = item["StartLine"]
                if type(line) is not int or line < 1:
                    raise ValueError("INVALID_SCANNER_OUTPUT")
                secrets = [item.get(key) for key in ("Secret", "Match")]
                if any(type(secret) is str and secret and secret in path for secret in secrets):
                    raise ValueError("INVALID_SCANNER_OUTPUT")
                findings.append({"path": path, "rule_digest": rule_digest, "line": line,
                                 "severity": "UNKNOWN", "verification_status": "SUSPECTED"})
            if bool(findings) != (exit_code == 10):
                return result("ERROR", reasons=("SCANNER_EXIT_OUTPUT_MISMATCH",))
            if not scope_paths:
                gaps.append("SCOPE_COVERAGE_INCOMPLETE")
            return result("INCONCLUSIVE" if gaps else "FAILED" if findings else "PASSED", findings, gaps=gaps)
        if tool_id == "semgrep":
            if (type(payload) is not dict or payload.get("version") != version
                    or type(payload.get("results")) is not list
                    or type(payload.get("errors")) is not list
                    or type(payload.get("paths")) is not dict
                    or type(payload.get("skipped_rules")) is not list):
                raise ValueError("INVALID_SCANNER_OUTPUT")
            scanned = payload["paths"].get("scanned")
            if type(scanned) is not list or type(payload["paths"].get("skipped", [])) is not list:
                raise ValueError("INVALID_SCANNER_OUTPUT")
            targets = {_path(item, scope_paths, scope_root) for item in scanned}
            if not targets or targets != set(scope_paths):
                gaps.append("SCOPE_COVERAGE_INCOMPLETE")
            if payload["paths"].get("skipped"):
                gaps.append("SCANNER_SKIPPED_PATHS")
            if payload["skipped_rules"]:
                gaps.append("SCANNER_SKIPPED_RULES")
            if payload["errors"]:
                gaps.append("SCANNER_ERRORS")
            for item in payload["results"]:
                if type(item) is not dict or type(item.get("extra")) is not dict:
                    raise ValueError("INVALID_SCANNER_OUTPUT")
                findings.append({"path": _path(item["path"], scope_paths, scope_root),
                                 "rule_id": _text(item["check_id"]),
                                 "severity": {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "INFO"}.get(item["extra"].get("severity"), "UNKNOWN"),
                                 "verification_status": "SUSPECTED"})
            if len(findings) > min(10000, max_items):
                return result("ERROR", reasons=("ITEM_LIMIT_EXCEEDED",))
            if bool(findings) != (exit_code == 1):
                return result("ERROR", reasons=("SCANNER_EXIT_OUTPUT_MISMATCH",))
            return result("INCONCLUSIVE" if gaps else "FAILED" if findings else "PASSED", findings, gaps=gaps)
        if type(payload) is not dict or type(payload.get("SchemaVersion")) is not int or payload["SchemaVersion"] != 2:
            raise ValueError("INVALID_SCANNER_OUTPUT")
        rows = payload["Results"]
        if type(rows) is not list:
            raise ValueError("INVALID_SCANNER_OUTPUT")
        targets = set()
        for row in rows:
            if (type(row) is not dict
                    or (row.get("Packages") is not None and type(row["Packages"]) is not list)
                    or (row.get("Vulnerabilities") is not None and type(row["Vulnerabilities"]) is not list)):
                raise ValueError("INVALID_SCANNER_OUTPUT")
            target = _path(row["Target"], scope_paths, scope_root)
            targets.add(target)
            for package in row.get("Packages", []) or []:
                packages.append({"path": target, "ecosystem": _text(row["Type"]),
                                 "package": _text(package["Name"]), "version": _text(package["Version"])})
                if package.get("ID") is not None:
                    packages[-1]["package_id"] = _text(package["ID"])
            for item in row.get("Vulnerabilities", []) or []:
                findings.append({"path": target, "advisory_id": _text(item["VulnerabilityID"]),
                                 "package": _text(item["PkgName"]), "version": _text(item["InstalledVersion"]),
                                 "severity": _severity(item.get("Severity", "UNKNOWN")),
                                 "verification_status": "SUSPECTED"})
                if item.get("PkgID") is not None:
                    findings[-1]["package_id"] = _text(item["PkgID"])
        if not packages:
            gaps.append("EMPTY_DEPENDENCY_INVENTORY")
        if not database_digest:
            gaps.append("DATABASE_EVIDENCE_MISSING")
        if targets != set(scope_paths):
            gaps.append("SCOPE_COVERAGE_INCOMPLETE")
        if len(findings) + len(packages) > min(10000, max_items):
            return result("ERROR", reasons=("ITEM_LIMIT_EXCEEDED",))
        if bool(findings) != (exit_code == 10):
            return result("ERROR", reasons=("SCANNER_EXIT_OUTPUT_MISMATCH",))
        return result("INCONCLUSIVE" if gaps else "FAILED" if findings else "PASSED", findings, packages, gaps)
    except (ValueError, TypeError, KeyError, OverflowError):
        return result("ERROR", reasons=("INVALID_SCANNER_OUTPUT",))
