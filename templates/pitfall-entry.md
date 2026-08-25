---
type: pitfall
status: unverified          # confirmed | unverified | deprecated | superseded
module: <模块/服务>
created: YYYY-MM-DD
verified: YYYY-MM-DD        # 可选：最近验证日期
verified_commit: <commit>   # 可选：验证所在提交
evidence: <证据指针>         # 可选：实测记录/接口文档位置
anchors:                    # 可选：类型化定位符（class:/method:/file:/route:/config:，跨仓库加 repo@ 前缀）
  - class:com.example.OrderService
supersedes: <被本条替代的条目路径>  # 可选
conflicts_with: []          # 可选：结论冲突的条目
related: []                 # 可选：相关条目
---

# <一句话标题>

- 触发: <改动哪类代码、配置，或排查哪类问题时需要读本条>

## 现象

<可观察的错误行为>

## 根因

<定位到的根本原因，含关键代码位置（标注定位日期，行号会漂移）>

## 修复方案

<怎么修的/打算怎么修；待人工确认的方案要标注>

## 验证

<如何确认修复有效；未修复的写明修复时需覆盖的场景>

## 校验点

<供巡检比对的补充事实与巡检记录（符号锚点已在前文 frontmatter，此处放路由/文件/配置与历次巡检结论）>

- 2026-XX-XX 巡检: <结论>
