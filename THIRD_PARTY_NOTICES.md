# Third-Party Notices

Agent Engineering Kit（AEK）自身采用 MIT License。
本文件记录 AEK 中使用、改编、再分发或提供兼容集成的第三方开源项目及其许可证信息。

第三方项目根据其与 AEK 的关系分为三类：

1. **Vendored / Redistributed Components**
   第三方内容以源码、模板、Skill、资源文件或其他形式随 AEK 仓库保存，并可能由 AEK 安装器继续部署到目标项目。

2. **Adapted Materials and Concepts**
   AEK 的部分 Skill、流程或工程方法直接参考或改编自第三方开源项目，但不捆绑其完整运行时或 CLI。

3. **Optional External Integrations and Dependencies**
   AEK 可以调用、兼容或选择性依赖这些项目，但其运行时本身不作为 AEK 仓库的一部分再分发。

上游项目名称仅用于来源和归属说明，不表示其作者、维护者或组织认可、赞助或支持 AEK。

---

# 1. Vendored / Redistributed Components

## 1.1 Archify

**Upstream project:** `tt-a1i/archify`
**License:** MIT

Archify 的 vendored 快照保存在：

```text
vendor/archify/
```

AEK 安装器会将该快照中的文件部署至目标项目的：

```text
.claude/skills/archify/
.agents/skills/archify/
```

该组件用于设计流水线中的架构图、业务流程图及相关可视化产物生成。

AEK 对 vendored Archify 快照进行供应链锁定，快照完整性信息记录在：

```text
vendor/archify.manifest.json
```

Archify 上游 LICENSE 中包含以下版权归属：

```text
Copyright (c) 2026 tt-a1i (Archify)
Copyright (c) 2025 Cocoon AI
```

Archify 上游同时说明其工作包含来源于 Cocoon AI `architecture-diagram-generator` 的内容。

完整上游许可证及第三方声明随 vendored 快照保留于：

```text
vendor/archify/LICENSE
vendor/archify/THIRD_PARTY_NOTICES.md
```

如根目录本文件与 vendored 快照中的许可证或 notice 出现差异，应以上游快照中保留的原始许可证及 notice 为准。

---

# 2. Adapted Materials and Concepts

以下项目的部分开源材料、工程方法或 Skill 设计被 AEK 参考或改编。

AEK 自己实现的 Route、Gate、Memory、安装生命周期和其他确定性工具仍属于 AEK 自身实现；下面的声明用于说明其第三方来源和许可证义务。

---

## 2.1 Everything Claude Code

**Upstream project:** `affaan-m/ECC`
**License:** MIT
**Copyright:** Copyright (c) 2026 Affaan Mustafa

AEK 中参考或改编的主要思想包括：

- search-first 的复用决策；

- 分阶段验证闭环；

- 多维代码审查；

- skeptic review；

- 应用与 Agent 配置安全审查；

- 安装与升级生命周期中的部分工程方法。

AEK 中相关实现经过重新组织并与自身的确定性 Gate、Route、Memory 和交付体系整合。

---

## 2.2 mattpocock/skills

**Upstream project:** `mattpocock/skills`
**License:** MIT
**Copyright:** Copyright (c) 2026 Matt Pocock

AEK 的 TDD 相关 Skill 参考和改编了该项目中的相关材料，主要包括：

- RED / GREEN / REFACTOR 开发纪律；

- 从测试困难反向发现架构问题；

- 测试应验证可观察行为而不是实现细节的工程原则。

AEK 中对应 Skill 已根据自身的研发流程、Route、Gate 和交付机制进行了调整。

---

## 2.3 BMAD Method

**Upstream project:** `bmad-code-org/BMAD-METHOD`
**License:** MIT
**Copyright:** Copyright (c) 2025 BMad Code, LLC

AEK 参考了 BMAD Method 中与任务分析和工作拆分相关的部分方法论，包括：

- 在调查现状后选择合适的实施路径；

- 根据不确定性、影响范围、可逆性等因素调整规划深度；

- 将较大的工作划分为能够独立实施和验证的工作单元。

AEK 的以下能力属于 AEK 自身实现：

```text
Route Card
Direct / Bounded / Standard / Initiative
route-eval
deterministic route checks
```

AEK 不捆绑 BMAD Method 的运行时或 CLI。

`BMad`、`BMAD-METHOD` 及相关名称和标识的权利仍属于其各自权利人。本文件中的使用仅用于来源说明。

---

## 2.4 PR-Agent

**Upstream project:** `The-PR-Agent/pr-agent`
**License:** MIT
**Copyright:** Copyright (c) 2026 The PR Agent

AEK 的风险触发式代码审查参考了 PR-Agent 将评审维度配置为可按需启用的思路。AEK 没有
复制或捆绑 PR-Agent 运行时、模型提示、Git 平台集成或 CLI；`performance-review`、Route
overlay、证据门槛与交付裁决均为 AEK 自身实现。

---

## 2.5 Squawk

**Upstream project:** `sbdchd/squawk`
**License:** MIT OR Apache License 2.0

AEK 的数据库迁移性能审查参考了 Squawk 对 PostgreSQL 迁移风险进行确定性、可定位规则检查
的思路，包括锁、表重写和并发索引等风险类别。AEK 不捆绑 Squawk 的解析器、规则实现、CLI
或 GitHub 集成，也不把 PostgreSQL 专属规则冒充跨数据库通用结论。

---

## 2.6 PostgreSQL Documentation

**Upstream project:** PostgreSQL
**License:** PostgreSQL License
**Copyright:** Portions Copyright © 1996-2026, The PostgreSQL Global Development Group; Portions Copyright © 1994, The Regents of the University of California

