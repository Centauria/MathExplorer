---
description: 派 hunter 补货 open problems（/hunt [field] [quota]）
---
派 hunter agent 出去猎取 open problems。参数：$ARGUMENTS（第一个为 field，第二个为 quota）。
- field 为空：先运行 `uv run python -m mathx.harvest next-field` 取轮转领域。
- quota 为空：默认 5。
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
