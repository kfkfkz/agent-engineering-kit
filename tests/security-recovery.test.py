#!/usr/bin/env python3
"""Real security carrier snapshots, CAS and restart behavior."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
spec = importlib.util.spec_from_file_location("security_test_data", REPO / "tests/security-model.test.py")
test_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(test_data)


class SecurityRecordTests(unittest.TestCase):
    def test_record_scope_and_lifecycle_scope_cannot_diverge_even_when_resealed(self):
        from aek.core.review.lifecycle import initialize_lifecycle, lifecycle_bytes
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        record = new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A")
        body = record_as_dict(record)
        other = initialize_lifecycle(stage="security", subject_digest="b" * 64, issues=(),
                                     request_digest="c" * 64, doc_hashes={"security-record.json": "d" * 64},
                                     iteration=1, max_iterations=3)
        body["lifecycle"] = json.loads(lifecycle_bytes(other))
        with self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
            seal_security_record(body)

    def test_candidate_record_keeps_dependency_proposals_for_restart_but_does_not_approve_them(self):
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        payload = test_data.request_payload()
        proposal = {"id": "DEP-1", "ecosystem": "maven", "package": "org.example:client",
                    "version": None, "purpose": "HTTP client", "scope": "production",
                    "design_refs": ["D2"], "evidence_refs": []}
        payload["proposals"] = [proposal]
        body = record_as_dict(new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A"))
        self.assertEqual(body["dependency_decisions"][0]["proposal"], proposal)
        self.assertEqual(body["dependency_decisions"][0]["status"], "PENDING")
        self.assertIn("VERSION_UNKNOWN", body["dependency_decisions"][0]["reason_codes"])
        for change in ({"status": "PASS"}, {"authority": "REVIEWER"}, {"dependency_ref": "another"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                seal_security_record({**body, "dependency_decisions": [{**body["dependency_decisions"][0], **change}]})

    def test_report_output_limit_is_explicit_not_a_truncated_complete_view(self):
        from aek.adapters.security_store import SecurityStore
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record

        base = record_as_dict(new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A"))
        base["coverage_gaps"] = ["x" * 2035 + f"{index:04}" for index in range(500)]
        record = seal_security_record(base)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence" / "REVIEW-A").mkdir(parents=True)
            store = SecurityStore(root, "evidence/REVIEW-A/record.json")
            store.create(record)
            request = test_data.request_payload("report")
            request["record_ref"] = "evidence/REVIEW-A/record.json"
            request["expected_revision_digest"] = record.revision_digest
            response = SecurityReviewService(record_port=store).report(json.dumps(request).encode())
            self.assertEqual(response["status"], "INCOMPLETE")
            self.assertIn("OUTPUT_LIMIT", response["reason_codes"])
            self.assertNotIn("report", response)
            self.assertLess(len(json.dumps(response).encode()), 2_000_000)
            self.assertEqual(store.load(), record)

    def test_carrier_rejects_parent_escape_and_does_not_read_another_record(self):
        from aek.adapters.security_store import SecurityStore
        from aek.core.security.record import new_security_record

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for ref in ("record.json", "../outside/record.json", "/absolute/record.json", "evidence/REVIEW-A/other.json"):
                with self.subTest(ref=ref), self.assertRaises(ValueError):
                    SecurityStore(root, ref)
            carrier = root / "evidence" / "REVIEW-A"
            carrier.mkdir(parents=True)
            store = SecurityStore(root, "evidence/REVIEW-A/record.json")
            wrong = new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-B")
            with self.assertRaises(ValueError):
                store.create(wrong)
            self.assertFalse((carrier / "record.json").exists())
            with self.assertRaises(ValueError):
                store.read("evidence/REVIEW-B/record.json")

    def test_only_one_real_process_can_commit_the_same_observed_revision(self):
        from aek.adapters.security_store import SecurityStore
        from aek.core.security.record import new_security_record, record_as_dict, record_bytes, seal_security_record

        first = new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A")
        program = """import sys
from pathlib import Path
from aek.adapters.security_store import SecurityStore, SecurityRevisionConflict
from aek.core.security.record import load_security_record
record = load_security_record(sys.stdin.buffer.read())
try:
    SecurityStore(Path(sys.argv[1]), 'evidence/REVIEW-A/record.json').write(record, expected_revision_digest=sys.argv[2])
