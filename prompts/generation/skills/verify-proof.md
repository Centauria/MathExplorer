---
name: verify-proof
description: Verify candidate proofs with the cross-model referee panel + deterministic aggregator. Use only when a full candidate proof of the entire problem has been assembled in markdown, and before publishing the final verified blueprint.
---

# Verify Proof

Use the referee panel as the canonical verifier before accepting a solution.
Do not use this skill for partial proofs, isolated subgoals, or branches that have not yet produced a full proof draft of the whole problem.

## Input Contract

Read:

- target theorem statement
- assembled proof blueprint candidate from `results/{problem_id}/blueprint.md` as pure markdown text
- relevant prior failure reports and branch context

## Procedure

1. Read the current `results/{problem_id}/blueprint.md` draft as pure text.
2. First check that `blueprint.md` contains a full proof draft of the entire target theorem rather than a partial proof, fragment, or exploratory notes. If it does not, do not call the referees yet.
3. Demand-coverage gate: read the problem's Statement and check that the main theorem's conclusion actually settles what the Statement demands (exactly what VERIFIER.md Phase 2.5 will check). In particular, if the conclusion merely restates the problem as open/unknown (e.g. "the exact value remains an open problem" when the Statement asks for the exact value), or establishes only bounds/numerics/partial cases without the demanded claim, do NOT spawn the referees — the panel would be guaranteed to fail and is wasted. Instead return to the proving loop and keep working toward the demand, or preserve progress and stall at the iteration cap.
3. Spawn the three referees in ONE parallel `tasks[]` batch (each item carries its own `agent` field; task text is only the problem_id):
   ```
   tasks: [
     {"agent": "referee-1", "task": "problem_id = <problem_id>"},
     {"agent": "referee-2", "task": "problem_id = <problem_id>"},
     {"agent": "referee-3", "task": "problem_id = <problem_id>"}
   ]
   ```
   Each referee independently reads `prompts/verification/VERIFIER.md`, the statement, and the blueprint; checks external citations live (web_search + `mathx.leansearch` via bash); and writes `results/{problem_id}/referee/v{1,2,3}.json`. This may take several minutes; that is normal.
4. Aggregate deterministically:
   `uv run python -m mathx.aggregate <problem_id>`
   The unanimous rule (all reports parseable and `"correct"` with zero `critical_errors` and zero `gaps`; an unparseable or missing report counts as wrong) is enforced by this code, not by judgment. It writes `results/{problem_id}/verification.json`.
5. Read `verification_report.summary`, `critical_errors`, `gaps`, `verdict`, and `repair_hints` from the command output (also persisted in `verification.json`).
6. Persist exactly what the aggregator returns into the `verification_reports` memory channel. Do not rename keys, add keys, or change the JSON structure.
7. Treat the proof as failed if any of the following hold:
   - `verdict` is `"wrong"`
   - `verification_report.critical_errors` is non-empty
   - `verification_report.gaps` is non-empty
8. Only treat the proof as passed when none of the failure conditions above hold.
9. If the proof passes, rename `results/{problem_id}/blueprint.md` to `results/{problem_id}/blueprint_verified.md`.

## Output Contract

Append to `verification_reports`:

```json
{
  "verification_report": {
    "summary": "string",
    "critical_errors": [
      {"location": "", "issue": "detailed description of the issue"}
    ],
    "gaps": [
      {"location": "", "issue": "detailed description of the gap"}
    ]
  },
  "verdict": "string",
  "repair_hints": "string"
}
```

Persist the aggregator response exactly as returned.

If verification fails, revise `blueprint.md` directly and append to `failed_paths` when a branch is invalidated.

## Tools

- `uv run python -m mathx.aggregate <problem_id>`
- `uv run python -m mathx.memory append <problem_id> verification_reports @record.json`
- `uv run python -m mathx.memory search <problem_id> "query" [--channels csv] [--limit 10]`
- `uv run python -m mathx.memory branch <problem_id> <branch_id> @state.json`
- your own `web_search` / `web_fetch` and `uv run python -m mathx.leansearch "query"` when a referee identifies a missing lemma or gap

## Failure Logging

Always persist verification output, including successful checks.
