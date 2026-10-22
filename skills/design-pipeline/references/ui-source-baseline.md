# 源码与区域设计基线

仅当前 UI 原型确需绑定组件、布局或设计令牌源码时读取；不是所有任务的必需文档。沿用 UI 设计、原型审查和原确认，不增加审批或独立 Gate。

先按现有 Memory/codebase 流程定位实际来源，再定向核实；不要从用户报出的框架名猜源码事实。把派生数据放在现有 UI设计.md 全局 `## 设计基线` 节的一个 `aek-ui-baseline` fenced JSON 区块中，显式 schema_version=2；不要嵌入单个 PAGE 章节，让共享来源表的修改无谓改变该页正文摘要。旧截图索引、原型清单及历史确认字段不变。

## 数据格式

```aek-ui-baseline
{
  "schema_version": 2,
  "sources": [{
    "id": "SRC-1",
    "kind": "token",
    "path": "frontend/theme.css",
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "certainty": "source_backed"
  }],
  "regions": [{
    "page_id": "PAGE-1", "area_id": "AREA-1", "region_id": "QUERY",
    "intent": "PRESERVE",
    "feature_ids": ["F1"], "acceptance_ids": ["A1"],
    "source_ids": ["SRC-1"], "screenshot_ids": ["P1"],
    "rationale": "保留现有查询区样式",
    "adjacent_context": null
  }]
}
```

示例摘要不是实际来源，不得照抄。path 只列本任务授权且相关的仓库相对文件，不能加入凭证或环境秘密。程序在原安全句柄边界回读实际字节，不执行源码；不接受绝对路径、穿越、隐藏路径、链接或特殊文件。

- kind 为 component / token / layout；certainty 为 source_backed / inferred / unknown。source_backed 仅表示来源可回读并绑定内容，不机械证明所有语义推断。推断/未知保持待核验，不能仅因哈希相等就升级成事实。
- 区域 identity 使用当前 PAGE/AREA 及稳定 region ID；F/A、截图引用必须属于该原型，不能反向扩大上游范围。source_ids 只引用本区依赖，共享 Token 列一次、多消费者分别引用；不要让全部页面依赖全部文件。
- 状态/权限专属来源在区域上同时填写 `state_id` 和 `context_id`，值须匹配实际原型及所选截图；此时 identity 为 PAGE/AREA/STATE/context/region。相同区域可在不同状态分别绑定组件来源。同一 PAGE/AREA 不混用专属行和旧共享行，避免隐式回退到其他状态或权限。旧行仍表示共享布局，不表示共享权限或批准。
- intent 为 PRESERVE / MODIFY / NEW / REMOVE。PRESERVE/MODIFY 需已有截图；NEW/REMOVE 可没有前态截图，但必须关联需求并明确 adjacent_context，不伪造前态。
- 已使用截图观察的区域增加 `"observations": [{"evidence_id":"P1","sha256":"实际观察内容摘要"}]`。引用当前截图的内容寻址观察，不填文件路径或批准字段；region_id 必须在该观察中为 observed，inferred/unknown 不算覆盖。每区同一证据只列一次，整个区块最多128条引用。未使用观察的旧区域可不加该字段，不批量改写历史数据。
- 区块一旦使用，目标必须与当前原型集合一致；专属模式需覆盖各 STATE/context，删目标行不能隐藏缺口。当前防护限额为128来源、32区域、JSON64KiB、单来源1MiB及集合8MiB；超限缩小真实相关来源，不能删除依赖冒充完整。

## 验证与变更

现有 UI 阶段检查回读源文件，基线摘要加入原型原有的联合依赖摘要。来源变化、未核实、缺失或不安全均报告待复核；只影响引用该来源的区域，无关区域保留。更新声明的 SHA 不是人工批准，旧确认不能覆盖新联合摘要。

观察引用同样进入该区域的联合摘要。门禁只读回核当前项目/需求域、实际截图、原权限适用性、受管提取契约及当前绑定；不可覆盖文件仍存在，不代表撤销后仍可复用。返回依赖摘要前再核对，变化只使消费者待复核；不会补建缺失观察或重跑模型。更新观察声明仍需原来的真实人工确认，不修改旧凭证来“同步”。

此区块不能取代截图视觉适用性或人工确认，也不证明上游功能覆盖完整或整版集成已通过。
