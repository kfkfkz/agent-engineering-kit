#!/usr/bin/env python3
"""Real CLI convergence retains failures and current verification authority."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = runpy.run_path(str(ROOT / "tests" / "review-v2.test.py"))
from aek.adapters.review_markdown import parse_review_documents


class ReviewConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / ".git").mkdir()
        self.sdd = self.repo / "docs" / "03-SDD" / "001-review"
        self.sdd.mkdir(parents=True)
        (self.sdd / "需求分析.md").write_text(FIXTURE["REQUIREMENTS"].replace(
            "冻结状态：未冻结", "冻结状态：已冻结"), encoding="utf-8")
        (self.sdd / "概要设计.md").write_text(FIXTURE["OUTLINE"], encoding="utf-8")
        self.reviews = self.sdd / "reviews"
        self.assertEqual(self.invoke("freeze", "--stage", "需求分析", "--by", "synthetic-k", "--json").returncode, 0)

    def invoke(self, command, *arguments):
        return subprocess.run([sys.executable, str(ROOT / "doc-gate"), command,
            str(self.sdd), *arguments], capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"})

    def prepare(self):
        result = self.invoke("review-input", "--stage", "概要设计", "--schema-version", "2", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def observe(self, output, issues):
        (self.reviews / "概要设计.review-receipt.json").write_text(json.dumps(output["receipt"], ensure_ascii=False), encoding="utf-8")
        (self.reviews / "概要设计.issues.json").write_text(json.dumps({"reviewer": "independent-fixture",
            "stage": "概要设计", "doc_hashes": output["receipt"]["doc_hashes"], "issues": issues}, ensure_ascii=False), encoding="utf-8")
        return self.invoke("gate", "--stage", "概要设计", "--json")

    def issue(self, location="概要设计.md / F1"):
        return {"id": "REV-1", "severity": "major", "category": "verifiability",
            "location": location, "problem": "查询重复处理缺失", "evidence": "未说明重复处理",
            "required_change": "说明重复查询策略", "affected_ids": ["F1", "A1"], "upstream": False}

    def test_unchanged_failure_stops_before_redelivery_and_preserves_open_issue(self):
        output = self.prepare()
        gated = self.observe(output, [self.issue("UNSCOPED")])
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        original = {path.name: path.read_bytes() for path in self.reviews.iterdir()
            if path.name.endswith(("review-request.json", "issue-lifecycle.json", "gate.json", "gate.log"))}
        stopped = self.invoke("review-input", "--stage", "概要设计", "--schema-version", "2", "--json")
        self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
        self.assertIn("NO_REVIEW_PROGRESS", stopped.stdout)
        self.assertNotIn("full_documents", stopped.stdout)
        for name, raw in original.items():
            self.assertEqual((self.reviews / name).read_bytes(), raw)

    def _claim_fixed_and_return_clean_review(self, location, *, include_locator=False):
        output = self.prepare()
        first_gate = self.observe(output, [self.issue(location)])
        self.assertEqual(first_gate.returncode, 1, first_gate.stdout + first_gate.stderr)
        lifecycle = json.loads((self.reviews / "概要设计.issue-lifecycle.json").read_text(encoding="utf-8"))
        issue = lifecycle["issues"][0]
        doc_hashes = {"概要设计.md": hashlib.sha256(
            (self.sdd / "概要设计.md").read_bytes()).hexdigest()}
        claim = {
            "schema_version": 1,
            "stage": "概要设计",
            "base_snapshot_digest": lifecycle["snapshot_digest"],
            "doc_hashes": doc_hashes,
            "claims": [{"fingerprint": issue["fingerprint"], "state": "FIXED"}],
        }
        (self.reviews / "概要设计.issue-claims.json").write_text(
            json.dumps(claim, ensure_ascii=False), encoding="utf-8")
        next_input = self.invoke("review-input", "--stage", "概要设计", "--schema-version", "2", "--json")
        self.assertEqual(next_input.returncode, 0, next_input.stdout + next_input.stderr)
        next_output = json.loads(next_input.stdout)
        receipt = next_output["receipt"]
        if include_locator:
            material = parse_review_documents(
                "概要设计", {"概要设计.md": (self.sdd / "概要设计.md").read_bytes().decode(
                    "utf-8")}, next_output["request"]["dependency_fingerprint"])
            issue = lifecycle["issues"][0]
            owner_ids = set(issue["occurrences"][-1]["affected_ids"])
            sections = [section for section in material.snapshot.sections
                        if owner_ids.intersection(section.defines_ids)]
            receipt["issue_locations"] = [{
                "fingerprint": issue["fingerprint"],
                "document": section.document,
                "heading": section.heading,
                "section_digest": section.digest,
            } for section in sections]
        (self.reviews / "概要设计.review-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        (self.reviews / "概要设计.issues.json").write_text(json.dumps({
            "reviewer": "independent-fixture", "stage": "概要设计",
            "doc_hashes": receipt["doc_hashes"], "issues": [],
        }, ensure_ascii=False), encoding="utf-8")
        gated = self.invoke("gate", "--stage", "概要设计", "--json")
        return gated, next_output

    def test_fixed_claim_needs_reviewer_coverage_for_unknown_location(self):
        gated, output = self._claim_fixed_and_return_clean_review("UNSCOPED")
        self.assertEqual(output["request"]["mode"], "full")
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        lifecycle = json.loads((self.reviews / "概要设计.issue-lifecycle.json").read_text(encoding="utf-8"))
        self.assertEqual(lifecycle["issues"][0]["state"], "FIXED")

    def test_incremental_owner_coverage_verifies_fixed_claim(self):
        gated, output = self._claim_fixed_and_return_clean_review(
            "概要设计.md / 功能点设计 / F1：查询")
        self.assertEqual(output["request"]["mode"], "incremental")
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        lifecycle = json.loads((self.reviews / "概要设计.issue-lifecycle.json").read_text(encoding="utf-8"))
        self.assertEqual(lifecycle["issues"][0]["state"], "CLOSED")

    def test_full_review_with_issue_locator_publishes_material_baseline_after_pass(self):
        gated, output = self._claim_fixed_and_return_clean_review(
            "UNSCOPED", include_locator=True)
        self.assertEqual(output["request"]["mode"], "full")
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        lifecycle = json.loads((self.reviews / "概要设计.issue-lifecycle.json").read_text(encoding="utf-8"))
        self.assertEqual(lifecycle["issues"][0]["state"], "CLOSED")
        baseline = json.loads((self.reviews / "概要设计.review-baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["accepted_full_request_digest"], output["request"]["request_digest"])
        replayed = self.invoke("gate", "--stage", "概要设计", "--json")
        self.assertEqual(replayed.returncode, 0, replayed.stdout + replayed.stderr)

    def test_crlf_full_review_locator_closes_issue_with_original_section_bytes(self):
        path = self.sdd / "概要设计.md"
        path.write_bytes(FIXTURE["OUTLINE"].replace("\n", "\r\n").encode("utf-8"))
        gated, output = self._claim_fixed_and_return_clean_review(
            "UNSCOPED", include_locator=True)
        self.assertEqual(dict(output["request"]["doc_hashes"]), {
            "概要设计.md": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        lifecycle = json.loads((self.reviews / "概要设计.issue-lifecycle.json").read_text(encoding="utf-8"))
        self.assertEqual(lifecycle["issues"][0]["state"], "CLOSED")


if __name__ == "__main__":
    unittest.main()
