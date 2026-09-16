# Third-Party Notices

本项目没有在基础安装中捆绑下列项目的运行时或 CLI，但部分技能流程参考并改编了其 MIT 许可材料。

## Everything Claude Code

- Source: <https://github.com/affaan-m/ECC>
- Copyright (c) 2026 Affaan Mustafa
- License: MIT
- Adapted concepts: search-first 复用决策、分阶段验证闭环、多维代码审查与 skeptic 复核、应用与 Agent 配置安全审查、安装生命周期设计。

## mattpocock/skills

- Source: <https://github.com/mattpocock/skills>
- Copyright (c) 2026 Matt Pocock
- License: MIT
- Adapted concepts: TDD 的 RED/GREEN/REFACTOR 纪律，以及从测试摩擦识别架构问题。

## BMAD Method

- Source: <https://github.com/bmad-code-org/BMAD-METHOD>
- Copyright (c) 2025 BMad Code, LLC
- License: MIT
- Adapted concepts: 调查后选择最小安全路径、把意图缺口/可逆性/影响范围用于规划深度，
  以及将较大工作拆成可交付实施单元。AEK 的 Route Card、四级任务路线和确定性
  `route-eval` 是本项目自己的实现，不包含 BMAD 运行时或 CLI。

完整的本项目许可见 [LICENSE](LICENSE)。上游名称仅用于归属说明，不表示上游作者为本项目背书。

## archify

- 来源：https://github.com/tt-a1i/archify
- 许可：MIT（上游 LICENSE 与 THIRD_PARTY_NOTICES.md 随 vendor 快照保留于 vendor/archify/）
- 用途：设计流水线的业务流程图/架构图渲染（vendor 快照锁定，安装器逐文件部署）
