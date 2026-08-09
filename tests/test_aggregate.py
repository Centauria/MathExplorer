"""tests for mathx.aggregate — referee model meta passthrough into verification.json."""

from __future__ import annotations

import json

import pytest

from mathx import aggregate


@pytest.fixture
def result_root(tmp_path, monkeypatch):
    monkeypatch.setattr(aggregate, "REPO_ROOT", tmp_path)
    return tmp_path


def _write_verdict(root, pid, i, verdict="correct", referee=None):
    path = root / "results" / pid / "referee" / f"v{i}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "verification_report": {"summary": f"s{i}", "critical_errors": [], "gaps": []},
        "verdict": verdict,
        "repair_hints": "",
    }
    if referee is not None:
        obj = {"referee": referee, **obj}
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def test_referees_array_carries_model_meta(result_root):
    pid = "demo"
    _write_verdict(result_root, pid, 1, referee={"name": "referee1-deepseekV4Flash-nu-e21e",
                                                 "model": "medeli/deepseek-v4-flash:xhigh"})
    _write_verdict(result_root, pid, 2)  # no meta supplied → empty strings
    _write_verdict(result_root, pid, 3, referee={"name": "referee3-step37Flash-number-ed2f",
                                                 "model": "mathx/step-3.7-flash:xhigh"})

    final = aggregate.aggregate(pid)
    assert final["referees"] == [
        {"index": 1, "name": "referee1-deepseekV4Flash-nu-e21e", "model": "medeli/deepseek-v4-flash:xhigh"},
        {"index": 2, "name": "", "model": ""},
        {"index": 3, "name": "referee3-step37Flash-number-ed2f", "model": "mathx/step-3.7-flash:xhigh"},
    ]
    # verdict logic is unchanged by the meta
    assert final["verdict"] == "correct"


def test_missing_report_gets_empty_referee_slot(result_root):
    pid = "demo"
    _write_verdict(result_root, pid, 1, referee={"name": "r1", "model": "m1"})
    _write_verdict(result_root, pid, 2, referee={"name": "r2", "model": "m2"})
    # v3.json missing

    final = aggregate.aggregate(pid)
    assert final["verdict"] == "wrong"  # missing report counts as wrong (unchanged rule)
    assert final["referees"][2] == {"index": 3, "name": "", "model": ""}


def test_verification_json_persists_referees(result_root):
    pid = "demo"
    _write_verdict(result_root, pid, 1, referee={"name": "r1", "model": "m1"})
    _write_verdict(result_root, pid, 2, referee={"name": "r2", "model": "m2"})
    _write_verdict(result_root, pid, 3, referee={"name": "r3", "model": "m3"})
    aggregate.aggregate(pid)
    persisted = json.loads((result_root / "results" / pid / "verification.json").read_text(encoding="utf-8"))
    assert [r["model"] for r in persisted["referees"]] == ["m1", "m2", "m3"]
