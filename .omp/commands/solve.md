---
description: 求解一个 open problem（/solve [problem_id]）
---
求解 open problem。参数：$ARGUMENTS。
- 参数为空：运行 `uv run python -m mathx.harvest list --status queued`，选 tractability 最小者。
- 否则 problem_id 取第一个参数。
用 task 工具后台派出 agent="solver"，task 文本：`Problem: <problem_id>. Read prompts/generation/AGENTS.md and follow it exactly. Begin with runstate init + memory init.` 派出后按项目根 AGENTS.md 的调度纪律管理其生命周期。
