---
description: task-tool dispatch in MathExplorer requires the role gate open (data/SOLVER_ON for solver/referee dispatches, data/HUNTER_ON for hunter dispatches); dispatch with a gate closed is a discipline violation
condition: \{\s*"agent"\s*:\s*"(solver|hunter|referee-[123])"
scope: ["tool:task"]
---

A `tasks[]` item with `"agent": "solver"|"hunter"|"referee-N"` is a MathExplorer dispatch. Dispatches are gated per role (white-list gates, files under `data/`):

- **solver / referee-N dispatches**: the mandatory pre-dispatch step `set-status <pid> tackling` raises "solver gate closed" unless `data/SOLVER_ON` exists. Referees are sub-agents of the solver and inherit the solver gate — no separate referee gate.
- **hunter dispatches**: `uv run python -m mathx.harvest gate-check hunter` MUST be run immediately before the dispatch; it raises "hunter gate closed" unless `data/HUNTER_ON` exists.

**If you are about to dispatch (this call has not executed yet):**
- solver/referee: first confirm `data/SOLVER_ON` exists (glob it). If it does NOT:
  - This is an automatic advance → DO NOT dispatch. Stop, report status, wait for the user (`/solver on` or an explicit user command).
  - This is a user-commanded dispatch (user explicitly asked to solve now) → you may proceed, but the pre-dispatch `set-status tackling` MUST be run with `--force` (that is the sanctioned bypass; do not skip the set-status step itself).
- hunter: run `gate-check hunter` first. If it fails:
  - Automatic refill → DO NOT dispatch. Stop, report, wait (`/hunter on` or an explicit user command).
  - User-commanded dispatch → re-run with `--force`.

The role gates exist because the dispatcher's own judgment failed once (settled a solve, then advanced without checking the gate). They are enforced in code at `set-status tackling` (solver) and `gate-check` (hunter); this rule is the second layer, catching the task-tool call itself.
