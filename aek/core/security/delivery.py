"""Pure delivery evidence rules. Integrity, provenance and adoption stay separate.

Observations come from the application's controlled SourceEvidencePort. This
module neither resolves version ranges nor reads files or grants approvals.
"""
from __future__ import annotations
from datetime import datetime, timedelta

from aek.core.context.identity import IdentityEnvelope, IdentityState, compare_identity, decode_identity
from aek.core.security.dependencies import evaluate_design_dependency, is_concrete_version, resolve_dynamic_dependency
from aek.core.security.model import SecurityInputError, _exact_object, _inventories, _proposals, _strings, _text
from aek.core.security.record import _sources, _timestamp


def evaluate_dependency_delivery(decision: dict, inventories: tuple[dict, ...], observations: tuple[dict, ...], *,
                                 current_identity: IdentityEnvelope | None, required_components: tuple[str, ...],
                                 scope_ref: str, now: datetime, max_age_days: int = 7,
                                 accepted_risk_ref: str | None = None,
                                 proposal_ids: tuple[str, ...] | None = None) -> dict:
    """An accepted_risk_ref is supplied only after application-port validation.

    The application binds the direct-user disposition to the current advisory
    digest, dependency/version, identity, scope and expiry before calling this
    function. Observations, cached statuses and raw approved fields cannot
    supply this control-plane argument.
    """
    _exact_object(decision, {"dependency_ref", "proposal", "status", "reason_codes", "source_refs",
                             "pending_delivery_requirements"})
    proposal = decision["proposal"]
    _proposals([proposal])
    proposal_ids = proposal_ids if proposal_ids is not None else (proposal["id"],)
    if (type(proposal_ids) is not tuple or len(set(proposal_ids)) != len(proposal_ids)
            or proposal["id"] not in proposal_ids):
        raise SecurityInputError()
    _strings(list(proposal_ids))
    known_proposals = set(proposal_ids)
    if (decision["dependency_ref"] != proposal["id"] or type(inventories) is not tuple
            or type(observations) is not tuple or current_identity is None):
        raise SecurityInputError()
    _inventories(list(inventories))
    _strings(decision["reason_codes"])
    _strings(decision["source_refs"])
    _strings(decision["pending_delivery_requirements"])
    if accepted_risk_ref is not None:
        _text(accepted_risk_ref)
    if (type(max_age_days) is not int or not 1 <= max_age_days <= 365
            or not isinstance(now, datetime) or now.tzinfo is None):
        raise SecurityInputError()
    valid = {}
    for observation in observations:
        try:
            metadata, facts = observation["metadata"], observation["facts"]
            _sources([metadata])
            checked = _timestamp(metadata["checked_at"])
            if (type(facts) is dict and observation["provenance_ref"]
                    and metadata["status"] == "VERIFIED" and metadata["scope_ref"] == scope_ref
                    and metadata["coverage"] == "COMPLETE_FOR_DECLARED_SCOPE" and not metadata["coverage_gaps"]
                    and checked <= now and now - checked <= timedelta(days=max_age_days)
                    and compare_identity(decode_identity(metadata["identity"]), current_identity,
                                         required_components).state == IdentityState.VALID
                    and compare_identity(decode_identity(metadata["identity"]), current_identity,
                                         tuple(metadata["required_components"])).state == IdentityState.VALID):
                valid[metadata["ref"]] = observation
        except (TypeError, ValueError, KeyError):
            continue
    package_keys = ("ecosystem", "package", "version")
    target_version = proposal['version']
    resolution_refs = []
    resolution_reasons = []
    if not is_concrete_version(target_version):
        resolution = resolve_dynamic_dependency(proposal, observations,
            current_identity=current_identity, required_components=required_components,
            scope_ref=scope_ref, now=now, max_age_days=max_age_days)
        target_version, resolution_refs = resolution['resolved_version'], resolution['source_refs']
        resolution_reasons = resolution['reason_codes']
    design_observations = tuple(o for o in observations if type(o) is dict and type(o.get("metadata")) is dict
        and (o["metadata"].get("ref") in {*proposal["evidence_refs"], *resolution_refs} or (
            o["metadata"].get("kind") in {"advisory", "compatibility"} and type(o.get("facts")) is dict
            and all(o["facts"].get(k) == (target_version if k == 'version' else proposal[k])
                    for k in package_keys))))
    available_design_refs = {o["metadata"]["ref"] for o in design_observations}
    design = evaluate_design_dependency(proposal, design_observations, current_identity=current_identity,
        required_components=required_components, scope_ref=scope_ref, now=now, max_age_days=max_age_days,
        unresolved_refs=tuple(ref for ref in proposal["evidence_refs"]
                              if ref not in available_design_refs and ref != accepted_risk_ref),
        accepted_risk_ref=accepted_risk_ref)
    reasons = list(resolution_reasons)
    evidence = list(design["source_refs"]) + resolution_refs
    failed = design["status"] == "REJECTED"
    if design["status"] != "ADOPTABLE":
        reasons.extend(design["reason_codes"] or ["DESIGN_ADOPTION_REVALIDATION_REQUIRED"])
    if accepted_risk_ref is None and ("ACCEPTED_RISK" in decision["reason_codes"]
                                     or "KNOWN_AFFECTED_DEPENDENCY" in design["reason_codes"]):
        reasons.append("RISK_DISPOSITION_REVALIDATION_REQUIRED")

    trusted_inventories = []
    instance_owners: dict[str, set[str]] = {}
    for item in inventories:
        if item["kind"] not in {"RESOLVED", "SHIPPED"}:
            continue
        refs = {p["source_ref"] for p in item["packages"]}
        sources = [valid[ref] for ref in refs if ref in valid]
        if any(o["metadata"]["kind"] != "inventory" or o["facts"].get("inventory") != item for o in sources):
            reasons.append("INVENTORY_SOURCE_BINDING_MISMATCH")
            continue
        if any(item["producer_ref"] != o["metadata"]["producer_ref"] for o in sources):
            reasons.append("INVENTORY_PRODUCER_MISMATCH")
            continue
        if (sources and len(sources) == len(refs)
                and item["scope_ref"] == scope_ref and item["identity_digest"] == current_identity.envelope_digest
                and item["coverage"] == "COMPLETE_FOR_DECLARED_SCOPE" and not item["coverage_gaps"]):
            trusted_inventories.append(item)
            evidence.extend(o["metadata"]["ref"] for o in sources)
            package_ids = {package["id"] for package in item["packages"]}
            for source in sources:
                if "package_proposals" not in source["facts"]:
                    continue
                mapping = source["facts"]["package_proposals"]
                if type(mapping) is not dict:
                    reasons.append("INVENTORY_PROPOSAL_MAPPING_INVALID")
                    continue
                for package_id, proposal_id in mapping.items():
                    if package_id not in package_ids or type(proposal_id) is not str or not proposal_id:
                        reasons.append("INVENTORY_PROPOSAL_MAPPING_INVALID")
                        continue
                    if proposal_id not in known_proposals:
                        reasons.append("INVENTORY_PROPOSAL_MAPPING_UNKNOWN")
                    instance_owners.setdefault(package_id, set()).add(proposal_id)
    if any(len(owners) > 1 for owners in instance_owners.values()):
        reasons.append("INVENTORY_PROPOSAL_MAPPING_CONFLICT")
    if not any(i["kind"] == "RESOLVED" for i in trusted_inventories):
        reasons.append("RESOLVED_INVENTORY_SOURCE_REQUIRED")
    candidates_by_kind = {kind: [package for item in trusted_inventories if item["kind"] == kind
                                for package in item["packages"]
                                if all(package[key] == proposal[key] for key in ("ecosystem", "package", "scope"))]
                          for kind in ("RESOLVED", "SHIPPED")}
    ambiguous_kinds = {kind for kind, packages in candidates_by_kind.items()
                       if len({package["id"] for package in packages}) > 1
                       and len({package["version"] for package in packages}) > 1}
    association_mode = any(package["id"] in instance_owners
                           for packages in candidates_by_kind.values() for package in packages)
    matching_kinds = set()
    for item in trusted_inventories:
        packages = []
        for package in item["packages"]:
            related = all(package[key] == proposal[key] for key in ("ecosystem", "package", "scope"))
            owners = instance_owners.get(package["id"])
            if owners:
                if len(owners) != 1 or proposal["id"] not in owners:
                    continue
                if not related:
                    reasons.append("INVENTORY_PROPOSAL_BINDING_MISMATCH")
                    continue
            elif related and (association_mode or item["kind"] in ambiguous_kinds):
                reasons.append("INVENTORY_INSTANCE_ASSOCIATION_REQUIRED")
                continue
            elif not related:
                continue
            packages.append(package)
        if not packages:
            continue
        matching_kinds.add(item["kind"])
        for package in packages:
            if package["version"] is None:
                reasons.append("DEPENDENCY_VERSION_UNKNOWN")
            elif target_version is not None and package["version"] != target_version:
                failed = True
                reasons.append("DEPENDENCY_VERSION_MISMATCH")
            if package["relationship"] == "UNKNOWN":
                reasons.append("DEPENDENCY_RELATIONSHIP_UNKNOWN")
            elif package["relationship"] == "transitive" and (
                    not package["introduced_by"] or not package["dependency_path"]):
                reasons.append("DEPENDENCY_PATH_INCOMPLETE")
    if (any(item["kind"] == "RESOLVED" for item in trusted_inventories)
            and "RESOLVED" not in matching_kinds):
        reasons.append("DEPENDENCY_INSTANCE_NOT_RESOLVED" if candidates_by_kind["RESOLVED"]
                       else "DEPENDENCY_PACKAGE_NOT_RESOLVED")

    delivery_scope = [o for o in valid.values() if o["metadata"]["kind"] == "delivery-scope"
                      and set(o["facts"]) == {"shipped_required"} and type(o["facts"]["shipped_required"]) is bool]
    if not delivery_scope:
        reasons.append("DELIVERY_SCOPE_EVIDENCE_REQUIRED")
    else:
        evidence.extend(o["metadata"]["ref"] for o in delivery_scope)
        if len({o["facts"]["shipped_required"] for o in delivery_scope}) > 1:
            reasons.append("DELIVERY_SCOPE_CONFLICT")
        if any(o["facts"]["shipped_required"] for o in delivery_scope) and not any(
                i["kind"] == "SHIPPED" for i in trusted_inventories):
            reasons.append("SHIPPED_INVENTORY_SOURCE_REQUIRED")
        elif (any(o["facts"]["shipped_required"] for o in delivery_scope)
                and "SHIPPED" not in matching_kinds):
            reasons.append("DEPENDENCY_INSTANCE_NOT_SHIPPED" if candidates_by_kind["SHIPPED"]
                           else "DEPENDENCY_PACKAGE_NOT_SHIPPED")
    regression = [o for o in valid.values() if target_version is not None
        and o["metadata"]["kind"] == "regression"
        and set(o["facts"]) == {"passed", "dependency_ref", "version"}
        and o["facts"]["dependency_ref"] == proposal["id"] and o["facts"]["version"] == target_version
        and type(o["facts"]["passed"]) is bool]
    if not regression:
        reasons.append("DEPENDENCY_REGRESSION_REQUIRED")
    else:
        evidence.extend(o["metadata"]["ref"] for o in regression)
        if any(o["facts"]["passed"] is False for o in regression):
            failed = True
            reasons.append("DEPENDENCY_REGRESSION_FAILED")
    return {"status": "FAIL" if failed else "PENDING" if reasons else "PASS",
            "reason_codes": sorted(set(reasons)), "evidence_refs": sorted(set(evidence))}
