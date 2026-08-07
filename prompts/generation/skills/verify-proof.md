---
name: verify-proof
description: Verify candidate proofs with the mathx.verify jury pipeline. Use only when a full candidate proof of the entire problem has been assembled in markdown, and before publishing the final verified blueprint.
---

# Verify Proof

Use the verification jury as the canonical verifier before accepting a solution.
Do not use this skill for partial proofs, isolated subgoals, or branches that have not yet produced a full proof draft of the whole problem.

## Input Contract

Read:

- target theorem statement
- assembled proof blueprint candidate from `results/{problem_id}/blueprint.md` as pure markdown text
- relevant prior failure reports and branch context

## Procedure

1. Read the current `results/{problem_id}/blueprint.md` draft as pure text.
2. First check that `blueprint.md` contains a full proof draft of the entire target theorem rather than a partial proof, fragment, or exploratory notes. If it does not, do not call the verifier yet.
3. Run the jury:
   `uv run python -m mathx.verify --problem <problem_id>`
   It reads the statement and blueprint itself, checks external citations, runs the jurors in parallel, and writes `results/<problem_id>/verification.json` plus `jury/v{1,2,3}.json`. It may take several minutes; that is normal.
4. Read `verification_report.summary`, `critical_errors`, `gaps`, `verdict`, and `repair_hints` from the command output (also persisted in `verification.json`).
5. Persist exactly what the jury returns into the `verification_reports` memory channel. Do not rename keys, add keys, or change the JSON structure.
6. Treat the proof as failed if any of the following hold:
   - `verdict` is `"wrong"`
   - `verification_report.critical_errors` is non-empty
   - `verification_report.gaps` is non-empty
7. Only treat the proof as passed when none of the failure conditions above hold.
8. If the proof passes, rename `results/{problem_id}/blueprint.md` to `results/{problem_id}/blueprint_verified.md`.
9. If `mathx.verify` exits with code 3 (no provider), fall back per the AGENTS.md mapping table: spawn 3 throwaway jurors SERIALLY via the task tool (task text = full `prompts/verification/VERIFIER.md` + Statement + Proof), aggregate per the unanimous rule, and write `verification.json` + `jury/v{1,2,3}.json` yourself.

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

Persist the jury response exactly as returned.

If verification fails, revise `blueprint.md` directly and append to `failed_paths` when a branch is invalidated.

## Tools

- `uv run python -m mathx.verify --problem <problem_id>`
- `uv run python -m mathx.memory append <problem_id> verification_reports @record.json`
- `uv run python -m mathx.memory search <problem_id> "query" [--channels csv] [--limit 10]`
- `uv run python -m mathx.memory branch <problem_id> <branch_id> @state.json`
- your own `web_search` / `web_fetch` and `uv run python -m mathx.leansearch "query"` when the verifier identifies a missing lemma or gap

## Failure Logging

Always persist verification output, including successful checks.
