"""tests for mathx.agents — deterministic model-bearing worker names + run.json stamping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mathx import agents
from mathx.agents import agent_name, model_tag, resolve_model, stamp


@pytest.fixture(autouse=True)
def _isolate_results(tmp_path, monkeypatch):
    """Route runstate writes to a temp results root."""
    import mathx.runstate

    monkeypatch.setattr(mathx.runstate, "RESULTS_ROOT", tmp_path / "results")
    yield


# ---------------------------------------------------------------- model tag

def test_model_tag_drops_provider_and_sampling_suffix():
    assert model_tag("mathx/step-3.7-flash") == "step37Flash"
    assert model_tag("mathx/step-3.7-flash:xhigh") == "step37Flash"
    assert model_tag("kimi-code/k3") == "k3"
    assert model_tag("medeli/deepseek-v4-flash:xhigh") == "deepseekV4Flash"
    assert model_tag("SharedGLM/glm-5.2:xhigh") == "glm52"
    assert model_tag("openai/gpt-5.2") == "gpt52"


# ---------------------------------------------------------------- name

@pytest.mark.parametrize("role", ["solver", "hunter", "referee-1", "referee-2", "referee-3"])
def test_name_embeds_model(role):
    n = agent_name(role, "number-theory/abc-conjecture")
    assert n.startswith({"solver": "solver-", "hunter": "hunter-", "referee-1": "referee1-",
                         "referee-2": "referee2-", "referee-3": "referee3-"}[role])
    assert model_tag(resolve_model(role)) in n  # the model tag is inside the name
    assert len(n) <= agents.MAX_NAME_LEN


def test_name_is_deterministic():
    assert agent_name("solver", "number-theory/abc-conjecture") == agent_name(
        "solver", "number-theory/abc-conjecture"
    )


def test_name_sanitizes_suffix():
    n = agent_name("hunter", "combinatorics")  # short: no truncation
    assert n == "hunter-step37Flash-combinatorics"
    assert all(c.isalnum() or c in "-_" for c in n)


def test_name_truncation_still_unique_per_suffix():
    long_a = "number-theory/a-very-long-title-that-never-fits"
    long_b = "number-theory/a-very-long-title-that-never-fitx"
    na = agent_name("solver", long_a)
    nb = agent_name("solver", long_b)
    assert len(na) <= agents.MAX_NAME_LEN
    assert na != nb  # hash tail keeps distinct suffixes distinct
    assert na.startswith("solver-k3-")


def test_name_keeps_suffix_when_tag_is_long():
    # referee-1's model tag (deepseekV4Flash) is long; the suffix must survive
    n = agent_name("referee-1", "number-theory/abc-conjecture")
    assert n.startswith("referee1-deepseekV4Flash-")
    assert len(n) <= agents.MAX_NAME_LEN
    assert n != agent_name("referee-1", "number-theory/xyz-conjecture")


def test_unknown_role_raises():
    with pytest.raises(agents.AgentNameError):
        agent_name("not-a-role", "x")
    with pytest.raises(agents.AgentNameError):
        resolve_model("not-a-role")


# ---------------------------------------------------------------- stamp

def test_stamp_writes_agent_into_run_json(tmp_path):
    pid = "algebra/modrep"
    name = agent_name("solver", pid)
    state = stamp(pid, name, resolve_model("solver"))
    assert state["agent"] == {"name": name, "model": "kimi-code/k3"}

    run_path = tmp_path / "results" / "algebra" / "modrep" / "run.json"
    assert run_path.exists()
    on_disk = json.loads(run_path.read_text(encoding="utf-8"))
    assert on_disk["agent"]["name"] == name
    # stamping pre-inits the runstate so a later solver init stays a no-op
    assert on_disk["status"] == "running"
    assert on_disk["iteration"] == 0


def test_stamp_is_idempotent_and_preserves_existing_state(tmp_path):
    import mathx.runstate as rs

    pid = "combinatorics/ramsey"
    rs.runstate_init(pid, max_iterations=5)
    rs.runstate_advance(pid, note="iter one")
    stamp(pid, "solver-k3-x", "kimi-code/k3")
    state = json.loads((tmp_path / "results" / "combinatorics" / "ramsey" / "run.json").read_text(encoding="utf-8"))
    assert state["max_iterations"] == 5
    assert state["iteration"] == 1
    assert state["agent"] == {"name": "solver-k3-x", "model": "kimi-code/k3"}


def test_stamp_rejects_empty_identity():
    with pytest.raises(ValueError):
        stamp("algebra/modrep", "", "kimi-code/k3")
    with pytest.raises(ValueError):
        stamp("algebra/modrep", "solver-k3-x", "")


# ---------------------------------------------------------------- CLI

def test_cli_name_outputs_json(capsys):
    from mathx.agents import main

    assert main(["name", "solver", "algebra/modrep"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"].startswith("solver-k3-")
    assert out["role"] == "solver"
    assert out["model"] == "kimi-code/k3"


def test_cli_errors_on_bad_role(capsys):
    from mathx.agents import main

    assert main(["name", "bogus", "x"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert "unknown role" in err["error"]


def test_missing_omp_config(capsys, monkeypatch):
    from mathx.agents import main

    monkeypatch.setattr(agents, "OMP_CONFIG", Path("/nonexistent/.omp/config.yml"))
    assert main(["model", "solver"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert "modelRoles config not found" in err["error"]