except SecurityRevisionConflict:
    raise SystemExit(2)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence" / "REVIEW-A").mkdir(parents=True)
            store = SecurityStore(root, "evidence/REVIEW-A/record.json")
            store.create(first)
            processes = []
            for actor in ("ACTOR-A", "ACTOR-B"):
                changed = record_as_dict(first)
                changed["revision"] = 2
                changed["events"] = [{"event_id": actor, "operation": "update", "input_digest": "a" * 64,
                                      "current_identity_digest": changed["identity"]["envelope_digest"],
                                      "trusted_actor_ref": actor, "evidence_refs": [], "occurred_at": "2026-10-10T07:00:00Z"}]
                process = subprocess.Popen([sys.executable, "-c", program, str(root), first.revision_digest],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"})
                process.stdin.write(record_bytes(seal_security_record(changed)))
                process.stdin.close()
                process.stdin = None
                processes.append(process)
            for process in processes:
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(stderr, b"")
            self.assertEqual(sorted(process.returncode for process in processes), [0, 2])
            persisted = record_as_dict(store.load())
            self.assertEqual(persisted["revision"], 2)
            self.assertEqual(len(persisted["events"]), 1)
            self.assertIn(persisted["events"][0]["event_id"], {"ACTOR-A", "ACTOR-B"})

    def test_gate_binding_does_not_accept_bare_pass_or_unstructured_judge_claims(self):
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record

        base = record_as_dict(new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A"))
        for binding in (True, {"status": "PASS"}, {"verdict": "READY", "authority": "JUDGE"},
                        {"gate_ref": "gate.json", "gate_digest": "a" * 64}):
            with self.subTest(binding=binding), self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
                seal_security_record({**base, "gate_binding": binding})

    def test_resolution_schema_does_not_confuse_disposition_with_a_fix(self):
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record

        base = record_as_dict(new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A"))
        resolution = {"finding_fingerprint": "a" * 64, "kind": "NONE", "proof_ref": None,
                      "decision_ref": None, "reason": "Not yet disposed", "scope_ref": "SCOPE-1",
                      "expires_at": None, "required_components": [],
                      "identity_digest": base["identity"]["envelope_digest"],
                      "trusted_actor_ref": "ACTOR-1", "event_id": "EVENT-1"}
        self.assertEqual(record_as_dict(seal_security_record({**base, "resolutions": [resolution]}))["resolutions"], [resolution])
        for change in ({"kind": "FIXED"}, {"kind": "ACCEPTED_RISK"}, {"kind": "FALSE_POSITIVE"},
                       {"proof_ref": "EV-1"}, {"expires_at": "not-a-date"}, {"authority": "JUDGE"},
                       {"identity_digest": "bad"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
                seal_security_record({**base, "resolutions": [{**resolution, **change}]})
        with self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
            seal_security_record({**base, "resolutions": [resolution, resolution]})

    def test_invariants_need_sources_to_be_confirmed_and_cannot_supply_authority(self):
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record

        base = record_as_dict(new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A"))
        invariant = {"id": "INV-1", "statement": "Authorized users cannot read other tenants",
                     "source_refs": [], "entry_refs": ["ENTRY-1"], "test_refs": [],
                     "status": "CANDIDATE", "evidence_refs": []}
        self.assertEqual(record_as_dict(seal_security_record({**base, "invariants": [invariant]}))["invariants"], [invariant])
        for change in ({"status": "CONFIRMED"}, {"status": "PASS"}, {"authority": "USER"},
                       {"statement": ""}, {"source_refs": "D1"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
                seal_security_record({**base, "invariants": [{**invariant, **change}]})

    def test_runs_cannot_claim_pass_without_timestamps_or_evidence(self):
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record

        base = record_as_dict(new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A"))
        run = {"id": "RUN-1", "plan_digest": "a" * 64, "identity_digest": base["identity"]["envelope_digest"],
               "producer_ref": "PRODUCER-1", "tool_version": None, "configuration_digest": "b" * 64,
               "scope_ref": "SCOPE-1", "status": "NOT_RUN", "evidence_refs": [], "coverage_gaps": [],
               "started_at": None, "finished_at": None, "reason_codes": ["AUTHORIZATION_REQUIRED"]}
        self.assertEqual(record_as_dict(seal_security_record({**base, "runs": [run]}))["runs"], [run])
        for change in ({"status": "PASS"}, {"status": "PASSED"}, {"exit_code": 0},
                       {"plan_digest": "bad"}, {"finished_at": "2026-10-10T07:00:00Z"},
                       {"status": "PASSED", "evidence_refs": ["EV-1"],
                        "started_at": "2026-10-10T08:00:00Z", "finished_at": "2026-10-10T07:00:00Z"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
                seal_security_record({**base, "runs": [{**run, **change}]})

    def test_source_metadata_is_strict_and_a_reseal_does_not_justify_false_schema(self):
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record

        base = record_as_dict(new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A"))
        source = {"id": "SOURCE-1", "ref": "evidence/advisory.json", "run_id": None,
                  "producer_ref": "PRODUCER-1", "identity": base["identity"],
                  "required_components": ["subject.scope_digest"], "kind": "advisory", "status": "DECLARED",
                  "checked_at": "2026-10-10T07:00:00Z", "scope_ref": "SCOPE-1", "tool_version": None,
                  "rules_digest": None, "database_digest": None, "coverage": "UNKNOWN",
                  "coverage_gaps": ["provenance unknown"], "summary": "Declared advisory"}
        self.assertEqual(record_as_dict(seal_security_record({**base, "sources": [source]}))["sources"], [source])
        for change in ({"status": "PASS"}, {"Secret": "private-canary"}, {"coverage": "COMPLETE"},
                       {"database_digest": "bad"}, {"checked_at": "2026-10-10"},
                       {"required_components": ["subject.missing"]}, {"required_components": []}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
                seal_security_record({**base, "sources": [{**source, **change}]})
        with self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
            seal_security_record({**base, "sources": [source, source]})

    def test_reports_select_one_record_and_revision_and_never_replace_it_with_latest(self):
        from aek.adapters.security_store import SecurityStore
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record

        first = new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            carrier = root / "evidence" / "REVIEW-A"
            carrier.mkdir(parents=True)
            store = SecurityStore(root, "evidence/REVIEW-A/record.json")
            store.create(first)
            service = SecurityReviewService(record_port=store)
            request = test_data.request_payload("report")
            request["record_ref"] = "evidence/REVIEW-A/record.json"
            request["expected_revision_digest"] = first.revision_digest
            json_report = service.report(json.dumps(request).encode())
            self.assertEqual(json_report["status"], "COMPLETE")
            self.assertEqual(json_report["record_digest"], first.revision_digest)
            self.assertEqual(json_report["report"], record_as_dict(first))
            request["format"] = "md"
            md_report = service.report(json.dumps(request).encode())
            self.assertEqual(md_report["record_digest"], first.revision_digest)
            self.assertIn(first.revision_digest, md_report["report"])
            self.assertIn("SOURCE_EVIDENCE_UNAVAILABLE", md_report["report"])
            json_report["report"]["coverage_gaps"] = []
            self.assertEqual(store.load(), first)
            changed = record_as_dict(first)
            changed["revision"] = 2
            changed["events"] = [{"event_id": "EVENT-1", "operation": "update", "input_digest": "a" * 64,
                                  "current_identity_digest": changed["identity"]["envelope_digest"],
                                  "trusted_actor_ref": "ACTOR-1", "evidence_refs": [], "occurred_at": "2026-10-10T07:00:00Z"}]
            second = seal_security_record(changed)
            store.write(second, expected_revision_digest=first.revision_digest)
            stale = service.report(json.dumps(request).encode())
            self.assertEqual(stale["status"], "INCOMPLETE")
            self.assertIn("SECURITY_REVISION_CONFLICT", stale["reason_codes"])
            self.assertNotIn("report", stale)
            request["expected_revision_digest"] = second.revision_digest
            self.assertEqual(service.report(json.dumps(request).encode())["status"], "COMPLETE")

    def test_resealed_events_still_require_unique_ids_valid_digests_and_timezone(self):
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record

        base = record_as_dict(new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A"))
        event = {"event_id": "EVENT-1", "operation": "update", "input_digest": "a" * 64,
                 "current_identity_digest": base["identity"]["envelope_digest"],
                 "trusted_actor_ref": "ACTOR-1", "evidence_refs": [], "occurred_at": "2026-10-10T07:00:00Z"}
        for events in ([event, event], [{**event, "input_digest": "bad"}],
                       [{**event, "occurred_at": "2026-10-10T07:00:00"}],
                       [{**event, "occurred_at": True}], [{**event, "authority": "JUDGE"}],
                       [{**event, "evidence_refs": ["EV-1", "EV-1"]}]):
            with self.subTest(events=events), self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
                seal_security_record({**base, "events": events})

    def test_real_store_cas_preserves_history_and_rejects_old_writers_after_restart(self):
        from aek.adapters.security_store import SecurityStore, SecurityRevisionConflict
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record

        first = new_security_record(json.dumps(test_data.request_payload()).encode(), review_id="REVIEW-A")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            carrier = root / "evidence" / "REVIEW-A"
            carrier.mkdir(parents=True)
            store = SecurityStore(root, "evidence/REVIEW-A/record.json")
            self.assertIsNone(store.load())
            store.create(first)
            self.assertEqual(SecurityStore(root, "evidence/REVIEW-A/record.json").load(), first)
            changed = record_as_dict(first)
            changed["revision"] = 2
            changed["coverage_gaps"].append("ADVISORY_NOT_CHECKED")
            changed["events"].append({
                "event_id": "EVENT-1", "operation": "update", "input_digest": "a" * 64,
                "current_identity_digest": changed["identity"]["envelope_digest"],
                "trusted_actor_ref": "ACTOR-1", "evidence_refs": [], "occurred_at": "2026-10-10T07:00:00Z"})
            second = seal_security_record(changed)
            store.write(second, expected_revision_digest=first.revision_digest)
            with self.assertRaisesRegex(SecurityRevisionConflict, "SECURITY_REVISION_CONFLICT"):
                store.write(second, expected_revision_digest=first.revision_digest)
            persisted = SecurityStore(root, "evidence/REVIEW-A/record.json").load()
            self.assertEqual(persisted, second)
            self.assertEqual(record_as_dict(persisted)["events"][0]["event_id"], "EVENT-1")
            self.assertEqual({path.name for path in carrier.iterdir()}, {"record.json", "record.lock"})

    def test_candidate_record_starts_open_and_cannot_self_verify_or_hide_findings(self):
        from aek.core.security.record import new_security_record, record_as_dict

        payload = test_data.request_payload()
        payload["findings"] = [{**test_data.finding_payload(), "verification_status": "STATIC_VERIFIED"}]
        first = record_as_dict(new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A"))
        self.assertEqual(first["findings"][0]["verification_status"], "INCONCLUSIVE")
        self.assertEqual(len(first["lifecycle"]["issues"]), 1)
        self.assertEqual(first["lifecycle"]["issues"][0]["state"], "OPEN")
        self.assertEqual(first["sources"], [])
        self.assertIsNone(first["gate_binding"])
        payload["findings"][0]["title"] = "Reworded title"
        second = record_as_dict(new_security_record(json.dumps(payload).encode(), review_id="REVIEW-A"))
        self.assertEqual(first["lifecycle"]["issues"][0]["fingerprint"],
                         second["lifecycle"]["issues"][0]["fingerprint"])

    def test_record_roundtrip_keeps_the_full_contract_and_rejects_content_tampering(self):
        from aek.core.security.record import (
            new_security_record, record_as_dict, record_bytes, load_security_record,
        )

        raw = json.dumps(test_data.request_payload()).encode()
        record = new_security_record(raw, review_id="REVIEW-A")
        payload = record_as_dict(record)
        self.assertEqual(set(payload), {
            "schema_version", "review_id", "revision", "revision_digest", "stage", "scope", "identity",
            "surfaces", "findings", "dependency_decisions", "inventories", "sources", "resolutions",
            "runs", "invariants", "events", "lifecycle", "gate_binding", "coverage_gaps",
            "required_evidence", "residual_risk"})
        self.assertEqual(payload["review_id"], "REVIEW-A")
        self.assertEqual(payload["revision"], 1)
        self.assertIsNone(payload["gate_binding"])
        self.assertEqual(payload["resolutions"], [])
        self.assertEqual(load_security_record(record_bytes(record)), record)
        for key, value in (("review_id", "REVIEW-B"), ("revision", True),
                           ("revision", 1.0), ("revision", 0), ("schema_version", True),
                           ("coverage_gaps", []), ("unknown", "private-canary")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "^INVALID_SECURITY_INPUT$"):
                load_security_record(json.dumps({**payload, key: value}).encode())


if __name__ == "__main__":
    unittest.main()
