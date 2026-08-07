"""config.toml — the single configuration source for the MathExplorer LLM fleet.

All reads/writes of config.toml go through this module: stdlib ``tomllib`` for
reading, a fixed-section-order full-file rewrite for writing (comments are NOT
preserved — the file is machine-managed). Pure-local modules (memory, runstate,
harvest core) never import this module, so the system keeps working locally if
config.toml disappears entirely.

CLI:
    uv run python -m mathx.config get <dot.path>
    uv run python -m mathx.config set <dot.path> <value>
    uv run python -m mathx.config show [--masked]
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config.toml"


class NoProviderError(RuntimeError):
    """No usable LLM provider: config.toml missing/incomplete, or all keys rejected."""


@dataclass
class ProviderConfig:
    api: str
    keys: list[str] = field(default_factory=list)


@dataclass
class Config:
    active_provider: str
    providers: dict[str, ProviderConfig]
    roles: dict[str, list[str]]
    quota: dict
    search: dict
    raw: dict  # untouched parsed TOML


# ---------------------------------------------------------------- load / validate

def load_config() -> Config:
    """Parse and validate config.toml. Raises NoProviderError on any defect."""
    if not CONFIG_PATH.exists():
        raise NoProviderError(
            f"config.toml not found at {CONFIG_PATH} — no LLM provider configured. "
            "Run `/config setup` or create the file manually."
        )
    try:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise NoProviderError(f"config.toml unreadable: {e}") from e
    providers: dict[str, ProviderConfig] = {}
    for name, section in (data.get("providers") or {}).items():
        section = section or {}
        providers[name] = ProviderConfig(
            api=str(section.get("api", "")),
            keys=[str(k) for k in section.get("keys", [])],
        )
    roles = {k: [str(s) for s in v] for k, v in (data.get("roles") or {}).items()}
    if not roles.get("default"):
        raise NoProviderError("config.toml: [roles] must define a non-empty 'default' selector list")
    for role_name, selectors in roles.items():
        for sel in selectors:
            if "/" not in sel:
                raise NoProviderError(
                    f"config.toml: roles.{role_name} selector {sel!r} is not '<provider>/<model>'"
                )
            provider_name = sel.split("/", 1)[0]
            if provider_name not in providers:
                raise NoProviderError(
                    f"config.toml: roles.{role_name} references unknown provider {provider_name!r}"
                )
            if not providers[provider_name].keys:
                raise NoProviderError(f"config.toml: provider {provider_name!r} has an empty keys pool")
    return Config(
        active_provider=str(data.get("active_provider", "")),
        providers=providers,
        roles=roles,
        quota=dict(data.get("quota") or {}),
        search=dict(data.get("search") or {}),
        raw=data,
    )


# ---------------------------------------------------------------- TOML writing

def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_value(v) -> str:
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot serialize {type(v).__name__} to TOML")


def _dump_toml(data: dict) -> str:
    """Fixed section order: active_provider -> [quota] -> [providers.*] (alpha) -> [roles] -> [search]."""
    lines: list[str] = []
    if data.get("active_provider"):
        lines.append(f"active_provider = {_toml_str(str(data['active_provider']))}")
        lines.append("")
    quota = data.get("quota") or {}
    if quota:
        lines.append("[quota]")
        for k in ("cmd", "used_re", "threshold"):
            if k in quota:
                lines.append(f"{k} = {_toml_value(quota[k])}")
        for k in sorted(quota):
            if k not in ("cmd", "used_re", "threshold"):
                lines.append(f"{k} = {_toml_value(quota[k])}")
        lines.append("")
    providers = data.get("providers") or {}
    for name in sorted(providers):
        section = providers[name] or {}
        lines.append(f"[providers.{name}]")
        if "api" in section:
            lines.append(f"api = {_toml_value(section['api'])}")
        if "keys" in section:
            lines.append(f"keys = {_toml_value(section['keys'])}")
        for k in sorted(section):
            if k not in ("api", "keys"):
                lines.append(f"{k} = {_toml_value(section[k])}")
        lines.append("")
    roles = data.get("roles") or {}
    if roles:
        lines.append("[roles]")
        for k in roles:  # keep document order
            lines.append(f"{k} = {_toml_value(roles[k])}")
        lines.append("")
    search = data.get("search") or {}
    if search:
        lines.append("[search]")
        for k in sorted(search):
            lines.append(f"{k} = {_toml_value(search[k])}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _parse_csv(raw: str) -> list[str]:
    return [item.strip().strip('"').strip("'") for item in raw.split(",") if item.strip()]


_QUOTA_KEYS = ("cmd", "used_re", "threshold")
_SEARCH_KEYS = ("leansearch_endpoint",)
_PROVIDER_KEYS = ("api", "keys")


def set_value(dotpath: str, raw: str) -> None:
    """Set one value and rewrite config.toml wholesale. Works on a missing file (recreate)."""
    if CONFIG_PATH.exists():
        try:
            data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise NoProviderError(f"config.toml unreadable, refusing to rewrite: {e}") from e
    else:
        data = {}
    parts = dotpath.split(".")
    if dotpath == "active_provider":
        data["active_provider"] = raw
    elif len(parts) == 2 and parts[0] == "quota" and parts[1] in _QUOTA_KEYS:
        section = data.setdefault("quota", {})
        section[parts[1]] = float(raw) if parts[1] == "threshold" else raw
    elif len(parts) == 2 and parts[0] == "search" and parts[1] in _SEARCH_KEYS:
        section = data.setdefault("search", {})
        section[parts[1]] = raw
    elif len(parts) == 2 and parts[0] == "roles":
        data.setdefault("roles", {})[parts[1]] = _parse_csv(raw)
    elif len(parts) == 3 and parts[0] == "providers" and parts[2] in _PROVIDER_KEYS:
        section = data.setdefault("providers", {}).setdefault(parts[1], {})
        section[parts[2]] = raw if parts[2] == "api" else _parse_csv(raw)
    else:
        raise ValueError(
            f"unknown config path: {dotpath!r} (supported: active_provider, "
            "quota.cmd|used_re|threshold, search.leansearch_endpoint, roles.<name>, "
            "providers.<name>.api, providers.<name>.keys)"
        )
    CONFIG_PATH.write_text(_dump_toml(data), encoding="utf-8")


# ---------------------------------------------------------------- masking / display

def _mask(key: str) -> str:
    return f"{key[:6]}...(len={len(key)})"


def _masked_copy(data: dict) -> dict:
    masked = copy.deepcopy(data)
    for section in (masked.get("providers") or {}).values():
        if isinstance(section, dict) and "keys" in section:
            section["keys"] = [_mask(str(k)) for k in section["keys"]]
    return masked


def masked_config_text() -> str:
    if not CONFIG_PATH.exists():
        return "config.toml: NOT FOUND"
    try:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        return f"config.toml: UNREADABLE ({e})"
    return _dump_toml(_masked_copy(data))


def _get_path(data: dict, dotpath: str):
    node = data
    for part in dotpath.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"config path not found: {dotpath!r}")
        node = node[part]
    if dotpath.endswith(".keys") and isinstance(node, list):
        return [_mask(str(k)) for k in node]
    return node


# ---------------------------------------------------------------- CLI

def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _stdio_utf8()
    ap = argparse.ArgumentParser(prog="mathx.config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="print one config value (keys are masked)")
    g.add_argument("path")
    s = sub.add_parser("set", help="set one config value and rewrite config.toml")
    s.add_argument("path")
    s.add_argument("value")
    sh = sub.add_parser("show", help="print the whole config (always key-masked)")
    sh.add_argument("--masked", action="store_true", help="accepted for explicitness; output is always masked")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "get":
            if not CONFIG_PATH.exists():
                raise NoProviderError(f"config.toml not found at {CONFIG_PATH}")
            data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            print(json.dumps(_get_path(data, args.path), ensure_ascii=False))
        elif args.cmd == "set":
            set_value(args.path, args.value)
            print(json.dumps({"ok": True, "path": args.path}, ensure_ascii=False))
        elif args.cmd == "show":
            print(masked_config_text())
        return 0
    except (NoProviderError, ValueError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
