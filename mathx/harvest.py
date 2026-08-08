"""harvest.py — problem registry: ingestion (inbox JSONL + seed .md), dedup, field rotation, status.

The registry (data/registry.json) is the single source of truth for the problem
queue. Hunter agents only ever append to data/inbox/*.jsonl; ingestion happens
here, serially, from the orchestrator. Dedup: BM25 (mathx.memory) shortlists the
top-3 closest existing problems, then one fleet judge call decides sameness; if
the judge call fails or is unparseable, the candidate is treated as NEW (we
would rather keep a duplicate than lose a problem).

Field rotation order is the line order of data/fields.txt — editing that file
changes the field universe without touching code.

CLI:
    uv run python -m mathx.harvest ingest
    uv run python -m mathx.harvest next-field
    uv run python -m mathx.harvest list [--status s] [--field f]
    uv run python -m mathx.harvest show [problem_id] [--status s] [--field f]
    uv run python -m mathx.harvest set-status <problem_id> <status>
    uv run python -m mathx.harvest mark-hunted <field>
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from mathx.memory import bm25_score_documents, tokenize_bm25
from mathx.aggregate import _extract_json

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
REGISTRY_PATH = DATA_DIR / "registry.json"
FIELDS_PATH = DATA_DIR / "fields.txt"
INBOX_DIR = DATA_DIR / "inbox"
SEEDS_DIR = DATA_DIR / "seeds"
PROBLEMS_DIR = DATA_DIR / "problems"

DEFAULT_FIELDS = [
    "number-theory",
    "combinatorics",
    "graph-theory",
    "analysis",
    "algebra",
    "geometry-topology",
    "probability-theory",
    "logic-foundations",
    "dynamical-systems",
    "computational-math",
]

ORIGINS = {"classic", "arxiv", "ai-generated", "user", "control"}
STATUSES = {"queued", "exploring", "solved", "falsified", "stalled"}

DEDUP_SYSTEM = (
    "You decide whether a candidate mathematics problem duplicates any problem already in a registry. "
    "Two problems are the same if a solution to one would essentially solve the other, even if worded "
    "differently. Equivalent reformulations or one being a trivial corollary of the other count as the "
    "same. Different strengthenings, different quantifier structure, or merely shared keywords count as "
    "DIFFERENT. Respond with ONLY a JSON object: {\"same\": true|false, \"reason\": \"one sentence\"}."
)

INITIAL_REGISTRY = {
    "version": 1,
    "hunter_state": {"last_run_utc": None, "rotation_index": 0, "per_field_counts": {}},
    "ingested_inboxes": [],
    "ingested_seeds": [],
    "problems": [],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- fields

def load_fields() -> list[str]:
    if FIELDS_PATH.exists():
        fields = [ln.strip() for ln in FIELDS_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if fields:
            return fields
    return list(DEFAULT_FIELDS)


# ---------------------------------------------------------------- registry io

def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return copy.deepcopy(INITIAL_REGISTRY)
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def save_registry(reg: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, REGISTRY_PATH)


def next_field() -> str:
    reg = load_registry()
    fields = load_fields()
    return fields[reg["hunter_state"]["rotation_index"] % len(fields)]


def mark_field_hunted(field: str) -> dict:
    reg = load_registry()
    state = reg["hunter_state"]
    state["rotation_index"] = int(state.get("rotation_index", 0)) + 1
    counts = state.setdefault("per_field_counts", {})
    counts[field] = int(counts.get(field, 0)) + 1
    state["last_run_utc"] = _utc_now()
    save_registry(reg)
    return state


# ---------------------------------------------------------------- ingestion helpers

def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    return slug.strip("-") or "untitled"


def _parse_scalar(raw: str):
    raw = raw.strip()
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw.strip('"').strip("'")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-frontmatter reader: flat 'key: value' pairs only (stdlib, no pyyaml)."""
    lines = text.splitlines()
    meta: dict = {}
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return meta, "\n".join(lines[i + 1 :]).strip()
            m = re.match(r"^([A-Za-z0-9_]+)\s*:\s*(.*)$", lines[i])
            if m:
                meta[m.group(1)] = _parse_scalar(m.group(2))
    return meta, text.strip()


