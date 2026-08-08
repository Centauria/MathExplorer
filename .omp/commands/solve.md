---
description: 求解一个 open problem（/solve [problem_id]）
---
求解 open problem。参数：$ARGUMENTS。
- 参数为空：运行 `uv run python -m mathx.harvest list --status queued`，选 tractability 最小者。
- 否则 problem_id 取第一个参数。
- 分派前先 `uv run python -m mathx.harvest set-status <problem_id> exploring`。
  - 若此命令报 "chain gate closed"（data/CHAIN_ON 不存在）：自动推进被代码闸拦截，**停手**，只汇报现状（用户明确指令时可用 `--force` 绕过，但那是用户命令，不是自动推进）。

用 task 工具后台派出。**task 调用必须包含 `"agent": "solver"` 字段**——照此骨架构造，逐项填变量，不得省略 agent：

```
tasks: [{
  "agent": "solver",
  "task": "problem_id = <problem_id>. Read prompts/generation/AGENTS.md and follow it exactly (memory policy, iteration protocol, stdin=DEVNULL discipline, _tmp_*.json cleanup). Resume from results/<problem_id>/run.json if it exists, else runstate init + memory init."
}]
```

派出后立即读 `history://<spawned-id>` 头部验证：agent 类型为 solver、model 符合 mathx_solver 角色；不对就 cancel 重派。之后按项目根 AGENTS.md 的调度纪律管理其生命周期。
