"""Requirements intent parsing, including multiline observable results."""
from pathlib import Path
import runpy
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(ROOT / 'doc-gate'))


class ScenarioIntentTests(unittest.TestCase):
    def test_heading_boundary_preserves_legacy_block_bytes_used_in_dependency_digests(self):
        text = '### F1：查询\n覆盖验收项：A1\n\n### F2：返回\n覆盖验收项：A2\n'
        self.assertEqual(GATE['_block'](text, 0), '### F1：查询\n覆盖验收项：A1\n')

    def test_multiline_nested_results_preserve_all_six_scenarios_without_changing_the_document(self):
        scenarios = '\n'.join(f'''### 场景 {number}：验证发行物

- **前置**：已生成发行物。
- **操作**：检查两种构建。
- **结果**：
  - `sdk-full` 的 `SDK-INF/lib/` 包含依赖。
  - `sdk-normal` 的 `capabilities.json` 保持正确。
''' for number in range(1, 7))
        parsed = GATE['parse_scenarios'](scenarios)
        self.assertEqual(set(parsed), {'1', '2', '3', '4', '5', '6'})
        for row in parsed.values():
            self.assertIn('SDK-INF/lib/', row['结果'])
            self.assertIn('capabilities.json', row['结果'])
        requirements = (scenarios + '\n## 验收项\n- [ ] A1：两类发行物布局正确。（场景 1）\n')
        with tempfile.TemporaryDirectory() as temporary:
            req = Path(temporary)
            path = req / '需求分析.md'
            raw = requirements.encode('utf-8')
            path.write_bytes(raw)
            # A business-flow document has numeric function headings, not scenario headings.
            (req / '业务流程设计.md').write_text(
                '## 3. 功能设计\n### 3.1 构建交付\n对应功能点：F1\n', encoding='utf-8')
            checks = GATE['run_checks'](req, '业务流程设计')
            intent = [row for row in checks.to_dict()['checks'] if row['group'] == 'intent']
            self.assertTrue(intent)
            self.assertTrue(all(row['status'] == 'PASS' for row in intent), intent)
            self.assertEqual(path.read_bytes(), raw)

    def test_inline_and_indented_paragraphs_work_but_empty_or_next_field_does_not_fill_a_result(self):
        for newline in ('\n', '\r\n'):
            text = '''### 场景 1：第一项
- **前置**：
  已有构建。
- **操作**：
  生成包。
- **结果**：首行摘要。
  - 第一条。
  - 第二条。

### 场景 2：空结果
- **前置**：已有构建。
- **操作**：检查。
- **结果**：
  -

### 场景 3：有结果
- **前置**：完成构建。
- **操作**：核验。
- **结果**：第三项结果。

## 其他章节
  这里不能填入第二项结果。
'''.replace('\n', newline)
            rows = GATE['parse_scenarios'](text)
            self.assertEqual(rows['1']['前置'], '已有构建。')
            self.assertIn('首行摘要。', rows['1']['结果'])
            self.assertIn('第二条。', rows['1']['结果'])
            self.assertEqual(rows['2']['结果'], '')
            self.assertEqual(rows['3']['结果'], '第三项结果。')

    def test_empty_first_field_cannot_borrow_the_next_field_or_unrelated_peer_paragraph(self):
        text = '''### 场景 1：空前置
- **前置**：
- **操作**：执行检查。
- **结果**：

无缩进的其他说明不能当结果。
  这是其他说明的延续。
'''
        row = GATE['parse_scenarios'](text)['1']
        self.assertEqual(row['前置'], '')
        self.assertEqual(row['结果'], '')

    def test_tab_heading_ends_the_previous_scenario_and_tab_continuations_are_valid(self):
        text = '''### 场景 1：空结果
- **前置**：已有输入。
- **操作**：执行。
- **结果**：
###\t场景 2：有效结果
- **前置**：已有输入。
- **操作**：执行。
- **结果**：
\t- 第二项有效结果。
'''
        rows = GATE['parse_scenarios'](text)
        self.assertEqual(rows['1']['结果'], '')
        self.assertEqual(rows['1']['前置'], '已有输入。')
        self.assertIn('第二项有效结果。', rows['2']['结果'])

    def test_nested_field_name_is_result_content_not_a_missing_top_level_field(self):
        text = '''### 场景 1：缺前置
- **前置**：
- **操作**：显示检查清单。
- **结果**：显示各列。
  - **前置**：这只是结果中的列名说明。
  - 可以继续检查下一步。
'''
        row = GATE['parse_scenarios'](text)['1']
        self.assertEqual(row['前置'], '')
        self.assertIn('这只是结果中的列名说明。', row['结果'])

    def test_duplicate_outer_fields_cannot_fill_a_missing_value(self):
        row = GATE['parse_scenarios']('''### 场景 1：重复结果
- **前置**：已有输入。
- **操作**：执行检查。
- **结果**：
- **结果**：后续补句不消除字段歧义。
''')['1']
        self.assertEqual(row['结果'], '')


if __name__ == '__main__':
    unittest.main()
