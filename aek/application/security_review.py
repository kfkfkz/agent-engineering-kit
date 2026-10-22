"""Security facts at the application boundary; data never grants authority."""
from __future__ import annotations

import json
from dataclasses import asdict, replace
import re
from datetime import datetime, timedelta, timezone
from typing import Protocol

from aek.core.context.identity import (
    IdentityEnvelope, IdentityState, compare_identity, decode_identity, identity_as_dict,
)
from aek.core.security.model import MAX_SECURITY_BYTES, SecurityInputError, decode_security_request
from aek.core.security.record import SecurityRecord, finding_fingerprint, record_as_dict, _timestamp
from aek.core.security.dependencies import (
    decode_advisories, dependency_evidence_digest, evaluate_design_dependency,
    is_concrete_version, resolve_dynamic_dependency,
)
from aek.core.security.delivery import evaluate_dependency_delivery
from aek.core.security.policy import SecurityPolicy
from aek.core.security.scanners import ScanEvidencePolicy, ScanEvidenceView, ScannerPlan, normalize_scan
from aek.core.security.knowledge import compare_security_baseline, derive_security_invariants


class SecurityRecordPort(Protocol):
    def read(self, record_ref: str) -> SecurityRecord | None: ...


class SourceEvidencePort(Protocol):
    def lookup(self, ref: str) -> object | None: ...


def _bounded_response(response: dict[str, object]) -> dict[str, object]:
    raw = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) <= MAX_SECURITY_BYTES:
        return response
    return {"schema_version": 1, "operation": response["operation"], "status": "INCOMPLETE",
            "reason_codes": ["OUTPUT_LIMIT"], "current_identity_digest": response["current_identity_digest"],
            "dependency_decisions": [], "findings": [], "coverage_gaps": ["OUTPUT_LIMIT"],
            "required_evidence": ["BOUNDED_REPORT_REQUIRED"], "record_digest": response["record_digest"]}


