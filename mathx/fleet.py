"""fleet.py — provider/role selector routing for batch LLM calls (omp modelRoles style).

Selector syntax: ``<provider>/<model>``. Roles map to selector lists in
config.toml ``[roles]``; multiple selectors rotate by task index (never random —
reproducible and coverage-guaranteed). Per-provider concurrency is bounded by a
semaphore of ``min(concurrency, len(provider keys))`` — the key pool size IS the
pool's concurrency. Error handling is purely error-code driven (no balance
querying): transient errors rotate keys with backoff, auth/quota errors rotate
keys and raise NoProviderError when the whole pool is dead.

CLI:
    uv run python -m mathx.fleet tasks.json -o out.json
    uv run python -m mathx.fleet --smoke

Exit code 3 = NoProviderError (caller falls back to the omp-subagent path).
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
from pathlib import Path

import httpx

from mathx.config import NoProviderError, load_config

NO_PROVIDER_MESSAGE = (
    "no LLM provider configured or all keys rejected "
    "(config.toml missing/incomplete/exhausted)"
)


class FleetError(RuntimeError):
    """Non-retryable fleet failure (unexpected 4xx, or transient retries exhausted)."""


_key_cycles: dict[str, "itertools.cycle[str]"] = {}


def _next_key(provider_name: str, keys: list[str]) -> str:
    cyc = _key_cycles.get(provider_name)
    if cyc is None:
        cyc = itertools.cycle(keys)
        _key_cycles[provider_name] = cyc
    return next(cyc)


def resolve_selector(cfg, role: str | None, index: int) -> tuple[str, str]:
    """Resolve a role + task index to (provider_name, model). Rotation, not random."""
    lst = cfg.roles.get(role or "") or cfg.roles["default"]
    sel = lst[index % len(lst)]
    provider_name, model = sel.split("/", 1)
    return provider_name, model


async def chat(
    messages: list[dict],
    *,
    role: str | None = None,
    index: int = 0,
    max_tokens: int = 16384,
    temperature: float = 0.6,
    timeout: float = 300.0,
    key: str | None = None,
) -> dict:
    """One chat completion. Returns {"content", "reasoning_content", "usage"}."""
    cfg = load_config()
    provider_name, model = resolve_selector(cfg, role, index)
    provider = cfg.providers[provider_name]
    url = provider.api.rstrip("/") + "/chat/completions"
    pool_size = 1 if key else len(provider.keys)
    dead_keys: set[str] = set()
    transient_left = 3
    backoff = 1.0
    token_halved = False
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            k = key or _next_key(provider_name, provider.keys)
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            try:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
                    json=payload,
                )
            except httpx.HTTPError as e:
                if transient_left > 0:
                    transient_left -= 1
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 4.0)
                    continue
                raise FleetError(f"network error after retries: {e}") from e
            if resp.status_code == 200:
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                return {
                    "content": msg.get("content") or "",
                    "reasoning_content": msg.get("reasoning_content") or "",
                    "usage": data.get("usage") or {},
                }
            body = resp.text[:500]
            status = resp.status_code
            if status in (401, 402, 403):
                dead_keys.add(k)
                if len(dead_keys) >= pool_size:
                    raise NoProviderError(
                        f"all {pool_size} key(s) of provider {provider_name!r} rejected (HTTP {status})"
                    )
                continue  # next key in the pool
            if status == 400 and "max_tokens" in body and not token_halved and max_tokens > 1024:
                max_tokens = max(1024, max_tokens // 2)
                token_halved = True
                continue
            if status == 429 or status >= 500:
                if transient_left > 0:
                    transient_left -= 1
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 4.0)
                    continue
                raise FleetError(f"transient HTTP {status} after retries: {body}")
            raise FleetError(f"HTTP {status}: {body}")


async def run_batch(tasks: list[dict], *, concurrency: int = 4) -> list[dict]:
    """Run tasks concurrently. Per-provider semaphore = min(concurrency, pool size).

    Task: {"id", "prompt", "system"?, "role"?, "max_tokens"?, "temperature"?}.
    With a role, the task's array index is passed as the selector index, so
    multi-model role lists spread naturally. Results keep input order; each is
    {"id", "ok", "content", "reasoning_content", "usage", "error"?}.
    NoProviderError propagates (it is a global failure, not a per-task one).
    """
    cfg = load_config()
    semaphores: dict[str, asyncio.Semaphore] = {}

    def semaphore_for(role: str | None, index: int) -> asyncio.Semaphore:
        provider_name, _ = resolve_selector(cfg, role, index)
        sem = semaphores.get(provider_name)
        if sem is None:
            sem = asyncio.Semaphore(min(concurrency, len(cfg.providers[provider_name].keys)))
            semaphores[provider_name] = sem
        return sem

    async def worker(index: int, task: dict) -> dict:
        role = task.get("role")
        messages: list[dict] = []
        if task.get("system"):
            messages.append({"role": "system", "content": task["system"]})
        messages.append({"role": "user", "content": task["prompt"]})
        async with semaphore_for(role, index):
            try:
                r = await chat(
                    messages,
                    role=role,
                    index=index,
                    max_tokens=int(task.get("max_tokens", 16384)),
                    temperature=float(task.get("temperature", 0.6)),
                )
            except NoProviderError:
                raise
            except Exception as e:
                return {
                    "id": task.get("id", str(index)),
                    "ok": False,
                    "content": "",
                    "reasoning_content": "",
                    "usage": {},
                    "error": f"{type(e).__name__}: {e}",
                }
        return {
            "id": task.get("id", str(index)),
            "ok": True,
            "content": r["content"],
            "reasoning_content": r["reasoning_content"],
            "usage": r["usage"],
        }

    return list(await asyncio.gather(*(worker(i, t) for i, t in enumerate(tasks))))


def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _stdio_utf8()
    ap = argparse.ArgumentParser(prog="mathx.fleet")
    ap.add_argument("tasks", nargs="?", help="tasks JSON file: {'tasks': [...]} or a bare array")
    ap.add_argument("-o", "--out", help="write {'results': [...]} here (default: stdout)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--smoke", action="store_true", help="one-shot probe via roles.default[0]")
    args = ap.parse_args(argv)
    try:
        if args.smoke:
            r = asyncio.run(
                chat([{"role": "user", "content": "Reply with exactly: OK"}], role="default", index=0)
            )
            print(r["content"])
            return 0
        if not args.tasks:
            ap.error("tasks file required unless --smoke")
        raw = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
        tasks = raw["tasks"] if isinstance(raw, dict) else raw
        results = asyncio.run(run_batch(tasks, concurrency=args.concurrency))
        out = json.dumps({"results": results}, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(out + "\n", encoding="utf-8")
        else:
            print(out)
        if results and all((not r["ok"]) and "NoProviderError" in r.get("error", "") for r in results):
            print(json.dumps({"error": NO_PROVIDER_MESSAGE}), file=sys.stderr)
            return 3
        return 0
    except NoProviderError:
        print(json.dumps({"error": NO_PROVIDER_MESSAGE}), file=sys.stderr)
        return 3
    except FleetError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
