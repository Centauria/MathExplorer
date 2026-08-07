"""aggregate.py — deterministic referee-verdict aggregator (no LLM calls).

The omp referee pipeline: three referee subagents (agent types referee-1/2/3,
each a different model) independently verify `results/<problem_id>/blueprint.md`
and write `results/<problem_id>/referee/v{1,2,3}.json` following the
VERIFIER.md output contract. This module is the ONLY judge of their combined
outcome — the rule lives in code, not in anyone's context:

    verdict = "correct"  iff  every report parses AND says "correct"
                            AND has zero critical_errors AND zero gaps.
    An unparseable or missing report counts as "wrong".

Outputs (upstream schema, unchanged from the retired mathx.verify pipeline):
  results/<problem_id>/verification.json — merged report + verdict + hints.

CLI:
    uv run python -m mathx.aggregate <problem_id>

Exit codes: 0 verdict written, 1 missing inputs (referee dir/files absent).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _safe_rel(problem_id: str) -> Path:
    p = Path(problem_id)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"invalid problem_id: {problem_id!r}")
    return p


def _extract_json(text: str) -> dict | None:
    """Leniently pull the first parseable {...} object out of text."""
    for start in [i for i, c in enumerate(text) if c == "{"]:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
    return None


def _norm_issues(items) -> list[dict]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            out.append({"location": str(item.get("location", "")), "issue": str(item.get("issue", ""))})
    return out


def _parse_verdict(content: str) -> dict | None:
    obj = _extract_json(content)
    if obj is None:
        return None
    report = obj.get("verification_report")
    verdict = obj.get("verdict")
    if not isinstance(report, dict) or verdict not in ("correct", "wrong"):
        return None
    return {
        "summary": str(report.get("summary", "")),
        "critical_errors": _norm_issues(report.get("critical_errors")),
        "gaps": _norm_issues(report.get("gaps")),
        "verdict": verdict,
        "repair_hints": str(obj.get("repair_hints", "")),
    }


def _unparseable_report(reason: str) -> dict:
    return {
        "summary": f"referee output unusable: {reason}",
        "critical_errors": [{"location": "referee", "issue": f"referee output unparseable ({reason})"}],
        "gaps": [],
        "verdict": "wrong",
        "repair_hints": "Re-run this referee; it failed to produce a parseable report.",
    }


def aggregate(problem_id: str, referees: int = 3) -> dict:
    """Aggregate referee verdict files into verification.json. Returns the final object."""
    rel = _safe_rel(problem_id)
    ref_dir = REPO_ROOT / "results" / rel / "referee"
    if not ref_dir.is_dir():
        raise FileNotFoundError(f"referee verdict directory not found: {ref_dir}")

    reports: list[dict] = []
    for i in range(1, referees + 1):
        path = ref_dir / f"v{i}.json"
        if not path.exists():
            reports.append(_unparseable_report(f"v{i}.json missing"))
            continue
        parsed = _parse_verdict(path.read_text(encoding="utf-8"))
        reports.append(parsed if parsed is not None else _unparseable_report("no verdict-schema JSON found"))

    verdict = (
        "correct"
        if all(p["verdict"] == "correct" and not p["critical_errors"] and not p["gaps"] for p in reports)
        else "wrong"
    )
    final = {
        "verification_report": {
            "summary": "\n\n".join(f"[V{i}] {p['summary']}" for i, p in enumerate(reports, 1)),
            "critical_errors": [
                {"location": f"[V{i}] {it['location']}", "issue": it["issue"]}
                for i, p in enumerate(reports, 1)
                for it in p["critical_errors"]
            ],
            "gaps": [
                {"location": f"[V{i}] {it['location']}", "issue": it["issue"]}
                for i, p in enumerate(reports, 1)
                for it in p["gaps"]
            ],
        },
        "verdict": verdict,
        "repair_hints": (
            ""
            if verdict == "correct"
            else "\n\n".join(f"[V{i}] {p['repair_hints']}" for i, p in enumerate(reports, 1) if p["repair_hints"])
        ),
    }

    out_path = REPO_ROOT / "results" / rel / "verification.json"
    out_path.write_text(json.dumps(final, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return final


def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _stdio_utf8()
    ap = argparse.ArgumentParser(prog="mathx.aggregate")
    ap.add_argument("problem_id", help="problem id, e.g. number-theory/goldbach")
    ap.add_argument("--referees", type=int, default=3)
    args = ap.parse_args(argv)
    try:
        final = aggregate(args.problem_id, referees=args.referees)
    except (FileNotFoundError, ValueError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
