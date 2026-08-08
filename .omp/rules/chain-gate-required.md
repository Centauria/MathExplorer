---
description: task-tool dispatch in MathExplorer is only allowed while the chain gate is open (data/CHAIN_ON exists); a dispatch attempt with the gate closed is a discipline violation
condition: \{\s*"agent"\s*:\s*"(solver|hunter|referee-[123])"
scope: ["tool:task"]
---

A `tasks[]` item with `"agent": "solver"|"hunter"|"referee-N"` is a MathExplorer dispatch. Dispatches are gated: `set-status <pid> exploring` (the mandatory pre-dispatch step) raises "chain gate closed" unless `data/CHAIN_ON` exists, and this task-tool call is the same class of action.

**If you are about to dispatch (this call has not executed yet):** first confirm `data/CHAIN_ON` exists (glob it). If it does NOT:
- This is an automatic advance → DO NOT dispatch. Stop, report status, wait for the user (`/chain on` or an explicit user command).
- This is a user-commanded dispatch (user explicitly asked to solve/hunt now) → you may proceed, but the pre-dispatch `set-status exploring` MUST be run with `--force` (that is the sanctioned bypass; do not skip the set-status step itself).

The chain gate exists because the dispatcher's own judgment failed once (settled a solve, then advanced without checking the gate). The gate is enforced in code at `set-status exploring`; this rule is the second layer, catching the task-tool call itself.
