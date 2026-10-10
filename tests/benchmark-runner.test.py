"""Prepared benchmark workspaces use a pinned fixture and independent storage."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "benchmark"))
from adapters.scenario_file import load_scenario  # noqa: E402
from adapters.workspace import prepare_pair  # noqa: E402


class PreparedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
                        GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
        self.code = b"def answer():\n    return 41\n"
        (self.fixture / "main.py").write_bytes(self.code)
        self.git(self.fixture, "init", "--template=")
        self.git(self.fixture, "add", "--", "main.py")
        self.git(self.fixture, "commit", "-m", "fixture")
        self.commit = self.git(self.fixture, "rev-parse", "HEAD").strip()
        # The schema's canonical manifest contract, with independent fixed file facts.
        payload = {"files": [{"path": "main.py", "mode": "100644",
                             "sha256": hashlib.sha256(self.code).hexdigest()}]}
        self.digest = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        (self.root / "checks").mkdir()
        check = self.root / "checks" / "verify.py"
        check.write_text("raise SystemExit(0)\n", encoding="utf-8")
        self.value = {
            "schema_version": 1, "id": "SC-001", "name": "fixed baseline", "category": "bug",
            "task": {"prompt": "Fix answer"},
            "fixture": {"path": "fixture", "commit": self.commit, "sha256": self.digest},
            "execution": {"agents": ["codex", "claude"], "variants": ["vanilla", "aek"],
                          "timeout_seconds": 5, "repeat": 1},
            "evaluation": {
                "checks": [{"id": "answer", "entrypoint": "checks/verify.py",
                            "sha256": hashlib.sha256(check.read_bytes()).hexdigest(),
                            "argv": ["{python}", "{check}", "{workspace}"],
                            "timeout_seconds": 2, "kind": "outcome"}],
                "allowed_paths": ["main.py"], "forbidden_paths": [],
            },
        }

    def git(self, root, *args):
        result = subprocess.run(
            ["git", "-c", "user.name=AEK fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-C", str(root), *args],
            env=self.env, capture_output=True, check=True)
        return result.stdout.decode("utf-8")

    def scenario(self):
        path = self.root / "scenario.json"
        path.write_text(json.dumps(self.value), encoding="utf-8")
        return load_scenario(path)

    def test_pinned_snapshot_and_independent_workspaces_are_not_live_isolation(self):
        loaded = self.scenario()
        (self.fixture / "main.py").write_text("dirty version", encoding="utf-8")
        (self.fixture / "untracked-secret.txt").write_text("host-only", encoding="utf-8")
        prepared = prepare_pair(loaded, self.outputs)
        left = prepared.workspace("vanilla")
        right = prepared.workspace("aek")
        for workspace in (left, right):
            self.assertEqual((workspace.path / "main.py").read_bytes(), self.code)
            self.assertFalse((workspace.path / "untracked-secret.txt").exists())
            self.assertTrue(workspace.home.is_dir())
            self.assertTrue(workspace.cache.is_dir())
            self.assertTrue(workspace.session.is_dir())
            self.assertEqual(self.git(workspace.path, "status", "--porcelain"), "")
            self.assertEqual(self.git(workspace.path, "rev-parse", "HEAD").strip(),
                             workspace.baseline_commit)
        self.assertEqual(left.baseline_commit, right.baseline_commit)
        self.assertNotEqual(left.path, right.path)
        self.assertNotEqual(left.home, right.home)
        (left.path / "main.py").write_text("changed", encoding="utf-8")
        self.assertEqual((right.path / "main.py").read_bytes(), self.code)
        report = prepared.manifest
        self.assertEqual(report["fixture"]["commit"], self.commit)
        self.assertEqual(report["fixture"]["sha256"], self.digest)
        self.assertEqual(report["isolation"]["filesystem"], "NOT_EVALUATED")
        self.assertEqual(report["isolation"]["network"], "NOT_EVALUATED")
        self.assertFalse(report["live_ready"])
        self.assertEqual(report["treatment"], "NOT_APPLIED")
        self.assertNotIn(str(self.root), json.dumps(report))
        raw = (prepared.root / "manifest.json").read_bytes()
        self.assertEqual(prepared.manifest_sha256, hashlib.sha256(raw).hexdigest())

    def test_wrong_commit_or_digest_creates_no_candidate_directories(self):
        for key, value, reason in (("commit", "a" * 40, "FIXTURE_GIT_FAILED"),
                                   ("sha256", "b" * 64, "FIXTURE_DIGEST_MISMATCH")):
            with self.subTest(key=key):
                previous = self.value["fixture"][key]
                self.value["fixture"][key] = value
                with self.assertRaisesRegex(ValueError, reason):
                    prepare_pair(self.scenario(), self.outputs)
                self.assertEqual(list(self.outputs.iterdir()), [])
                self.value["fixture"][key] = previous

    def test_changed_scenario_is_rejected_before_output_is_created(self):
        loaded = self.scenario()
        self.value["task"]["prompt"] = "different task"
        self.scenario()
        with self.assertRaisesRegex(ValueError, "SCENARIO_SOURCE_CHANGED"):
            prepare_pair(loaded, self.outputs)
        self.assertEqual(list(self.outputs.iterdir()), [])

    def test_link_and_submodule_entries_are_not_copied(self):
        blob = self.git(self.fixture, "rev-parse", "HEAD:main.py").strip()
        for mode, oid, name in (("120000", blob, "link"),
                                ("160000", self.commit, "submodule")):
            with self.subTest(mode=mode):
                self.git(self.fixture, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{name}")
                self.git(self.fixture, "commit", "-m", "unsafe fixture entry")
                self.value["fixture"]["commit"] = self.git(self.fixture, "rev-parse", "HEAD").strip()
                with self.assertRaisesRegex(ValueError, "FIXTURE_ENTRY_TYPE_UNSUPPORTED"):
                    prepare_pair(self.scenario(), self.outputs)
                self.assertEqual(list(self.outputs.iterdir()), [])
                self.git(self.fixture, "update-index", "--force-remove", "--", name)

    def test_case_collisions_and_windows_reserved_names_fail_on_all_hosts(self):
        blob = self.git(self.fixture, "rev-parse", "HEAD:main.py").strip()
        for name, reason in (("Main.py", "FIXTURE_PATH_COLLISION"),
                             ("NUL.txt", "FIXTURE_PATH_NOT_PORTABLE")):
            with self.subTest(name=name):
                self.git(self.fixture, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
                self.git(self.fixture, "commit", "-m", "nonportable fixture entry")
                self.value["fixture"]["commit"] = self.git(self.fixture, "rev-parse", "HEAD").strip()
                with self.assertRaisesRegex(ValueError, reason):
                    prepare_pair(self.scenario(), self.outputs)
                self.assertEqual(list(self.outputs.iterdir()), [])
                self.git(self.fixture, "update-index", "--force-remove", "--", name)

    def fake_agent(self, source):
        script = self.root / "agent.py"
        script.write_text(source, encoding="utf-8")
        return script, hashlib.sha256(script.read_bytes()).hexdigest()

    def test_fake_attempt_is_recorded_once_without_claiming_outcome_or_tokens(self):
        from adapters.fake_run import run_fake
        prepared = prepare_pair(self.scenario(), self.outputs)
        script, digest = self.fake_agent(
            "from pathlib import Path\nimport sys\n"
            "p = Path(sys.argv[1])/'counter.txt'\n"
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n"
            "print('PASS and pretend token count 0')\n")
        first = run_fake(prepared, "vanilla", script, script_sha256=digest, timeout_seconds=2)
        second = run_fake(prepared, "vanilla", script, script_sha256=digest, timeout_seconds=2)
        self.assertEqual(first["status"], "COMPLETED")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["status"], "COMPLETED")
        self.assertEqual(first["request_digest"], second["request_digest"])
        self.assertEqual((prepared.workspace("vanilla").path / "counter.txt").read_text(), "1")
        self.assertEqual(first["outcome"], "NOT_EVALUATED")
        self.assertIsNone(first["usage"]["tokens"])
        self.assertEqual(first["usage"]["coverage"], "UNKNOWN")
        self.assertNotIn("pretend", json.dumps(first))
        self.assertNotIn(str(self.root), json.dumps(first))

    def test_corrupted_cached_result_does_not_leak_unknown_fields_or_restart(self):
        from adapters.fake_run import run_fake
        prepared = prepare_pair(self.scenario(), self.outputs)
        script, digest = self.fake_agent(
            "from pathlib import Path\nimport sys\n"
            "p=Path(sys.argv[1])/'counter.txt'\n"
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n")
        run_fake(prepared, "vanilla", script, script_sha256=digest, timeout_seconds=2)
        path = prepared.root / "vanilla.execution-result.json"
        result = json.loads(path.read_bytes())
        result["unexpected"] = "secret-canary"
        raw = json.dumps(result).encode()
        path.write_bytes(raw)
        receipt_path = prepared.root / "vanilla.execution-receipt.json"
        receipt = json.loads(receipt_path.read_bytes())
        receipt["result_sha256"] = hashlib.sha256(raw).hexdigest()
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        after = run_fake(prepared, "vanilla", script, script_sha256=digest, timeout_seconds=2)
        self.assertEqual(after["status"], "NEEDS_HUMAN")
        self.assertNotIn("secret-canary", json.dumps(after))
        self.assertEqual((prepared.workspace("vanilla").path / "counter.txt").read_text(), "1")

    def test_interrupted_attempt_keeps_started_receipt_and_is_not_relaunched(self):
        from adapters.fake_run import run_fake
        prepared = prepare_pair(self.scenario(), self.outputs)
        script, digest = self.fake_agent("raise SystemExit(0)\n")
        with mock.patch("adapters.fake_run.run_process", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_fake(prepared, "vanilla", script, script_sha256=digest, timeout_seconds=2)
        receipt = json.loads((prepared.root / "vanilla.execution-receipt.json").read_bytes())
        self.assertEqual(receipt["state"], "STARTED")
        with mock.patch("adapters.fake_run.run_process", side_effect=AssertionError("must not restart")):
            resumed = run_fake(prepared, "vanilla", script, script_sha256=digest, timeout_seconds=2)
        self.assertEqual(resumed["status"], "NEEDS_HUMAN")

    def test_controller_interrupt_terminates_the_started_process(self):
        from adapters.process import run_process
        launched = []
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            if args[0][0] == sys.executable:
                launched.append(child)
            return child

        try:
            with mock.patch("adapters.process.subprocess.Popen", side_effect=launch), \
                    mock.patch("adapters.process.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_process((sys.executable, "-I", "-c", "import time; time.sleep(30)"),
                                self.root, timeout_seconds=2)
            self.assertEqual(len(launched), 1)
            self.assertIsNotNone(launched[0].poll())
        finally:
            # Only the exact process created by this test is eligible for cleanup.
            for child in launched:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_paired_cli_from_any_cwd_is_explicitly_development_only(self):
        loaded = self.scenario()
        script, digest = self.fake_agent("print('pretend everything passed')\n")
        entry = REPO / "tests" / "benchmark" / "run.py"
        result = subprocess.run(
            [sys.executable, str(entry), "paired", "--scenario-file", str(loaded.source),
             "--output-parent", str(self.outputs), "--fake-agent", str(script), "--fake-sha256", digest],
            cwd=self.root, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["validity"]["status"], "NOT_EVALUATED")
        self.assertEqual(len(report["runs"]), 2)
        self.assertEqual({run["variant"] for run in report["runs"]}, {"vanilla", "aek"})
        self.assertTrue(all(run["attempt"]["status"] == "COMPLETED" for run in report["runs"]))
        self.assertTrue(all(run["attempt"]["outcome"] == "NOT_EVALUATED" for run in report["runs"]))
        self.assertNotIn("pretend", result.stdout)
        self.assertNotIn(str(self.root), result.stdout)

    def test_lost_work_unit_does_not_overwrite_existing_committed_evidence(self):
        from adapters.fake_run import run_fake
        prepared = prepare_pair(self.scenario(), self.outputs)
        script, digest = self.fake_agent("raise SystemExit(0)\n")
        run_fake(prepared, "vanilla", script, script_sha256=digest, timeout_seconds=2)
        state_files = tuple((prepared.root / "work-units").glob("*/state.json"))
        self.assertEqual(len(state_files), 1)
        state_files[0].unlink()  # Exact test-owned file; model a lost state sidecar.
        receipt_path = prepared.root / "vanilla.execution-receipt.json"
        before = receipt_path.read_bytes()
        with mock.patch("adapters.fake_run.run_process", side_effect=AssertionError("must not restart")):
            after = run_fake(prepared, "vanilla", script, script_sha256=digest, timeout_seconds=2)
        self.assertEqual(after["status"], "NEEDS_HUMAN")
        self.assertEqual(receipt_path.read_bytes(), before)

    def test_process_failure_and_timeout_are_sealed_attempts_not_retry_permissions(self):
        from adapters.fake_run import run_fake
        for source, status in (("raise SystemExit(7)\n", "FAILED"),
                               ("import time\ntime.sleep(30)\n", "TIMED_OUT")):
            with self.subTest(status=status):
                prepared = prepare_pair(self.scenario(), self.outputs)
                script, digest = self.fake_agent(source)
                first = run_fake(prepared, "aek", script, script_sha256=digest, timeout_seconds=1)
                self.assertEqual(first["status"], status)
                with mock.patch("adapters.fake_run.run_process", side_effect=AssertionError("must not retry")):
                    repeated = run_fake(prepared, "aek", script, script_sha256=digest, timeout_seconds=1)
                self.assertTrue(repeated["reused"])
                self.assertEqual(repeated["status"], status)
                changed_request = run_fake(prepared, "aek", script, script_sha256=digest, timeout_seconds=2)
                self.assertEqual(changed_request["status"], "NEEDS_HUMAN")

    def test_cli_live_request_fails_before_preparation_or_execution(self):
        loaded = self.scenario()
        result = subprocess.run(
            [sys.executable, str(REPO / "tests" / "benchmark" / "run.py"), "paired",
             "--scenario-file", str(loaded.source), "--output-parent", str(self.outputs), "--live"],
            cwd=self.root, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["reason_code"], "LIVE_RUNNER_NOT_AVAILABLE")
        self.assertEqual(list(self.outputs.iterdir()), [])

    def test_private_environment_does_not_inherit_host_credentials(self):
        from adapters.fake_run import run_fake
        prepared = prepare_pair(self.scenario(), self.outputs)
        script, digest = self.fake_agent(
            "import os, sys\nfrom pathlib import Path\n"
            "assert 'AEK_TEST_TOKEN_CANARY' not in os.environ\n"
            "assert 'PYTHONPATH' not in os.environ\n"
            "root=Path(sys.argv[1]).parent\n"
            "assert Path(os.environ['HOME']) == root/'home'\n"
            "assert Path(os.environ['XDG_CACHE_HOME']) == root/'cache'\n"
            "assert Path(os.environ['CODEX_HOME']) == root/'session'\n")
        with mock.patch.dict(os.environ, {"AEK_TEST_TOKEN_CANARY": "secret-canary"}):
            result = run_fake(prepared, "aek", script, script_sha256=digest, timeout_seconds=2)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotIn("secret-canary", json.dumps(result))

    def test_reloaded_pair_requires_external_manifest_anchor_and_reuses_execution(self):
        from adapters.fake_run import run_fake
        from adapters.workspace import load_prepared_pair
        prepared = prepare_pair(self.scenario(), self.outputs)
        script, digest = self.fake_agent("raise SystemExit(0)\n")
        run_fake(prepared, "aek", script, script_sha256=digest, timeout_seconds=2)
        reloaded = load_prepared_pair(prepared.root, expected_manifest_sha256=prepared.manifest_sha256)
        with mock.patch("adapters.fake_run.run_process", side_effect=AssertionError("must not restart")):
            restored = run_fake(reloaded, "aek", script, script_sha256=digest, timeout_seconds=2)
        self.assertTrue(restored["reused"])
        with self.assertRaisesRegex(ValueError, "PREPARED_MANIFEST_DIGEST_MISMATCH"):
            load_prepared_pair(prepared.root, expected_manifest_sha256="a" * 64)

    def test_cli_resume_does_not_repeat_either_completed_variant(self):
        loaded = self.scenario()
        script, digest = self.fake_agent(
            "from pathlib import Path\nimport sys\n"
            "p=Path(sys.argv[1])/'counter.txt'\n"
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n")
        entry = str(REPO / "tests" / "benchmark" / "run.py")
        common = ["--fake-agent", str(script), "--fake-sha256", digest]
        first = subprocess.run(
            [sys.executable, entry, "paired", "--scenario-file", str(loaded.source),
             "--output-parent", str(self.outputs), *common], cwd=self.root,
            capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(first.stdout)
        pair_root = self.outputs / report["pair_id"]
        resumed = subprocess.run(
            [sys.executable, entry, "paired", "--resume-pair", str(pair_root),
             "--manifest-sha256", report["manifest_sha256"], *common], cwd=self.root,
            capture_output=True, text=True, check=False)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(all(run["attempt"]["reused"] for run in json.loads(resumed.stdout)["runs"]))
        for variant in ("vanilla", "aek"):
            self.assertEqual((pair_root / variant / "workspace" / "counter.txt").read_text(), "1")

    def test_unsupported_fake_budgets_fail_before_workspace_creation(self):
        for key, value in (("timeout_seconds", 3601), ("max_turns", 2), ("repeat", 2)):
            with self.subTest(key=key):
                previous = self.value["execution"].get(key)
                self.value["execution"][key] = value
                with self.assertRaisesRegex(ValueError, "FAKE_BUDGET_UNSUPPORTED"):
                    prepare_pair(self.scenario(), self.outputs)
                self.assertEqual(list(self.outputs.iterdir()), [])
                if previous is None:
                    del self.value["execution"][key]
                else:
                    self.value["execution"][key] = previous

    def test_committed_receipt_recovers_crash_before_final_state_without_reexecution(self):
        from adapters.fake_run import run_fake
        from adapters.workspace import load_prepared_pair
        prepared = prepare_pair(self.scenario(), self.outputs)
        script, digest = self.fake_agent(
            "from pathlib import Path\nimport sys\n"
            "p=Path(sys.argv[1])/'counter.txt'\n"
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n")
        real_replace = os.replace

        def crash_before_state_publish(source, destination):
            if Path(destination).name == "state.json":
                value = json.loads(Path(source).read_bytes())
                if value["steps"][0]["state"] == "COMPLETED":
                    raise KeyboardInterrupt
            return real_replace(source, destination)

        with mock.patch("aek.adapters.atomic_file.os.replace", side_effect=crash_before_state_publish):
            with self.assertRaises(KeyboardInterrupt):
                run_fake(prepared, "vanilla", script, script_sha256=digest, timeout_seconds=2)
        state_path, = (prepared.root / "work-units").glob("*/state.json")
        self.assertEqual(json.loads(state_path.read_bytes())["steps"][0]["state"], "IN_PROGRESS")
        restored = load_prepared_pair(prepared.root, expected_manifest_sha256=prepared.manifest_sha256)
        with mock.patch("adapters.fake_run.run_process", side_effect=AssertionError("must not restart")):
            result = run_fake(restored, "vanilla", script, script_sha256=digest, timeout_seconds=2)
        self.assertTrue(result["reused"])
        self.assertEqual(json.loads(state_path.read_bytes())["steps"][0]["state"], "COMPLETED")
        self.assertEqual((restored.workspace("vanilla").path / "counter.txt").read_text(), "1")

    def test_concurrent_request_does_not_launch_a_second_agent(self):
        from adapters.fake_run import run_fake
        prepared = prepare_pair(self.scenario(), self.outputs)
        workspace = prepared.workspace("vanilla").path
        script, digest = self.fake_agent(
            "from pathlib import Path\nimport sys, time\n"
            "root=Path(sys.argv[1])\n"
            "p=root/'counter.txt'\n"
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n"
            "while not (root/'release.txt').exists(): time.sleep(0.01)\n")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run_fake, prepared, "vanilla", script,
                                script_sha256=digest, timeout_seconds=3)
            deadline = time.monotonic() + 2
            try:
                while not (workspace / "counter.txt").exists() and time.monotonic() < deadline:
                    if first.done():
                        self.fail(f"first attempt ended before startup: {first.result()}")
                    time.sleep(0.01)
                self.assertTrue((workspace / "counter.txt").exists())
                second = run_fake(prepared, "vanilla", script, script_sha256=digest, timeout_seconds=3)
                self.assertEqual(second["status"], "NEEDS_HUMAN")
            finally:
                (workspace / "release.txt").write_text("continue", encoding="utf-8")
            self.assertEqual(first.result(timeout=5)["status"], "COMPLETED")
        self.assertEqual((workspace / "counter.txt").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
