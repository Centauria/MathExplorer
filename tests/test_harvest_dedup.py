"""tests for mathx.harvest dedup — BM25 top-5 + forced known_similar_ids + same-field pool."""

from __future__ import annotations

import pytest

from mathx import harvest

FIELDS = ["number-theory", "combinatorics", "algebra"]


def _entry(pid: str, field: str, title: str, statement: str = "") -> dict:
    return {
        "id": pid,
        "title": title,
        "field": field,
        "origin": "classic",
        "status": "queued",
        "tractability": 2,
        "attempts": 0,
        "sources": [],
        "added_at_utc": "2026-08-09T00:00:00+00:00",
        "last_activity_utc": "2026-08-09T00:00:00+00:00",
    }


def _reg(problems: list[dict]) -> dict:
    return {"version": 1, "hunter_state": {}, "ingested_inboxes": [], "ingested_seeds": [], "problems": problems}


def _cand(**kw) -> dict:
    # Chinese text tokenizes to zero BM25 tokens → guaranteed score 0 against
    # any English problem, isolating the forced-pool paths from BM25 overlap.
    base = {
        "title": "一个全新的中文命题标题",
        "field": "number-theory",
        "statement": "某个与既有问题完全没有共享词汇的命题表述。",
        "origin": "classic",
        "tractability": 2,
        "why_interesting": "",
        "sources": [],
        "still_open_evidence": "",
    }
    base.update(kw)
    return base


@pytest.fixture
def problems_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(harvest, "PROBLEMS_DIR", tmp_path / "problems")
    return tmp_path / "problems"


def _ids(matches) -> list[str]:
    return [m["entry"]["id"] for m in matches]


# ---------------------------------------------------------------- forced pool

def test_known_similar_ids_forced_into_judge_and_merges(problems_dir, monkeypatch):
    """Hunter-flagged suspect with zero BM25 overlap still reaches the judge; same → merged."""
    existing = [_entry("algebra/goldbach", "algebra", "Goldbach conjecture", "Every even integer > 2 is a sum of two primes.")]
    reg = _reg(existing)
    seen: dict = {}

    def fake_judge(candidate, matches):
        seen["matches"] = matches
        return True  # duplicate

    monkeypatch.setattr(harvest, "_dedup_judge", fake_judge)
    out = harvest._ingest_one(reg, _cand(known_similar_ids=["algebra/goldbach"]), FIELDS)

    assert out["action"] == "merged"
    assert out["into"] == "algebra/goldbach"
    assert "algebra/goldbach" in _ids(seen["matches"])
    assert seen["matches"][0]["why"] == "hunter"
    assert out["dedup_checked_against"] == 1


def test_same_field_forced_into_judge(problems_dir, monkeypatch):
    """A same-field problem with zero BM25 overlap is still judged (not silently added)."""
    existing = [_entry("number-theory/abc", "number-theory", "abc conjecture", "For every epsilon there exists a constant C...")]
    reg = _reg(existing)
    seen: dict = {}

    def fake_judge(candidate, matches):
        seen["matches"] = matches
        return False  # distinct → added

    monkeypatch.setattr(harvest, "_dedup_judge", fake_judge)
    out = harvest._ingest_one(reg, _cand(), FIELDS)

    assert out["action"] == "added"
    assert out["dedup_checked_against"] >= 1
    assert "number-theory/abc" in _ids(seen["matches"])
    assert any(m["why"] == "same-field" for m in seen["matches"])


def test_bm25_top5_pool(problems_dir, monkeypatch):
    """Up to five BM25-overlapping problems go to the judge in one call."""
    existing = [
        _entry(f"number-theory/e{i}", "number-theory", f"Unit fraction problem {i}", f"4/n = 1/x + 1/y + 1/z topic {i}")
        for i in range(6)
    ]
    reg = _reg(existing)
    seen: dict = {}

    def fake_judge(candidate, matches):
        seen["matches"] = matches
        return False

    monkeypatch.setattr(harvest, "_dedup_judge", fake_judge)
    cand = _cand(title="Unit fraction 4/n 1/x 1/y 1/z conjecture", statement="Unit fraction 4/n 1/x 1/y 1/z 1/x 1/y decomposition")
    out = harvest._ingest_one(reg, cand, FIELDS)

    assert out["action"] == "added"
    assert len(seen["matches"]) >= 5
    assert out["dedup_checked_against"] == len(seen["matches"])


def test_same_field_pool_deduped_with_bm25(problems_dir, monkeypatch):
    """A problem picked twice (BM25 top + same-field) appears only once in the judge pool."""
    existing = [
        _entry("number-theory/e0", "number-theory", "Unit fraction problem", "4/n = 1/x + 1/y + 1/z"),
        _entry("combinatorics/c0", "combinatorics", "Unrelated ramsey", "coloring of hypergraphs"),
    ]
    reg = _reg(existing)
    seen: dict = {}

    def fake_judge(candidate, matches):
        seen["matches"] = matches
        return False

    monkeypatch.setattr(harvest, "_dedup_judge", fake_judge)
    cand = _cand(title="Unit fraction 4/n 1/x 1/y 1/z conjecture", statement="Unit fraction 4/n decomposition")
    harvest._ingest_one(reg, cand, FIELDS)

    ids = _ids(seen["matches"])
    assert len(ids) == len(set(ids))  # no duplicate entries
    assert "number-theory/e0" in ids


def test_malformed_known_similar_ids_ignored(problems_dir, monkeypatch):
    """Non-list known_similar_ids must not crash ingest."""
    existing = [_entry("number-theory/abc", "number-theory", "abc conjecture", "For every epsilon...")]
    reg = _reg(existing)
    seen: dict = {}

    def fake_judge(candidate, matches):
        seen["matches"] = matches
        return False

    monkeypatch.setattr(harvest, "_dedup_judge", fake_judge)
    out = harvest._ingest_one(reg, _cand(known_similar_ids="number-theory/abc"), FIELDS)
    assert out["action"] == "added"
    assert "number-theory/abc" in _ids(seen["matches"])  # same-field force still applies
