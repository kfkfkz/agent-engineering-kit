from __future__ import annotations

import hashlib
import json
import os
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
            capture_output=True, text=True, check=False,
        )

    def invoke_raw(self, *args: str, raw: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER), *args, "--repo-root", str(self.repo)],
            input=raw, capture_output=True, text=True, check=False,
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

    def test_concurrent_different_scaffolds_never_overwrite_the_winner_or_leave_staging(self) -> None:
        first = request()
        second = request()
        second["screenshots"][0]["id"] = "P2"  # type: ignore[index]
        second["screenshots"][0]["page"] = "详情"  # type: ignore[index]
        second["coverage"][0]["screenshot_ids"] = ["P2"]  # type: ignore[index]
        second["coverage"][1]["screenshot_ids"] = ["P2"]  # type: ignore[index]
        commands = []
        for value in (first, second):
            commands.append(subprocess.Popen(
                [sys.executable, str(HELPER), "scaffold", "--repo-root", str(self.repo)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            ))
        outputs = [proc.communicate(json.dumps(value, ensure_ascii=False)) for proc, value in zip(commands, (first, second))]
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
        processes = [subprocess.Popen(
            [sys.executable, str(HELPER), "record-review", "--repo-root", str(self.repo),
             "--change-id", "CHG-001", "--expected-index-sha256", index_sha],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for _ in range(2)]
        results = [proc.communicate(json.dumps(payload)) for proc, payload in zip(processes, (one, two))]
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


if __name__ == "__main__":
    unittest.main()
