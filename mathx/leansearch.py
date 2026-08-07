"""leansearch.py — theorem/lemma/definition retrieval, ported from Rethlas.

Same endpoint contract as upstream search_arxiv_theorems (requests -> httpx).
The endpoint defaults to https://leansearch.net/thm/search and can be overridden
by the optional [search].leansearch_endpoint key in config.toml; if config.toml
is missing or unreadable the default is used (this module never hard-requires
the LLM config).

CLI:
    uv run python -m mathx.leansearch "query" [--num 10]

Exit code 2 = network/HTTP error (caller falls back to web_search).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

import httpx

THEOREM_SEARCH_URL = "https://leansearch.net/thm/search"
THEOREM_SEARCH_TASK = (
    "Given a math statement, retrieve useful references, such as theorems, "
    "lemmas, and definitions, that are useful for solving the given problem."
)


def default_endpoint() -> str:
    """config.toml [search].leansearch_endpoint if readable, else the built-in URL."""
    try:
        from mathx.config import load_config

        endpoint = load_config().search.get("leansearch_endpoint")
        if isinstance(endpoint, str) and endpoint.strip():
            return endpoint.strip()
    except Exception:
        pass
    return THEOREM_SEARCH_URL


def search_arxiv_theorems(
    query: str,
    num_results: int = 10,
    endpoint: str | None = None,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    if not query.strip():
        raise ValueError("query must be non-empty")
    if num_results <= 0:
        raise ValueError("num_results must be > 0")
    endpoint = endpoint or default_endpoint()

    payload = {
        "query": query,
        "task": THEOREM_SEARCH_TASK,
        "num_results": num_results,
    }

    response = httpx.post(endpoint, json=payload, timeout=timeout_seconds)
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, list):
        raise ValueError("The theorem endpoint must return a JSON list")

    normalized: list[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "title": str(item.get("title", "")),
                "theorem": str(item.get("theorem", "")),
                "arxiv_id": str(item.get("arxiv_id", "")),
                "theorem_id": str(item.get("theorem_id", "")),
            }
        )

    return {
        "query": query,
        "count": len(normalized),
        "results": normalized,
        "endpoint": endpoint,
    }


def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _stdio_utf8()
    ap = argparse.ArgumentParser(prog="mathx.leansearch")
    ap.add_argument("query")
    ap.add_argument("--num", type=int, default=10, help="number of results")
    args = ap.parse_args(argv)
    try:
        result = search_arxiv_theorems(args.query, num_results=args.num)
    except (httpx.HTTPError, ValueError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
