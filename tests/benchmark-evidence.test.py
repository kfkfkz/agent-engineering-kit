"""Artifact process evidence needs trusted requirements and current versions."""
from __future__ import annotations

import hashlib
import json
import runpy
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmark"))
from core.evidence import PlanAnchor, evaluate_artifacts, evaluate_receipt  # noqa: E402

from aek.application.identity import bind_receipt, bound_receipt_as_dict  # noqa: E402
from aek.core.artifact.plan import (  # noqa: E402
    bind_stage,
    build_plan,
    freeze_plan,
    resolved_stage_prerequisites,
)
from aek.core.artifact.registry import ARTIFACT_REGISTRY  # noqa: E402
from aek.core.context.identity import build_identity  # noqa: E402
from aek.core.planning.policy import preview_plan  # noqa: E402


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def encoded(value):
    return json.dumps(asdict(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


class ArtifactEvidenceTests(unittest.TestCase):
    def setUp(self):
        helper = runpy.run_path(str(Path(__file__).with_name("artifact-plan.test.py")))
        self.subject = helper["SUBJECT"]
        facts = helper["known_false"]()
        self.plan = build_plan(preview_plan("standard", self.subject, facts), facts)
        self.credential = freeze_plan(self.plan, input_digest="b" * 64,
                                      generation_mode="native", frozen_at="2026-10-08")
        self.anchor = PlanAnchor(self.plan.digest, self.credential.credential_digest, self.subject)
        self.documents = {}
        self.bindings = {}
        self.gates = {}
        self.parents = {}
        self.trusted_bindings = {}
        for stage in ARTIFACT_REGISTRY.groups:
            active = [item for item in self.plan.items
                      if ARTIFACT_REGISTRY.get_artifact(item.artifact_id).group_id == stage
                      and item.classification != "skipped"]
            if not active:
                continue
            hashes = {}
            for item in active:
                filename = ARTIFACT_REGISTRY.get_artifact(item.artifact_id).filename
                body = ("verified " + filename).encode()
                self.documents[filename] = body
                hashes[filename] = hashlib.sha256(body).hexdigest()
            self.gates[stage] = digest("gate:" + stage)
            self.parents[stage] = {name: digest("parent:" + name)
                                  for name in resolved_stage_prerequisites(self.plan, stage)}
            binding = bind_stage(
                self.plan, self.credential, stage, gate_digest=self.gates[stage],
                doc_hashes=hashes, prerequisite_fingerprints=self.parents[stage])
            self.bindings[stage] = encoded(binding)
            self.trusted_bindings[stage] = binding.binding_digest

    def evaluate(self, **changes):
        arguments = dict(anchor=self.anchor, current_subject_digest=self.subject,
                         documents=self.documents, bindings=self.bindings,
                         gate_digests=self.gates, prerequisite_fingerprints=self.parents,
                         trusted_binding_digests=self.trusted_bindings)
        arguments.update(changes)
        return evaluate_artifacts(encoded(self.plan), encoded(self.credential), **arguments)

    def test_skipped_artifacts_do_not_require_documents(self):
        result = self.evaluate()
        self.assertEqual(result.status, "PASS")
        rows = {item.artifact_id: item for item in result.artifacts}
        self.assertEqual(rows["ui-design"].status, "SKIPPED")
        self.assertEqual(rows["database-design"].status, "SKIPPED")
        self.assertNotIn("数据库设计.md", self.documents)

    def test_agent_sidecars_without_trusted_anchors_are_not_proof(self):
        result = self.evaluate(anchor=None)
        self.assertEqual(result.status, "NOT_EVALUATED")
        self.assertIn("TRUST_ANCHOR_MISSING", result.reason_codes)
        result = self.evaluate(trusted_binding_digests={})
        self.assertEqual(result.status, "NOT_EVALUATED")

    def test_changed_documents_and_subject_invalidate_old_evidence(self):
        documents = dict(self.documents)
        documents["详细设计.md"] = b"Agent says PASS, but content changed"
        self.assertEqual(self.evaluate(documents=documents).status, "STALE")
        missing = dict(self.documents)
        missing.pop("详细设计.md")
        self.assertEqual(self.evaluate(documents=missing).status, "NOT_EVALUATED")
        self.assertEqual(self.evaluate(current_subject_digest="f" * 64).status, "STALE")

    def test_changed_upstream_cannot_leave_downstream_evidence_current(self):
        documents = dict(self.documents)
        documents["概要设计.md"] = b"changed upstream, old gate still exists"
        result = self.evaluate(documents=documents)
        rows = {row.artifact_id: row for row in result.artifacts}
        self.assertEqual(result.status, "STALE")
        self.assertEqual(rows["detail-design"].status, "STALE")

    def test_self_resealed_binding_cannot_replace_trusted_review(self):
        documents = dict(self.documents)
        documents["详细设计.md"] = b"made-up approved design"
        hashes = {"详细设计.md": hashlib.sha256(documents["详细设计.md"]).hexdigest()}
        forged = bind_stage(self.plan, self.credential, "详细设计",
                            gate_digest=self.gates["详细设计"], doc_hashes=hashes,
                            prerequisite_fingerprints=self.parents["详细设计"])
        bindings = dict(self.bindings)
        bindings["详细设计"] = encoded(forged)
        self.assertEqual(self.evaluate(documents=documents, bindings=bindings).status, "INVALID")

    def test_unknown_fields_and_corrupt_plan_are_rejected(self):
        arguments = dict(anchor=self.anchor, current_subject_digest=self.subject,
                         documents=self.documents, bindings=self.bindings,
                         gate_digests=self.gates, prerequisite_fingerprints=self.parents,
                         trusted_binding_digests=self.trusted_bindings)
        for raw in (b'{', b'{}', encoded(self.plan).replace(b'"schema_version":1',
                                                         b'"schema_version":99')):
            self.assertEqual(evaluate_artifacts(raw, encoded(self.credential),
                                                **arguments).status, "INVALID")

    def test_receipt_requires_trusted_provenance_current_identity_and_pass_verdict(self):
        parts = {name: {"digest": char * 64} for name, char in zip(
            ("subject", "artifact", "policy", "context", "evidence"), "abcde")}
        identity = build_identity(parts)
        receipt = bind_receipt(identity, {"status": "PASS"})
        raw = bound_receipt_as_dict(receipt)
        required = ("subject.digest", "policy.digest", "evidence.digest")
        self.assertEqual(evaluate_receipt(raw, identity, required,
                         trusted_receipt_digest=receipt.receipt_digest).status, "PASS")
        self.assertEqual(evaluate_receipt(raw, identity, required,
                         trusted_receipt_digest=None).status, "NOT_EVALUATED")
        changed = dict(parts)
        changed["policy"] = {"digest": "f" * 64}
        self.assertEqual(evaluate_receipt(raw, build_identity(changed), required,
                         trusted_receipt_digest=receipt.receipt_digest).status, "STALE")
        failed = bind_receipt(identity, {"status": "FAIL"})
        self.assertEqual(evaluate_receipt(bound_receipt_as_dict(failed), identity, required,
                         trusted_receipt_digest=failed.receipt_digest).status, "FAIL")
        forged = bind_receipt(identity, {"status": "PASS", "made_up": True})
        self.assertEqual(evaluate_receipt(bound_receipt_as_dict(forged), identity, required,
                         trusted_receipt_digest=receipt.receipt_digest).status, "INVALID")


if __name__ == "__main__":
    unittest.main()
