#!/usr/bin/env python3
"""Managed safety code installs; user policies and evidence survive lifecycle."""
from __future__ import annotations

import importlib.util
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
import shutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from installer.doctor import run_doctor_checks
from installer.registry import AEK_PACKAGE_FILES
from aek.core.context.reference_catalog import SECURITY_REFERENCE_CATALOG

spec = importlib.util.spec_from_file_location("security_install_data", ROOT / "tests/security-model.test.py")
data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(data)


class SecurityInstallTests(unittest.TestCase):
    def test_common_main_entry_keeps_utf8_when_redirected_host_encoding_is_narrow(self):
        self.install()
        program = "import sys; from installer import main; raise SystemExit(main(sys.argv[1:]))"
        result = subprocess.run([sys.executable, "-c", program, "--doctor", str(self.repository)],
            env={**self.env, "PYTHONIOENCODING": "cp1252"}, cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(result.stderr, b"")
        self.assertIn("HEALTHY", result.stdout.decode("utf-8", errors="strict"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(self.root / "config"),
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT)}

    def crash_after_member(self, repository, workspace, member_id):
        program = """import os, sys
sys.path.insert(0, sys.argv[1])
import installer.transaction as tx
import installer.install as install
actual = tx.apply_copy_member
def interrupt(target, step, tx_id):
    actual(target, step, tx_id)
    if step.member_spec_id == sys.argv[4]:
        os._exit(91)
tx.apply_copy_member = interrupt
install.apply_copy_member = interrupt
from installer import main
raise SystemExit(main(['--codex-root', sys.argv[3], '--link-strategy', 'copy', sys.argv[2]]))
"""
        result = subprocess.run([sys.executable, "-c", program, str(ROOT), str(repository),
                                 str(workspace), member_id], env=self.env, cwd=ROOT,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 91, result.stdout.decode() + result.stderr.decode())

    def recover(self, repository, direction):
        result = subprocess.run([sys.executable, "-m", "installer", f"--recover={direction}",
                                 str(repository)], env=self.env, cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())

    def assert_only_clean_transaction_history(self, expected_status):
        from installer.transaction import find_unresolved, load_transaction
        from unittest.mock import patch

        records = list((self.repository / ".repo-memory-kit/tx").glob("*/record.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual({path.name for path in records[0].parent.iterdir()}, {"record.json"})
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.env["XDG_CONFIG_HOME"]}):
            history = load_transaction(self.repository, records[0].parent.name)
            self.assertEqual(history.status, expected_status)
            self.assertTrue(history.mac_verified)
            self.assertEqual(find_unresolved(self.repository), [])

    def install(self, *flags):
        result = subprocess.run([sys.executable, "-m", "installer", *flags,
                                 "--codex-root", str(self.workspace),
                                 "--link-strategy", "copy", str(self.repository)],
                                cwd=ROOT, env=self.env, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        return result

    def test_real_install_exposes_security_cli_and_import_closure(self):
        self.install()
        tool = self.repository / ".repo-memory-kit/bin/security-check"
        self.assertTrue(tool.is_file(), "security-check missing from installed registry")
        result = subprocess.run([sys.executable, str(tool)],
                                input=json.dumps(data.request_payload()).encode(),
                                cwd=self.repository, env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr.decode())
        self.assertEqual(json.loads(result.stdout)["status"], "INCOMPLETE")
        self.assertIn("SOURCE_EVIDENCE_UNAVAILABLE", json.loads(result.stdout)["reason_codes"])
        library = self.repository / ".repo-memory-kit/lib"
        program = "import importlib, sys; sys.path.insert(0, sys.argv[1]); [importlib.import_module(name) for name in sys.argv[2:]]"
        modules = ["aek." + path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
                   for path in AEK_PACKAGE_FILES if "security" in path]
        imported = subprocess.run([sys.executable, "-I", "-c", program, str(library), *modules],
                                  cwd=self.repository, env=self.env, capture_output=True, timeout=10)
        self.assertEqual(imported.returncode, 0, imported.stderr.decode())
        self.assertGreaterEqual(len(modules), 13)

    def test_static_scanner_diagnostics_do_not_execute_optional_binaries(self):
        scanner_bin = self.root / "optional-bin"
        scanner_bin.mkdir()
        for name in ("trivy", "semgrep", "gitleaks"):
            candidate = scanner_bin / (name + (".exe" if os.name == "nt" else ""))
            candidate.write_text("#!/bin/sh\nprintf scanner-ran > '" +
                                 str(self.root / "scanner-ran") + "'\n", encoding="utf-8")
            candidate.chmod(0o755)
        self.install()
        from unittest.mock import patch
        actual_run = subprocess.run
        with patch.dict(os.environ, {"PATH": str(scanner_bin) + os.pathsep + os.defpath}), \
                patch("subprocess.run", wraps=actual_run) as observed_processes:
            report = run_doctor_checks(self.repository, self.workspace)
        self.assertFalse(any(Path(call.args[0][0]).name.removesuffix(".exe")
                             in {"trivy", "semgrep", "gitleaks"}
                             for call in observed_processes.call_args_list))
        findings = [item for item in report.findings if item.spec_id.startswith("security-tool-")]
        self.assertEqual({item.spec_id for item in findings},
                         {"security-tool-trivy", "security-tool-semgrep", "security-tool-gitleaks"})
        for item in findings:
            self.assertIn("version=UNKNOWN", item.detail)
            self.assertIn("authorized=UNKNOWN", item.detail)
        self.assertFalse((self.root / "scanner-ran").exists())

    def test_missing_optional_scanners_and_v2_policy_are_read_only_diagnostics(self):
        from installer.doctor import governance_defaults_finding, security_tool_findings
        from installer.governance import default_security_governance_policy
        from unittest.mock import patch

        self.install()
        policy = self.repository / ".repo-memory-kit/governance.json"
        self.assertEqual(json.loads(policy.read_bytes())["version"], 1)
        policy.write_text(json.dumps(default_security_governance_policy()), encoding="utf-8")
        before = policy.read_bytes()
        finding = governance_defaults_finding(self.repository)
        self.assertEqual(finding.status, "managed")
        self.assertEqual(policy.read_bytes(), before)
        empty = self.root / "empty-bin"
        empty.mkdir()
        with patch.dict(os.environ, {"PATH": str(empty)}):
            findings = security_tool_findings()
        self.assertEqual(len(findings), 3)
        self.assertTrue(all(item.status == "unverifiable" for item in findings))
        self.assertTrue(all("installed=false" in item.detail for item in findings))
        self.assertEqual(policy.read_bytes(), before)

    def test_update_and_uninstall_preserve_team_policy_business_and_history(self):
        policy = self.repository / ".repo-memory-kit/governance.json"
        policy.parent.mkdir()
        policy.write_bytes(b'{"version":1,"rules":[{"id":"TEAM","require":["custom-check"]}]}')
        history = self.repository / "evidence/REVIEW-A/history.json"
        history.parent.mkdir(parents=True)
        history.write_bytes(b'{"historical":"team evidence"}')
        business = self.repository / "src/user.py"
        business.parent.mkdir()
        business.write_bytes(b"USER_CODE = 1\n")
        before = {path: path.read_bytes() for path in (policy, history, business)}
        self.install()
        for tree in ("claude", "agents"):
            for reference in SECURITY_REFERENCE_CATALOG.references.values():
                target = self.repository / f".{tree}" / reference.source_path
                self.assertEqual(target.read_bytes(), (ROOT / reference.source_path).read_bytes())
        for reference in SECURITY_REFERENCE_CATALOG.references.values():
            workspace_copy = self.workspace / ".agents" / reference.source_path
            self.assertEqual(workspace_copy.read_bytes(), (ROOT / reference.source_path).read_bytes())
            self.assertFalse(workspace_copy.is_symlink())
        self.install("--update")
        self.install("--uninstall")
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((self.repository / ".repo-memory-kit/bin/security-check").exists())
        self.assertFalse((self.repository / ".repo-memory-kit/lib/aek/core/security/model.py").exists())
        self.assertFalse((self.repository / ".agents/skills/security-review/references/workflow.md").exists())
        self.assertFalse((self.workspace / ".agents/skills/security-review/references/workflow.md").exists())

    def test_installed_report_reads_exact_saved_revision_without_rewriting_record(self):
        from aek.adapters.security_store import SecurityStore
        from aek.core.security.record import new_security_record

        self.install()
        carrier = self.repository / "evidence/REVIEW-A"
        carrier.mkdir(parents=True)
        record = new_security_record(json.dumps(data.request_payload()).encode(), review_id="REVIEW-A")
        store = SecurityStore(self.repository, "evidence/REVIEW-A/record.json")
        store.create(record)
        before = (carrier / "record.json").read_bytes()
        request = data.request_payload("report")
        request["expected_revision_digest"] = record.revision_digest
        request["record_ref"] = "evidence/REVIEW-A/record.json"
        for command in (ROOT / "security-check", self.repository / ".repo-memory-kit/bin/security-check"):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, str(command)],
                                        input=json.dumps(request).encode(), cwd=self.repository,
                                        env={**self.env, "PYTHONIOENCODING": "cp1252"},
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(json.loads(result.stdout)["record_digest"], record.revision_digest)
                self.assertEqual((carrier / "record.json").read_bytes(), before)

    def test_doctor_detects_missing_workspace_reference_and_update_repairs_it(self):
        self.install()
        reference = self.workspace / ".agents/skills/security-review/references/workflow.md"
        reference.unlink()
        report = run_doctor_checks(self.repository, self.workspace)
        finding = next(item for item in report.findings if item.spec_id == "codex-link-security-review")
        self.assertEqual(finding.status, "drifted")
        self.install("--update")
        self.assertEqual(reference.read_bytes(), (ROOT / "skills/security-review/references/workflow.md").read_bytes())

    def test_real_crash_after_each_reference_rolls_forward_complete_closure(self):
        for reference in SECURITY_REFERENCE_CATALOG.references.values():
            with self.subTest(reference=reference.id):
                repository = self.root / ("repo-" + reference.id)
                workspace = self.root / ("workspace-" + reference.id)
                repository.mkdir()
                workspace.mkdir()
                member_id = f"skill-agents-security-review-reference-{reference.id}"
                self.crash_after_member(repository, workspace, member_id)
                self.recover(repository, "roll-forward")
                for member in SECURITY_REFERENCE_CATALOG.references.values():
                    copied = workspace / ".agents" / member.source_path
                    self.assertEqual(copied.read_bytes(), (ROOT / member.source_path).read_bytes())
                self.assertEqual(list((repository / ".repo-memory-kit/tx").iterdir()), [])

    def test_real_crash_rollback_removes_only_created_owned_members(self):
        member_id = "skill-agents-security-review-reference-security-dependencies"
        self.crash_after_member(self.repository, self.workspace, member_id)
        user = self.workspace / ".agents/skills/security-review/user-notes.md"
        user.write_bytes(b"team notes")
        self.recover(self.repository, "rollback")
        self.assertEqual(user.read_bytes(), b"team notes")
        self.assertFalse((self.workspace / ".agents/skills/security-review/SKILL.md").exists())
        self.assertFalse((self.workspace / ".agents/skills/security-review/references/dependencies.md").exists())

    def test_modified_workspace_reference_is_not_overwritten_or_deleted(self):
        self.install()
        reference = self.workspace / ".agents/skills/security-review/references/dependencies.md"
        reference.write_bytes(b"team-owned replacement")
        result = subprocess.run([sys.executable, "-m", "installer", "--update", "--codex-root",
                                 str(self.workspace), "--link-strategy", "copy", str(self.repository)],
                                env=self.env, cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(reference.read_bytes(), b"team-owned replacement")
        self.recover(self.repository, "rollback")
        self.install("--uninstall")
        self.assertEqual(reference.read_bytes(), b"team-owned replacement")

    def test_new_member_mac_is_verified_and_real_103_loader_cannot_ignore_it(self):
        from installer import transaction as current
        from installer.registry import REGISTRY_BY_ID
        from unittest.mock import patch

        self.install()
        module = types.ModuleType("installer._legacy_transaction_103")
        module.__package__ = "installer"
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        original = subprocess.run(["git", "show", "8641ce8ed16b3eee9cda00506a10434fa3bfbe66:installer/transaction.py"],
                                  cwd=ROOT, capture_output=True, check=True).stdout
        exec(compile(original, "1.0.3/installer/transaction.py", "exec"), module.__dict__)
        legacy = current.ExternalStep("codex-link-security-review", "security-review", str(self.workspace),
                                      "content_matches", strategy="copy", expected_hash="a" * 64).to_dict()
        self.assertEqual(current.ExternalStep.from_dict(legacy).to_dict(), legacy)
        self.assertEqual(module.ExternalStep.from_dict(legacy).to_dict(), legacy)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.env["XDG_CONFIG_HOME"]}):
            tx = current.TransactionRecord.new(self.repository, "install")
            tx.status = "needs_human"
            tx.external = current.prepare_copy_members(
                self.repository, self.workspace, tx, REGISTRY_BY_ID["codex-link-security-review"])
            tx.write(self.repository)
            self.assertTrue(current.load_transaction(self.repository, tx.tx_id).mac_verified)
            self.assertFalse(module.load_transaction(self.repository, tx.tx_id).mac_verified)
            path = self.repository / f".repo-memory-kit/tx/{tx.tx_id}/record.json"
            record = json.loads(path.read_bytes())
            record["payload"]["external"][0]["prior_hash"] = "0" * 64
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertFalse(current.load_transaction(self.repository, tx.tx_id).mac_verified)

    def test_interrupted_real_reference_upgrade_restores_old_bytes_then_upgrades(self):
        from installer.registry import REGISTRY

        self.install()
        relative = "skills/security-review/references/dependencies.md"
        old = (ROOT / relative).read_bytes()
        kit = self.root / "next-kit"
        kit.mkdir()
        for resource in REGISTRY:
            if resource.source_path is not None:
                destination = kit / resource.source_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / resource.source_path, destination)
        shutil.copy2(ROOT / "VERSION", kit / "VERSION")
        legacy_path = kit / "installer/legacy_hashes.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy = json.loads((ROOT / "installer/legacy_hashes.json").read_bytes())
        legacy["kit_versions"]["copy-upgrade-fixture"] = {
            f"skill-{tree}-security-review-reference-security-dependencies":
            "canonical_sha256:" + hashlib.sha256(old).hexdigest()
            for tree in ("agents", "claude")}
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        new = old + b"\nNext release fixture changes this registered reference.\n"
        (kit / relative).write_bytes(new)
        program = """import os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import installer.registry as registry
import installer.install as install
registry.KIT_DIR = Path(sys.argv[4]); registry._LEGACY_HASHES = None
install.KIT_DIR = registry.KIT_DIR
actual = install.apply_copy_member
def interrupt(target, step, tx_id):
    actual(target, step, tx_id)
    if sys.argv[5] == 'crash' and step.member_spec_id == 'skill-agents-security-review-reference-security-dependencies':
        os._exit(91)
install.apply_copy_member = interrupt
from installer import main
raise SystemExit(main(['--update', '--codex-root', sys.argv[3], '--link-strategy', 'copy', sys.argv[2]]))
"""
        argv = [sys.executable, "-c", program, str(ROOT), str(self.repository), str(self.workspace), str(kit)]
        # The public entry point must configure UTF-8 even when the host uses
        # a narrow redirected code page; do not bypass it in fault injection.
        program_env = {**self.env, "PYTHONIOENCODING": "cp1252"}
        crash = subprocess.run([*argv, "crash"], env=program_env, cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(crash.returncode, 91, crash.stdout.decode() + crash.stderr.decode())
        copied = self.workspace / ".agents" / relative
        self.assertEqual(copied.read_bytes(), new)
        self.recover(self.repository, "rollback")
        self.assertEqual(copied.read_bytes(), old)
        self.assertEqual((self.repository / ".agents" / relative).read_bytes(), old)
        updated = subprocess.run([*argv, "complete"], env=program_env, cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(updated.returncode, 0, updated.stdout.decode() + updated.stderr.decode())
        self.assertEqual(copied.read_bytes(), new)
        self.assert_only_clean_transaction_history("rolled_back")

    def test_missing_or_corrupt_removal_backup_requires_human_and_preserves_copy(self):
        from installer import transaction as current
        from installer.registry import REGISTRY_BY_ID
        from unittest.mock import patch

        self.install()
        copied = self.workspace / ".agents/skills/security-review/references/workflow.md"
        before = copied.read_bytes()
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.env["XDG_CONFIG_HOME"]}):
            tx = current.TransactionRecord.new(self.repository, "uninstall")
            tx.status = "needs_human"
            steps = current.prepare_copy_members(self.repository, self.workspace, tx,
                                                 REGISTRY_BY_ID["codex-link-security-review"], operation="remove")
            step = next(item for item in steps if item.member_spec_id.endswith("security-workflow"))
            tx.external = [step]
            tx.write(self.repository)
            missing = current.recover_manual(self.repository, "roll-forward")
            self.assertEqual(missing.status, "needs_human")
            self.assertEqual(copied.read_bytes(), before)
            current.backup_copy_member(self.repository, tx, step)
            backup = self.repository / f".repo-memory-kit/tx/{tx.tx_id}/external-backups/{step.member_spec_id}"
            backup.write_bytes(b"corrupted backup")
            conflict = current.recover_manual(self.repository, "roll-forward")
            self.assertEqual(conflict.status, "needs_human")
            self.assertEqual(copied.read_bytes(), before)

    def test_workspace_reference_parent_symlink_does_not_escape(self):
        self.install()
        references = self.workspace / ".agents/skills/security-review/references"
        # Remove only the known copies created by this test fixture.
        for reference in SECURITY_REFERENCE_CATALOG.references.values():
            (self.workspace / ".agents" / reference.source_path).unlink()
        references.rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        canary = outside / "dependencies.md"
        canary.write_bytes(b"outside user data")
        try:
            references.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("host does not permit directory symlinks")
        result = subprocess.run([sys.executable, "-m", "installer", "--update", "--codex-root",
                                 str(self.workspace), "--link-strategy", "copy", str(self.repository)],
                                env=self.env, cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(canary.read_bytes(), b"outside user data")
        self.assertTrue(references.is_symlink())
        self.assertIn("路径", result.stdout.decode())

    def test_real_removal_crash_restores_before_internal_commit_and_completes_after_commit(self):
        self.install()
        user = self.workspace / ".agents/skills/security-review/user-notes.md"
        user.write_bytes(b"user notes survive both recoveries")
        program = """import os, sys
sys.path.insert(0, sys.argv[1])
import installer.uninstall as uninstall
actual_remove = uninstall.execute_external_removal
actual_commit = uninstall.commit_one
def remove(target, step, **kwargs):
    actual_remove(target, step, **kwargs)
    if sys.argv[4] == 'external' and step.member_spec_id == 'skill-agents-security-review-reference-security-dependencies':
        os._exit(92)
def commit(target, step, tx_id):
    result = actual_commit(target, step, tx_id)
    if sys.argv[4] == 'internal' and step.spec_id != 'state.manifest':
        os._exit(93)
    return result
uninstall.execute_external_removal = remove
uninstall.commit_one = commit
from installer import main
raise SystemExit(main(['--uninstall', '--codex-root', sys.argv[3], sys.argv[2]]))
"""
        argv = [sys.executable, "-c", program, str(ROOT), str(self.repository), str(self.workspace)]
        before_commit = subprocess.run([*argv, "external"], env=self.env, cwd=ROOT,
                                       capture_output=True, timeout=30)
        self.assertEqual(before_commit.returncode, 92, before_commit.stdout.decode() + before_commit.stderr.decode())
        self.recover(self.repository, "rollback")
        for reference in SECURITY_REFERENCE_CATALOG.references.values():
            self.assertEqual((self.workspace / ".agents" / reference.source_path).read_bytes(),
                             (ROOT / reference.source_path).read_bytes())
        after_commit = subprocess.run([*argv, "internal"], env=self.env, cwd=ROOT,
                                      capture_output=True, timeout=30)
        self.assertEqual(after_commit.returncode, 93, after_commit.stdout.decode() + after_commit.stderr.decode())
        self.recover(self.repository, "roll-forward")
        for reference in SECURITY_REFERENCE_CATALOG.references.values():
            self.assertFalse((self.workspace / ".agents" / reference.source_path).exists())
        self.assertEqual(user.read_bytes(), b"user notes survive both recoveries")
        self.assert_only_clean_transaction_history("failed")


if __name__ == "__main__":
    unittest.main()
