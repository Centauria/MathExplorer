"""gateway.py — config.toml-driven LLM key-pooling reverse proxy.

A single local endpoint that lets ANY HTTP client (omp, scripts, tests) share
the provider pools declared in config.toml — the single source of truth for
endpoints and keys. The gateway is deliberately LLM-agnostic: it never parses
response bodies and only reads the request's "model" field to pick a provider,
so /chat/completions, /responses, /messages (Anthropic wire), and any future
path are all covered by the same transparent forwarding.

Per request:
  1. resolve provider via the model map built from config.toml [roles]
     selectors ("<provider>/<model>"); unknown models fall back to
     active_provider, then to roles.default's provider;
  2. acquire a key from the provider's round-robin pool (dead-key tracking:
     401/402/403 kill a key for the process lifetime; 429/5xx/network errors
     fail over to the next key, both only BEFORE any byte reaches the client);
  3. forward method + path + query + body verbatim, stripping client auth and
     injecting the pooled key (Authorization: Bearer, or x-api-key for
     */messages paths);
  4. stream the upstream response back raw (SSE-friendly, no buffering).

GET /v1/models synthesizes one entry per known model with
supported_endpoint_types derived from each provider's optional `wire` key in
config.toml (default "openai") — point omp's `discovery.type: proxy` at the
gateway and every model's wire protocol is bound once at discovery time.

GET /healthz reports pool sizes, dead keys, and per-key request counts.

CLI:
    uv run python -m mathx.gateway [--host 127.0.0.1] [--port 8399]
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys

import httpx
import uvicorn
from pathlib import Path
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from mathx.config import NoProviderError, load_config

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}

AUTH_HEADERS = {"authorization", "x-api-key"}


class KeyPool:
    """Round-robin key pool with sticky dead-key tracking (per provider)."""

    def __init__(self, keys: list[str]) -> None:
        self._cycle = itertools.cycle(keys)
        self.size = len(keys)
        self.dead: set[str] = set()
        self.used: dict[str, int] = {}

    def acquire(self) -> str:
        for _ in range(self.size):
            key = next(self._cycle)
            if key not in self.dead:
                self.used[key] = self.used.get(key, 0) + 1
                return key
        raise NoProviderError(f"all {self.size} key(s) of the pool are dead")

    def mark_dead(self, key: str) -> None:
        self.dead.add(key)

    def stats(self) -> dict:
        return {"size": self.size, "dead": len(self.dead), "used": self.used}


def _provider_wire(cfg, name: str) -> str:
    """Optional per-provider `wire = "anthropic"` in config.toml (default openai)."""
    section = (cfg.raw.get("providers") or {}).get(name) or {}
    return str(section.get("wire", "openai")).lower()


def build_model_map(cfg) -> dict[str, str]:
    """model id (and full 'provider/model' selector) -> provider name."""
    out: dict[str, str] = {}
    for selectors in cfg.roles.values():
        for sel in selectors:
            provider, model = sel.split("/", 1)
            out[sel] = provider
            out.setdefault(model, provider)
    return out


def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def make_app(cfg) -> Starlette:
    providers = cfg.providers
    pools = {name: KeyPool(p.keys) for name, p in providers.items()}
    model_map = build_model_map(cfg)
    default_provider = (
        cfg.active_provider
        if cfg.active_provider in providers
        else cfg.roles["default"][0].split("/", 1)[0]
    )
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=600, write=120, pool=10))

    def resolve_provider(body: bytes) -> str:
        try:
            model = json.loads(body).get("model")
        except (json.JSONDecodeError, AttributeError):
            model = None
        if isinstance(model, str):
            if model in model_map:
                return model_map[model]
            if "/" in model and model.split("/", 1)[0] in providers:
                return model.split("/", 1)[0]
        return default_provider

    def upstream_headers(request: Request, key: str) -> dict[str, str]:
        headers = {
            k.lower(): v
            for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() not in AUTH_HEADERS
        }
        if request.url.path.rstrip("/").endswith("/messages"):
            headers["x-api-key"] = key
        else:
            headers["authorization"] = f"Bearer {key}"
        return headers

    async def relay(resp: httpx.Response) -> Response:
        async def streamer():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
        return StreamingResponse(streamer(), status_code=resp.status_code, headers=headers)

    async def proxy(request: Request) -> Response:
        body = await request.body()
        provider_name = resolve_provider(body)
        provider = providers[provider_name]
        pool = pools[provider_name]
        # The gateway exposes a /v1/* namespace; provider.api already carries
        # the version prefix, so strip one leading "/v1" before joining.
        path = request.url.path
        if path.startswith("/v1/"):
            path = path[len("/v1"):]
        url = provider.api.rstrip("/") + path
        backoff = 0.5
        for _attempt in range(pool.size):
            try:
                key = pool.acquire()
            except NoProviderError:
                break
            req = client.build_request(
                request.method,
                url,
                params=dict(request.query_params),
                headers=upstream_headers(request, key),
                content=body,
            )
            try:
                resp = await client.send(req, stream=True)
            except httpx.HTTPError:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 4.0)
                continue
            if resp.status_code in (401, 402, 403):
                pool.mark_dead(key)
                await resp.aclose()
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                await resp.aclose()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 4.0)
                continue
            # 2xx streams through; other 4xx are caller's errors — relay verbatim.
            return await relay(resp)
        return JSONResponse(
            {"error": {"message": f"gateway: provider {provider_name!r} key pool exhausted"}},
            status_code=503,
        )

    async def models(_request: Request) -> Response:
        data = [
            {
                "id": model,
                "object": "model",
                "owned_by": provider,
                "supported_endpoint_types": [_provider_wire(cfg, provider)],
            }
            for sel, provider in sorted(model_map.items())
            if "/" in sel
            for model in [sel.split("/", 1)[1]]
        ]
        return JSONResponse({"object": "list", "data": data})

    async def healthz(_request: Request) -> Response:
        return JSONResponse(
            {
                "ok": True,
                "default_provider": default_provider,
                "models": sorted(k for k in model_map if "/" in k),
                "pools": {name: pool.stats() for name, pool in pools.items()},
            }
        )

    import contextlib

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        yield
        await client.aclose()

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/v1/models", models, methods=["GET"]),
            Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]),
        ],
        lifespan=lifespan,
    )


def main(argv: list[str] | None = None) -> int:
    _stdio_utf8()
    ap = argparse.ArgumentParser(prog="mathx.gateway")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8399)
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
    except NoProviderError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    app = make_app(cfg)
    pid_path = Path(__file__).resolve().parents[1] / "logs" / "gateway.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    print(f"mathx.gateway listening on http://{args.host}:{args.port} (providers: {', '.join(cfg.providers)})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
