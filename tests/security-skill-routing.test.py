#!/usr/bin/env python3
"""Risk reference selection stays independent of workload and scope evidence."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aek.core.context import reference_catalog


class SecuritySkillRoutingTests(unittest.TestCase):
    def test_dependency_design_loads_only_fixed_relevant_reference(self):
        catalog = reference_catalog.SECURITY_REFERENCE_CATALOG
        selected = catalog.for_risks(("dependencies",), "design")
        self.assertEqual(tuple(item.id for item in selected), ("security-dependencies",))
        self.assertEqual(selected[0].source_path,
                         "skills/security-review/references/dependencies.md")
        self.assertEqual(catalog.for_risks((), "execution"), ())
        self.assertEqual(tuple(item.id for item in
                               reference_catalog.REFERENCE_CATALOG.for_route("direct", "route")),
                         ("direct",))

    def test_source_and_baseline_topics_are_deduplicated_without_wildcard_loading(self):
        catalog = reference_catalog.SECURITY_REFERENCE_CATALOG
        selected = catalog.for_risks(("application", "agent_configuration", "baseline"), "closeout")
        self.assertEqual(tuple(item.id for item in selected),
                         ("security-source", "security-baseline"))
        self.assertEqual(len({item.authority for item in selected}), len(selected))
        for item in selected:
            self.assertTrue((ROOT / item.source_path).is_file())
        for risks, stage in ((("unknown",), "design"), (("dependencies",), "unknown"),
                              (["dependencies"], "design")):
            with self.subTest(risks=risks, stage=stage), self.assertRaises(ValueError):
                catalog.for_risks(risks, stage)

    def test_reference_spec_rejects_root_escape_and_unregistered_risks(self):
        for path in ("skills/security-review/references/../escape.md",
                     "skills/security-review/references/nested/escape.md",
                     "skills/repo-delivery/references/direct.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                reference_catalog.SecurityReferenceSpec("invalid", path, ("application",), "invalid")
        with self.assertRaises(ValueError):
            reference_catalog.SecurityReferenceSpec(
                "invalid", "skills/security-review/references/source-review.md", ("unknown",), "invalid")


if __name__ == "__main__":
    unittest.main()
