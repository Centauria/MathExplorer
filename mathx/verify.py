"""verify.py — N-juror verification jury (replaces the Rethlas Codex verification service).

Pipeline for one problem:
  1. extract external citations from the proof (role=judge, lenient parse)
  2. check each citation against leansearch, building an appendix
  3. run N jurors in parallel (role=jury, temperature ladder 0.2/0.5/0.8)
  4. strict aggregation: verdict "correct" iff ALL jurors say correct with zero
     critical_errors and zero gaps; an unparseable juror counts as "wrong"
  5. if wrong and >=2 reports parseable, merge them into one repair briefing
     (role=judge); else concatenate raw hints

Outputs: results/<problem_id>/verification.json (upstream schema) and
results/<problem_id>/jury/v{i}.json (raw per-juror reports).

CLI:
    uv run python -m mathx.verify --problem <problem_id> [--jurors 3]

Exit codes: 0 ok, 1 missing inputs, 2 citation/merge judge call failed (degraded
output still written), 3 NoProviderError (nothing is written).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mathx import fleet
from mathx.config import NoProviderError
from mathx.leansearch import search_arxiv_theorems

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "prompts" / "verification"
TEMPERATURE_LADDER = [0.2, 0.5, 0.8]


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1 :]).strip()
    return text.strip()


def _safe_rel(problem_id: str) -> Path:
    p = Path(problem_id)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"invalid problem_id: {problem_id!r}")
    return p


def _extract_json(text: str) -> dict | None:
    """Leniently pull the first parseable {...} object out of model output."""
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
    """Parse one juror's output; None if unparseable."""
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
        "summary": f"juror output unusable: {reason}",
        "critical_errors": [{"location": "verifier", "issue": f"verifier output unparseable ({reason})"}],
        "gaps": [],
        "verdict": "wrong",
        "repair_hints": "Re-run verification; the juror failed to produce a parseable report.",
    }


async def _extract_citations(proof: str, warnings: list[str]) -> list[dict]:
    r = await fleet.chat(
        [
            {"role": "system", "content": _load_prompt("extract_citations.md")},
            {"role": "user", "content": proof},
        ],
        role="judge",
        index=0,
    )
    obj = _extract_json(r["content"])
    if obj is None or not isinstance(obj.get("citations"), list):
        warnings.append("citation extraction unparseable; treating proof as having no external citations")
        return []
    citations = []
    for c in obj["citations"]:
        if isinstance(c, dict) and c.get("statement"):
            citations.append(
                {
                    "location": str(c.get("location", "")),
                    "statement": str(c.get("statement", "")),
                    "source_hint": str(c.get("source_hint", "")),
                }
            )
    return citations


def _build_appendix(citations: list[dict]) -> str:
    if not citations:
        return "No external citations were extracted from this proof."
    lines: list[str] = []
    for i, cit in enumerate(citations, 1):
        lines.append(f"Citation {i}:")
        lines.append(f"  Location: {cit['location']}")
        lines.append(f"  Statement as used: {cit['statement']}")
        if cit["source_hint"]:
            lines.append(f"  Source hint: {cit['source_hint']}")
        try:
            res = search_arxiv_theorems(cit["statement"], num_results=5)
            matches = res["results"]
        except Exception as e:
            lines.append(f"  Theorem search unavailable for this citation ({type(e).__name__}: {e}).")
            continue
        if matches:
            lines.append("  Theorem-search matches:")
            for m in matches:
                theorem = m["theorem"]
                if len(theorem) > 400:
                    theorem = theorem[:400] + "..."
                lines.append(f"    - [arXiv:{m['arxiv_id']}] {m['title']}: {theorem}")
        else:
            lines.append("  Theorem search returned no matches for this citation.")
    return "\n".join(lines)