def _section(body: str, name: str) -> str:
    m = re.search(rf"^##\s+{re.escape(name)}\s*\n(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    return m.group(1).strip() if m else ""


def _candidate_from_seed(path: Path) -> dict:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    statement = _section(body, "Statement") or body
    return {
        "title": str(meta.get("title") or path.stem),
        "field": str(meta.get("field") or ""),
        "statement": statement,
        "origin": "user",
        "tractability": meta.get("tractability", 3),
        "why_interesting": str(meta.get("why_interesting") or ""),
        "sources": meta.get("sources") if isinstance(meta.get("sources"), list) else [],
        "still_open_evidence": _section(body, "Triage"),
        "found_at_utc": _utc_now(),
    }


def _existing_problem_text(entry: dict) -> str:
    path = PROBLEMS_DIR / f"{entry['id']}.md"
    statement = ""
    if path.exists():
        _, body = split_frontmatter(path.read_text(encoding="utf-8"))
        statement = _section(body, "Statement") or body
    return f"{entry['title']}\n{statement[:500]}"


def _dedup_judge(candidate: dict, matches: list[dict]) -> bool | None:
    """True=same, False=different, None=judge unavailable (treated as different)."""
    from mathx import fleet  # lazy: harvest must work with no provider configured
    from mathx.config import NoProviderError

    user_parts = [
        "Candidate problem:",
        f"Title: {candidate['title']}",
        f"Statement: {candidate['statement']}",
        "",
        "Closest registry problems:",
    ]
    for i, m in enumerate(matches, 1):
        user_parts.append(f"[{i}] id={m['entry']['id']}\n{_existing_problem_text(m['entry'])}")
    user = "\n\n".join(user_parts)
    try:
        r = asyncio.run(
            fleet.chat(
                [{"role": "system", "content": DEDUP_SYSTEM}, {"role": "user", "content": user}],
                role="judge",
                index=0,
                timeout=30.0,
            )
        )
    except NoProviderError:
        raise
    except Exception:
        return None
    obj = _extract_json(r["content"])
    if obj is None or not isinstance(obj.get("same"), bool):
        return None
    return obj["same"]


def _problem_file_text(entry: dict, cand: dict) -> str:
    sources = "\n".join(f"- {s}" for s in entry["sources"]) or "- (none recorded)"
    frontmatter = {
        "id": entry["id"],
        "title": entry["title"],
        "field": entry["field"],
        "origin": entry["origin"],
        "status": entry["status"],
        "tractability": entry["tractability"],
        "added_at_utc": entry["added_at_utc"],
    }
    fm_lines = ["---"] + [f'{k}: {json.dumps(str(v), ensure_ascii=False)}' for k, v in frontmatter.items()] + ["---"]
    return (
        "\n".join(fm_lines)
        + "\n\n## Statement\n\n"
        + (cand.get("statement") or "").strip()
        + "\n\n## Context\n\n"
        + (cand.get("why_interesting") or "").strip()
        + "\n\n## Known partial results\n\n"
        + (cand.get("still_open_evidence") or "").strip()
        + "\n\n## References\n\n"
        + sources
        + "\n"
    )


def _ingest_one(reg: dict, cand: dict, fields: list[str]) -> dict:
    title = str(cand.get("title") or "").strip()
    statement = str(cand.get("statement") or "").strip()
    field = str(cand.get("field") or "").strip() or fields[0]
    origin = str(cand.get("origin") or "classic").strip()
    if origin not in ORIGINS:
        origin = "classic"
    try:
        tractability = int(cand.get("tractability", 3))
    except (TypeError, ValueError):
        tractability = 3
    tractability = min(max(tractability, 1), 5)
    sources = [str(s) for s in (cand.get("sources") or []) if str(s).strip()]

    # BM25 shortlist over existing problems
    existing = reg["problems"]
    matches: list[dict] = []
    if existing:
        query_tokens_text = f"{title}\n{statement[:500]}"
        docs = [tokenize_bm25(_existing_problem_text(e)) for e in existing]
        scores = bm25_score_documents(query_tokens_text, docs)
        ranked = sorted(zip(existing, scores), key=lambda p: -p[1])
        matches = [{"entry": e, "score": s} for e, s in ranked[:3] if s > 0]

    if matches:
        verdict = _dedup_judge({"title": title, "statement": statement}, matches)
        if verdict is True:
            target = matches[0]["entry"]
            merged_sources = sorted(set(target.get("sources", [])) | set(sources))
            target["sources"] = merged_sources
            target["last_activity_utc"] = _utc_now()
            return {
                "action": "merged",
                "into": target["id"],
                "title": title,
                "note": "judge: duplicate of top BM25 match",
            }
        if verdict is None:
            note = "dedup judge unavailable; treated as new"
        else:
            note = "judge: distinct"
    else:
        note = "no similar registry problems"

    slug = slugify(title)
    pid = f"{field}/{slug}"
    taken = {e["id"] for e in existing}
    n = 1
    while pid in taken or (PROBLEMS_DIR / f"{pid}.md").exists():
        n += 1
        pid = f"{field}/{slug}-{n}"

    now = _utc_now()
    entry = {
        "id": pid,
        "title": title,
        "field": field,
        "origin": origin,
        "status": "queued",
        "tractability": tractability,
        "attempts": 0,
        "sources": sources,
        "added_at_utc": now,
        "last_activity_utc": now,
    }
    file_cand = {**cand, "statement": statement, "why_interesting": cand.get("why_interesting", ""),
                 "still_open_evidence": cand.get("still_open_evidence", "")}
    path = PROBLEMS_DIR / f"{pid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_problem_file_text(entry, file_cand), encoding="utf-8")
    reg["problems"].append(entry)
    return {"action": "added", "id": pid, "title": title, "note": note}


def ingest() -> dict:
    reg = load_registry()
    fields = load_fields()
    candidates: list[dict] = []
    skipped = 0

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    for inbox in sorted(INBOX_DIR.glob("*.jsonl")):
        if inbox.name in reg["ingested_inboxes"]:
            continue
        for line in inbox.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(obj, dict) or not str(obj.get("title", "")).strip() or not str(obj.get("statement", "")).strip():
                skipped += 1
                continue
            candidates.append(obj)
        reg["ingested_inboxes"].append(inbox.name)

    SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    for seed in sorted(SEEDS_DIR.glob("*.md")):
        if seed.name in reg["ingested_seeds"]:
            continue
        candidates.append(_candidate_from_seed(seed))
        reg["ingested_seeds"].append(seed.name)

    added = merged = 0
    details = []
    for cand in candidates:
        outcome = _ingest_one(reg, cand, fields)
        details.append(outcome)
        if outcome["action"] == "added":
            added += 1
        else:
            merged += 1

    save_registry(reg)
    return {"added": added, "merged": merged, "skipped_lines": skipped, "details": details}


# ---------------------------------------------------------------- status

def set_status(problem_id: str, status: str, force: bool = False) -> dict:
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}; allowed: {sorted(STATUSES)}")
    if status == "exploring" and not force:
        # Hard chain gate: automatic dispatch (the only caller of exploring)
        # is blocked unless data/CHAIN_ON exists. User-commanded dispatch
        # passes --force. This is enforced in code, not in prose, because the
        # dispatcher's own discipline is the failure mode this guards against.
        if not (DATA_DIR / "CHAIN_ON").exists():
            raise ValueError(
                "chain gate closed: data/CHAIN_ON absent. Automatic dispatch is blocked. "
                "Run /chain on to open the gate, or pass --force for a user-commanded dispatch."
            )
    reg = load_registry()
    for entry in reg["problems"]:
        if entry["id"] == problem_id:
            if status == "exploring" and entry["status"] != "exploring":
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["status"] = status
            entry["last_activity_utc"] = _utc_now()
            save_registry(reg)
            return entry
    raise KeyError(f"problem not found: {problem_id}")


