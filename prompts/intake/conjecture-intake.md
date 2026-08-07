# Conjecture Intake

The user has submitted a mathematical statement — maybe an open problem, maybe a known theorem, maybe false. Your job: find out which, fast, then route it. Do NOT dispatch the solver worker before completing triage; hours of solver compute are justified only after the cheap checks fail.

## Step 1 — Precise restatement
Rewrite the statement into a self-contained form: define every non-standard term, make all quantifiers and domains explicit (n ∈ ℕ? n ≥ 2?), fix notation. If the user's phrasing is ambiguous, pick the most charitable reading and note the ambiguity in the record.

## Step 2 — Literature check
- Run `uv run python -m mathx.leansearch "<restatement>" --num 5`.
- Run one web search: `<key phrase> theorem|conjecture|counterexample`.
- If it is a known result (proved or refuted): report to the user with the exact reference (name, arXiv id / source), then file it (Step 4) with status `solved` (known true) or `falsified` (known false) and stop. Do not dispatch the solver.
- If it is a known OPEN problem: tell the user, then continue to Step 4 and dispatch like any queued problem.

## Step 3 — Cheap computation / small cases
When the statement is checkable for small cases (number theory, combinatorics, finite structures):
- Use the Wolfram MCP tools or node_repl to test the smallest instances (n = 1..10 or the smallest meaningful sizes).
- Actively hunt for a counterexample; try edge cases (n=0, n=1, empty/degenerate objects) first.
- Counterexample found → report it to the user, file with status `falsified` (attach the counterexample), stop.
- All small cases pass → record the evidence; it raises confidence but proves nothing.

## Step 4 — File and route
1. Write `data/seeds/<slug>.md` with YAML frontmatter (`title`, `field` (the closest matching line of `data/fields.txt`), `origin: user`, `tractability` 1–5 honest estimate) and body sections `## Statement` (the precise restatement) and `## Triage` (findings and evidence from Steps 2–3).
2. Run `uv run python -m mathx.harvest ingest` (uniform registration + dedup). Report the assigned problem id.
3. Tell the user the triage outcome in 2–3 sentences. Unless the user asked only for an assessment, dispatch: spawn agent="solver" in the background with the new problem id, per the dispatcher discipline in the root AGENTS.md.

## Hard rules
- Never dispatch the solver on a statement you have not restated precisely.
- Never claim a conjecture is "probably true" without recording the Step 3 evidence.
- If tooling fails (no network, Wolfram unavailable), say which check was skipped and file the statement with a `triage_incomplete: true` note instead of silently skipping.