class SecurityReviewService:
    """Read-only evaluation, with missing trusted sources disclosed explicitly.

    No source/identity capability can be constructed from the request. The
    standalone reader therefore cannot approve dependencies or close issues.
    """

    def __init__(self, *, record_port: SecurityRecordPort | None = None,
                 source_port: SourceEvidencePort | None = None, approval_port: object | None = None,
                 policy: SecurityPolicy | None = None, clock=None, scanner_ports: dict | None = None,
                 scanner_options: dict | None = None,
                 scan_evidence_policy: ScanEvidencePolicy | None = None) -> None:
        self.record_port = record_port
        self.source_port = source_port
        self.approval_port = approval_port
        self.policy = policy if policy is not None else SecurityPolicy()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.scanner_ports = scanner_ports if scanner_ports is not None else {}
        # Only the composition root supplies local config/cache options. The
        # request union deliberately has no executable, network or grant keys.
        self.scanner_options = scanner_options if scanner_options is not None else {}
        if scan_evidence_policy is not None and type(scan_evidence_policy) is not ScanEvidencePolicy:
            raise SecurityInputError()
        # Independent of advisory age; only the composition root supplies it.
        self.scan_evidence_policy = scan_evidence_policy

    def inspect_scan_v2(self, tool_id: str, raw: bytes, *, plan: ScannerPlan,
                        current_identity: IdentityEnvelope, exit_code: int,
                        resolved_inventory_ref: str, scan_evidence_ref: str,
                        database_evidence_refs: tuple[str, ...], scope_root: str | None = None) -> dict:
        """Read-only evidence view, not an execution or Composer endpoint.

        No request-selected source port, clock, authority or freshness policy.
        The old request union, Record and two-field Composer rows stay intact.
        """
        if type(tool_id) is not str or type(plan) is not ScannerPlan or plan.tool_id != tool_id:
            raise SecurityInputError()
        for value, ceiling in ((plan.max_items, 10000), (plan.max_output_bytes, 2000000)):
            if type(value) is not int or not 0 < value <= ceiling:
                raise SecurityInputError()
        identity = identity_as_dict(current_identity)
        scope = identity['components']['subject'].get('scope_digest')
        if type(scope) is not str or not scope:
            raise SecurityInputError()
        adapter = self.scanner_ports.get(tool_id)
        if (self.scan_evidence_policy is None or tool_id != 'trivy'
                or not callable(getattr(adapter, 'normalize_v2', None))):
            reason = ('DATABASE_POLICY_UNAVAILABLE' if self.scan_evidence_policy is None
                      else 'SCANNER_V2_UNAVAILABLE')
            # Missing control-side capability must not erase already reported
            # vulnerabilities or turn their absence into a safety conclusion.
            scan = normalize_scan(tool_id, raw, exit_code=exit_code, version=plan.tool_version,
                scope_paths=tuple(path for path, _sha in plan.input_files), scope_root=scope_root,
                max_items=min(self.policy.max_items, plan.max_items),
                max_output_bytes=min(self.policy.max_output_bytes, plan.max_output_bytes))
            scan = replace(scan, status=scan.status if scan.status in ('ERROR', 'BLOCKED') else 'INCONCLUSIVE',
                           coverage_gaps=tuple(sorted(set((*scan.coverage_gaps, reason)))))
            return asdict(ScanEvidenceView(scan, {'status': 'UNVERIFIED', 'reason_code': reason},
                {'status': 'UNKNOWN'}, {'status': 'INCONCLUSIVE'},
                {'status': 'UNKNOWN', 'reason_code': reason}))
        view = adapter.normalize_v2(raw, plan=plan,
            exit_code=exit_code, version=plan.tool_version,
            scope_paths=tuple(path for path, _sha in plan.input_files),
            identity_digest=current_identity.envelope_digest, scope_ref=scope,
            source_port=self.source_port, resolved_inventory_ref=resolved_inventory_ref,
            scan_evidence_ref=scan_evidence_ref, database_evidence_refs=database_evidence_refs,
            policy=self.scan_evidence_policy, clock=self._clock, scope_root=scope_root,
            max_items=self.policy.max_items, max_output_bytes=self.policy.max_output_bytes)
        return asdict(view)

    def knowledge(self, record: SecurityRecord, *, current_identity: IdentityEnvelope,
                  required_components: tuple[str, ...], repository_ref: str,
                  baseline: SecurityRecord | None = None) -> dict:
        """Project current source-bound knowledge without rewriting any record.

        A baseline is selected by the control-side caller, never by searching
        for an arbitrary latest file. Confirmation is a separate user action,
        not an accepted-risk receipt or a stored CONFIRMED label.
        """
        body = record_as_dict(record)
        refs = {row["ref"] for row in body["sources"]}
        refs.update(ref for row in body["invariants"] for ref in row["source_refs"])
        observations, _missing = self._observations(sorted(refs))
        scope = body["scope"]["subject_digest"]
        approvals = []
        if self.approval_port is not None:
            for observation in observations:
                facts = observation["facts"]
                if observation["metadata"]["kind"] != "approved-invariant" or type(facts.get("approval_ref")) is not str:
                    continue
                receipt = self.approval_port.read_provenance(facts["approval_ref"], live=True)
                if receipt is None:
                    continue
                event, binding = receipt["event"], receipt["binding"]
                if (event["kind"] == "user-confirmation" and event["operation"] == "confirm-invariant"
                        and binding.get("operation") == "confirm-invariant" and binding.get("scope_ref") == scope
                        and binding.get("identity_digest") == current_identity.envelope_digest
                        and _timestamp(binding["expires_at"]) > self._clock()
                        and type(binding.get("invariants")) is list and binding["invariants"] == facts.get("invariants")):
                    approvals.append(facts["approval_ref"])
        result = derive_security_invariants(tuple(body["invariants"]), tuple(observations),
            current_identity=current_identity, required_components=required_components, scope_ref=scope,
            now=self._clock(), max_age_days=self.policy.max_advisory_age_days, confirmed_user_refs=tuple(sorted(set(approvals))))
        comparison = compare_identity(decode_identity(body["identity"]), current_identity, required_components)
        if comparison.state != IdentityState.VALID:
            result["invariants"] = [{**row, "status": "INVALIDATED"} for row in result["invariants"]]
            result["coverage_gaps"] = sorted(set(result["coverage_gaps"]) | {"CURRENT_IDENTITY_INVALID"})
        if baseline is not None:
            candidates = [o for o in observations if o["metadata"]["kind"] == "baseline"
                          and o["facts"].get("record_digest") == baseline.revision_digest]
            facts = {json.dumps(o["facts"], sort_keys=True, separators=(",", ":")) for o in candidates}
            source = candidates[0] if candidates and len(facts) == 1 else None
            result["baseline"] = compare_security_baseline(baseline, record, baseline_observation=source,
                observations=tuple(observations), current_identity=current_identity,
                required_components=required_components, scope_ref=scope, repository_ref=repository_ref,
                now=self._clock(), max_age_days=self.policy.max_advisory_age_days)
        return result

    def plan(self, raw: bytes, *, current_identity: IdentityEnvelope | None = None,
             required_components: tuple[str, ...] = ()) -> dict[str, object]:
        request = decode_security_request(raw)
        if request.operation != "plan":
            raise SecurityInputError()
        payload = json.loads(request.payload_json)
        if not set(payload["input_refs"]).issubset(payload["scope"]["paths"]):
            raise SecurityInputError()
        response = {"schema_version": 1, "operation": "plan", "status": "INCOMPLETE",
                    "reason_codes": [], "current_identity_digest": None, "dependency_decisions": [],
                    "findings": [], "coverage_gaps": [], "required_evidence": [], "record_digest": None}
        adapter = self.scanner_ports.get(payload["tool_id"])
        if adapter is None:
            response.update(reason_codes=["TOOL_CONFIG_UNAVAILABLE"], required_evidence=["TOOL_CONFIGURATION_REQUIRED"])
            return response
        if current_identity is None:
            response.update(reason_codes=["CURRENT_IDENTITY_UNAVAILABLE"], required_evidence=["CURRENT_IDENTITY_REQUIRED"])
            return response
        response["current_identity_digest"] = current_identity.envelope_digest
        comparison = compare_identity(request.identity, current_identity, required_components)
        if comparison.state != IdentityState.VALID:
            response["reason_codes"] = ["IDENTITY_INVALID" if comparison.state == IdentityState.INVALID else "EVIDENCE_STALE"]
            return response
        identity_scope = identity_as_dict(current_identity)["components"]["subject"].get("scope_digest")
        if identity_scope != payload["scope"]["subject_digest"]:
            response.update(reason_codes=["SCOPE_BINDING_MISMATCH"], required_evidence=["CURRENT_SCOPE_REQUIRED"])
            return response
        try:
            plan = adapter.plan(tuple(payload["input_refs"]), identity_digest=current_identity.envelope_digest,
                canonical_scope_ref=identity_scope,
                timeout_seconds=self.policy.max_duration_seconds, max_output_bytes=self.policy.max_output_bytes,
                max_items=self.policy.max_items, **self.scanner_options.get(payload["tool_id"], {}))
        except (ValueError, OSError):
            response.update(reason_codes=["SCANNER_PLAN_UNAVAILABLE"], required_evidence=["TOOL_CONFIGURATION_REQUIRED"])
            return response
        response.update(status="COMPLETE", plan=asdict(plan))
        return _bounded_response(response)

    def doctor(self, raw: bytes) -> dict[str, object]:
        request = decode_security_request(raw)
        if request.operation != "doctor":
            raise SecurityInputError()
        payload = json.loads(request.payload_json)
        capabilities = []
        missing = []
        for tool_id in payload["tool_ids"]:
            adapter = self.scanner_ports.get(tool_id)
            if adapter is None:
                missing.append(tool_id)
            else:
                capabilities.append(asdict(adapter.probe()))
        return _bounded_response({"schema_version": 1, "operation": "doctor",
            "status": "INCOMPLETE" if missing else "COMPLETE",
            "reason_codes": ["TOOL_CONFIG_UNAVAILABLE"] if missing else [],
            "current_identity_digest": None, "dependency_decisions": [], "findings": [],
            "coverage_gaps": missing, "required_evidence": [], "record_digest": None,
            "capabilities": capabilities})

    def compose_evidence(self, record: SecurityRecord, *, current_identity: IdentityEnvelope,
                         required_components: tuple[str, ...], requirements: tuple[str, ...],
                         stage: str | None = None) -> dict[str, dict]:
        """Use the original Composer's exact two-field evidence rows."""
        decisions = self.inspect_evidence(record, current_identity=current_identity,
            required_components=required_components, requirements=requirements, stage=stage)
        return {key: {"status": row["status"], "evidence_refs": row["evidence_refs"]}
                for key, row in decisions.items()}

    def inspect_evidence(self, record: SecurityRecord, *, current_identity: IdentityEnvelope,
                         required_components: tuple[str, ...], requirements: tuple[str, ...],
                         stage: str | None = None) -> dict[str, dict]:
        """Read-only diagnostics, separate from the stable Composer contract."""
        body = record_as_dict(record)
        stage = stage if stage is not None else body["stage"]
        if (type(requirements) is not tuple or len(set(requirements)) != len(requirements)
                or any(key not in {"checks:dependency-design-check", "checks:dependency-delivery-check"}
                       for key in requirements)):
            raise SecurityInputError()
        comparison = compare_identity(decode_identity(body["identity"]), current_identity, required_components)
        if comparison.state != IdentityState.VALID:
            reason = "IDENTITY_INVALID" if comparison.state == IdentityState.INVALID else "EVIDENCE_STALE"
            return {key: {"status": "PENDING", "evidence_refs": [],
                          "details": {"reason_codes": [reason], "record_digest": record.revision_digest}}
                    for key in requirements}
        recorded_observations, _missing = self._observations(row["ref"] for row in body["sources"])
        proposals = []
        for row in body["dependency_decisions"]:
            proposal = row["proposal"]
            target_version = proposal['version']
            resolution_refs = set()
            if target_version is not None and not is_concrete_version(target_version):
                resolution = resolve_dynamic_dependency(proposal, tuple(recorded_observations),
                    current_identity=current_identity, required_components=required_components,
                    scope_ref=body['scope']['subject_digest'], now=self._clock(),
                    max_age_days=self.policy.max_advisory_age_days)
                target_version = resolution['resolved_version']
                resolution_refs.update(resolution['source_refs'])
            extra = {o["metadata"]["ref"] for o in recorded_observations
                     if o["metadata"]["kind"] in {"advisory", "compatibility"}
                     and target_version is not None and o['facts'].get('version') == target_version
                     and all(o["facts"].get(k) == proposal[k] for k in ("ecosystem", "package"))}
            proposals.append({**proposal, "evidence_refs": sorted(set(proposal["evidence_refs"]) | extra | resolution_refs)})
        payload = {"schema_version": 1, "operation": "evaluate", "stage": stage,
                   "scope": body["scope"], "identity": body["identity"],
                   "proposals": proposals,
                   "surfaces": body["surfaces"], "findings": body["findings"],
                   "inventories": body["inventories"], "run_refs": [], "invariant_refs": []}
        evaluated = self.evaluate(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                  current_identity=current_identity, required_components=required_components)
        decisions = evaluated["dependency_decisions"]
        if not decisions:
            if any(finding["dependency_ref"] is not None for finding in body["findings"]):
                return {key: {"status": "PENDING", "evidence_refs": [],
                              "details": {"reason_codes": ["DEPENDENCY_PROPOSAL_REQUIRED"],
                                          "record_digest": record.revision_digest}} for key in requirements}
            observations = recorded_observations
            scope_rows = [o for o in observations if o["metadata"]["kind"] == "scope"
                          and self._valid_observation(o, current_identity, required_components,
                                                      body["scope"]["subject_digest"])
                          and set(o["facts"]) == {"scope", "dependency_change"}
                          and o["facts"]["scope"] == body["scope"]
                          and type(o["facts"]["dependency_change"]) is bool]
            proven = scope_rows and all(o["facts"]["dependency_change"] is False for o in scope_rows)
            return {key: {"status": "PASS" if proven else "PENDING",
                          "evidence_refs": sorted(o["metadata"]["ref"] for o in scope_rows) if proven else [],
                          "details": {"reason_codes": [] if proven else ["DEPENDENCY_SCOPE_EVIDENCE_REQUIRED"],
                                      "record_digest": record.revision_digest}} for key in requirements}
        result = {}
        for key in requirements:
            rows = decisions
            status = ("FAIL" if any(row["status"] == "REJECTED" for row in rows) else
                      "PASS" if rows and all(row["status"] == "ADOPTABLE" for row in rows) else "PENDING")
            refs = sorted({ref for row in rows for ref in row["source_refs"]})
            if key == "checks:dependency-delivery-check":
                all_refs = {row["ref"] for row in body["sources"]}
                all_refs.update(ref for row in body["dependency_decisions"] for ref in row["proposal"]["evidence_refs"])
                all_refs.update(pkg["source_ref"] for item in body["inventories"] for pkg in item["packages"])
                observations, _missing = self._observations(sorted(all_refs))
                rows = []
                for decision in body["dependency_decisions"]:
                    proposal = decision["proposal"]
                    target_version = proposal['version']
                    resolution_refs = set()
                    if target_version is not None and not is_concrete_version(target_version):
                        resolution = resolve_dynamic_dependency(proposal, tuple(observations),
                            current_identity=current_identity, required_components=required_components,
                            scope_ref=body['scope']['subject_digest'], now=self._clock(),
                            max_age_days=self.policy.max_advisory_age_days)
                        target_version = resolution['resolved_version']
                        resolution_refs.update(resolution['source_refs'])
                    design_observations = tuple(o for o in observations if
                        o["metadata"]["ref"] in set(proposal["evidence_refs"]) | resolution_refs or (
                            o["metadata"]["kind"] in {"advisory", "compatibility"}
                            and target_version is not None and o['facts'].get('version') == target_version
                            and all(o["facts"].get(k) == proposal[k] for k in ("ecosystem", "package"))))
                    approval = self._approval(proposal, design_observations, body["scope"]["subject_digest"],
                                              current_identity.envelope_digest, version=target_version)
                    rows.append(evaluate_dependency_delivery(decision, tuple(body["inventories"]), tuple(observations),
                        current_identity=current_identity, required_components=required_components,
                        scope_ref=body["scope"]["subject_digest"], now=self._clock(),
                        max_age_days=self.policy.max_advisory_age_days, accepted_risk_ref=approval,
                        proposal_ids=tuple(row["proposal"]["id"] for row in body["dependency_decisions"])))
                status = ("FAIL" if any(row["status"] == "FAIL" for row in rows) else
                          "PASS" if rows and all(row["status"] == "PASS" for row in rows) else "PENDING")
                refs = sorted({ref for row in rows for ref in row["evidence_refs"]})
            result[key] = {"status": status, "evidence_refs": refs,
                           "details": {"reason_codes": sorted({reason for row in rows for reason in row["reason_codes"]}),
                                       "record_digest": record.revision_digest}}
        return result

    def _valid_observation(self, observation, current, required, scope_ref):
        metadata = observation["metadata"]
        checked = _timestamp(metadata["checked_at"])
        now = self._clock()
        return (metadata["status"] == "VERIFIED" and bool(observation["provenance_ref"])
                and metadata["scope_ref"] == scope_ref
                and metadata["coverage"] == "COMPLETE_FOR_DECLARED_SCOPE" and not metadata["coverage_gaps"]
                and checked <= now and now - checked <= timedelta(days=self.policy.max_advisory_age_days)
                and compare_identity(decode_identity(metadata["identity"]), current, required).state == IdentityState.VALID
                and compare_identity(decode_identity(metadata["identity"]), current,
                                     tuple(metadata["required_components"])).state == IdentityState.VALID)

    def _observations(self, refs):
        observations, unresolved = [], []
        for ref in refs:
            observation = self.source_port.lookup(ref) if self.source_port is not None else None
            if observation is None:
                unresolved.append(ref)
            else:
                observations.append({"metadata": observation.metadata, "facts": observation.facts,
                                     "provenance_ref": observation.provenance_ref})
        return observations, unresolved

    def _approval(self, proposal, observations, scope_ref, current_digest, *, version):
        if self.approval_port is not None and version is not None:
            for ref in proposal["evidence_refs"]:
                if self.approval_port.validate_disposition(ref, scope_ref=scope_ref, identity_digest=current_digest,
                        dependency_ref=proposal["id"], version=version,
                        evidence_digest=dependency_evidence_digest(tuple(observations))):
                    return ref
        return None

    def _static_verification(self, finding, payload, current, required):
        surfaces = [surface for surface in payload["surfaces"] if surface["id"] in finding["surface_refs"]]
        if (not surfaces or {surface["id"] for surface in surfaces} != set(finding["surface_refs"])
                or any(surface["coverage"] != "COMPLETE_FOR_DECLARED_SCOPE" or surface["coverage_gaps"]
                       or not surface["boundaries"] or not surface["controls"]
                       or any(control["status"] != "ABSENT_WITH_EVIDENCE"
                              for control in surface["controls"]) for surface in surfaces)):
            return False
        observations, _missing = self._observations(finding["evidence_refs"])
        repo = payload["identity"]["components"]["subject"].get("repo_digest")
        for observation in observations:
            facts = observation["facts"]
            if (observation["metadata"]["kind"] != "static-path"
                    or not self._valid_observation(observation, current, required, payload["scope"]["subject_digest"])
                    or set(facts) != {"finding_fingerprint", "verification_status", "preconditions", "surfaces", "path", "complete_path"}
                    or facts["finding_fingerprint"] != finding_fingerprint(finding, repo)
                    or facts["verification_status"] != "STATIC_VERIFIED" or facts["complete_path"] is not True
                    or facts["preconditions"] != finding["preconditions"] or facts["surfaces"] != surfaces
                    or type(facts["path"]) is not list or len(facts["path"]) < 3
                    or any(type(node) is not str for node in facts["path"])):
                continue
            if all(surface["entry_ref"] in facts["path"]
                   and facts["path"][0] in surface["controllable_inputs"]
                   and facts["path"][-1] in surface["sink_refs"] for surface in surfaces):
                return True
        return False

    def _dependency_verification(self, finding, payload, current, required):
        proposal = next((row for row in payload["proposals"] if row["id"] == finding["dependency_ref"]), None)
        if proposal is None or proposal["version"] is None:
            return False
        observations, _missing = self._observations(sorted(set(proposal["evidence_refs"]) | set(finding["evidence_refs"])))
        now = self._clock()
        target_version = proposal['version']
        if not is_concrete_version(target_version):
            resolution = resolve_dynamic_dependency(proposal, tuple(observations), current_identity=current,
                required_components=required, scope_ref=payload['scope']['subject_digest'],
                now=now, max_age_days=self.policy.max_advisory_age_days)
            target_version = resolution['resolved_version']
            if target_version is None:
                return False
        for observation in observations:
            facts = observation["facts"]
            if (observation["metadata"]["kind"] != "advisory"
                    or not self._valid_observation(observation, current, required, payload["scope"]["subject_digest"])
                    or set(facts) != {"ecosystem", "package", "version", "advisories"}
                    or any(facts[key] != proposal[key] for key in ("ecosystem", "package"))
                    or facts['version'] != target_version):
                continue
            try:
                advisories = decode_advisories(facts["advisories"])
            except ValueError:
                continue
            for row in advisories:
                checked = _timestamp(row["checked_at"])
                if (row["match_status"] == "AFFECTED"
                        and checked <= now and now - checked <= timedelta(days=self.policy.max_advisory_age_days)
                        and ({row["canonical_id"], *row["aliases"]} & set(finding["advisory_refs"]))):
                    return True
        return False

    def report(self, raw: bytes, *, current_identity: IdentityEnvelope | None = None,
               required_components: tuple[str, ...] = (), repository_ref: str | None = None,
               baseline: SecurityRecord | None = None) -> dict[str, object]:
        request = decode_security_request(raw)
        if request.operation != "report":
            raise SecurityInputError()
        payload = json.loads(request.payload_json)
        response = {"schema_version": 1, "operation": "report", "status": "INCOMPLETE",
                    "reason_codes": [], "current_identity_digest": None, "dependency_decisions": [],
                    "findings": [], "coverage_gaps": [], "required_evidence": [], "record_digest": None}
        if self.record_port is None:
            response["reason_codes"] = ["SECURITY_RECORD_SOURCE_UNAVAILABLE"]
            return response
        record = self.record_port.read(payload["record_ref"])
        if record is None:
            response["reason_codes"] = ["SECURITY_RECORD_NOT_FOUND"]
            return response
        if record.revision_digest != payload["expected_revision_digest"]:
            response["reason_codes"] = ["SECURITY_REVISION_CONFLICT"]
            return response
        body = record_as_dict(record)
        response.update(status="COMPLETE", record_digest=record.revision_digest,
                        findings=body["findings"], dependency_decisions=body["dependency_decisions"],
                        coverage_gaps=body["coverage_gaps"], required_evidence=body["required_evidence"],
                        reason_codes=["SOURCE_PROVENANCE_NOT_VERIFIED"])
        if current_identity is not None and repository_ref is not None:
            response["derived_knowledge"] = self.knowledge(record, current_identity=current_identity,
                required_components=required_components, repository_ref=repository_ref, baseline=baseline)
        if payload["format"] == "json":
            response["report"] = body
        else:
            text = json.dumps(body, sort_keys=True, ensure_ascii=False, indent=2)
            longest = max((len(match) for match in re.findall(r"~+", text)), default=0)
            fence = "~" * max(3, longest + 1)
            response["report"] = ("# 安全审查记录\n\n"
                                  "记录摘要不代表来源授权或交付放行；来源凭证须独立核验。\n\n"
                                  f"{fence}json\n{text}\n{fence}\n")
        return _bounded_response(response)

    def evaluate(self, raw: bytes, *, current_identity: IdentityEnvelope | None = None,
                 required_components: tuple[str, ...] = ()) -> dict[str, object]:
        request = decode_security_request(raw)
        if request.operation != "evaluate":
            raise SecurityInputError()
        reasons = ["SOURCE_EVIDENCE_UNAVAILABLE"]
        current_digest = None
        identity_required = True
        if current_identity is None:
            reasons.insert(0, "CURRENT_IDENTITY_UNAVAILABLE")
        else:
            try:
                current_digest = identity_as_dict(current_identity)["envelope_digest"]
            except (TypeError, ValueError):
                raise SecurityInputError() from None
            comparison = compare_identity(request.identity, current_identity, required_components)
            if comparison.state == IdentityState.INVALID:
                reasons.insert(0, "IDENTITY_INVALID")
            elif comparison.state == IdentityState.STALE:
                reasons.insert(0, "EVIDENCE_STALE")
            else:
                identity_required = False
        payload = json.loads(request.payload_json)
        findings = payload["findings"]
        for finding in findings:
            if not identity_required and (
                    self._static_verification(finding, payload, current_identity, required_components)
                    or (finding["dependency_ref"] is not None
                        and self._dependency_verification(finding, payload, current_identity, required_components))):
                finding["verification_status"] = "STATIC_VERIFIED"
                finding["coverage_gaps"] = [gap for gap in finding["coverage_gaps"] if gap != "SOURCE_EVIDENCE_UNAVAILABLE"]
                continue
            if finding["verification_status"] != "SUSPECTED":
                finding["verification_status"] = "INCONCLUSIVE"
            if "SOURCE_EVIDENCE_UNAVAILABLE" not in finding["coverage_gaps"]:
                finding["coverage_gaps"].append("SOURCE_EVIDENCE_UNAVAILABLE")
        decisions = []
        for proposal in payload["proposals"]:
            observations = []
            unresolved = []
            if self.source_port is not None and not identity_required:
                for ref in proposal["evidence_refs"]:
                    observation = self.source_port.lookup(ref)
                    if observation is not None:
                        observations.append({"metadata": observation.metadata, "facts": observation.facts,
                                             "provenance_ref": observation.provenance_ref})
                    else:
                        unresolved.append(ref)
            accepted_risk_ref = None
            if self.approval_port is not None and not identity_required:
                target_version = proposal['version']
                if target_version is not None and not is_concrete_version(target_version):
                    resolution = resolve_dynamic_dependency(proposal, tuple(observations),
                        current_identity=current_identity, required_components=required_components,
                        scope_ref=payload['scope']['subject_digest'], now=self._clock(),
                        max_age_days=self.policy.max_advisory_age_days)
                    target_version = resolution['resolved_version']
                accepted_risk_ref = self._approval(proposal, observations, payload['scope']['subject_digest'],
                                                   current_digest, version=target_version)
                if accepted_risk_ref in unresolved:
                    unresolved.remove(accepted_risk_ref)
            decisions.append(evaluate_design_dependency(proposal, tuple(observations),
                current_identity=current_identity if not identity_required else None,
                required_components=required_components, scope_ref=payload["scope"]["subject_digest"],
                now=self._clock(), max_age_days=self.policy.max_advisory_age_days,
                unresolved_refs=tuple(unresolved), accepted_risk_ref=accepted_risk_ref))
        return _bounded_response({
            "schema_version": 1,
            "operation": request.operation,
            "status": "INCOMPLETE",
            "reason_codes": reasons,
            "current_identity_digest": current_digest,
            "dependency_decisions": decisions,
            "findings": findings,
            "coverage_gaps": reasons.copy(),
            "required_evidence": (["CURRENT_IDENTITY_REQUIRED"] if identity_required else [])
                                 + ["SCOPE_EVIDENCE_REQUIRED", "SOURCE_EVIDENCE_REQUIRED"],
            "record_digest": None,
        })
