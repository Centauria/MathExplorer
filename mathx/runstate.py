"""runstate.py — iteration state machine for the solver loop (replaces run_example.sh resume logic).

State file: results/<problem_id>/run.json
    {"problem_id", "status": "running|solved|unsolvable|postponed", "iteration": 0,
     "max_iterations": 3, "phase": "search",
     "history": [{"iteration", "phase", "note", "utc"}]}

Phase semantics (mirror run_example.sh's odd/even alternation; iteration 0 = search):
- "search":    the solver may use web search / leansearch.
- "deepthink": no retrieval — memory + reasoning + fleet sub-agents only.

CLI:
    uv run python -m mathx.runstate init <problem_id> [--max-iterations 3]
    uv run python -m mathx.runstate advance <problem_id> [--note "..."]
    uv run python -m mathx.runstate status <problem_id>
    uv run python -m mathx.runstate stop <problem_id> <solved|unsolvable|postponed> [--verdict true|false]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = REPO_ROOT / "results"

PHASES = ("search", "deepthink")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_rel(problem_id: str) -> Path:
    p = Path(problem_id)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"invalid problem_id: {problem_id!r}")
    return p


def _state_path(problem_id: str) -> Path:
    return RESULTS_ROOT / _safe_rel(problem_id) / "run.json"


def _load(problem_id: str) -> dict:
    path = _state_path(problem_id)
    if not path.exists():
        raise FileNotFoundError(f"no runstate for {problem_id!r} (run: mathx.runstate init {problem_id})")
    return json.loads(path.read_text(encoding="utf-8"))


def _save(state: dict) -> None:
    path = _state_path(state["problem_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def runstate_init(problem_id: str, max_iterations: int = 3) -> dict:
    path = _state_path(problem_id)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))  # no-op resume semantics
    state = {
        "problem_id": problem_id,
        "status": "running",
        "iteration": 0,
        "max_iterations": int(max_iterations),
        "phase": "search",
        "history": [],
    }
    _save(state)
    return state


def runstate_advance(problem_id: str, note: str = "") -> dict:
    state = _load(problem_id)
    if state["status"] != "running":
        return state
    state["iteration"] = int(state["iteration"]) + 1
    state["phase"] = "deepthink" if state["phase"] == "search" else "search"
    state["history"].append(
        {"iteration": state["iteration"], "phase": state["phase"], "note": note, "utc": _utc_now()}
    )
    _save(state)
    return state


def runstate_stop(problem_id: str, outcome: str, verdict: str | None = None) -> dict:
    if outcome not in ("solved", "unsolvable", "postponed"):
        raise ValueError("outcome must be 'solved', 'unsolvable' or 'postponed'")
    if verdict is not None and verdict not in ("true", "false"):
        raise ValueError("verdict must be 'true' or 'false'")
    state = _load(problem_id)
    state["status"] = outcome
    if verdict is not None:
        state["verdict"] = verdict
    state["history"].append(
        {"iteration": state["iteration"], "phase": state["phase"], "note": f"stopped: {outcome}", "utc": _utc_now()}
    )
    _save(state)
    return state


def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _stdio_utf8()
    ap = argparse.ArgumentParser(prog="mathx.runstate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init")
    p_init.add_argument("problem_id")
    p_init.add_argument("--max-iterations", type=int, default=3)
    p_adv = sub.add_parser("advance")
    p_adv.add_argument("problem_id")
    p_adv.add_argument("--note", default="")
    p_status = sub.add_parser("status")
    p_status.add_argument("problem_id")
    p_stop = sub.add_parser("stop")
    p_stop.add_argument("problem_id")
    p_stop.add_argument("outcome", choices=["solved", "unsolvable", "postponed"])
    p_stop.add_argument("--verdict", choices=["true", "false"], help="mathematical outcome (solved only)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "init":
            state = runstate_init(args.problem_id, args.max_iterations)
        elif args.cmd == "advance":
            state = runstate_advance(args.problem_id, args.note)
        elif args.cmd == "status":
            state = _load(args.problem_id)
        elif args.cmd == "stop":
            state = runstate_stop(args.problem_id, args.outcome, verdict=args.verdict)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