AEK 的 SQL 运行证据口径参考 PostgreSQL 官方 `EXPLAIN` 文档，用于区分静态查询审查与基于
执行计划、数据基数和实际运行指标的验证。AEK 没有复制或再分发 PostgreSQL 源码、数据库
运行时或完整文档；其他数据库应使用各自原生计划与许可条款。

---

## 2.7 MIT Permission Notice for Adapted Materials

对于上述采用 MIT License、且其材料被 AEK 改编的项目，应保留对应版权声明以及 MIT License 的许可文本。

MIT License:

```text
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

# 3. Optional External Integrations and Dependencies

以下项目可被 AEK 使用或集成，但不作为对应第三方运行时随 AEK 基础仓库再分发。

用户启用这些能力时，应同时遵守相应第三方项目自己的许可证、NOTICE 及使用条件。

---

## 3.1 codebase-memory-mcp

**Upstream project:** `DeusData/codebase-memory-mcp`
**License:** MIT
**Copyright:** Copyright (c) 2025 DeusData

AEK 的 `codebase-memory` 能力可以与该项目集成，用于代码库的符号、调用关系和代码上下文分析。

AEK 可以通过可选安装流程启用该集成。

该项目的运行时和完整实现不作为 AEK 自身运行时的一部分进行 vendor 分发。

AEK 中的 `codebase-memory` Skill 主要承担与 AEK 工作流之间的集成和使用约定。

---

## 3.2 zvec

**Upstream project:** `alibaba/zvec`
**License:** Apache License 2.0

zvec 是 AEK 项目记忆语义检索层的可选依赖。

当用户启用语义检索能力时，AEK 的 `memory-recall` 可以使用 zvec 提供：

- 本地索引存储；

- Full-Text Search；

- tokenizer；

- 多字段检索；

- RRF 等结果融合能力。

zvec 不随 AEK 仓库进行 vendor 分发，用户通过独立 Python 包安装：

```text
pip install zvec
```

zvec 自身的 Apache-2.0 License、NOTICE 及其第三方依赖声明由 zvec 项目负责维护；用户安装和使用 zvec 时应同时遵守其上游许可证要求。

AEK 当前使用 zvec 的 `jieba` tokenizer 配置，但 AEK 本身没有因此直接 vendor 或直接导入独立的 `jieba` Python 项目。

---

## 3.3 GitHub Spec Kit

**Upstream project:** `github/spec-kit`
**License:** MIT

AEK 提供与 Spec Kit 项目结构和部分工件约定相关的迁移/兼容能力，例如识别已有的：

```text
spec.md
plan.md
tasks.md
.specify/specs/
```

该兼容能力主要用于将已有 Spec Kit 项目纳入 AEK 的工程治理流程。

AEK 不因该兼容入口而捆绑 Spec Kit 的 CLI 或运行时。

如未来 AEK 直接复制、修改或 vendor Spec Kit 的模板、脚本或其他实质性内容，应将对应文件路径和上游版权信息补充到本文件的 “Adapted Materials” 或 “Vendored / Redistributed Components” 章节中。

---

# 4. External Tools Used at Runtime

AEK 的某些能力还可能依赖用户环境中已经安装的外部工具，例如：

```text
Git
SVN
Node.js
Chrome / Chromium
Python
```

这些工具不是 AEK 仓库中的 vendored 第三方组件，其许可证和分发义务由各自项目负责。

仅仅调用系统已经安装的外部程序，不代表该外部程序成为 AEK 的组成部分。

---

# 5. Provenance Policy

为了避免第三方来源随着项目迭代逐渐丢失，AEK 对第三方内容遵循以下原则：

- 直接 vendor 的第三方内容必须保留上游 LICENSE 和必要的 NOTICE；

- vendor 快照应记录来源和版本或提交信息；

- 对第三方内容进行了修改时，应尽量记录修改范围；

- Skill 或文档如果明确从第三方材料改编，应在文件自身或本文件中保留来源；

- 可选外部依赖与实际 vendor 内容应明确区分；

- 仅仅受到某项目思想启发，不等同于复制了其受版权保护的实现；

- 不应仅因为两个项目采用相似方法，就推断存在派生关系；

- 新增或更新第三方内容时，应同步更新本文件。

---

# 6. Maintainer Checklist

新增第三方项目时，维护者应确认：

```text
[ ] 是否复制或修改了第三方源码、Skill、Prompt、模板或文档？
[ ] 是否将第三方内容提交到了本仓库？
[ ] 安装器是否会把该内容继续分发到用户项目？
[ ] 上游 License 是否允许当前使用方式？
[ ] 是否需要保留 copyright notice？
[ ] 是否需要保留 NOTICE？
[ ] 是否存在上游项目自身的第三方依赖 notice？
[ ] 是否记录了来源仓库和版本 / commit？
[ ] 是否明确记录了本项目的修改？
[ ] THIRD_PARTY_NOTICES.md 是否同步更新？
```

---

# 7. License Scope

AEK 自身的许可见仓库根目录：

```text
LICENSE
```

第三方项目仍然分别受其各自许可证约束。

AEK 的 MIT License 不替代、覆盖或重新许可任何第三方项目自身的许可证条款。

对于 vendored 内容，应同时阅读对应 vendor 目录中保留的上游 LICENSE 和 THIRD_PARTY_NOTICES。

---

# 8. Attribution and No Endorsement

本文件列出的项目名称、组织名称及商标仅用于：

```text
来源识别
版权归属
许可证合规
兼容性说明
```

不表示这些第三方项目、作者、维护者或组织：

```text
赞助 AEK
认可 AEK
与 AEK 存在官方合作
或为 AEK 提供任何形式的担保
```
