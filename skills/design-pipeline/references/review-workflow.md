# 送审与定向复审

仅在当前参与阶段需要 AI 评审时使用。人工签核走设计入口的人工评审流程。

## 准备当前材料

内部运行：

```bash
.repo-memory-kit/bin/doc-gate review-input <SDD目录> --stage <阶段> --schema-version 2 --json --precheck-summary --compact-json
```

入口在同一快照完整预检。返回失败时按位置修订并重新准备；用 `full_result_ref` 和 `result_digest` 核对需要补读的报告。有有效 request 或可读 request_ref、receipt 后才提交 Reviewer。旧 v1 CLI 与凭证沿其原契约。

- Reviewer 可直接读取同一仓库时可加 `--request-ref`。短请求仍内联。长请求由实际 Reviewer 在同目录和阶段调用 `review-input --read-request <request_ref.sha256> --record-delivery`，接收并登记原字节。纯核对用 `--read-request`。不可直接读取时内联；摘要失配、不可读或超限时按实际诊断重新准备。
- 按 ReviewService 的 `request.mode=full|incremental` 交付 `full_documents` 或 `section_documents`，加 `reference_sections` 的必要背景正文、`supporting_context` 和 `scope_proof`。沿机器选择的范围送审。缓存命中仍要交付必要正文，收件范围和原摘要保持严格绑定。
- 当前范围、上游、凭证或全局约束失配时按工具结论全文送审。旧文档没有可靠边界时按实际模式处理，标签本身不构成增量资格。收到无进展、需定位或达到上限的诊断时保留 Gate、问题和轮次，等待新相关证据再恢复。

## 交给 Reviewer

上层允许且可用时，使用未参与写作的独立 Reviewer。子 Agent 继承当前模型，只有用户明确指定时覆盖。继承不可确认或只能用同一上下文时披露，后者记录为 `same-context`，项目要求独立评审时先满足该要求。修订优先复用仍独立、模型一致的原会话；本轮必读材料、摘要及 receipt 仍要核对。

把本轮受控输入和 [评审问题与定位格式](reviewer-issues.md) 交给 Reviewer。Reviewer 只报告可行动的问题及证据，Author 定向修订，Gate 裁决。上游问题标 `upstream: true`，交维护者决定，并按实际前置变化重审受影响阶段。

## 保存结果与重审

- 保存 `reviews/<阶段>.issues.json` 和 `reviews/<阶段>.review-receipt.json`。无定位澄清时保存原 receipt；仅实际 Reviewer 可补 `issue_locations`，其余输入绑定字段保持原样。历史 occurrence 不反写，保存的 receipt 表示送审输入身份。
- Author 针对已修复问题读取当前 `issue-lifecycle.json` 的 `snapshot_digest` 和稳定 fingerprint，按实际修改后文档摘要写 `issue-claims.json`。字段保持现有格式：

```json
{"schema_version":1,"stage":"详细设计","base_snapshot_digest":"<当前 lifecycle digest>",
 "doc_hashes":{"详细设计.md":"<修改后 sha256>"},
 "claims":[{"fingerprint":"<目标 fingerprint>","state":"FIXED"}]}
```

- 重新准备当前输入并定向复核。有效 FIXED 必须经过当前范围复核才能 VERIFIED；重复命中追加 occurrence 并 REOPENED。同一生命周期快照的 PASS gate 才能派生 CLOSED。恢复时沿原问题和 `max_iterations=3` 继续，换会话、改临时 ID 或无关编辑均保留原轮次。
