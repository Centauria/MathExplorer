---
description: task-tool dispatches in MathExplorer MUST name an agent type (solver/hunter/referee-N); an item whose first key is "task" is a violation
condition: \{\s*"task"\s*:
scope: ["tool:task"]
---

Every `tasks[]` item in a MathExplorer dispatch MUST declare its agent type, as the FIRST key, AND carry a `name` that embeds the worker's model:

```json
tasks: [{"agent": "solver", "name": "solver-k3-number-theory-abc-0af0", "task": "..."}]
```

Generate the name with `uv run python -m mathx.agents name <role> <suffix>` (role ∈ solver|hunter|referee-1|2|3; suffix = problem_id or hunt field); pass `agent_name` + `model` in the task text too. An omitted `name` yields an opaque random identifier (e.g. "IntegratedGibbon") that the archives can't tie to a model.

An item beginning with `"task"` (no leading `"agent"`) is a protocol violation: it silently falls back to the generic task worker, bypassing the agent frontmatter (model role, tool whitelist). This exact failure happened 7 times in one day.

If this call has not executed yet, rebuild the payload with `"agent"` as the first key of every item (`solver` for proof work, `hunter` for harvesting, `referee-1|2|3` for verification) plus a model-bearing `name` per the CLI above. If it already executed, verify the spawned agent type via `history://<id>` and cancel + redispatch on mismatch, per AGENTS.md rule 3.