def list_problems(status: str | None = None, field: str | None = None) -> list[dict]:
    reg = load_registry()
    out = []
    for entry in reg["problems"]:
        if status and entry["status"] != status:
            continue
        if field and entry["field"] != field:
            continue
        out.append(entry)
    return out


def show_problems(
    problem_id: str | None = None, status: str | None = None, field: str | None = None
) -> dict:
    """Registry entries plus full statement text from data/problems/<id>.md.

    Bare-token resolution order: exact id > status name (queued/exploring/...)
    > field name > unique prefix on the id or its slug part. Ambiguous prefix ->
    {"ambiguous": true, "matches": [...]}. Status/field tokens act as filters
    on top of any --status/--field flags. Without a token, every entry passing
    the filters is shown.
    """
    entries = list_problems(status=status, field=field)
    if problem_id:
        exact = [e for e in entries if e["id"] == problem_id]
        if exact:
            entries = exact
        elif problem_id in STATUSES:
            entries = [e for e in entries if e["status"] == problem_id]
        elif problem_id in load_fields():
            entries = [e for e in entries if e["field"] == problem_id]
        else:
            matches = [
                e
                for e in entries
                if e["id"].startswith(problem_id) or e["id"].rsplit("/", 1)[-1].startswith(problem_id)
            ]
            if not matches:
                raise KeyError(f"problem not found: {problem_id}")
            if len(matches) > 1:
                return {
                    "ambiguous": True,
                    "matches": [{"id": e["id"], "title": e["title"]} for e in matches],
                }
            entries = matches
    out = []
    for e in entries:
        path = PROBLEMS_DIR / f"{e['id']}.md"
        statement = path.read_text(encoding="utf-8") if path.exists() else None
        out.append(
            {
                "entry": e,
                "statement_file": str(path.relative_to(REPO_ROOT)),
                "statement": statement,
            }
        )
    return {"count": len(out), "problems": out}