async def _verify(problem_id: str, jurors: int) -> tuple[dict, list[dict]]:
    rel = _safe_rel(problem_id)
    problem_path = REPO_ROOT / "data" / "problems" / rel.with_suffix(".md")
    proof_path = REPO_ROOT / "results" / rel / "blueprint.md"
    if not problem_path.exists():
        raise FileNotFoundError(f"problem statement not found: {problem_path}")
    if not proof_path.exists():
        raise FileNotFoundError(f"proof blueprint not found: {proof_path}")
    statement = _strip_frontmatter(problem_path.read_text(encoding="utf-8"))
    proof = proof_path.read_text(encoding="utf-8")

    warnings: list[str] = []

    # Stage 1+2: citations -> appendix
    citations = await _extract_citations(proof, warnings)
    appendix = _build_appendix(citations)

    # Stage 3: jury
    verifier_system = _load_prompt("VERIFIER.md")
    user = (
        f"Statement:\n{statement}\n\nProof:\n{proof}\n\n"
        f"External citation check appendix:\n{appendix}\n\n"
        "Respond with ONLY the JSON object."
    )
    tasks = [
        {
            "id": f"v{i}",
            "system": verifier_system,
            "prompt": user,
            "role": "jury",
            "temperature": TEMPERATURE_LADDER[(i - 1) % len(TEMPERATURE_LADDER)],
        }
        for i in range(1, jurors + 1)
    ]
    results = await fleet.run_batch(tasks, concurrency=jurors)

    reports: list[dict] = []
    raw_reports: list[dict] = []
    for i, res in enumerate(results, 1):
        if not res["ok"]:
            parsed = _unparseable_report(res.get("error", "unknown fleet error"))
        else:
            parsed = _parse_verdict(res["content"])
            if parsed is None:
                parsed = _unparseable_report("no JSON object matching the verdict schema found")
        reports.append(parsed)
        raw_entry = {"juror": i, "temperature": TEMPERATURE_LADDER[(i - 1) % len(TEMPERATURE_LADDER)], **parsed}
        if not res["ok"] or "unusable" in parsed["summary"]:
            raw_entry["raw_content"] = res.get("content", "")[:2000]
        raw_reports.append(raw_entry)

    # Aggregate: unanimous correct with zero findings
    verdict = "correct" if all(
        p["verdict"] == "correct" and not p["critical_errors"] and not p["gaps"] for p in reports
    ) else "wrong"

    summary = "\n\n".join(f"[V{i}] {p['summary']}" for i, p in enumerate(reports, 1))
    critical_errors = [
        {"location": f"[V{i}] {it['location']}", "issue": it["issue"]}
        for i, p in enumerate(reports, 1)
        for it in p["critical_errors"]
    ]
    gaps = [
        {"location": f"[V{i}] {it['location']}", "issue": it["issue"]}
        for i, p in enumerate(reports, 1)
        for it in p["gaps"]
    ]

    # Repair hints
    if verdict == "correct":
        repair_hints = ""
    else:
        parseable = [p for p in reports if "unusable" not in p["summary"]]
        repair_hints = ""
        if len(parseable) >= 2:
            merge_input = "\n\n".join(
                f"Report {i}:\n{json.dumps(p, ensure_ascii=False, indent=2)}" for i, p in enumerate(parseable, 1)
            )
            try:
                r = await fleet.chat(
                    [
                        {"role": "system", "content": _load_prompt("merge_reports.md")},
                        {"role": "user", "content": merge_input},
                    ],
                    role="judge",
                    index=0,
                )
                repair_hints = r["content"].strip()
            except NoProviderError:
                raise
            except Exception as e:
                warnings.append(f"merge_reports call failed ({type(e).__name__}: {e}); concatenating raw hints")
        if not repair_hints:
            repair_hints = "\n\n".join(
                f"[V{i}] {p['repair_hints']}" for i, p in enumerate(reports, 1) if p["repair_hints"]
            )

    final = {
        "verification_report": {
            "summary": summary,
            "critical_errors": critical_errors,
            "gaps": gaps,
        },
        "verdict": verdict,
        "repair_hints": repair_hints,
    }
    if warnings:
        final["warnings"] = warnings

    out_dir = REPO_ROOT / "results" / rel
    jury_dir = out_dir / "jury"
    jury_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "verification.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for i, raw in enumerate(raw_reports, 1):
        (jury_dir / f"v{i}.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return final, raw_reports


def verify_problem(problem_id: str, jurors: int = 3) -> dict:
    final, _ = asyncio.run(_verify(problem_id, jurors))
    return final


def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _stdio_utf8()
    ap = argparse.ArgumentParser(prog="mathx.verify")
    ap.add_argument("--problem", required=True, help="problem id, e.g. number-theory/goldbach")
    ap.add_argument("--jurors", type=int, default=3)
    args = ap.parse_args(argv)
    if args.jurors < 1:
        print(json.dumps({"error": "--jurors must be >= 1"}), file=sys.stderr)
        return 1
    try:
        final = verify_problem(args.problem, jurors=args.jurors)
    except NoProviderError as e:
        print(json.dumps({"error": f"{e} (no files written)"}, ensure_ascii=False), file=sys.stderr)
        return 3
    except FileNotFoundError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
