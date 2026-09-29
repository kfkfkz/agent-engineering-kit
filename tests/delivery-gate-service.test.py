#!/usr/bin/env python3
"""Delivery Gate composes route, governance, identity and evidence."""
from __future__ import annotations

import unittest

from aek.application.delivery_gate import (
    DeliveryGateError,
    bind_delivery_gate_receipt,
    compose_delivery_gate,
)
from aek.application.identity import bound_receipt_as_dict
from aek.application.review_evidence import (
    ReviewEvidencePlan,
    ReviewEvidenceStep,
)
from aek.core.context.identity import (
    IdentityComparison,
    IdentityState,
    build_identity,
)


def route_result() -> dict[str, object]:
    return {
        "schema_version": 1,
        "effective_route": "bounded",
        "route_verdict": {"status": "DECIDED"},
        "requirements": {
            "required_checks": ["targeted-tests", "security-review"],
        },
        "governance": {
            "profile": "strict",
            "verdict": "DECIDED",
            "matched_rules": [{
                "rule_id": "security",
                "required": ["required_check:security-review",
                             "independent_review"],
            }],
            "risk_requirements": {
                "checks": ["security-review"],
                "reviews": ["independent"],
                "receipts": ["formal"],
                "artifacts": [],
            },
            "provenance": [
                {"kind": "checks", "requirement": "security-review",
                 "source": "required_check:security-review"},
                {"kind": "reviews", "requirement": "independent",
                 "source": "independent_review"},
                {"kind": "receipts", "requirement": "formal",
                 "source": "profile:strict"},
            ],
        },
        "ok": True,
    }


def review_plan() -> ReviewEvidencePlan:
    return ReviewEvidencePlan(
        steps=(ReviewEvidenceStep(
            "codebase_graph", "GRAPH_FRESH", ("search_graph",)),),
        blocking_reasons=(), limitations=(), confidence="high",
        may_claim_complete_structure=True,
    )


def evidence(*, review_status: str = "PASS") -> dict[str, object]:
    return {
        "checks:targeted-tests": {
            "status": "PASS", "evidence_refs": ["evidence/tests.json"]},
        "checks:security-review": {
            "status": "PASS", "evidence_refs": ["evidence/security.json"]},
        "reviews:independent": {
            "status": review_status,
            "evidence_refs": (["evidence/review.json"]
                              if review_status == "PASS" else [])},
        "receipts:formal": {
            "status": "PASS", "evidence_refs": ["evidence/receipt.json"]},
    }


def envelope():
    return build_identity({
        "subject": {"change_id": "a" * 64, "task_id": "b" * 64,
                    "head_sha": "c" * 64},
        "artifact": {"revision": "d" * 64},
        "policy": {"version": "e" * 64},
        "context": {"closure": "f" * 64},
        "evidence": {"verification": "1" * 64},
    })


class DeliveryGateServiceTests(unittest.TestCase):
    def test_ready_result_has_policy_fact_and_evidence_for_every_requirement(self):
        result = compose_delivery_gate(
            route_result=route_result(),
            identity=IdentityComparison(IdentityState.VALID),
            review_plan=review_plan(), evidence_by_requirement=evidence())

        self.assertEqual(result["verdict"], "READY")
        rows = {row["requirement_id"]: row
                for row in result["requirements"]}
        self.assertEqual(set(rows), {
            "checks:targeted-tests", "checks:security-review",
            "reviews:independent", "receipts:formal"})
        self.assertEqual(rows["checks:security-review"]["policy_ids"],
                         ["security"])
        self.assertEqual(rows["checks:security-review"]["fact_ids"],
                         ["rule_match:security"])
        self.assertEqual(rows["checks:targeted-tests"]["policy_ids"],
                         ["route:bounded"])
        self.assertEqual(rows["checks:targeted-tests"]["fact_ids"],
                         ["route:bounded"])
        self.assertEqual(rows["receipts:formal"]["policy_ids"],
                         ["profile:strict"])
        self.assertEqual(rows["checks:security-review"]["evidence_refs"],
                         ["evidence/security.json"])

    def test_stale_identity_blocks_even_when_evidence_passes(self):
        result = compose_delivery_gate(
            route_result=route_result(),
            identity=IdentityComparison(
                IdentityState.STALE,
                changed=("policy.version",),
                reason_codes=("IDENTITY_COMPONENT_CHANGED",)),
            review_plan=review_plan(), evidence_by_requirement=evidence())

        self.assertEqual(result["verdict"], "NOT_READY")
        self.assertEqual(result["identity"]["state"], "STALE")
        self.assertIn("IDENTITY_COMPONENT_CHANGED", result["reason_codes"])

    def test_pending_independent_review_needs_human(self):
        result = compose_delivery_gate(
            route_result=route_result(),
            identity=IdentityComparison(IdentityState.VALID),
            review_plan=review_plan(),
            evidence_by_requirement=evidence(review_status="PENDING"))

        self.assertEqual(result["verdict"], "NEEDS_HUMAN_REVIEW")
        self.assertIn("reviews:independent", result["pending_requirements"])

    def test_receipt_is_bound_to_the_full_identity(self):
        result = compose_delivery_gate(
            route_result=route_result(),
            identity=IdentityComparison(IdentityState.VALID),
            review_plan=review_plan(), evidence_by_requirement=evidence())
        receipt = bound_receipt_as_dict(
            bind_delivery_gate_receipt(envelope(), result))

        self.assertEqual(receipt["payload"]["verdict"], "READY")
        self.assertEqual(receipt["identity"]["components"]["subject"]
                         ["head_sha"], "c" * 64)

    def test_unknown_evidence_or_malformed_route_fails_closed(self):
        bad = evidence()
        bad["checks:targeted-tests"] = {
            "status": "MAYBE", "evidence_refs": []}
        with self.assertRaises(DeliveryGateError):
            compose_delivery_gate(
                route_result=route_result(),
                identity=IdentityComparison(IdentityState.VALID),
                review_plan=review_plan(), evidence_by_requirement=bad)
        malformed = route_result()
        malformed["governance"] = {"verdict": "DECIDED"}
        with self.assertRaises(DeliveryGateError):
            compose_delivery_gate(
                route_result=malformed,
                identity=IdentityComparison(IdentityState.VALID),
                review_plan=review_plan(), evidence_by_requirement=evidence())


if __name__ == "__main__":
    unittest.main()
