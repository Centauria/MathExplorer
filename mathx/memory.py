"""memory.py — file-backed per-problem memory, ported from Rethlas agents/generation/mcp/server.py.

Behavior is identical to upstream (append-only JSONL channels, BM25 retrieval with
k1=1.5/b=0.75, problem-id sanitization against path traversal). Only the FastMCP
wrapper and the network functions (search_arxiv_theorems, verify_proof_service)
were removed; BM25 helpers are public here because mathx.harvest reuses them for
registry dedup. Pure-local module: never imports mathx.config, works with no LLM
provider configured.

CLI (all output JSON on stdout; record/state args accept @path.json):
    uv run python -m mathx.memory init <problem_id> [--meta JSON|@file]
    uv run python -m mathx.memory append <problem_id> <channel> <record-json|@file>
    uv run python -m mathx.memory search <problem_id> <query> [--channels csv] [--limit 10]
    uv run python -m mathx.memory branch <problem_id> <branch_id> <state-json|@file>
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_ROOT = REPO_ROOT / "memory"

CHANNEL_FILES: Dict[str, str] = {
    "immediate_conclusions": "immediate_conclusions.jsonl",
    "toy_examples": "toy_examples.jsonl",
    "counterexamples": "counterexamples.jsonl",
    "big_decisions": "big_decisions.jsonl",
    "subgoals": "subgoals.jsonl",
    "proof_steps": "proof_steps.jsonl",
    "failed_paths": "failed_paths.jsonl",
    "verification_reports": "verification_reports.jsonl",
    "branch_states": "branch_states.jsonl",
    "events": "events.jsonl",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_problem_component(raw: str) -> str:
    cleaned = re.sub(r"\s+", "_", raw.strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned


def sanitize_problem_id(raw: str) -> str:
    """Return a safe problem id while preserving relative path components."""
    normalized = raw.strip().replace("\\", "/")
    parts: List[str] = []
    for part in normalized.split("/"):
        stripped = part.strip()
        if stripped in {"", "."}:
            continue
        if stripped == "..":
            raise ValueError("problem_id must not contain '..' path components")
        cleaned = _sanitize_problem_component(stripped)
        if cleaned:
            parts.append(cleaned)
    return "/".join(parts) or "problem"


def build_problem_id(source: str, identifier: str) -> str:
    return sanitize_problem_id(f"{source}_{identifier}")


def _problem_dir(problem_id: str) -> Path:
    sanitized_problem_id = sanitize_problem_id(problem_id)
    problem_dir = (MEMORY_ROOT / sanitized_problem_id).resolve()
    memory_root = MEMORY_ROOT.resolve()
    if not problem_dir.is_relative_to(memory_root):
        raise ValueError("problem_id resolves outside memory root")
    return problem_dir


def _channel_path(problem_id: str, channel: str) -> Path:
    if channel not in CHANNEL_FILES:
        allowed = ", ".join(sorted(CHANNEL_FILES))
        raise ValueError(f"Unknown channel '{channel}'. Allowed channels: {allowed}")
    return _problem_dir(problem_id) / CHANNEL_FILES[channel]


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def tokenize_bm25(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9_]+", text.lower())


def bm25_score_documents(
    query: str,
    documents: List[List[str]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> List[float]:
    query_tokens = tokenize_bm25(query)
    if not query_tokens or not documents:
        return [0.0 for _ in documents]

    query_term_counts = Counter(query_tokens)
    document_frequencies: Counter[str] = Counter()
    document_term_counts = [Counter(document) for document in documents]
    document_lengths = [len(document) for document in documents]
    avg_doc_length = sum(document_lengths) / len(document_lengths) if document_lengths else 0.0
    total_documents = len(documents)

    for document in documents:
        for token in set(document):
            document_frequencies[token] += 1

    scores: List[float] = []
    for doc_counts, doc_length in zip(document_term_counts, document_lengths):
        score = 0.0
        norm = k1 * (1.0 - b + b * (doc_length / avg_doc_length)) if avg_doc_length > 0 else k1
        for token, query_tf in query_term_counts.items():
            term_frequency = doc_counts.get(token, 0)
            if term_frequency <= 0:
                continue
            document_frequency = document_frequencies.get(token, 0)
            idf = math.log(1.0 + ((total_documents - document_frequency + 0.5) / (document_frequency + 0.5)))
            numerator = term_frequency * (k1 + 1.0)
            denominator = term_frequency + norm
            score += query_tf * idf * (numerator / denominator)
        scores.append(score)

    return scores


def memory_init(
    problem_id: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sanitized_problem_id = sanitize_problem_id(problem_id)
    problem_dir = _problem_dir(sanitized_problem_id)
    problem_dir.mkdir(parents=True, exist_ok=True)

    created_files: Dict[str, str] = {}
    for channel, filename in CHANNEL_FILES.items():
        channel_path = problem_dir / filename
        channel_path.touch(exist_ok=True)
        created_files[channel] = str(channel_path)

    meta_path = problem_dir / "meta.json"
    existing_meta: Dict[str, Any] = {}
    if meta_path.exists() and meta_path.stat().st_size > 0:
        with meta_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
            if isinstance(loaded, dict):
                existing_meta = loaded

    merged_meta: Dict[str, Any] = {
        "problem_id": sanitized_problem_id,
        "created_at_utc": existing_meta.get("created_at_utc", _utc_now()),
        "updated_at_utc": _utc_now(),
    }
    merged_meta.update(existing_meta)
    if meta:
        merged_meta.update(meta)

    with meta_path.open("w", encoding="utf-8") as handle:
        json.dump(merged_meta, handle, indent=2, ensure_ascii=False)

    return {
        "problem_id": sanitized_problem_id,
        "memory_dir": str(problem_dir),
        "meta_path": str(meta_path),
        "channels": created_files,
    }


def memory_append(
    problem_id: str,
    channel: str,
    record: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")

    memory_init(problem_id)

    entry = {
        "timestamp_utc": _utc_now(),
        "channel": channel,
        "record": record,
    }
    target = _channel_path(problem_id, channel)
    _append_jsonl(target, entry)

    if channel != "events":
        event_entry = {
            "timestamp_utc": _utc_now(),
            "event_type": "memory_append",
            "channel": channel,
        }
        _append_jsonl(_channel_path(problem_id, "events"), event_entry)

    return {
        "status": "ok",
        "channel": channel,
        "path": str(target),
        "entry": entry,
    }


def memory_search(
    problem_id: str,
    query: str,
    channels: Optional[List[str]] = None,
    limit_per_channel: int = 10,
) -> Dict[str, Any]:
    if not query.strip():
        raise ValueError("query must be non-empty")
    if limit_per_channel <= 0:
        raise ValueError("limit_per_channel must be > 0")

    if channels is None:
        search_channels = [name for name in CHANNEL_FILES if name != "events"]
    else:
        search_channels = channels

    results_by_channel: Dict[str, Dict[str, Any]] = {}
    total_results = 0
    for channel in search_channels:
        path = _channel_path(problem_id, channel)
        items = list(_iter_jsonl(path))
        documents = [json.dumps(item, ensure_ascii=False) for item in items]
        tokenized_documents = [tokenize_bm25(document) for document in documents]
        scores = bm25_score_documents(query, tokenized_documents)

        ranked_results: List[Dict[str, Any]] = []
        for item, score in sorted(
            zip(items, scores),
            key=lambda pair: (
                -pair[1],
                pair[0].get("timestamp_utc", ""),
            ),
        ):
            if score <= 0:
                continue
            ranked_results.append(
                {
                    "score": score,
                    "item": item,
                }
            )
            if len(ranked_results) >= limit_per_channel:
                break

        results_by_channel[channel] = {
            "count": len(ranked_results),
            "results": ranked_results,
        }
        total_results += len(ranked_results)

    return {
        "problem_id": sanitize_problem_id(problem_id),
        "query": query,
        "channels": search_channels,
        "limit_per_channel": limit_per_channel,
        "count": total_results,
        "results_by_channel": results_by_channel,
    }


def branch_update(
    problem_id: str,
    branch_id: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    payload = {
        "branch_id": branch_id,
        "state": state,
    }
    return memory_append(problem_id, "branch_states", payload)


# ---------------------------------------------------------------- CLI

def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def _json_arg(raw: str) -> Dict[str, Any]:
    """Parse a JSON object argument; '@path.json' reads the object from a file."""
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    _stdio_utf8()
    ap = argparse.ArgumentParser(prog="mathx.memory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create the channel files for a problem")
    p_init.add_argument("problem_id")
    p_init.add_argument("--meta", help="meta JSON object, or @path.json")

    p_append = sub.add_parser("append", help="append one record to a channel")
    p_append.add_argument("problem_id")
    p_append.add_argument("channel")
    p_append.add_argument("record", help="record JSON object, or @path.json")

    p_search = sub.add_parser("search", help="BM25-search the channels of a problem")
    p_search.add_argument("problem_id")
    p_search.add_argument("query")
    p_search.add_argument("--channels", help="comma-separated channel names (default: all but events)")
    p_search.add_argument("--limit", type=int, default=10, help="limit per channel")

    p_branch = sub.add_parser("branch", help="record a branch state")
    p_branch.add_argument("problem_id")
    p_branch.add_argument("branch_id")
    p_branch.add_argument("state", help="state JSON object, or @path.json")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "init":
            meta = _json_arg(args.meta) if args.meta else None
            _print(memory_init(args.problem_id, meta))
        elif args.cmd == "append":
            _print(memory_append(args.problem_id, args.channel, _json_arg(args.record)))
        elif args.cmd == "search":
            channels = [c.strip() for c in args.channels.split(",") if c.strip()] if args.channels else None
            _print(memory_search(args.problem_id, args.query, channels=channels, limit_per_channel=args.limit))
        elif args.cmd == "branch":
            _print(branch_update(args.problem_id, args.branch_id, _json_arg(args.state)))
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
