---
description: 派 hunter 补货 open problems（/hunt [field] [quota]）
---
派 hunter agent 出去猎取 open problems。参数：$ARGUMENTS（第一个为 field，第二个为 quota）。
- field 为空：先运行 `uv run python -m mathx.harvest next-field` 取轮转领域。
- quota 为空：默认 5。
用 task 工具派出 agent=hunter：mode=harvest，inbox_file=data/inbox/<当前UTC>_hunter.jsonl。hunter 返回后运行 `uv run python -m mathx.harvest ingest` 和 `uv run python -m mathx.harvest mark-hunted <field>`，并汇报本轮新增/合并数量。
