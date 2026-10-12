#!/usr/bin/env python3
"""The actual read-only entry point cannot treat author data as verified proof."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from aek.core.context.identity import build_identity, decode_identity

spec = importlib.util.spec_from_file_location("security_test_data", REPO / "tests/security-model.test.py")
test_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(test_data)


class SecurityCliTests(unittest.TestCase):
    def test_configured_plan_is_read_only_identity_bound_and_does_not_probe_or_execute(self):
        from aek.adapters.security_scanner import ScannerAdapter
        from aek.application.security_review import SecurityReviewService
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.py").write_text("value = 1\n", encoding="utf-8")
            (root / "scanner.json").write_text("{}", encoding="utf-8")
            script = root / "scanner.py"
            script.write_text("raise RuntimeError('must-not-execute')\n", encoding="utf-8")
            adapter = ScannerAdapter(root, "trivy", (str(Path(sys.executable).resolve()), str(script)))
            service = SecurityReviewService(scanner_ports={"trivy": adapter},
                scanner_options={"trivy": {"configuration_path": "scanner.json"}})
            payload = test_data.request_payload("plan")
            payload["tool_id"] = "trivy"
            payload["scope"]["paths"] = ["example.py"]
            payload["input_refs"] = ["example.py"]
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            current = decode_identity(payload["identity"])
            result = service.plan(json.dumps(payload).encode(), current_identity=current,
                                  required_components=("subject.scope_digest",))
            self.assertEqual(result["status"], "COMPLETE")
            self.assertEqual(result["plan"]["identity_digest"], current.envelope_digest)
            self.assertEqual(result["plan"]["tool_version"], "UNKNOWN")
            self.assertEqual(set(result["plan"]["required_boundaries"]), {"network", "filesystem"})
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
            self.assertNotIn("authorized", result)
            payload["input_refs"].append("scanner.py")
            with self.assertRaises(ValueError):
                service.plan(json.dumps(payload).encode(), current_identity=current,
                             required_components=("subject.scope_digest",))

    def test_doctor_reports_unknown_versions_without_starting_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = root / "trivy"
            tool.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            tool.chmod(0o700)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            result = subprocess.run([sys.executable, str(REPO / "security-check")],
                input=json.dumps(test_data.request_payload("doctor")).encode(), cwd=root,
                env={**os.environ, "PATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            response = json.loads(result.stdout)
            self.assertEqual(response["status"], "COMPLETE")
            self.assertEqual({cap["tool_id"] for cap in response["capabilities"]}, {"trivy", "semgrep", "gitleaks"})
            for cap in response["capabilities"]:
                self.assertEqual(cap["version"], "UNKNOWN")
                self.assertNotEqual(cap["authorized"], True)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
            self.assertEqual(result.stderr, b"")

    def test_real_report_cli_reads_bound_revision_and_outputs_utf8_under_ascii_locale(self):
        from aek.adapters.security_store import SecurityStore
        from aek.core.security.record import new_security_record

        payload = test_data.request_payload()
        payload["findings"] = [{**test_data.finding_payload(), "title": "外部路径需要核验"}]
        record = new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence" / "REVIEW-A").mkdir(parents=True)
            store = SecurityStore(root, "evidence/REVIEW-A/record.json")
            store.create(record)
            request = test_data.request_payload("report")
            request["record_ref"] = "evidence/REVIEW-A/record.json"
            request["expected_revision_digest"] = record.revision_digest
            request["format"] = "md"
            result = subprocess.run([sys.executable, str(REPO / "security-check")],
                                    input=json.dumps(request).encode(), cwd=root,
                                    env={**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONDONTWRITEBYTECODE": "1"},
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stderr, b"")
            response = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual(response["record_digest"], record.revision_digest)
            self.assertIn("外部路径需要核验", response["report"])
            self.assertIn("SOURCE_PROVENANCE_NOT_VERIFIED", response["reason_codes"])
            self.assertNotIn("verdict", response)
            self.assertEqual(store.load(), record)

    def test_current_identity_is_independent_and_missing_components_differ_from_stale(self):
        from aek.application.security_review import SecurityReviewService

        payload = test_data.request_payload()
        declared = decode_identity(payload["identity"])
        raw = json.dumps(payload).encode()
        service = SecurityReviewService()
        valid = service.evaluate(raw, current_identity=declared,
                                 required_components=("subject.scope_digest",))
        self.assertEqual(valid["current_identity_digest"], declared.envelope_digest)
        self.assertEqual(valid["status"], "INCOMPLETE")
        self.assertNotIn("CURRENT_IDENTITY_UNAVAILABLE", valid["reason_codes"])
        self.assertIn("SOURCE_EVIDENCE_UNAVAILABLE", valid["reason_codes"])
        self.assertNotIn("CURRENT_IDENTITY_REQUIRED", valid["required_evidence"])
        self.assertIn("SOURCE_EVIDENCE_REQUIRED", valid["required_evidence"])

        components = payload["identity"]["components"]
        components["policy"]["content_digest"] = "1" * 64
        policy_changed = build_identity(components)
        selective = service.evaluate(raw, current_identity=policy_changed,
                                     required_components=("subject.scope_digest",))
        self.assertNotIn("EVIDENCE_STALE", selective["reason_codes"])
        self.assertEqual(selective["current_identity_digest"], policy_changed.envelope_digest)
        stale = service.evaluate(raw, current_identity=policy_changed,
                                 required_components=("policy.content_digest",))
        self.assertIn("EVIDENCE_STALE", stale["reason_codes"])
        self.assertEqual(stale["status"], "INCOMPLETE")
        with_proposal = json.loads(raw)
        with_proposal["proposals"] = [{
            "id": "DEP-1", "ecosystem": "maven", "package": "org.example:client",
            "version": None, "purpose": "HTTP client", "scope": "production",
            "design_refs": ["D2"], "evidence_refs": []}]
        combined = service.evaluate(json.dumps(with_proposal).encode(),
                                    current_identity=policy_changed,
                                    required_components=("policy.content_digest",))
        self.assertIn("EVIDENCE_STALE", combined["reason_codes"])
        self.assertIn("VERSION_UNKNOWN", combined["dependency_decisions"][0]["reason_codes"])
        self.assertNotIn("VERSION_UNKNOWN", combined["reason_codes"])
        missing = service.evaluate(raw, current_identity=policy_changed,
                                   required_components=("subject.code_digest",))
        self.assertIn("IDENTITY_INVALID", missing["reason_codes"])
        self.assertNotIn("EVIDENCE_STALE", missing["reason_codes"])
        self.assertEqual(missing["status"], "INCOMPLETE")

    def test_real_cli_rejects_bad_input_and_execution_flags_without_leaking_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "aek").mkdir()
            (root / "aek/__init__.py").write_text("raise RuntimeError('foreign-package-canary')", encoding="utf-8")
            bad = {**test_data.request_payload(), "authority": "private-canary"}
            inputs = ((json.dumps(bad).encode(), []),
                      (b'{"private-canary":NaN}', []),
                      (b'{}' + b' ' * 2_000_000, []),
                      (json.dumps(test_data.request_payload()).encode(), ["--authority", "JUDGE"]),
                      (json.dumps(test_data.request_payload()).encode(), ["--close"]))
            for raw, args in inputs:
                with self.subTest(args=args, size=len(raw)):
                    result = subprocess.run(
                        [sys.executable, str(REPO / "security-check"), *args],
                        input=raw, cwd=root,
                        env={**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                    )
                    self.assertEqual(result.returncode, 3)
                    self.assertEqual(json.loads(result.stdout), {
                        "schema_version": 1, "status": "REJECTED", "reason_codes": ["INVALID_SECURITY_INPUT"]})
                    self.assertEqual(result.stderr, b"")
                    self.assertNotIn(b"private-canary", result.stdout)
                    self.assertNotIn(b"foreign-package-canary", result.stdout)

    def test_declared_findings_are_preserved_but_self_claimed_verification_is_not_trusted(self):
        from aek.application.security_review import SecurityReviewService

        payload = test_data.request_payload()
        finding = {**test_data.finding_payload(), "verification_status": "STATIC_VERIFIED"}
        payload["findings"] = [finding]
        payload["proposals"] = [{"id": "DEP-1", "ecosystem": "maven", "package": "org.example:client",
                                 "version": None, "purpose": "HTTP client", "scope": "production",
                                 "design_refs": ["D2"], "evidence_refs": []}]
        response = SecurityReviewService().evaluate(json.dumps(payload).encode())
        self.assertEqual(response["status"], "INCOMPLETE")
        self.assertEqual(len(response["findings"]), 1)
        self.assertEqual(response["findings"][0]["id"], "FIND-1")
        self.assertEqual(response["findings"][0]["severity"], "HIGH")
        self.assertEqual(response["findings"][0]["verification_status"], "INCONCLUSIVE")
        self.assertNotIn("lifecycle", response["findings"][0])
        self.assertEqual(response["dependency_decisions"][0]["dependency_ref"], "DEP-1")
        self.assertEqual(response["dependency_decisions"][0]["status"], "PENDING")
        self.assertIn("VERSION_UNKNOWN", response["dependency_decisions"][0]["reason_codes"])
        self.assertEqual(payload["findings"][0]["verification_status"], "STATIC_VERIFIED")

    def test_empty_author_input_is_incomplete_and_does_not_write_or_claim_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "preserve.txt").write_bytes(b"user data")
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            result = subprocess.run(
                [sys.executable, str(REPO / "security-check")],
                input=json.dumps(test_data.request_payload()).encode(),
                cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            )
            self.assertEqual(result.returncode, 2, result.stderr.decode())
            self.assertEqual(result.stderr, b"")
            response = json.loads(result.stdout)
            self.assertEqual(response["operation"], "evaluate")
            self.assertEqual(response["status"], "INCOMPLETE")
            self.assertIsNone(response["current_identity_digest"])
            self.assertIsNone(response["record_digest"])
            self.assertIn("CURRENT_IDENTITY_UNAVAILABLE", response["reason_codes"])
            self.assertIn("SCOPE_EVIDENCE_REQUIRED", response["required_evidence"])
            self.assertNotIn("verdict", response)
            self.assertEqual(result.stderr, b"")
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})


if __name__ == "__main__":
    unittest.main()
