#!/usr/bin/env python3
"""Composable identity only invalidates consumers of changed components."""
from __future__ import annotations

from dataclasses import replace
import unittest

from aek.core.context.identity import (
    IdentityState,
    build_identity,
    compare_identity,
    decode_identity,
    identity_as_dict,
)
from aek.application.identity import (
    bind_receipt,
    bound_receipt_as_dict,
    decode_bound_receipt,
    verify_bound_receipt,
)


def components(**overrides: str) -> dict[str, dict[str, str]]:
    values = {
        "subject": {
            "change_id": "a" * 64,
            "task_id": "b" * 64,
            "head_sha": "c" * 64,
            "worktree_digest": "d" * 64,
            "scope_digest": "e" * 64,
        },
        "artifact": {"plan_digest": "f" * 64},
        "policy": {"content_digest": "1" * 64},
        "context": {"source_set_digest": "2" * 64},
        "evidence": {"verification_digest": "3" * 64},
    }
    for qualified_name, digest in overrides.items():
        namespace, key = qualified_name.split(".", 1)
        values[namespace][key] = digest
    return values


class IdentityTests(unittest.TestCase):
    def test_only_required_component_changes_make_a_consumer_stale(self) -> None:
        expected = build_identity(components())
        policy_changed = build_identity(components(**{"policy.content_digest": "4" * 64}))

        capsule = compare_identity(
            expected, policy_changed,
            ("subject.worktree_digest", "context.source_set_digest"),
        )
        delivery = compare_identity(
            expected, policy_changed,
            ("subject.worktree_digest", "policy.content_digest"),
        )

        self.assertEqual(capsule.state, IdentityState.VALID)
        self.assertEqual(capsule.changed, ())
        self.assertEqual(delivery.state, IdentityState.STALE)
        self.assertEqual(delivery.changed, ("policy.content_digest",))
        self.assertEqual(delivery.reason_codes, ("IDENTITY_COMPONENT_CHANGED",))

    def test_canonical_digest_is_order_independent_and_serializable(self) -> None:
        forward = components()
        reverse = {
            namespace: dict(reversed(tuple(values.items())))
            for namespace, values in reversed(tuple(forward.items()))
        }
        first = build_identity(forward)
        second = build_identity(reverse)
        self.assertEqual(first, second)
        self.assertEqual(identity_as_dict(first)["envelope_digest"],
                         first.envelope_digest)
        self.assertEqual(decode_identity(identity_as_dict(first)), first)

    def test_missing_required_component_is_stale_but_tampering_is_invalid(self) -> None:
        expected = build_identity(components())
        current_values = components()
        del current_values["policy"]["content_digest"]
        current_values["policy"]["version_digest"] = "5" * 64
        current = build_identity(current_values)
        missing = compare_identity(
            expected, current, ("policy.content_digest",))
        self.assertEqual(missing.state, IdentityState.STALE)
        self.assertEqual(missing.missing, ("policy.content_digest",))

        tampered = replace(current, envelope_digest="0" * 64)
        invalid = compare_identity(expected, tampered, ("subject.scope_digest",))
        self.assertEqual(invalid.state, IdentityState.INVALID)
        self.assertIn("IDENTITY_DIGEST_INVALID", invalid.reason_codes)

    def test_invalid_namespace_digest_and_component_reference_are_rejected(self) -> None:
        bad_namespace = components()
        bad_namespace["unknown"] = {"digest": "6" * 64}
        with self.assertRaises(ValueError):
            build_identity(bad_namespace)
        bad_digest = components()
        bad_digest["policy"]["content_digest"] = "not-a-digest"
        with self.assertRaises(ValueError):
            build_identity(bad_digest)
        envelope = build_identity(components())
        invalid = compare_identity(envelope, envelope, ("policy",))
        self.assertEqual(invalid.state, IdentityState.INVALID)

    def test_bound_receipt_detects_payload_tampering_and_current_staleness(self) -> None:
        expected = build_identity(components())
        receipt = bind_receipt(expected, {
            "change_id": "CHG-001",
            "status": "PASS",
            "evidence_refs": ["evidence/tests/T1.json"],
        })
        encoded = bound_receipt_as_dict(receipt)
        self.assertEqual(encoded["identity_digest"], expected.envelope_digest)
        self.assertEqual(decode_bound_receipt(encoded), receipt)
        self.assertEqual(
            verify_bound_receipt(
                receipt, expected,
                ("subject.worktree_digest", "policy.content_digest"),
            ).state,
            IdentityState.VALID,
        )

        policy_changed = build_identity(
            components(**{"policy.content_digest": "4" * 64}))
        self.assertEqual(
            verify_bound_receipt(
                receipt, policy_changed,
                ("subject.worktree_digest", "policy.content_digest"),
            ).state,
            IdentityState.STALE,
        )
        tampered = replace(
            receipt,
            payload_json='{"change_id":"CHG-001","status":"FAIL"}',
        )
        self.assertEqual(
            verify_bound_receipt(
                tampered, expected, ("subject.worktree_digest",),
            ).state,
            IdentityState.INVALID,
        )
        encoded["unknown_authority"] = True
        with self.assertRaises(ValueError):
            decode_bound_receipt(encoded)


if __name__ == "__main__":
    unittest.main()