# ---------------------------------------------------------------- CLI

def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    _stdio_utf8()
    ap = argparse.ArgumentParser(prog="mathx.harvest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="ingest new data/inbox/*.jsonl and data/seeds/*.md into the registry")
    sub.add_parser("next-field", help="print the next field in the rotation")
    p_list = sub.add_parser("list", help="list registry problems")
    p_list.add_argument("--status")
    p_list.add_argument("--field")
    p_show = sub.add_parser("show", help="show problem(s) with full statement text")
    p_show.add_argument("problem_id", nargs="?")
    p_show.add_argument("--status")
    p_show.add_argument("--field")
    p_set = sub.add_parser("set-status")
    p_set.add_argument("problem_id")
    p_set.add_argument("status")
    p_set.add_argument("--force", action="store_true", help="bypass the chain gate (user-commanded dispatch)")
    p_mark = sub.add_parser("mark-hunted")
    p_mark.add_argument("field")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "ingest":
            _print(ingest())
        elif args.cmd == "next-field":
            print(next_field())
        elif args.cmd == "list":
            _print(list_problems(status=args.status, field=args.field))
        elif args.cmd == "show":
            _print(show_problems(problem_id=args.problem_id, status=args.status, field=args.field))
        elif args.cmd == "set-status":
            _print(set_status(args.problem_id, args.status, force=args.force))
        elif args.cmd == "mark-hunted":
            _print(mark_field_hunted(args.field))
        return 0
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
