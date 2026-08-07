---
name: recursive-proving
description: Launch one fleet sub-agent per decomposition plan after direct screening has identified the key stuck points for each plan. Use when all current plans have been screened by direct proving, none fully solves the problem, and parallel recursive work is needed.
---

# Recursive Proving

Use this skill when direct proving has failed on the current decomposition plans.

## Input Contract

Read:

- the current set of decomposition plans
- the direct-proving reports and key stuck points for each plan
- the known stuck points from other plans
- relevant `failed_paths`, `branch_states`, and search results

## Procedure

1. Confirm that all current decomposition plans have already been attempted with `$direct-proving` and that none has fully solved the problem.
2. Construct ONE fleet task per decomposition plan, combine them into a single `tasks.json`, and run the whole batch at once:
   `uv run python -m mathx.fleet tasks.json -o out.json`
3. Each task carries `"role": "prover"` and its prompt embeds:
   - the full target theorem
   - the assigned decomposition plan
   - the key stuck points for its own plan
   - the key stuck points found in the other plans
   - a single deep-reasoning instruction: treat the assigned plan as the starting point (not a restart from zero); refine, extend, or locally revise it only if new evidence justifies that; preserve continuity with the assigned plan
   - the report contract: structured markdown with `## Proved subgoals` / `## Unproved subgoals` (with reasons) / `## New ideas`
4. Fleet sub-agents have no network access and do NOT write to memory; they return one report each in `out.json`.
5. Collect every report from `out.json` and persist each into the shared memory yourself (`proof_steps`, `failed_paths`, or `subgoals` as appropriate), under the same `problem_id`.
6. If any plan's report solves its subgoals, assemble the proof draft from that plan.
7. If all plans fail, hand the collected reports to `$identify-key-failures`.
8. If `mathx.fleet` exits with code 3 (no provider), fall back per the AGENTS.md mapping table: work each plan yourself, sequentially, as an ordinary direct/recursive proving loop.

## Output Contract

Append an `events` record for the recursive round:

```json
{
  "event_type": "recursive_proving_round",
  "plan_ids": ["..."],
  "shared_stuck_points": {
    "plan_id": ["..."]
  },
  "status": "running|completed",
  "successful_plan_ids": ["..."],
  "failed_plan_ids": ["..."]
}
```

Update `branch_states` with the recursive round status and per-plan outcomes.

## Tools

- `uv run python -m mathx.fleet tasks.json -o out.json` — one batch, one task per plan, each with `"role": "prover"`
- `uv run python -m mathx.memory search <problem_id> "query" [--channels csv] [--limit 10]`
- `uv run python -m mathx.memory append <problem_id> <channel> @record.json`
- `uv run python -m mathx.memory branch <problem_id> <branch_id> @state.json`
- `uv run python -m mathx.leansearch "query" [--num 10]`

## Failure Logging

If every plan fails in the recursive round, append a summary record to `failed_paths` and immediately invoke `$identify-key-failures`.
