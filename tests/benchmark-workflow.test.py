"""Workflow advice requires semantic identities; CLI text is not intent evidence."""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmark"))
from analyzers.workflow import WorkflowEvent, analyze_workflow  # noqa: E402


class WorkflowAnalysisTests(unittest.TestCase):
    def event(self, identifier, kind="memory.retrieve", *, time=1.0, data=()):
        return WorkflowEvent(identifier, "RUN-1", "PAIR-1", "TASK-1", kind, "review", "review_required",
                             "a" * 64, "b" * 64, time, "COMPLETE", data)

    def analyze(self, events, **overrides):
        kwargs = {"trusted_event_digests": frozenset(event.digest for event in events), "complete_capture": True}
        kwargs.update(overrides)
        return analyze_workflow(events, **kwargs)

    def test_same_memory_retrieval_requires_same_material_code_purpose_and_window(self):
        first = self.event("EVENT-1")
        second = self.event("EVENT-2", time=2.0)
        report = self.analyze((first, second))
        self.assertEqual(report["rules"]["WA-001"]["status"], "FINDINGS")
        self.assertEqual(report["findings"][0]["evidence_events"], ["EVENT-1", "EVENT-2"])
        for changed in (replace(second, code_sha256="c" * 64), replace(second, material_sha256="c" * 64),
                        replace(second, purpose="design_required"), replace(second, timestamp_seconds=1000.0)):
            self.assertEqual(self.analyze((first, changed))["rules"]["WA-001"]["status"], "NO_FINDING")

    def test_graph_refresh_requires_unchanged_generation_and_code(self):
        first = self.event("EVENT-1", "codebase.refresh", data=(("generation_sha256", "c" * 64),))
        second = replace(first, event_id="EVENT-2", timestamp_seconds=2.0)
        self.assertEqual(self.analyze((first, second))["rules"]["WA-002"]["status"], "FINDINGS")
        changed = replace(second, data=(("generation_sha256", "d" * 64),))
        self.assertEqual(self.analyze((first, changed))["rules"]["WA-002"]["status"], "NO_FINDING")

    def test_review_and_gate_repeats_exempt_changed_material_scope_or_reason(self):
        for kind, rule, data in (
            ("review.completed", "WA-004", (("review_scope_sha256", "c" * 64), ("reviewer_sha256", "d" * 64))),
            ("gate.failed", "WA-005", (("failure_sha256", "c" * 64),)),
        ):
            with self.subTest(rule=rule):
                first = self.event("EVENT-1", kind, data=data)
                second = replace(first, event_id="EVENT-2", timestamp_seconds=2.0)
                self.assertEqual(self.analyze((first, second))["rules"][rule]["status"], "FINDINGS")
                changed = replace(second, material_sha256="e" * 64)
                self.assertEqual(self.analyze((first, changed))["rules"][rule]["status"], "NO_FINDING")
                changed = replace(second, data=tuple((key, "f" * 64) for key, _ in data))
                self.assertEqual(self.analyze((first, changed))["rules"][rule]["status"], "NO_FINDING")

    def test_direct_full_design_exempts_governance_required_or_user_override(self):
        event = self.event("EVENT-1", "design.generated", data=(("route", "direct"),
                           ("artifact", "full_sdd"), ("requirement", "skipped"), ("user_override", False)))
        self.assertEqual(self.analyze((event,))["rules"]["WA-003"]["status"], "FINDINGS")
        for override in ({"requirement": "required"}, {"user_override": True}, {"route": "standard"}):
            values = dict(event.data)
            values.update(override)
            changed = replace(event, data=tuple(values.items()))
            self.assertEqual(self.analyze((changed,))["rules"]["WA-003"]["status"], "NO_FINDING")

    def test_recovery_reexecution_requires_valid_prior_commit_evidence(self):
        event = self.event("EVENT-1", "stage.execute", data=(("recovered", True),
                           ("prior_commit", "COMMITTED"), ("prior_identity", "VALID")))
        self.assertEqual(self.analyze((event,))["rules"]["WA-006"]["status"], "FINDINGS")
        for override in ({"recovered": False}, {"prior_identity": "STALE"}, {"prior_commit": "NONE"}):
            data = dict(event.data)
            data.update(override)
            self.assertEqual(self.analyze((replace(event, data=tuple(data.items())),))["rules"]["WA-006"]["status"],
                             "NO_FINDING")

    def test_hidden_assertion_rule_requires_independent_hidden_isolation_not_a_label(self):
        public = self.event("EVENT-p", "verification.result", data=(("verification_scope", "public"),
                            ("verification_status", "PASS"), ("verification_suite_sha256", "c" * 64)))
        hidden = replace(public, event_id="EVENT-h", timestamp_seconds=2.0,
                         data=(("verification_scope", "isolated_hidden"), ("verification_status", "FAIL"),
                               ("verification_suite_sha256", "c" * 64), ("isolation_sha256", "d" * 64)))
        self.assertEqual(self.analyze((public, hidden))["rules"]["WA-007"]["status"], "NOT_EVALUATED")
        report = self.analyze((public, hidden), trusted_hidden_isolation_digests=frozenset({"d" * 64}))
        self.assertEqual(report["rules"]["WA-007"]["status"], "FINDINGS")
        unchanged = replace(hidden, data=tuple((key, "PASS" if key == "verification_status" else value)
                                               for key, value in hidden.data))
        report = self.analyze((public, unchanged), trusted_hidden_isolation_digests=frozenset({"d" * 64}))
        self.assertEqual(report["rules"]["WA-007"]["status"], "NO_FINDING")

    def test_missing_or_replayed_evidence_and_partial_capture_abstain(self):
        first, second = self.event("EVENT-1"), self.event("EVENT-2", time=2.0)
        for events, overrides in (
            ((first, second), {"complete_capture": False}),
            ((first, second), {"trusted_event_digests": frozenset()}),
            ((first, replace(second, coverage="PARTIAL")), {}),
            ((first, replace(second, stage=None)), {}),
            ((first, first), {}),
        ):
            with self.subTest(overrides=overrides):
                report = self.analyze(events, **overrides)
                self.assertEqual(report["rules"]["WA-001"]["status"], "NOT_EVALUATED")
                self.assertEqual(report["findings"], [])

    def test_cli_envelopes_without_semantics_do_not_guess_from_tool_names(self):
        from core.telemetry import NormalizedEvent
        event = NormalizedEvent(1, "a" * 64, "RUN-1", "PAIR-1", "TASK-1", "codex.jsonl", "item.completed",
                                "b" * 64, "command_execution")
        report = analyze_workflow((event,), complete_capture=True)
        self.assertTrue(all(rule["status"] == "NOT_EVALUATED" for rule in report["rules"].values()))
        self.assertEqual(report["findings"], [])

    def test_details_do_not_leak_to_public_advice(self):
        import json
        first = self.event("EVENT-1", data=(("irrelevant", "secret-canary"),))
        second = replace(first, event_id="EVENT-2", timestamp_seconds=2.0)
        self.assertNotIn("secret-canary", repr(first))
        report = self.analyze((first, second))
        self.assertNotIn("secret-canary", json.dumps(report))
        self.assertFalse(report["modifies_source"])

    def test_missing_current_identity_does_not_authorize_recovery_finding(self):
        event = self.event("EVENT-1", "stage.execute", data=(("recovered", True),
                           ("prior_commit", "COMMITTED"), ("prior_identity", "VALID")))
        incomplete = replace(event, code_sha256=None)
        self.assertEqual(self.analyze((incomplete,))["rules"]["WA-006"]["status"], "NOT_EVALUATED")

    def test_distinct_pair_identities_never_form_a_repeat_finding(self):
        first = self.event("EVENT-1")
        second = replace(first, event_id="EVENT-2", pair_id="PAIR-2", timestamp_seconds=2.0)
        self.assertEqual(self.analyze((first, second))["rules"]["WA-001"]["status"], "NO_FINDING")

    def test_advice_has_a_stable_identity_without_exposing_semantic_details(self):
        first = self.event("EVENT-1")
        second = self.event("EVENT-2", time=2.0)
        report = self.analyze((first, second))
        identifier = report["findings"][0]["finding_id"]
        self.assertRegex(identifier, r"^FND-[0-9a-f]{64}$")
        self.assertEqual(self.analyze((first, second))["findings"][0]["finding_id"], identifier)

    def test_verification_matching_has_linear_identity_comparison_cost(self):
        class ObservedId(str):
            comparisons = 0
            __hash__ = str.__hash__

            def __eq__(self, other):
                type(self).comparisons += 1
                return str.__eq__(self, other)

        # Count comparisons at the public string-identity seam, not internal calls
        # or wall-clock latency; the design promises linear event processing.
        count = 256
        events = []
        for index in range(count):
            public = replace(self.event(f"PUB-{index}", "verification.result"), run_id=ObservedId(f"RUN-{index}"),
                             data=(("verification_scope", "public"), ("verification_status", "PASS"),
                                   ("verification_suite_sha256", "c" * 64)))
            hidden = replace(public, event_id=f"HID-{index}", run_id=ObservedId(f"RUN-{index}"),
                             data=(("verification_scope", "isolated_hidden"), ("verification_status", "FAIL"),
                                   ("verification_suite_sha256", "c" * 64), ("isolation_sha256", "d" * 64)))
            events.extend((public, hidden))
        anchors = frozenset(event.digest for event in events)
        ObservedId.comparisons = 0
        report = analyze_workflow(tuple(events), trusted_event_digests=anchors, complete_capture=True,
                                  trusted_hidden_isolation_digests=frozenset({"d" * 64}))
        self.assertEqual(len(report["findings"]), count)
        self.assertLessEqual(ObservedId.comparisons, 16 * count,
                             "matching rescans unrelated run identities instead of indexing them")


if __name__ == "__main__":
    unittest.main()
