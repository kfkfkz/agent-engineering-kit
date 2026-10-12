#!/usr/bin/env python3
"""Security inputs are bounded data, never authority or a safety verdict."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aek.core.context.identity import build_identity, identity_as_dict


def request_payload(operation="evaluate"):
    identity = build_identity({
        "subject": {"scope_digest": "a" * 64},
        "artifact": {"design_digest": "b" * 64},
        "policy": {"content_digest": "c" * 64},
        "context": {"source_digest": "d" * 64},
        "evidence": {"verification_digest": "e" * 64},
    })
    variants = {
        "evaluate": {key: [] for key in (
            "proposals", "surfaces", "findings", "inventories",
            "run_refs", "invariant_refs")},
        "report": {"record_ref": "evidence/review-a/record.json",
                   "expected_revision_digest": "f" * 64, "format": "json"},
        "doctor": {"tool_ids": ["trivy", "semgrep", "gitleaks"]},
        "plan": {"tool_id": "trivy", "input_refs": ["pom.xml"]},
    }
    return {"schema_version": 1, "operation": operation, "stage": "design",
            "scope": {"mode": "targeted", "subject_digest": "a" * 64,
                      "paths": ["src/app.py"], "focus": ["dependencies"]},
            "identity": identity_as_dict(identity), **variants[operation]}


def finding_payload():
    return {"id": "FIND-1", "category": "PATH_TRAVERSAL", "severity": "HIGH",
            "verification_status": "SUSPECTED", "title": "Untrusted path reaches file access",
            "location_refs": ["LOC-1"], "surface_refs": [], "dependency_ref": None,
            "advisory_refs": [], "evidence_refs": [], "counterevidence_refs": [],
            "preconditions": ["request-controlled path"], "impact": "Read outside allowed root",
            "required_change": "Constrain path to authorized root", "affected_refs": ["REQ-1"],
            "reachability": "UNKNOWN", "coverage_gaps": ["independent review not performed"]}


class SecurityInputTests(unittest.TestCase):
    def test_scope_identity_component_cannot_describe_another_declared_scope(self):
        from aek.core.security.model import decode_security_request
        payload = request_payload()
        payload["scope"]["subject_digest"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
            decode_security_request(json.dumps(payload).encode())

    def test_findings_keep_risk_verification_and_reachability_separate_from_lifecycle(self):
        from aek.core.security.model import decode_security_request

        finding = finding_payload()
        payload = request_payload()
        payload["findings"] = [finding]
        decoded = decode_security_request(json.dumps(payload).encode())
        self.assertEqual(json.loads(decoded.payload_json)["findings"], [finding])
        for change in ({"severity": "major"}, {"severity": True},
                       {"verification_status": "PASS"}, {"reachability": "SAFE"},
                       {"reachability": "NOT_OBSERVED"}, {"lifecycle": "CLOSED"},
                       {"authority": "JUDGE"}, {"gate_status": "READY"},
                       {"evidence_refs": ["EV-1", "EV-1"]}, {"preconditions": "authenticated"},
                       {"dependency_ref": True}, {"category": ""}):
            payload["findings"] = [{**finding, **change}]
            with self.subTest(change=change), self.assertRaisesRegex(
                    ValueError, "^INVALID_SECURITY_INPUT$"):
                decode_security_request(json.dumps(payload).encode())
        payload["findings"] = [finding, finding]
        with self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
            decode_security_request(json.dumps(payload).encode())

    def test_inventory_keeps_instance_scope_and_declared_resolved_shipped_distinct(self):
        from aek.core.security.model import decode_security_request

        package = {"id": "PKG-1", "ecosystem": "maven", "package": "org.example:client",
                   "version": None, "scope": "provided", "relationship": "UNKNOWN",
                   "introduced_by": [], "dependency_path": [], "source_ref": "EV-1"}
        inventory = {"id": "INV-1", "kind": "RESOLVED", "scope_ref": "SCOPE-1",
                     "identity_digest": "a" * 64, "producer_ref": "PRODUCER-1",
                     "packages": [package], "coverage": "PARTIAL", "coverage_gaps": ["private packages unknown"]}
        payload = request_payload()
        for kind in ("PROPOSED", "RESOLVED", "SHIPPED"):
            payload["inventories"] = [{**inventory, "kind": kind}]
            decoded = decode_security_request(json.dumps(payload).encode())
            self.assertEqual(json.loads(decoded.payload_json)["inventories"][0]["kind"], kind)
        for change in ({"kind": "SAFE"}, {"identity_digest": "bad"},
                       {"verified": True}, {"coverage": "COMPLETE"},
                       {"packages": [package, package]},
                       {"packages": [{**package, "relationship": "safe"}]},
                       {"packages": [{**package, "version": True}]},
                       {"packages": [{**package, "dependency_path": "PKG-1"}]}):
            payload["inventories"] = [{**inventory, **change}]
            with self.subTest(change=change), self.assertRaisesRegex(
                    ValueError, "^INVALID_SECURITY_INPUT$"):
                decode_security_request(json.dumps(payload).encode())

    def test_attack_surface_controls_require_explicit_evidence_and_known_coverage(self):
        from aek.core.security.model import decode_security_request

        surface = {"id": "SURF-1", "entry_ref": "ENTRY-1", "actors": ["anonymous"],
                   "controllable_inputs": ["request.path"], "boundaries": ["http-to-file"],
                   "sink_refs": ["SINK-1"], "controls": [
                       {"kind": "path-canonicalization", "status": "UNKNOWN", "evidence_refs": []}],
                   "evidence_refs": [], "coverage": "PARTIAL", "coverage_gaps": ["call path unknown"]}
        payload = request_payload()
        payload["surfaces"] = [surface]
        self.assertEqual(json.loads(decode_security_request(json.dumps(payload).encode()).payload_json)
                         ["surfaces"][0]["controls"][0]["status"], "UNKNOWN")
        for change in ({"coverage": "COMPLETE"}, {"coverage": True},
                       {"authority": "REVIEWER"}, {"actors": ["anonymous", "anonymous"]},
                       {"controls": [{"kind": "filter", "status": "OBSERVED", "evidence_refs": []}]},
                       {"controls": [{"kind": "filter", "status": "ABSENT_WITH_EVIDENCE", "evidence_refs": []}]},
                       {"controls": [{"kind": "filter", "status": "SAFE", "evidence_refs": ["EV-1"]}]}):
            payload["surfaces"] = [{**surface, **change}]
            with self.subTest(change=change), self.assertRaisesRegex(
                    ValueError, "^INVALID_SECURITY_INPUT$"):
                decode_security_request(json.dumps(payload).encode())

    def test_dependency_proposals_preserve_unknown_versions_without_authority(self):
        from aek.core.security.model import decode_security_request

        proposal = {"id": "DEP-1", "ecosystem": "maven", "package": "org.example:client",
                    "version": None, "purpose": "HTTP client", "scope": "production",
                    "design_refs": ["D2"], "evidence_refs": ["EV-1"]}
        payload = request_payload()
        payload["proposals"] = [proposal]
        decoded = decode_security_request(json.dumps(payload).encode())
        self.assertIsNone(json.loads(decoded.payload_json)["proposals"][0]["version"])
        for key, value in (("id", ""), ("package", ""), ("version", True),
                           ("scope", "runtime"), ("evidence_refs", ["EV-1", "EV-1"]),
                           ("design_refs", "D2"), ("approved_by", "k")):
            payload["proposals"] = [{**proposal, key: value}]
            with self.subTest(key=key), self.assertRaisesRegex(
                    ValueError, "^INVALID_SECURITY_INPUT$"):
                decode_security_request(json.dumps(payload).encode())
        payload["proposals"] = [proposal, proposal]
        with self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
            decode_security_request(json.dumps(payload).encode())

    def test_scope_and_report_selectors_reject_escape_duplicates_and_bad_digests(self):
        from aek.core.security.model import decode_security_request

        for path in ("../outside", "/absolute", "a/../outside", "a/./b",
                     "a//b", "a\\b", "C:/outside", "file:stream", "a\x00b", "",
                     "a/.. /outside", "a/target. ", "a/target."):
            for operation in ("evaluate", "report"):
                payload = request_payload(operation)
                if operation == "report":
                    payload["record_ref"] = path
                else:
                    payload["scope"]["paths"] = [path]
                with self.subTest(path=path, operation=operation), \
                        self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
                    decode_security_request(json.dumps(payload).encode())
        for key, value in (("mode", "all"), ("subject_digest", "bad"),
                           ("paths", ["a", "a"]), ("paths", "a"),
                           ("focus", ["dependencies", "dependencies"]),
                           ("focus", [True]), ("extra", True)):
            payload = request_payload()
            payload["scope"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(
                    ValueError, "^INVALID_SECURITY_INPUT$"):
                decode_security_request(json.dumps(payload).encode())
        payload = request_payload("report")
        payload["expected_revision_digest"] = "bad"
        with self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
            decode_security_request(json.dumps(payload).encode())

    def test_request_is_a_closed_readonly_operation_union_with_legacy_identity(self):
        from aek.core.security.model import decode_security_request

        for operation in ("evaluate", "report", "doctor", "plan"):
            payload = request_payload(operation)
            decoded = decode_security_request(json.dumps(payload).encode())
            self.assertEqual(decoded.operation, operation)
            self.assertEqual(decoded.stage, "design")
            self.assertEqual(decoded.identity.envelope_digest,
                             payload["identity"]["envelope_digest"])
            for extra in ("authority", "approved_by", "lifecycle", "command"):
                bad = {**payload, extra: "private-canary"}
                with self.subTest(operation=operation, extra=extra), \
                        self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
                    decode_security_request(json.dumps(bad).encode())
        bad_variants = [
            {**request_payload(), "operation": "execute"},
            {**request_payload(), "schema_version": True},
            {**request_payload(), "schema_version": 1.0},
            {**request_payload(), "schema_version": 2},
            {**request_payload(), "stage": "unknown"},
            {**request_payload("report"), "findings": []},
            {**request_payload("report"), "format": "html"},
            {**request_payload("doctor"), "tool_ids": ["strix"]},
            {**request_payload("plan"), "tool_id": "sh"},
        ]
        missing = request_payload()
        del missing["run_refs"]
        bad_variants.append(missing)
        for payload in bad_variants:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                    ValueError, "^INVALID_SECURITY_INPUT$"):
                decode_security_request(json.dumps(payload).encode())

    def test_duplicate_json_keys_are_rejected_even_inside_nested_evidence(self):
        from aek.core.security.model import decode_security_json

        for raw in (
            b'{"schema_version":1,"schema_version":2}',
            b'{"sources":[{"status":"DECLARED","status":"VERIFIED"}]}',
            b'{"identity":{"digest":"first","digest":"second"}}',
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    ValueError, "^INVALID_SECURITY_INPUT$"):
                decode_security_json(raw)

    def test_input_budgets_reject_instead_of_silently_truncating(self):
        from aek.core.security.model import decode_security_json

        at_limit = b'{}' + b' ' * 1_999_998
        self.assertEqual(decode_security_json(at_limit), {})
        self.assertEqual(len(decode_security_json(json.dumps([""] * 10_000).encode())),
                         10_000)
        self.assertEqual(decode_security_json(json.dumps("x" * 2048).encode()),
                         "x" * 2048)
        for raw in (at_limit + b' ', json.dumps([""] * 10_001).encode(),
                    json.dumps({"a": [""] * 5000, "b": [""] * 5001}).encode(),
                    json.dumps("x" * 2049).encode(), b'[' * 2000 + b']' * 2000,
                    "{}", None):
            with self.subTest(kind=type(raw).__name__), self.assertRaisesRegex(
                    ValueError, "^INVALID_SECURITY_INPUT$"):
                decode_security_json(raw)

    def test_bad_encoding_syntax_and_nonfinite_numbers_have_only_a_safe_reason(self):
        from aek.core.security.model import decode_security_json

        for raw in (b'{"secret":"private-canary",}', b'\xff',
                    b'{"secret":NaN}', b'{"secret":Infinity}',
                    b'{"secret":-Infinity}', b'{"secret":1e999}',
                    b'{"secret":"\\ud800"}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    ValueError, "^INVALID_SECURITY_INPUT$"):
                decode_security_json(raw)


if __name__ == "__main__":
    unittest.main()
