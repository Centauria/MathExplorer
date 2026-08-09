"""tests for mathx.harvest hunter dispatch leases (data/hunts/*.json).

Leases are the disk ground truth for live hunter count (cross-session), the
concurrency cap, and field-conflict avoidance. Written BEFORE the task spawns,
removed AFTER settlement.
"""

from __future__ import annotations

import json

import pytest

from mathx import harvest


@pytest.fixture
def hunts_env(tmp_path, monkeypatch):
    monkeypatch.setattr(harvest, "HUNTS_DIR", tmp_path / "hunts")
    return tmp_path


def test_begin_writes_lease(hunts_env):
    lease = harvest.hunt_begin(
        "number-theory", "hunter-test-abc", "data/inbox/x.jsonl",
        model="mathx/step-3.7-flash", quota=1,
    )
    assert lease["agent_name"] == "hunter-test-abc"
    assert lease["field"] == "number-theory"
    assert lease["status"] == "running"
    assert lease["dispatched_at_utc"]
    # file on disk, one per hunter
    path = hunts_env / "hunts" / "hunter-test-abc.json"
    assert path.exists()
    assert json.loads(path.read_text())["field"] == "number-theory"


def test_concurrency_cap_rejects_second(hunts_env):
    harvest.hunt_begin("number-theory", "hunter-a", "data/inbox/a.jsonl")
    with pytest.raises(ValueError, match="concurrency cap"):
        harvest.hunt_begin("algebra", "hunter-b", "data/inbox/b.jsonl")


def test_force_bypasses_concurrency_cap(hunts_env):
    harvest.hunt_begin("number-theory", "hunter-a", "data/inbox/a.jsonl")
    lease = harvest.hunt_begin(
        "algebra", "hunter-b", "data/inbox/b.jsonl", force=True,
    )
    assert lease["agent_name"] == "hunter-b"
    assert len(harvest._active_hunts()) == 2


def test_field_conflict_rejects_same_field(hunts_env, monkeypatch):
    monkeypatch.setattr(harvest, "HUNTER_MAX_CONCURRENCY", 4)
    harvest.hunt_begin("number-theory", "hunter-a", "data/inbox/a.jsonl")
    with pytest.raises(ValueError, match="field conflict"):
        harvest.hunt_begin("number-theory", "hunter-b", "data/inbox/b.jsonl")


def test_end_removes_lease(hunts_env):
    harvest.hunt_begin("number-theory", "hunter-a", "data/inbox/a.jsonl")
    ended = harvest.hunt_end("hunter-a")
    assert ended["agent_name"] == "hunter-a"
    assert not (hunts_env / "hunts" / "hunter-a.json").exists()
    assert harvest.hunts()["active"] == []


def test_end_missing_lease_raises(hunts_env):
    with pytest.raises(KeyError, match="no hunter lease"):
        harvest.hunt_end("nobody")


def test_hunts_lists_active_and_stale(hunts_env):
    harvest.hunt_begin("number-theory", "hunter-a", "data/inbox/a.jsonl")
    out = harvest.hunts()
    assert len(out["active"]) == 1
    assert out["active"][0]["stale"] is False
    # age the lease past the (overridden) threshold → stale
    out2 = harvest.hunts(stale_hours=0)
    assert out2["active"][0]["stale"] is True


def test_concurrency_reads_config_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(harvest, "REPO_ROOT", tmp_path)
    # no config.toml → default 1
    assert harvest._hunter_concurrency_from_config() == 1
    # [hunter] max_concurrency honored
    (tmp_path / "config.toml").write_text(
        "active_provider = 'x'\n[hunter]\nmax_concurrency = 3\n", encoding="utf-8"
    )
    assert harvest._hunter_concurrency_from_config() == 3
    # bad values degrade to 1
    (tmp_path / "config.toml").write_text(
        "[hunter]\nmax_concurrency = 'two'\n", encoding="utf-8"
    )
    assert harvest._hunter_concurrency_from_config() == 1
    (tmp_path / "config.toml").write_text("not [ valid toml", encoding="utf-8")
    assert harvest._hunter_concurrency_from_config() == 1
