from __future__ import annotations

import hashlib
import contextlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/design-pipeline/scripts/ui-reference.py"


def chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png_bytes(width: int = 1, height: int = 1, *, color: int = 6, depth: int = 8,
              filter_type: int = 0, indexed_transparency: bool = False) -> bytes:
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    row_bytes = (width * channels * depth + 7) // 8
    raw = b"".join(bytes([filter_type]) + bytes(row_bytes) for _ in range(height))
    chunks = [chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0))]
    if color == 3:
        chunks.append(chunk(b"PLTE", b"\x00\x00\x00"))
        if indexed_transparency:
            chunks.append(chunk(b"tRNS", b"\x80"))
    chunks.extend((chunk(b"IDAT", zlib.compress(raw)), chunk(b"IEND", b"")))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def request() -> dict[str, object]:
    return {
        "schema_version": 1,
        "change_id": "CHG-001",
        "design_system": {"name": "Ant Design", "version": "5", "theme": "default",
                          "source": "user", "evidence": []},
        "coverage": [
            {"kind": "feature", "id": "F1", "screenshot_ids": ["P1"]},
            {"kind": "acceptance", "id": "A1", "screenshot_ids": ["P1"]},
        ],
        "screenshots": [{
            "id": "P1", "page": "列表", "state": "default", "purpose": "查看列表",
            "feature_ids": ["F1"], "acceptance_ids": ["A1"],
            "permissions": {"function_id": "FP01", "data_id": "DP01"},
        }],
    }


def finish_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
        process.communicate(timeout=10)


class UiReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invoke(self, *args: str, payload: object | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER), *args, "--repo-root", str(self.repo)],
            input=None if payload is None else json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

    def invoke_raw(self, *args: str, raw: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER), *args, "--repo-root", str(self.repo)],
            input=raw, capture_output=True, text=True, encoding="utf-8", check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

    @property
    def material_root(self) -> Path:
        return self.repo / "docs/01-需求/CHG-001/原始材料/ui-reference"

    def test_scaffold_creates_fixed_empty_placeholder_and_index(self) -> None:
        result = self.invoke("scaffold", payload=request())
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        index = json.loads((self.material_root / "截图索引.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schema_version"], 1)
        row = index["screenshots"][0]
        self.assertEqual(row["path"], "function/FP01/data/DP01/P1.png")
        self.assertEqual((self.material_root / row["path"]).read_bytes(), b"")
        self.assertEqual(row["placeholder_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(row["validation"]["status"], "pending")
        self.assertEqual(row["applies_to"][0]["content_review"]["status"], "unverified")

    def test_human_summary_separates_file_validity_from_visual_applicability(self) -> None:
        self.assertEqual(self.invoke('scaffold', payload=request()).returncode, 0)
        index = json.loads((self.material_root / '截图索引.json').read_text(encoding='utf-8'))
        (self.material_root / index['screenshots'][0]['path']).write_bytes(png_bytes())
        result = self.invoke('validate', '--change-id', 'CHG-001', '--format', 'human')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('P1', result.stdout)
        self.assertIn('文件：valid', result.stdout)
        self.assertIn('适用性：unverified', result.stdout)
        self.assertIn('时效：unknown', result.stdout)
        self.assertIn(str(Path('function/FP01/data/DP01/P1.png')), result.stdout)
        self.assertIn(str(self.material_root / index['screenshots'][0]['path']), result.stdout)
        self.assertIn('页面：列表；状态：default', result.stdout)
        self.assertIn('采集权限：', result.stdout)
        self.assertIn('上下文权限：', result.stdout)
        self.assertIn('DP01', result.stdout)
        self.assertIn('下一动作：VERIFY_APPLICABILITY', result.stdout)
        self.assertIn('$design-pipeline', result.stdout)
        legacy = self.invoke('validate', '--change-id', 'CHG-001')
        self.assertEqual(set(json.loads(legacy.stdout)), {'change_id', 'status', 'screenshots'})
        self.assertEqual(legacy.returncode, 1)
        self.assertEqual(set(json.loads(legacy.stdout)['screenshots'][0]),
                         {'id', 'path', 'validation', 'git_status', 'git_staging'})

    def test_v1_human_scaffold_shows_chinese_directory_scope_without_changing_default_json(self):
        value = request()
        value.update(change_id='CHG-003', requirement_dir='003-中文清单')
        result = self.invoke('scaffold', '--format', 'human', payload=value)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = self.repo / 'docs/01-需求/003-中文清单/原始材料/ui-reference/function/FP01/data/DP01/P1.png'
        self.assertIn(str(path), result.stdout)
        self.assertIn('页面：列表；状态：default', result.stdout)
        self.assertIn('采集权限：', result.stdout)
        self.assertIn('上下文权限：', result.stdout)
        self.assertIn('P1-CAPTURED · 适用性：unverified；时效：unknown', result.stdout)
        self.assertIn('下一动作：REPLACE_IMAGE', result.stdout)
        self.assertIn('PARTIAL', result.stdout)
        self.assertEqual(path.read_bytes(), b'')
        legacy = self.invoke('scaffold', payload=value)
        self.assertEqual(legacy.returncode, 0, legacy.stdout + legacy.stderr)
        payload = json.loads(legacy.stdout)
        self.assertEqual(set(payload), {'change_id', 'status', 'screenshots'})
        self.assertEqual(payload['screenshots'], [{'id': 'P1',
            'path': 'function/FP01/data/DP01/P1.png', 'status': 'pending'}])

    def test_human_projection_bounds_targets_and_contexts_without_hiding_the_gap(self):
        value = request()
        row = value['screenshots'][0]
        value['screenshots'] = [{**row, 'id': f'P{number}'} for number in range(1, 22)]
        value['screenshots'][0]['applies_to'] = [dict(context_id=f'C{number:02}',
            permissions=row['permissions'], relation='shared_layout') for number in range(1, 21)]
        result = self.invoke('scaffold', '--format', 'human', payload=value)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        targets = [line for line in result.stdout.splitlines() if line.startswith('P') and ' · ' in line]
        self.assertEqual(len(targets), 20)
        self.assertNotIn('P21 · ', result.stdout)
        self.assertNotIn('C20 · ', result.stdout)
        self.assertIn('未显示其余 1 项', result.stdout)
        self.assertIn('其余权限上下文未展开', result.stdout)
        self.assertIn('未显示不代表通过', result.stdout)
        self.assertIn('PARTIAL', result.stdout)
        original = self.invoke('scaffold', payload=value)
        self.assertEqual(original.returncode, 0)
        self.assertEqual(len(json.loads(original.stdout)['screenshots']), 21)

    def test_explicit_v2_target_validation_keeps_sibling_pending_and_returns_full_identity(self) -> None:
        value = request()
        value['requirement_dir'] = '001-中文需求'
        sibling = {**value['screenshots'][0], 'id': 'P2', 'page': '详情'}
        value['screenshots'].append(sibling)
        self.assertEqual(self.invoke('scaffold', payload=value).returncode, 0)
        root = self.repo / 'docs/01-需求/001-中文需求/原始材料/ui-reference'
        index_path = root / '截图索引.json'
        index = json.loads(index_path.read_text(encoding='utf-8'))
        (root / index['screenshots'][0]['path']).write_bytes(png_bytes())
        before_sibling = index['screenshots'][1]
        v2 = {'schema_version': 2, 'change_id': 'CHG-001', 'requirement_dir': '001-中文需求',
              'targets': ['P1'], 'expected_index_sha256': hashlib.sha256(index_path.read_bytes()).hexdigest()}
        result = self.invoke('validate', '--schema-version', '2', '--change-id', 'CHG-001',
                             '--requirement-dir', '001-中文需求', payload=v2)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['schema_version'], 2)
        self.assertEqual(output['operation'], 'validate')
        self.assertEqual(output['coverage'], 'PARTIAL')
        self.assertFalse(output['truncated'])
        self.assertEqual(output['targets'][0]['id'], 'P1')
        self.assertEqual(output['targets'][0]['file_status'], 'valid')
        self.assertEqual(output['targets'][0]['full_path'], str(root / index['screenshots'][0]['path']))
        self.assertEqual(output['targets'][0]['contexts'][0]['applicability'], 'unverified')
        self.assertTrue(any(row['id'] == 'P2' for row in output['gaps']))
        self.assertEqual(json.loads(index_path.read_text(encoding='utf-8'))['screenshots'][1], before_sibling)
        saved = index_path.read_bytes()
        conflict = self.invoke('validate', '--schema-version', '2', '--change-id', 'CHG-001',
                               '--requirement-dir', '001-中文需求', payload=v2)
        self.assertEqual(conflict.returncode, 3)
        self.assertEqual(json.loads(conflict.stdout)['status'], 'CONFLICT')
        self.assertEqual(index_path.read_bytes(), saved)

    def test_explicit_v2_cannot_silently_fall_back_to_legacy_for_null_request(self) -> None:
        self.assertEqual(self.invoke('scaffold', payload=request()).returncode, 0)
        index_path = self.material_root / '截图索引.json'
        before = index_path.read_bytes()
        result = self.invoke_raw('validate', '--schema-version', '2', '--change-id', 'CHG-001', raw='null')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['schema_version'], 2)
        self.assertEqual(json.loads(result.stdout)['status'], 'ERROR')
        self.assertEqual(index_path.read_bytes(), before)

    def test_v2_target_validation_rejects_unknown_targets_and_unsafe_sibling_paths_without_writes(self) -> None:
        value = request()
        value['screenshots'].append({**value['screenshots'][0], 'id': 'P2'})
        self.assertEqual(self.invoke('scaffold', payload=value).returncode, 0)
        index_path = self.material_root / '截图索引.json'
        index = json.loads(index_path.read_text(encoding='utf-8'))
        (self.material_root / index['screenshots'][0]['path']).write_bytes(png_bytes())
        (self.material_root / index['screenshots'][1]['path']).unlink()
        for targets in (['P99'], ['P1', 'P1'], [], ['P1'] * 33):
            with self.subTest(targets=targets):
                before = index_path.read_bytes()
                payload = {'schema_version': 2, 'change_id': 'CHG-001', 'requirement_dir': 'CHG-001',
                           'targets': targets, 'expected_index_sha256': hashlib.sha256(before).hexdigest()}
                result = self.invoke('validate', '--schema-version', '2', '--change-id', 'CHG-001', payload=payload)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(index_path.read_bytes(), before)
        before_sibling = index['screenshots'][1]
        payload.update(targets=['P1'], expected_index_sha256=hashlib.sha256(index_path.read_bytes()).hexdigest())
        result = self.invoke('validate', '--schema-version', '2', '--change-id', 'CHG-001', payload=payload)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertEqual(json.loads(index_path.read_text(encoding='utf-8'))['screenshots'][1], before_sibling)
        index = json.loads(index_path.read_text(encoding='utf-8'))
        index['screenshots'][1]['path'] = '../outside.png'
        index_path.write_text(json.dumps(index, ensure_ascii=False), encoding='utf-8')
        before = index_path.read_bytes()
        payload['expected_index_sha256'] = hashlib.sha256(before).hexdigest()
        unsafe = self.invoke('validate', '--schema-version', '2', '--change-id', 'CHG-001', payload=payload)
        self.assertEqual(unsafe.returncode, 2, unsafe.stdout + unsafe.stderr)
        self.assertEqual(index_path.read_bytes(), before)

    def test_v2_rechecks_target_bytes_after_git_diagnostic_before_publishing(self) -> None:
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('ui_reference_v2_window', HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        self.assertEqual(self.invoke('scaffold', payload=request()).returncode, 0)
        image = self.material_root / 'function/FP01/data/DP01/P1.png'
        image.write_bytes(png_bytes())
        index_path = self.material_root / '截图索引.json'
        before = index_path.read_bytes()
        payload = {'schema_version': 2, 'change_id': 'CHG-001', 'requirement_dir': 'CHG-001',
                   'targets': ['P1'], 'expected_index_sha256': hashlib.sha256(before).hexdigest()}
        def changed_by_user(*_args):
            image.write_bytes(png_bytes(width=2))
            return 'unknown', 'unknown'
        with patch.object(helper, '_git_status', side_effect=changed_by_user):
            with self.assertRaisesRegex(helper.UiReferenceError, 'screenshot changed'):
                helper._validate_command(self.repo, 'CHG-001', request_v2=payload)
        self.assertEqual(index_path.read_bytes(), before)

    def test_chinese_requirement_directory_is_bound_to_machine_identity(self) -> None:
        value = request()
        value["change_id"] = "CHG-003"
        value["requirement_dir"] = "003-中文需求名称"
        result = self.invoke("scaffold", payload=value)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        root = self.repo / "docs/01-需求/003-中文需求名称/原始材料/ui-reference"
        index_path = root / "截图索引.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(index["change_id"], "CHG-003")
        self.assertEqual(index["requirement_dir"], "003-中文需求名称")
        (root / index["screenshots"][0]["path"]).write_bytes(png_bytes())
        validated = self.invoke("validate", "--change-id", "CHG-003",
                                "--requirement-dir", "003-中文需求名称")
        self.assertEqual(validated.returncode, 1, validated.stdout + validated.stderr)
        review = {"schema_version": 1, "updates": [{
            "screenshot_id": "P1", "context_id": "P1-CAPTURED",
            "content_review": {"status": "applicable", "reviewer_type": "maintainer",
                               "reviewer": "维护者", "reviewed_at": "2026-10-12T12:00:00Z",
                               "rationale": "中文目录中的当前截图"},
            "freshness": {"status": "confirmed_current", "confirmer_type": "maintainer",
                          "confirmer": "维护者", "confirmed_at": "2026-10-12T12:00:00Z"},
        }]}
        updated = self.invoke("record-review", "--change-id", "CHG-003", "--requirement-dir",
                              "003-中文需求名称", "--expected-index-sha256",
                              hashlib.sha256(index_path.read_bytes()).hexdigest(), payload=review)
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        ready = self.invoke("validate", "--change-id", "CHG-003", "--requirement-dir", "003-中文需求名称")
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        before = index_path.read_bytes()
        wrong = self.invoke("validate", "--change-id", "CHG-002",
                            "--requirement-dir", "003-中文需求名称")
        self.assertEqual(wrong.returncode, 2)
        self.assertEqual(index_path.read_bytes(), before)
        self.assertFalse(self.material_root.exists())

    def test_requirement_directory_rejects_traversal_windows_ambiguity_and_wrong_mapping(self) -> None:
        for directory in ("../003-中文", "003-中文/child", "003-中文\\child", "C:中文",
                          "003-中文.", "CON", "003-e\u0301", "003-中文"):
            with self.subTest(directory=directory):
                value = request()
                value["requirement_dir"] = directory
                result = self.invoke("scaffold", payload=value)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((self.repo / "docs").exists())

    def test_stored_requirement_directory_cannot_be_rebound_by_copying_materials(self) -> None:
        value = request()
        value["change_id"] = "CHG-003"
        value["requirement_dir"] = "003-中文需求名称"
        self.assertEqual(self.invoke("scaffold", payload=value).returncode, 0)
        original = self.repo / "docs/01-需求/003-中文需求名称"
        copied = self.repo / "docs/01-需求/003-另一需求名称"
        shutil.copytree(original, copied)
        index = copied / "原始材料/ui-reference/截图索引.json"
        before = index.read_bytes()
        result = self.invoke("validate", "--change-id", "CHG-003", "--requirement-dir", copied.name)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("requirement_dir does not match", result.stdout)
        self.assertEqual(index.read_bytes(), before)

    def test_installed_helper_loads_kit_adapter_with_utf8_output(self) -> None:
        shutil.copytree(ROOT / "aek", self.repo / ".repo-memory-kit/lib/aek")
        value = request()
        value["change_id"] = "CHG-003"
        value["requirement_dir"] = "003-中文需求名称"
        for surface in (".agents", ".claude"):
            with self.subTest(surface=surface):
                installed = self.repo / surface / "skills/design-pipeline/scripts/ui-reference.py"
                installed.parent.mkdir(parents=True)
                shutil.copy2(HELPER, installed)
                result = subprocess.run(
                    [sys.executable, str(installed), "scaffold", "--repo-root", str(self.repo)],
                    input=json.dumps(value, ensure_ascii=False), capture_output=True,
                    text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "ascii"}, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["change_id"], "CHG-003")
        index = self.repo / "docs/01-需求/003-中文需求名称/原始材料/ui-reference/截图索引.json"
        self.assertTrue(index.is_file())

    def test_rejects_request_that_attempts_to_set_derived_path_before_writing(self) -> None:
        value = request()
        value["screenshots"][0]["path"] = "../../outside.png"  # type: ignore[index]
        result = self.invoke("scaffold", payload=value)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.material_root.exists())

    def test_rejects_duplicate_json_keys_before_creating_material_root(self) -> None:
        result = self.invoke_raw("scaffold", raw='{"schema_version":1,"schema_version":1}')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.material_root.exists())

    def test_scaffold_fails_closed_when_fixed_root_contains_symlink(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        docs = self.repo / "docs"
        docs.symlink_to(outside, target_is_directory=True)
        result = self.invoke("scaffold", payload=request())
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(outside.iterdir()), [])

    def test_validate_reports_placeholder_then_valid_png_without_claiming_visual_review(self) -> None:
        created = self.invoke("scaffold", payload=request())
        self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
        image = self.material_root / "function/FP01/data/DP01/P1.png"
        image.write_bytes(png_bytes())
        result = self.invoke("validate", "--change-id", "CHG-001")
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        index = json.loads((self.material_root / "截图索引.json").read_text(encoding="utf-8"))
        row = index["screenshots"][0]
        self.assertEqual(row["validation"]["status"], "valid")
        self.assertEqual(row["validation"]["content_sha256"], hashlib.sha256(png_bytes()).hexdigest())
        self.assertEqual(row["applies_to"][0]["content_review"]["status"], "unverified")
        self.assertIn('"git_status": "unknown"', result.stdout)
        self.assertFalse((self.material_root / ".ui-reference.lock").exists())

    def test_validate_is_idempotent_for_same_image(self) -> None:
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        image = self.material_root / "function/FP01/data/DP01/P1.png"
        image.write_bytes(png_bytes())
        first = self.invoke("validate", "--change-id", "CHG-001")
        second = self.invoke("validate", "--change-id", "CHG-001")
        self.assertEqual(first.returncode, 1, first.stderr + first.stdout)
        self.assertEqual(second.returncode, 1, second.stderr + second.stdout)
        self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))

    def test_record_review_binds_both_visual_and_freshness_decisions_to_current_png_hash(self) -> None:
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        image = self.material_root / "function/FP01/data/DP01/P1.png"
        image.write_bytes(png_bytes())
        self.assertEqual(self.invoke("validate", "--change-id", "CHG-001").returncode, 1)
        index_path = self.material_root / "截图索引.json"
        index_raw = index_path.read_bytes()
        digest = hashlib.sha256(index_raw).hexdigest()
        payload = {"schema_version": 1, "updates": [{
            "screenshot_id": "P1", "context_id": "P1-CAPTURED",
            "content_review": {"status": "applicable", "reviewer_type": "maintainer",
                               "reviewer": "k", "reviewed_at": "2026-10-12T12:00:00Z",
                               "rationale": "与目标页面/状态及采集上下文相符"},
            "freshness": {"status": "confirmed_current", "confirmer_type": "maintainer",
                          "confirmer": "k", "confirmed_at": "2026-10-12T12:00:00Z"},
        }]}
        updated = self.invoke("record-review", "--change-id", "CHG-001",
                              "--expected-index-sha256", digest, payload=payload)
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        self.assertEqual(self.invoke("validate", "--change-id", "CHG-001").returncode, 0)
        saved = json.loads(index_path.read_text(encoding="utf-8"))["screenshots"][0]["applies_to"][0]
        current = hashlib.sha256(image.read_bytes()).hexdigest()
        self.assertEqual(saved["content_review"]["reviewed_sha256"], current)
        self.assertEqual(saved["freshness"]["confirmed_sha256"], current)

    def test_record_review_with_stale_index_sha_does_not_modify_index(self) -> None:
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        index_path = self.material_root / "截图索引.json"
        before = index_path.read_bytes()
        payload = {"schema_version": 1, "updates": []}
        result = self.invoke("record-review", "--change-id", "CHG-001",
                             "--expected-index-sha256", "0" * 64, payload=payload)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(index_path.read_bytes(), before)

    def test_unready_sibling_does_not_block_review_or_binding_of_a_current_image(self) -> None:
        base = self.repo
        for missing in (False, True):
            with self.subTest(missing_sibling=missing):
                self.repo = base / ("missing" if missing else "placeholder")
                self.repo.mkdir()
                value = request()
                sibling = json.loads(json.dumps(value["screenshots"][0]))
                sibling.update(id="P2", page="详情")
                value["screenshots"].append(sibling)
                for coverage in value["coverage"]:
                    coverage["screenshot_ids"].append("P2")
                created = self.invoke("scaffold", payload=value)
                self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
                index_path = self.material_root / "截图索引.json"
                index = json.loads(index_path.read_text(encoding="utf-8"))
                (self.material_root / index["screenshots"][0]["path"]).write_bytes(png_bytes())
                if missing:
                    (self.material_root / index["screenshots"][1]["path"]).unlink()
                self.assertEqual(self.invoke("validate", "--change-id", "CHG-001").returncode, 1)
                before = json.loads(index_path.read_text(encoding="utf-8"))["screenshots"][1]
                review = {"schema_version": 1, "updates": [{
                    "screenshot_id": "P1", "context_id": "P1-CAPTURED",
                    "content_review": {"status": "applicable", "reviewer_type": "maintainer",
                        "reviewer": "k", "reviewed_at": "2026-10-15T07:00:00Z", "rationale": "当前目标图"},
                    "freshness": {"status": "confirmed_current", "confirmer_type": "maintainer",
                        "confirmer": "k", "confirmed_at": "2026-10-15T07:00:00Z"},
                }]}
                result = self.invoke("record-review", "--change-id", "CHG-001",
                    "--expected-index-sha256", hashlib.sha256(index_path.read_bytes()).hexdigest(), payload=review)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                prototype = {"schema_version": 1, "artifact_plan_id": "AP-001", "gate_digest": "a" * 64,
                    "approved_root": "docs/03-SDD/CHG-001", "prototype_refs": [{
                        "prototype_path": "ui/list.html", "prototype_sha256": "b" * 64,
                        "page_id": "PAGE1", "area_id": "AREA1", "feature_ids": ["F1"],
                        "acceptance_ids": ["A1"], "change_intent": "当前列表", "depends_on": ["P1"]}]}
                bound = self.invoke("bind-prototype", "--change-id", "CHG-001",
                    "--expected-index-sha256", hashlib.sha256(index_path.read_bytes()).hexdigest(), payload=prototype)
                self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
                saved = json.loads(index_path.read_text(encoding="utf-8"))["screenshots"]
                self.assertEqual(saved[0]["applies_to"][0]["content_review"]["status"], "applicable")
                self.assertEqual(len(saved[0]["prototype_refs"]), 1)
                self.assertEqual(saved[1], before, "local success must not change the sibling's facts")
                self.assertEqual(self.invoke("validate", "--change-id", "CHG-001").returncode, 1,
                                 "local success is not whole-change readiness")
                # Local readiness is not a path-safety exemption for siblings.
                current = json.loads(index_path.read_text(encoding="utf-8"))
                sibling_path = self.material_root / current["screenshots"][1]["path"]
                if sibling_path.exists():
                    sibling_path.unlink()
                outside = base / ("outside-missing.png" if missing else "outside-placeholder.png")
                outside.write_bytes(png_bytes(2, 1))
                sibling_path.symlink_to(outside)
                index_before = index_path.read_bytes()
                refused = self.invoke("record-review", "--change-id", "CHG-001",
                    "--expected-index-sha256", hashlib.sha256(index_before).hexdigest(), payload=review)
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertEqual(index_path.read_bytes(), index_before)
                self.assertEqual(outside.read_bytes(), png_bytes(2, 1))

    def test_target_replacement_during_update_is_rechecked_before_publication(self) -> None:
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        image = self.material_root / "function/FP01/data/DP01/P1.png"
        image.write_bytes(png_bytes())
        self.assertEqual(self.invoke("validate", "--change-id", "CHG-001").returncode, 1)
        index_path = self.material_root / "截图索引.json"
        before = index_path.read_bytes()
        prototype = {"schema_version": 1, "artifact_plan_id": "AP-001", "gate_digest": "a" * 64,
            "approved_root": "docs/03-SDD/CHG-001", "prototype_refs": [{
                "prototype_path": "ui/list.html", "prototype_sha256": "b" * 64,
                "page_id": "PAGE1", "area_id": "AREA1", "feature_ids": ["F1"],
                "acceptance_ids": ["A1"], "change_intent": "当前列表", "depends_on": ["P1"]}]}
        replacement = png_bytes(2, 1)
        # Inject at the external filesystem seam: after the first validated
        # image handle closes, before the command can publish its new index.
        wrapper = '''
import os, runpy, sys
from pathlib import Path
from unittest.mock import patch
module = runpy.run_path(sys.argv[1])
image = Path(sys.argv[2])
identity = (image.stat().st_dev, image.stat().st_ino)
replacement = bytes.fromhex(sys.argv[3])
original_close = os.close
mutated = False
def close_and_replace(fd):
    global mutated
    info = os.fstat(fd)
    target = (info.st_dev, info.st_ino) == identity
    original_close(fd)
    if target and not mutated:
        mutated = True
        image.write_bytes(replacement)
with patch.object(os, 'close', side_effect=close_and_replace):
    code = module['main'](sys.argv[4:])
assert mutated, 'fault injection did not reach the target image'
raise SystemExit(code)
'''
        result = subprocess.run([sys.executable, "-c", wrapper, str(HELPER), str(image), replacement.hex(),
            "bind-prototype", "--repo-root", str(self.repo), "--change-id", "CHG-001",
            "--expected-index-sha256", hashlib.sha256(before).hexdigest()],
            input=json.dumps(prototype, ensure_ascii=False), capture_output=True, text=True,
            encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("referenced screenshot changed", result.stdout)
        self.assertEqual(index_path.read_bytes(), before)
        self.assertEqual(image.read_bytes(), replacement)

    def test_cross_permission_shared_layout_starts_with_independent_unverified_context(self) -> None:
        value = request()
        value["screenshots"][0]["applies_to"] = [{
            "context_id": "P1-OTHER", "permissions": {"function_id": "FP02", "data_id": "DP02"},
            "relation": "shared_layout",
        }]
        result = self.invoke("scaffold", payload=value)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        row = json.loads((self.material_root / "截图索引.json").read_text(encoding="utf-8"))["screenshots"][0]
        self.assertEqual(len(row["applies_to"]), 2)
        self.assertEqual(row["applies_to"][1]["permissions"], {"function_id": "FP02", "data_id": "DP02"})
        self.assertEqual(row["applies_to"][1]["content_review"]["status"], "unverified")
        self.assertEqual(row["applies_to"][1]["freshness"]["status"], "unknown")

    def test_png_corruption_is_invalid_and_preserves_input(self) -> None:
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        image = self.material_root / "function/FP01/data/DP01/P1.png"
        invalid = b"not a png"
        image.write_bytes(invalid)
        result = self.invoke("validate", "--change-id", "CHG-001")
        self.assertEqual(result.returncode, 1)
        row = json.loads((self.material_root / "截图索引.json").read_text(encoding="utf-8"))["screenshots"][0]
        self.assertEqual(row["validation"]["status"], "invalid")
        self.assertEqual(image.read_bytes(), invalid)

    def test_png_supported_bit_depth_and_color_type_matrix(self) -> None:
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        image = self.material_root / "function/FP01/data/DP01/P1.png"
        matrix = [(0, depth) for depth in (1, 2, 4, 8, 16)]
        matrix += [(2, depth) for depth in (8, 16)]
        matrix += [(3, depth) for depth in (1, 2, 4, 8)]
        matrix += [(4, depth) for depth in (8, 16)]
        matrix += [(6, depth) for depth in (8, 16)]
        for color, depth in matrix:
            image.write_bytes(png_bytes(color=color, depth=depth,
                                        indexed_transparency=(color == 3)))
            result = self.invoke("validate", "--change-id", "CHG-001")
            self.assertEqual(result.returncode, 1, f"color={color}, depth={depth}: {result.stdout}{result.stderr}")
            row = json.loads((self.material_root / "截图索引.json").read_text(encoding="utf-8"))["screenshots"][0]
            self.assertEqual(row["validation"]["status"], "valid", f"color={color}, depth={depth}: {row}")

    def test_png_rejects_bad_filter_crc_trailing_bytes_and_oversized_row(self) -> None:
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        image = self.material_root / "function/FP01/data/DP01/P1.png"
        invalids = [
            png_bytes(filter_type=5),
            png_bytes()[:-1] + bytes([png_bytes()[-1] ^ 1]),
            png_bytes() + b"trailing",
            b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 3_000_000, 1, 16, 6, 0, 0, 0)),
        ]
        for invalid in invalids:
            image.write_bytes(invalid)
            result = self.invoke("validate", "--change-id", "CHG-001")
            self.assertEqual(result.returncode, 1)
            row = json.loads((self.material_root / "截图索引.json").read_text(encoding="utf-8"))["screenshots"][0]
            self.assertEqual(row["validation"]["status"], "invalid")

    def test_git_unavailable_reports_unknown_and_never_untracked(self) -> None:
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        result = self.invoke("validate", "--change-id", "CHG-001")
        self.assertEqual(result.returncode, 1)
        self.assertIn('"git_status": "unknown"', result.stdout)
        self.assertNotIn('"git_status": "untracked"', result.stdout)

    def test_git_tracked_screenshot_is_warned_without_changing_git_status(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        subprocess.run(["git", "-C", str(self.repo), "add", "docs/01-需求/CHG-001/原始材料/ui-reference"], check=True)
        before = subprocess.run(["git", "-C", str(self.repo), "ls-files"],
                                capture_output=True, text=True, check=True).stdout
        result = self.invoke("validate", "--change-id", "CHG-001")
        after = subprocess.run(["git", "-C", str(self.repo), "ls-files"],
                               capture_output=True, text=True, check=True).stdout
        self.assertIn('"git_status": "tracked"', result.stdout)
        self.assertEqual(before, after)

    def test_staged_image_diagnosis_is_read_only_and_uses_literal_chinese_path(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "core.filemode", "false"], check=True)
        value = request()
        value["change_id"] = "CHG-003"
        value["requirement_dir"] = "003-中文[截图]"
        self.assertEqual(self.invoke("scaffold", payload=value).returncode, 0)
        root = self.repo / "docs/01-需求/003-中文[截图]/原始材料/ui-reference"
        image = root / "function/FP01/data/DP01/P1.png"
        image.write_bytes(png_bytes())
        subprocess.run(["git", "-C", str(self.repo), "--literal-pathspecs", "add", "--",
                        image.relative_to(self.repo).as_posix()], check=True)
        before_index = (self.repo / ".git/index").read_bytes()
        before_config = (self.repo / ".git/config").read_bytes()
        result = self.invoke("validate", "--change-id", "CHG-003",
                             "--requirement-dir", "003-中文[截图]")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        row = json.loads(result.stdout)["screenshots"][0]
        self.assertEqual(row["git_status"], "tracked")
        self.assertEqual(row["git_staging"], "added")
        stored = json.loads((root / "截图索引.json").read_text(encoding="utf-8"))["screenshots"][0]
        self.assertIn("暂存", stored["git_warning"])
        self.assertEqual((self.repo / ".git/index").read_bytes(), before_index)
        self.assertEqual((self.repo / ".git/config").read_bytes(), before_config)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=UI test",
                        "-c", "user.email=ui@example.invalid", "commit", "-q", "-m", "baseline"], check=True)
        image.write_bytes(png_bytes(width=2))
        subprocess.run(["git", "-C", str(self.repo), "--literal-pathspecs", "add", "--",
                        image.relative_to(self.repo).as_posix()], check=True)
        before_index = (self.repo / ".git/index").read_bytes()
        modified = self.invoke("validate", "--change-id", "CHG-003",
                               "--requirement-dir", "003-中文[截图]")
        self.assertEqual(json.loads(modified.stdout)["screenshots"][0]["git_staging"], "modified")
        self.assertEqual((self.repo / ".git/index").read_bytes(), before_index)
        self.assertEqual((self.repo / ".git/config").read_bytes(), before_config)

    def test_concurrent_different_scaffolds_never_overwrite_the_winner_or_leave_staging(self) -> None:
        first = request()
        second = request()
        second["screenshots"][0]["id"] = "P2"  # type: ignore[index]
        second["screenshots"][0]["page"] = "详情"  # type: ignore[index]
        second["coverage"][0]["screenshot_ids"] = ["P2"]  # type: ignore[index]
        second["coverage"][1]["screenshot_ids"] = ["P2"]  # type: ignore[index]
        commands = []
        with contextlib.ExitStack() as cleanup:
            for value in (first, second):
                proc = cleanup.enter_context(subprocess.Popen(
                    [sys.executable, str(HELPER), "scaffold", "--repo-root", str(self.repo)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                ))
                cleanup.callback(finish_process, proc)
                commands.append(proc)
            outputs = [proc.communicate(json.dumps(value, ensure_ascii=False), timeout=10)
                       for proc, value in zip(commands, (first, second))]
            codes = [proc.returncode for proc in commands]
        self.assertEqual(sorted(codes), [0, 2], outputs)
        index = json.loads((self.material_root / "截图索引.json").read_text(encoding="utf-8"))
        self.assertIn(index["screenshots"][0]["id"], {"P1", "P2"})
        self.assertEqual(list(self.material_root.parent.glob(".kit-stage-*")), [])
        self.assertEqual(len(list(self.material_root.rglob("*.png"))), 1)

    def test_competing_index_writers_with_same_expected_sha_have_one_winner(self) -> None:
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        image = self.material_root / "function/FP01/data/DP01/P1.png"
        image.write_bytes(png_bytes())
        self.assertEqual(self.invoke("validate", "--change-id", "CHG-001").returncode, 1)
        index_sha = hashlib.sha256((self.material_root / "截图索引.json").read_bytes()).hexdigest()
        shared = {"screenshot_id": "P1", "context_id": "P1-CAPTURED",
                  "content_review": {"status": "applicable", "reviewer_type": "maintainer",
                                     "reviewer": "first", "reviewed_at": "2026-10-12T12:00:00Z",
                                     "rationale": "same index race"},
                  "freshness": {"status": "confirmed_current", "confirmer_type": "maintainer",
                                "confirmer": "first", "confirmed_at": "2026-10-12T12:00:00Z"}}
        one = {"schema_version": 1, "updates": [shared]}
        other_update = json.loads(json.dumps(shared))
        other_update["content_review"]["reviewer"] = "second"
        other_update["freshness"]["confirmer"] = "second"
        two = {"schema_version": 1, "updates": [other_update]}
        processes = []
        with contextlib.ExitStack() as cleanup:
            for _ in range(2):
                proc = cleanup.enter_context(subprocess.Popen(
                    [sys.executable, str(HELPER), "record-review", "--repo-root", str(self.repo),
                     "--change-id", "CHG-001", "--expected-index-sha256", index_sha],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                ))
                cleanup.callback(finish_process, proc)
                processes.append(proc)
            results = [proc.communicate(json.dumps(payload, ensure_ascii=False), timeout=10)
                       for proc, payload in zip(processes, (one, two))]
        self.assertEqual(sorted(proc.returncode for proc in processes), [0, 2], results)
        saved = json.loads((self.material_root / "截图索引.json").read_text(encoding="utf-8"))
        self.assertIn(saved["screenshots"][0]["applies_to"][0]["content_review"]["reviewer"], {"first", "second"})

    def test_bind_prototype_stores_verified_identity_without_reading_prototype(self) -> None:
        self.assertEqual(self.invoke("scaffold", payload=request()).returncode, 0)
        image = self.material_root / "function/FP01/data/DP01/P1.png"
        image.write_bytes(png_bytes())
        self.assertEqual(self.invoke("validate", "--change-id", "CHG-001").returncode, 1)
        index_sha = hashlib.sha256((self.material_root / "截图索引.json").read_bytes()).hexdigest()
        prototype = {"schema_version": 1, "artifact_plan_id": "AP-001", "gate_digest": "a" * 64,
                     "approved_root": "docs/03-SDD/CHG-001",
                     "prototype_refs": [{"prototype_path": "ui/list.html", "prototype_sha256": "b" * 64,
                                         "page_id": "PAGE1", "area_id": "AREA1", "feature_ids": ["F1"],
                                         "acceptance_ids": ["A1"], "change_intent": "展示列表",
                                         "depends_on": ["P1", "FP01", "TOKEN1"]}]}
        result = self.invoke("bind-prototype", "--change-id", "CHG-001",
                             "--expected-index-sha256", index_sha, payload=prototype)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        index = json.loads((self.material_root / "截图索引.json").read_text(encoding="utf-8"))
        ref = index["screenshots"][0]["prototype_refs"][0]
        self.assertEqual(ref["prototype_sha256"], "b" * 64)
        self.assertFalse((self.repo / "docs/03-SDD/CHG-001/ui/list.html").exists())
        index_path = self.material_root / "截图索引.json"
        before = index_path.read_bytes()
        repeated = self.invoke("bind-prototype", "--change-id", "CHG-001",
            "--expected-index-sha256", hashlib.sha256(before).hexdigest(), payload=prototype)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(index_path.read_bytes(), before, "retry must not append duplicate bindings")
        prototype["prototype_refs"][0]["prototype_sha256"] = "c" * 64
        updated = self.invoke("bind-prototype", "--change-id", "CHG-001",
            "--expected-index-sha256", hashlib.sha256(before).hexdigest(), payload=prototype)
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        current_refs = json.loads(index_path.read_text(encoding="utf-8"))["screenshots"][0]["prototype_refs"]
        self.assertEqual(len(current_refs), 1)
        self.assertEqual(current_refs[0]["prototype_sha256"], "c" * 64)

    def test_explicit_v2_binding_keeps_same_page_area_contexts_separate_and_retries_idempotent(self):
        value = request()
        value['screenshots'][0]['applies_to'] = [{'context_id': 'CTX-OTHER',
            'permissions': {'function_id': 'FP01', 'data_id': 'DP02'}, 'relation': 'shared_layout'}]
        self.assertEqual(self.invoke('scaffold', payload=value).returncode, 0)
        (self.material_root / 'function/FP01/data/DP01/P1.png').write_bytes(png_bytes())
        validated = self.invoke('validate', '--change-id', 'CHG-001')
        self.assertEqual(validated.returncode, 1, validated.stdout + validated.stderr)
        self.assertEqual(json.loads(validated.stdout)['screenshots'][0]['validation'], 'valid')
        ref = {'prototype_path': 'ui/list.html', 'prototype_sha256': 'b' * 64,
            'page_id': 'PAGE-1', 'area_id': 'AREA-1', 'state_id': 'default', 'context_id': 'P1-CAPTURED',
            'feature_ids': ['F1'], 'acceptance_ids': ['A1'], 'change_intent': '当前列表', 'depends_on': ['P1']}
        payload = {'schema_version': 2, 'artifact_plan_id': 'AP-001', 'gate_digest': 'a' * 64,
            'approved_root': 'docs/03-SDD/CHG-001', 'prototype_refs': [ref, {**ref, 'context_id': 'CTX-OTHER'}]}
        path = self.material_root / '截图索引.json'
        def bind(request):
            return self.invoke('bind-prototype', '--change-id', 'CHG-001',
                '--expected-index-sha256', hashlib.sha256(path.read_bytes()).hexdigest(), payload=request)
        result = bind(payload)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before = path.read_bytes()
        repeated = bind(payload)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(path.read_bytes(), before)
        ref['prototype_sha256'] = 'c' * 64
        changed = bind({**payload, 'prototype_refs': [ref]})
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        refs = json.loads(path.read_bytes())['screenshots'][0]['prototype_refs']
        self.assertEqual(len(refs), 2)
        self.assertEqual({row['context_id']: row['prototype_sha256'] for row in refs},
                         {'P1-CAPTURED': 'c' * 64, 'CTX-OTHER': 'b' * 64})
        self.assertTrue(all(row['schema_version'] == 2 for row in refs))
        for mutation in ({'state_id': 'loading'}, {'context_id': 'MISSING'}, {'depends_on': ['P1', 'P99']}):
            before = path.read_bytes()
            rejected = bind({**payload, 'prototype_refs': [{**ref, **mutation}]})
            self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
