"""agents.py — deterministic worker agent naming + archive stamping.

Every MathExplorer worker (solver / hunter / referee-N) is spawned with a
deterministic `name` that embeds the model it runs on, so the archives
(run.json `agent` field, referee vN.json `referee` meta, transcripts) show
which model produced / reviewed each result:

    <role>-<modelTag>[-<suffix>]      e.g. solver-kimiCodeK3-algebra-modrep

The model source of truth is `.omp/config.yml` `modelRoles` — the exact
binding the omp frontmatter (`model: "@<role>"`) resolves to, so the tag
never drifts from what the worker actually runs.

CLI:
    uv run python -m mathx.agents name <role> <suffix>
        → {"name": "solver-kimiCodeK3-<suffix>", "role": ..., "model": ...}
    uv run python -m mathx.agents model <role>
        → {"role": ..., "model": "kimi-code/k3"}
    uv run python -m mathx.agents stamp <problem_id> <agent_name> <model>
        → writes results/<problem_id>/run.json "agent" = {name, model}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OMP_CONFIG = REPO_ROOT / ".omp" / "config.yml"

MAX_NAME_LEN = 32  # task-tool name cap: role + modelTag + suffix must fit

# omp agent type → modelRoles key in .omp/config.yml
ROLE_KEYS = {
    "solver": "mathx_solver",
    "hunter": "mathx_hunter",
    "referee-1": "referee_1",
    "referee-2": "referee_2",
    "referee-3": "referee_3",
}
# short role tag used inside the name (dash-free so parts stay parseable)
ROLE_TAGS = {
    "solver": "solver",
    "hunter": "hunter",
    "referee-1": "referee1",
    "referee-2": "referee2",
    "referee-3": "referee3",
}


class AgentNameError(RuntimeError):
    """Unresolvable role/model (bad role, or .omp/config.yml missing/defective)."""


def _read_model_roles() -> dict[str, str]:
    """Parse `modelRoles:` from .omp/config.yml (flat `role: model` lines, stdlib only)."""
    if not OMP_CONFIG.exists():
        raise AgentNameError(f"omp modelRoles config not found: {OMP_CONFIG}")
    roles: dict[str, str] = {}
    in_roles = False
    for line in OMP_CONFIG.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\s*modelRoles\s*:", line):
            in_roles = True
            continue
        if in_roles:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line[:1].isspace():
                in_roles = False  # back at a top-level key
                continue
            key, _, val = line.strip().partition(":")
            if key and val.strip():
                roles[key.strip()] = val.strip()
    return roles


def resolve_model(role: str) -> str:
    """Raw model string the omp worker role runs on, e.g. 'kimi-code/k3'."""
    key = ROLE_KEYS.get(role)
    if key is None:
        raise AgentNameError(f"unknown role {role!r}; expected one of {sorted(ROLE_KEYS)}")
    roles = _read_model_roles()
    model = roles.get(key)
    if not model:
        raise AgentNameError(f"role {role!r} ({key}) missing from {OMP_CONFIG} modelRoles")
    return model


def model_tag(model: str) -> str:
    """Compact camel tag from a model string, e.g. 'mathx/step-3.7-flash' → 'step37Flash'.

    Drops the provider prefix (last '/' segment) and any ':<suffix>' sampling
    spec, then camel-cases the remaining parts.
    """
    core = model.rsplit("/", 1)[-1].split(":", 1)[0]
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", core) if p]
    if not parts:
        raise AgentNameError(f"cannot derive a model tag from {model!r}")
    return parts[0].lower() + "".join(p.capitalize() for p in parts[1:])


def agent_name(role: str, suffix: str) -> str:
    """Deterministic dispatch name embedding the model: <role>-<modelTag>-<suffix>.

    The suffix (problem_id / hunt field) is sanitized to [A-Za-z0-9_-]. When the
    full name would exceed MAX_NAME_LEN, both the tag and the suffix are trimmed
    (suffix keeps at least 1 char) and a 4-char hash of the untruncated parts is
    appended, so the name stays unique per (role, model, suffix) while the role
    and the model fragment are always preserved.
    """
    model = resolve_model(role)
    tag = model_tag(model)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", suffix).strip("-") or "x"
    role_tag = ROLE_TAGS[role]

    joined = f"{role_tag}-{tag}-{safe}"
    if len(joined) <= MAX_NAME_LEN:
        return joined

    digest = hashlib.sha1(f"{tag}|{safe}".encode("utf-8")).hexdigest()[:4]
    room = MAX_NAME_LEN - len(role_tag) - 7  # fixed: "-tag-" + "-" + hash(4)
    suffix_budget = max(1, room - len(tag))
    tag_budget = room - suffix_budget
    tag_part = tag if tag_budget >= len(tag) else tag[:tag_budget]
    return f"{role_tag}-{tag_part}-{safe[:suffix_budget]}-{digest}"


def stamp(problem_id: str, agent_name_value: str, model: str) -> dict:
    """Persist worker identity into results/<problem_id>/run.json (`agent` field)."""
    from mathx.runstate import set_agent  # local: runstate owns the run.json schema

    return set_agent(problem_id, agent_name_value, model)


# ---------------------------------------------------------------- CLI

def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    _stdio_utf8()
    ap = argparse.ArgumentParser(prog="mathx.agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_name = sub.add_parser("name")
    p_name.add_argument("role", help="solver | hunter | referee-1|2|3")
    p_name.add_argument("suffix", help="problem_id (or hunt field for hunter)")
    p_model = sub.add_parser("model")
    p_model.add_argument("role")
    p_stamp = sub.add_parser("stamp")
    p_stamp.add_argument("problem_id")
    p_stamp.add_argument("agent_name")
    p_stamp.add_argument("model")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "name":
            result = {
                "name": agent_name(args.role, args.suffix),
                "role": args.role,
                "model": resolve_model(args.role),
            }
        elif args.cmd == "model":
            result = {"role": args.role, "model": resolve_model(args.role)}
        elif args.cmd == "stamp":
            result = stamp(args.problem_id, args.agent_name, args.model)
        _print(result)
        return 0
    except (AgentNameError, ValueError, FileNotFoundError, KeyError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
