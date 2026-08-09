---
description: 派 hunter 补货 open problems（/hunt [field] [quota]）
---
派 hunter agent 出去猎取 open problems。参数：$ARGUMENTS（第一个为 field，第二个为 quota）。
- field 为空：先运行 `uv run python -m mathx.harvest next-field` 取轮转领域。
- quota 为空：默认 1（一次一题，把每题 open 验证做透；自动续派补足轮次）。
- **链闸检查**：hunter 分派也走 `set-status tackling`（若用 registry 题做 hunt 目标）或直接派——自动补货前确认 `data/CHAIN_ON` 存在；若 `set-status tackling` 报 "chain gate closed"，停手只汇报（用户明确指令时用 `--force`）。
用 task 工具派出 agent=hunter：mode=harvest，inbox_file=data/inbox/<当前UTC>_hunter.jsonl。**task 调用必须包含 `"agent": "hunter"` 字段**——照此骨架构造，不得省略 agent；`name` 用 `uv run python -m mathx.agents name hunter <field>` 生成（含模型，如 `hunter-step37Flash-combinatorics`）：

```
tasks: [{
  "agent": "hunter",
  "name": "<mathx.agents name hunter <field> 输出的 name>",
  "task": "mode=harvest, field=<field>, quota=<quota>, inbox_file=data/inbox/<当前UTC>_hunter.jsonl, agent_name=<name>, model=<mathx.agents model hunter 的输出>. Follow .omp/agents/hunter.md exactly."
}]
```

派出后立即读 `history://<spawned-id>` 头部验证 agent 类型与模型；不对就 cancel 重派。hunter 返回后运行 `uv run python -m mathx.harvest ingest` 和 `uv run python -m mathx.harvest mark-hunted <field>`，并汇报本轮新增/合并数量。

## 连续补货（低水位自动续派）

hunter 不常驻——每轮跑完 quota 就 yield，由调度员结算后决定续派：

1. 触发：队列 `queued < 5` 且 `data/CHAIN_ON` 存在且无 hunter 在跑（无论 solver 是否在跑，两者角色独立）。
2. 续派：本轮 ingest + mark-hunted 后，若 `queued` 仍 < 5 且闸开 → 立即按本文件流程派下一轮 hunter（field 用 `mathx.harvest next-field` 轮转）。
3. 停止：`queued ≥ 5` / `data/STOP` 存在 / 链闸关闭 / quota gate 触发（`[quota] threshold`）→ 停手只汇报。
4. 每轮 quota 默认 1（一次一题，细验证）；水位与 quota 可在命令参数里临时调整。
