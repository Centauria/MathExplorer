---
description: task-tool dispatches in MathExplorer MUST name an agent type (solver/hunter/referee-N); an item whose first key is "task" is a violation
condition: \{\s*"task"\s*:
scope: ["tool:task"]
---

Every `tasks[]` item in a MathExplorer dispatch MUST declare its agent type, as the FIRST key:

```json
tasks: [{"agent": "solver", "task": "..."}]
```

An item beginning with `"task"` (no leading `"agent"`) is a protocol violation: it silently falls back to the generic task worker, bypassing the agent frontmatter (model role, tool whitelist). This exact failure happened 7 times in one day.

If this call has not executed yet, rebuild the payload with `"agent"` as the first key of every item (`solver` for proof work, `hunter` for harvesting, `referee-1|2|3` for verification). If it already executed, verify the spawned agent type via `history://<id>` and cancel + redispatch on mismatch, per AGENTS.md rule 3.
