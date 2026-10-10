"""Trusted benchmark scenarios reject ambiguous or unsafe execution inputs."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmark"))
from core.scenario import (  # noqa: E402
    ScenarioError,
    parse_scenario,
    scenario_from_json,
)


def scenario_value():
    return {
        "schema_version": 1, "id": "SC-001", "name": "局部修复", "category": "bug",
        "task": {"prompt": "修复空值处理，保持现有接口。"},
        "fixture": {"path": "fixture", "commit": "a" * 40, "sha256": "b" * 64},
        "execution": {"agents": ["codex", "claude"], "variants": ["vanilla", "aek"],
                      "timeout_seconds": 60, "repeat": 1},
        "evaluation": {
            "checks": [{"id": "handles-null", "entrypoint": "checks/verify.py",
                        "sha256": "c" * 64, "argv": ["{python}", "{check}", "{workspace}"],
                        "timeout_seconds": 10, "kind": "outcome"}],
            "allowed_paths": ["src/**", "tests/**"],
            "forbidden_paths": ["secrets/**"],
        },
    }


class ScenarioTests(unittest.TestCase):
    def test_valid_scenario_is_immutable_and_binds_independent_checks(self):
        value = scenario_value()
        parsed = parse_scenario(value)
        self.assertEqual(parsed.id, "SC-001")
        self.assertEqual(parsed.checks[0].entrypoint, "checks/verify.py")
        self.assertEqual(parsed.checks[0].argv, ("{python}", "{check}", "{workspace}"))
        self.assertEqual(len(parsed.digest), 64)
        value["evaluation"]["checks"][0]["argv"].append("changed")
        self.assertEqual(len(parsed.checks[0].argv), 3)
        reordered = dict(reversed(list(scenario_value().items())))
        self.assertEqual(parse_scenario(reordered).digest, parsed.digest)
        changed = copy.deepcopy(scenario_value())
        changed["task"]["prompt"] = "不同任务"
        self.assertNotEqual(parse_scenario(changed).digest, parsed.digest)

    def test_wrong_types_and_unknown_fields_are_controlled_errors(self):
        mutations = (
            lambda x: x.update(schema_version=True),
            lambda x: x.update(schema_version=2),
            lambda x: x.update(secret_canary="do-not-print"),
            lambda x: x["task"].update(extra=True),
            lambda x: x["execution"].update(timeout_seconds=True),
            lambda x: x["execution"].update(repeat=0),
            lambda x: x["execution"].update(max_turns=None),
            lambda x: x["execution"].update(variants=["aek"]),
            lambda x: x["execution"].update(agents=[]),
            lambda x: x["execution"].update(agents=["codex", "codex"]),
            lambda x: x["evaluation"]["checks"][0].update(kind={}),
            lambda x: x["fixture"].update(commit="main"),
            lambda x: x["fixture"].update(sha256="not-a-sha"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = scenario_value()
                mutate(value)
                with self.assertRaises(ScenarioError) as error:
                    parse_scenario(value)
                self.assertNotIn("do-not-print", str(error.exception))

    def test_independent_checks_cannot_be_omitted_or_moved_into_agent_fixture(self):
        mutations = (
            lambda x: x["evaluation"].update(checks=[]),
            lambda x: x["evaluation"]["checks"].append(copy.deepcopy(x["evaluation"]["checks"][0])),
            lambda x: x["evaluation"]["checks"][0].update(entrypoint="fixture/check.py"),
            lambda x: x["evaluation"]["checks"][0].update(argv=["sh", "-c", "anything"]),
            lambda x: x["evaluation"]["checks"][0].update(argv=["{python}", "{check}", "{unknown}"]),
            lambda x: x["evaluation"]["checks"][0].update(kind="memory"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = scenario_value()
                mutate(value)
                with self.assertRaises(ScenarioError):
                    parse_scenario(value)

    def test_paths_reject_escape_and_platform_ambiguity(self):
        for path in ("/etc/fixture", "../fixture", "a/../fixture", "./fixture", "a//b",
                     "C:/fixture", "a\\b", "a\nb", "a/*"):
            with self.subTest(path=path):
                value = scenario_value()
                value["fixture"]["path"] = path
                with self.assertRaises(ScenarioError):
                    parse_scenario(value)
        value = scenario_value()
        value["evaluation"]["allowed_paths"] = ["../**"]
        with self.assertRaises(ScenarioError):
            parse_scenario(value)

    def test_json_rejects_duplicate_fields_nonfinite_values_and_oversized_input(self):
        for raw in (b'{"schema_version":1,"schema_version":1}',
                    b'{"schema_version":NaN}', b'\xff', b'{',
                    b' ' * (1024 * 1024 + 1), b'[' * 20000 + b'0' + b']' * 20000):
            with self.subTest(prefix=raw[:30], size=len(raw)):
                with self.assertRaises(ScenarioError):
                    scenario_from_json(raw)
        self.assertEqual(scenario_from_json(json.dumps(scenario_value()).encode()).id, "SC-001")

    def test_scenario_file_loader_is_read_only_and_refuses_links(self):
        from adapters.scenario_file import load_scenario

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scenario.json"
            raw = json.dumps(scenario_value(), ensure_ascii=False).encode("utf-8")
            source.write_bytes(raw)
            loaded = load_scenario(source)
            self.assertEqual(loaded.scenario.id, "SC-001")
            self.assertEqual(loaded.scenario_root, root.resolve())
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(len(loaded.source_sha256), 64)
            link = root / "linked.json"
            try:
                link.symlink_to(source)
            except (OSError, NotImplementedError):
                return
            with self.assertRaises(ScenarioError):
                load_scenario(link)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_scenario_file_loader_rejects_special_files_without_waiting(self):
        from adapters.scenario_file import load_scenario

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "scenario.json"
            os.mkfifo(source)
            with self.assertRaises(ScenarioError):
                load_scenario(source)


if __name__ == "__main__":
    unittest.main()
