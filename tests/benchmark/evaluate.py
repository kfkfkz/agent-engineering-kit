#!/usr/bin/env python3
"""Agent 行为评测引擎——L2（流程遵守）+ L3（交付质量）。

评测模型（三层）：
  L1 组件测试 = 现有六套件（脚本/CLI/安装器是否正确）
  L2 流程遵守 = Agent 是否按 kit 的流程工作（本引擎度量）
  L3 交付质量 = Agent 装了 kit 后代码质量是否更好（本引擎度量）

核心指标：
  - Task Correctness：任务是否完成（测试通过、功能正确）
  - Evidence Completeness：是否有设计文档/测试方案/验证回执
  - Change Precision：是否只改了该改的文件（precision = expected∩actual / actual）
  - Memory Utilization：是否检索并引用了相关记忆

全部机械判断（无 LLM judge）——检查文件存在性、diff 范围、测试结果、
回执内容、记忆引用。这保持与 kit 的"从玄学 Prompt 变成可测试软件工程"一致。

用法：
  # 单场景评测（Agent 完成任务后运行）
  python3 tests/benchmark/evaluate.py --repo /path/to/agent-output --scenario crud-add-field

  # paired 对比（vanilla vs kit）
  python3 tests/benchmark/evaluate.py --compare /path/to/vanilla /path/to/kit --scenario crud-add-field
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ══════════════════════════ 数据模型 ══════════════════════════

@dataclass
class Scenario:
    """一个评测场景的定义。"""
    id: str
    name: str
    description: str
    # 变更范围（Change Precision）
    allowed_paths: list[str] = field(default_factory=list)    # 允许修改的路径 glob
    forbidden_paths: list[str] = field(default_factory=list)  # 禁止修改的路径 glob
    # 证据链（Evidence Completeness）
    expected_artifacts: list[str] = field(default_factory=list)  # 必须存在的文件
    expected_receipt: bool = False                               # 是否需要交付回执
    # 测试（Task Correctness）
    test_command: str = ""                                        # 验证命令
    # 记忆（Memory Utilization）
    memory_entry_path: str = ""           # 场景相关记忆条目路径（agent 应检索到）
    memory_keyword: str = ""               # 决策一致性关键词（diff 中应体现）


@dataclass
class BenchmarkResult:
    """一个场景的评测结果。"""
    scenario_id: str
    # L3: Task Correctness
    task_success: bool = False
    task_detail: str = ""
    # L2+L3: Evidence Completeness
    has_design_doc: bool = False
    has_test_plan: bool = False
    has_receipt: bool = False
    has_regression_test: bool = False
    evidence_score: float = 0.0    # 0-1（4 项中命中的比例）
    # L3: Change Precision
    actual_changed: list[str] = field(default_factory=list)
    unexpected_changes: list[str] = field(default_factory=list)
    forbidden_violations: list[str] = field(default_factory=list)
    precision_score: float = 0.0   # 0-1
    # L2: Memory Utilization
    memory_referenced: bool = False
    memory_consistent: bool = False
    memory_score: float = 0.0      # 0-1

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario_id,
            "task_correctness": {"success": self.task_success, "detail": self.task_detail},
            "evidence_completeness": {
                "score": self.evidence_score,
                "has_design_doc": self.has_design_doc,
                "has_test_plan": self.has_test_plan,
                "has_receipt": self.has_receipt,
                "has_regression_test": self.has_regression_test,
            },
            "change_precision": {
                "score": self.precision_score,
                "actual_changed": self.actual_changed,
                "unexpected_changes": self.unexpected_changes,
                "forbidden_violations": self.forbidden_violations,
            },
            "memory_utilization": {
                "score": self.memory_score,
                "referenced": self.memory_referenced,
                "consistent": self.memory_consistent,
            },
        }


# ══════════════════════════ 场景定义 ══════════════════════════

def load_scenarios() -> list[Scenario]:
    """内置评测场景（后续可从 YAML/JSON 文件加载）。"""
    return [
        Scenario(
            id="crud-add-field",
            name="为实体增加字段并提供查询接口",
            description="给 User 增加 status 字段（SMALLINT），提供查询接口，含持久化",
            allowed_paths=[
                "src/**/user/**", "src/**/model/**", "src/**/api/**",
                "test/**/user/**", "test/**/model/**",
                "docs/01-需求/**", "docs/delivery-receipts/**",
            ],
            forbidden_paths=[
                "pom.xml", "build.gradle", "infrastructure/**",
                "src/**/security/**", "src/**/auth/**",
                "src/**/billing/**",  # 无关业务模块
            ],
            expected_artifacts=[
                "docs/01-需求/*/需求.md",
                "docs/01-需求/*/测试方案.md",
            ],
            expected_receipt=True,
            test_command="python -m pytest test/ -x -q",
            memory_entry_path="docs/memory/*/2026-*schema*.md",
            memory_keyword="SMALLINT",
        ),
        Scenario(
            id="bug-fix-regression",
            name="修复已知 bug 并建立回归测试",
            description="修复并发场景下幂等判断与业务操作不在同一事务边界的 bug",
            allowed_paths=[
                "src/**/service/**", "src/**/handler/**",
                "test/**",
                "docs/01-需求/**", "docs/delivery-receipts/**",
            ],
            forbidden_paths=[
                "pom.xml", "infrastructure/**",
                "src/**/billing/**",
            ],
            expected_artifacts=[
                "docs/01-需求/*/需求.md",
            ],
            expected_receipt=True,
            test_command="python -m pytest test/ -x -q",
            memory_entry_path="docs/memory/*/2026-*事务*.md",
            memory_keyword="幂等",
        ),
        Scenario(
            id="refactor-no-scope-creep",
            name="小范围重构不扩大影响面",
            description="重构通知发送模块的错误处理，统一异常类型——不改外部 API",
            allowed_paths=[
                "src/**/notification/**", "src/**/notify/**",
                "test/**/notification/**", "test/**/notify/**",
                "docs/delivery-receipts/**",
            ],
            forbidden_paths=[
                "src/**/user/**", "src/**/billing/**", "src/**/security/**",
                "src/**/api/**",  # 不应改外部 API
                "pom.xml",
            ],
            expected_artifacts=[],
            expected_receipt=False,  # 小改动，lightweight 下 inline 即可
            test_command="python -m pytest test/ -x -q",
        ),
    ]


# ══════════════════════════ 评测引擎 ══════════════════════════

def _glob_match(path: str, pattern: str) -> bool:
    """glob 匹配（支持 **）。"""
    import fnmatch
    import re
    if "**" in pattern:
        escaped = re.escape(pattern)
        escaped = escaped.replace(r"\*\*/", "(?:[^/]*/)*")
        escaped = escaped.replace(r"\*\*", ".*")
        escaped = escaped.replace(r"\*", "[^/]*")
        return re.fullmatch(escaped, path) is not None
    return fnmatch.fnmatch(path, pattern)


def _get_changed_files(repo: Path, base: str | None = None) -> list[str]:
    """获取 git 变更的文件列表。

    base 给定时：diff base..HEAD + 工作区（多提交任务不漏改动）。
    缺省：工作区 + 最近一次提交（旧行为）。
    """
    files = set()
    cmds = [["git", "-C", str(repo), "diff", "--name-only", "HEAD"]]
    if base:
        cmds.append(["git", "-C", str(repo), "diff", "--name-only", f"{base}..HEAD"])
    else:
        cmds.append(["git", "-C", str(repo), "diff", "--name-only", "HEAD~1..HEAD"])
    for cmd in cmds:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                for l in result.stdout.split("\n"):
                    if l.strip():
                        files.add(l.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return sorted(files)


def _new_test_files(repo: Path, base: str | None = None) -> list[str]:
    """diff 中**新增**的测试文件（不是任何名字带 test 的文件——
    P2 修复：存量测试文件与『测试方案.md』类文档不算回归测试）。"""
    import re
    new_files: list[str] = []
    for ref in ([f"{base}..HEAD"] if base else ["HEAD~1..HEAD"]):
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=A", ref],
                capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                new_files += [l.strip() for l in r.stdout.split("\n") if l.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=A", "HEAD"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            new_files += [l.strip() for l in r.stdout.split("\n") if l.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    pattern = re.compile(r"(^|/)(tests?)(/|_)|[_-]test\.[a-z]+$|^test_|Test\.[a-z]+$")
    return [f for f in set(new_files) if pattern.search(f)]


def _check_artifacts(repo: Path, patterns: list[str]) -> tuple[bool, list[str]]:
    """检查期望的 artifact 是否存在（rglob 支持多级 + 中英目录名兼容）。"""
    found = []
    for pattern in patterns:
        # 尝试原模式
        matches = list(repo.rglob(pattern.split("/")[-1]))
        # 过滤路径前缀匹配
        prefix_parts = [p for p in pattern.split("/")[:-1] if p != "*"]
        for m in matches:
            rel = str(m.relative_to(repo))
            parts = rel.split("/")
            # 检查前缀部分是否匹配（支持中英文目录名）
            if len(parts) >= len(prefix_parts):
                ok = True
                for i, pp in enumerate(prefix_parts):
                    if pp.startswith("01-"):
                        # 兼容 01-需求 / 01-requirements
                        if not parts[i].startswith("01-"):
                            ok = False; break
                    elif pp != parts[i] and pp != "*":
                        ok = False; break
                if ok:
                    found.append(rel)
    return bool(found), found


def evaluate_change_precision(
        changed_files: list[str], scenario: Scenario) -> tuple[float, list[str], list[str]]:
    """计算 Change Precision。

    precision = allowed∩actual / actual
    同时检查 forbidden violations。
    无变更 = 0.0（P2 修复：代码任务零改动不是完美精准，是无产出——
    此前误给 1.0）。
    """
    if not changed_files:
        return 0.0, [], []  # 无变更记录 = 无产出（非满分）

    allowed = []
    unexpected = []
    forbidden = []

    for f in changed_files:
        is_allowed = any(_glob_match(f, p) for p in scenario.allowed_paths)
        is_forbidden = any(_glob_match(f, p) for p in scenario.forbidden_paths)
        if is_forbidden:
            forbidden.append(f)
        elif is_allowed:
            allowed.append(f)
        else:
            unexpected.append(f)

    precision = len(allowed) / len(changed_files)
    return precision, unexpected, forbidden


def evaluate_evidence_chain(repo: Path, scenario: Scenario,
                            base: str | None = None) -> dict[str, bool]:
    """检查证据链完整性。"""
    checks = {
        "has_design_doc": False,
        "has_test_plan": False,
        "has_receipt": False,
        "has_regression_test": False,
    }

    # 设计文档
    if scenario.expected_artifacts:
        found, _ = _check_artifacts(repo, [
            p for p in scenario.expected_artifacts if "需求" in p or "设计" in p])
        checks["has_design_doc"] = found

    # 测试方案
    found, _ = _check_artifacts(repo, [
        p for p in scenario.expected_artifacts if "测试" in p or "test" in p.lower()])
    checks["has_test_plan"] = found

    # 交付回执
    if scenario.expected_receipt:
        found, _ = _check_artifacts(repo, ["docs/delivery-receipts/*.md"])
        checks["has_receipt"] = found

    # 回归测试（diff 中**新增**的测试文件——P2 修复：不是任何名字含 test 的文件）
    checks["has_regression_test"] = bool(_new_test_files(repo, base))

    return checks


def evaluate_memory_usage(
        repo: Path, scenario: Scenario,
        base: str | None = None) -> tuple[bool, bool]:
    """检查记忆引用：与场景**期望的记忆条目**求交（第七轮审计 P2：引用任意
    无关条目即满分是骗分）——referenced = 引用了场景期望的条目且其存在；
    consistent = 命中条目正文含决策关键词，且变更文件中也体现。"""
    if not scenario.memory_entry_path:
        return False, False

    import glob as _glob
    import re as _re

    # 场景期望的条目（glob 展开为磁盘上存在的集合）
    expected = {str(p.relative_to(repo)).replace(os.sep, "/")
                for p in _glob.glob(str(repo / scenario.memory_entry_path),
                                    recursive=True)}

    receipt_files = list((repo / "docs" / "delivery-receipts").glob("*.md")) if \
        (repo / "docs" / "delivery-receipts").is_dir() else []
    design_files = list((repo / "docs" / "01-需求").rglob("*.md")) if \
        (repo / "docs" / "01-需求").is_dir() else []

    referenced = False
    matched_entries: list[Path] = []
    for f in receipt_files + design_files:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for m in _re.finditer(r"docs/memory/[^\s\)\]】》\|，,；;]+\.md", text):
            rel = m.group(0)
            if rel in expected:
                referenced = True
                p = repo / rel
                if p.is_file() and p not in matched_entries:
                    matched_entries.append(p)

    consistent = False
    keyword = scenario.memory_keyword
    if referenced and keyword:
        # 关键词须出现在**命中的记忆条目正文**（决策确实来自该记忆）
        in_entry = any(keyword in p.read_text(encoding="utf-8", errors="ignore")
                       for p in matched_entries)
        if in_entry:
            for cf in _get_changed_files(repo, base):
                full = repo / cf
                if full.is_file():
                    try:
                        if keyword in full.read_text(encoding="utf-8"):
                            consistent = True
                            break
                    except (OSError, UnicodeError):
                        pass

    return referenced, consistent


def run_tests(repo: Path, command: str) -> tuple[bool, str]:
    """运行验证命令。"""
    if not command:
        return True, "no test command"
    try:
        result = subprocess.run(
            command, shell=True, cwd=str(repo),
            capture_output=True, text=True, timeout=120)
        return result.returncode == 0, result.stdout[-200:] if result.stdout else ""
    except subprocess.TimeoutExpired:
        return False, "test timeout"
    except Exception as e:
        return False, str(e)


def evaluate_scenario(repo: Path, scenario: Scenario,
                       base: str | None = None) -> BenchmarkResult:
    """对一个场景做完整评测。"""
    result = BenchmarkResult(scenario_id=scenario.id)

    # Task Correctness
    success, detail = run_tests(repo, scenario.test_command)
    result.task_success = success
    result.task_detail = detail

    # Evidence Completeness
    evidence = evaluate_evidence_chain(repo, scenario, base)
    result.has_design_doc = evidence["has_design_doc"]
    result.has_test_plan = evidence["has_test_plan"]
    result.has_receipt = evidence["has_receipt"]
    result.has_regression_test = evidence["has_regression_test"]
    result.evidence_score = sum(evidence.values()) / 4 if evidence else 0

    # Change Precision
    changed = _get_changed_files(repo, base)
    result.actual_changed = changed
    precision, unexpected, forbidden = evaluate_change_precision(changed, scenario)
    result.precision_score = precision
    result.unexpected_changes = unexpected
    result.forbidden_violations = forbidden

    # Memory Utilization
    referenced, consistent = evaluate_memory_usage(repo, scenario, base)
    result.memory_referenced = referenced
    result.memory_consistent = consistent
    result.memory_score = (int(referenced) + int(consistent)) / 2 if scenario.memory_keyword else 0

    return result


# ══════════════════════════ 对比报告 ══════════════════════════

def compare_results(vanilla: BenchmarkResult, kit: BenchmarkResult) -> str:
    """生成 paired 对比报告。"""
    lines = [
        f"{'':30s} {'Vanilla':>10s} {'With Kit':>10s} {'Delta':>10s}",
        "=" * 65,
        f"{'Task Correctness':30s} {'✓' if vanilla.task_success else '✗':>10s} "
        f"{'✓' if kit.task_success else '✗':>10s}",
        f"{'Evidence Completeness':30s} {vanilla.evidence_score:>10.0%} "
        f"{kit.evidence_score:>10.0%} "
        f"{kit.evidence_score - vanilla.evidence_score:>+10.0%}",
        f"{'Change Precision':30s} {vanilla.precision_score:>10.0%} "
        f"{kit.precision_score:>10.0%} "
        f"{kit.precision_score - vanilla.precision_score:>+10.0%}",
        f"{'Memory Utilization':30s} {vanilla.memory_score:>10.0%} "
        f"{kit.memory_score:>10.0%} "
        f"{kit.memory_score - vanilla.memory_score:>+10.0%}",
        "=" * 65,
    ]
    if kit.forbidden_violations:
        lines.append(f"⚠ Forbidden violations (kit): {kit.forbidden_violations}")
    if vanilla.forbidden_violations:
        lines.append(f"⚠ Forbidden violations (vanilla): {vanilla.forbidden_violations}")
    if kit.unexpected_changes:
        lines.append(f"⚠ Unexpected changes (kit): {kit.unexpected_changes[:5]}")
    if vanilla.unexpected_changes:
        lines.append(f"⚠ Unexpected changes (vanilla): {vanilla.unexpected_changes[:5]}")
    return "\n".join(lines)


# ══════════════════════════ CLI ══════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Agent 行为评测引擎")
    ap.add_argument("--repo", help="Agent 完成任务后的仓库路径")
    ap.add_argument("--scenario", help="场景 ID（crud-add-field / bug-fix-regression / refactor-no-scope-creep）")
    ap.add_argument("--compare", nargs=2, metavar=("VANILLA", "KIT"),
                    help="对比两个仓库的结果")
    ap.add_argument("--base", help="diff 基线 ref（多提交任务不漏改动；缺省=工作区+最近提交）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = ap.parse_args()

    scenarios = {s.id: s for s in load_scenarios()}

    if args.compare:
        vanilla_repo, kit_repo = Path(args.compare[0]), Path(args.compare[1])
        scenario = scenarios.get(args.scenario or "crud-add-field")
        if not scenario:
            print(f"✗ 未知场景: {args.scenario}")
            return 1
        v_result = evaluate_scenario(vanilla_repo, scenario, args.base)
        k_result = evaluate_scenario(kit_repo, scenario, args.base)
        print(compare_results(v_result, k_result))
        return 0

    if not args.repo:
        print("用法: evaluate.py --repo <path> --scenario <id>")
        print(f"可用场景: {', '.join(scenarios.keys())}")
        return 1

    scenario = scenarios.get(args.scenario or "crud-add-field")
    if not scenario:
        print(f"✗ 未知场景: {args.scenario}")
        return 1

    result = evaluate_scenario(Path(args.repo), scenario, args.base)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"\n场景: {scenario.id} — {scenario.name}")
        print(f"{'='*50}")
        print(f"Task Correctness:    {'✓' if result.task_success else '✗'} {result.task_detail[:50]}")
        print(f"Evidence Score:      {result.evidence_score:.0%}")
        print(f"  design_doc: {result.has_design_doc}  test_plan: {result.has_test_plan}  "
              f"receipt: {result.has_receipt}  regression: {result.has_regression_test}")
        print(f"Change Precision:   {result.precision_score:.0%}")
        if result.unexpected_changes:
            print(f"  unexpected: {result.unexpected_changes[:5]}")
        if result.forbidden_violations:
            print(f"  ⚠ FORBIDDEN: {result.forbidden_violations}")
        print(f"Memory Score:        {result.memory_score:.0%}")
        print(f"  referenced: {result.memory_referenced}  consistent: {result.memory_consistent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
