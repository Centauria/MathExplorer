"""tests for mathx.harvest ingest — bad-line handling (retryable inboxes, skipped_details)."""

from __future__ import annotations

import json

import pytest

from mathx import harvest


@pytest.fixture
def ingest_env(tmp_path, monkeypatch):
    root = tmp_path
    monkeypatch.setattr(harvest, "DATA_DIR", root)
    monkeypatch.setattr(harvest, "REGISTRY_PATH", root / "registry.json")
    monkeypatch.setattr(harvest, "INBOX_DIR", root / "inbox")
    monkeypatch.setattr(harvest, "SEEDS_DIR", root / "seeds")
    monkeypatch.setattr(harvest, "PROBLEMS_DIR", root / "problems")
    (root / "inbox").mkdir()
    (root / "seeds").mkdir()
    return root


def _reg(ingested: list[str] | None = None) -> dict:
    return {
        "version": 1,
        "hunter_state": {},
        "ingested_inboxes": ingested or [],
        "ingested_seeds": [],
        "problems": [],
    }


def _line(title: str = "Unit fraction 4/n conjecture", statement: str = "4/n = 1/x + 1/y + 1/z", **kw) -> str:
    obj = {
        "title": title,
        "field": "number-theory",
        "statement": statement,
        "origin": "classic",
        "tractability": 2,
        "why_interesting": "",
        "sources": [],
        "still_open_evidence": "open per sources",
        "found_at_utc": "2026-08-09T00:00:00Z",
    }
    obj.update(kw)
    return json.dumps(obj, ensure_ascii=False)


def test_bad_line_reports_detail_and_keeps_inbox_retryable(ingest_env, monkeypatch):
    inbox = ingest_env / "inbox" / "x_hunter.jsonl"
    # second line has an unescaped LaTeX backslash → invalid JSON escape
    inbox.write_text(_line() + "\n" + '{"title": "broken \\m conject", "statement": "x"}\n', encoding="utf-8")
    harvest.REGISTRY_PATH.write_text(json.dumps(_reg()), encoding="utf-8")
    monkeypatch.setattr(harvest, "_dedup_judge", lambda c, m: False)

    out = harvest.ingest()
    assert out["added"] == 1
    assert out["skipped_lines"] == 1
    assert out["skipped_details"][0]["inbox"] == "x_hunter.jsonl"
    assert out["skipped_details"][0]["line"] == 2
    assert out["skipped_details"][0]["reason"].startswith("bad JSON: Invalid \\escape")
    # the inbox is NOT marked ingested → fixing the line allows a retry
    reg = json.loads(harvest.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert "x_hunter.jsonl" not in reg["ingested_inboxes"]


def test_fixed_line_can_be_retried(ingest_env, monkeypatch):
    inbox = ingest_env / "inbox" / "x_hunter.jsonl"
    good = _line()
    bad = '{"title": "broken \\m conject", "statement": "x"}'
    inbox.write_text(good + "\n" + bad + "\n", encoding="utf-8")
    harvest.REGISTRY_PATH.write_text(json.dumps(_reg()), encoding="utf-8")
    monkeypatch.setattr(harvest, "_dedup_judge", lambda c, m: False)

    first = harvest.ingest()  # good line added, bad line skipped, inbox retryable
    assert first["added"] == 1 and first["skipped_lines"] == 1

    # fix the bad line (valid JSON now), retry: good line re-judged and merged
    # into itself, fixed line added → inbox finally marked ingested
    fixed = json.dumps({"title": "Fixed conjecture", "field": "number-theory", "statement": "fixed statement",
                        "origin": "classic", "tractability": 2, "why_interesting": "", "sources": [],
                        "still_open_evidence": "open", "found_at_utc": "2026-08-09T00:00:00Z"})
    inbox.write_text(good + "\n" + fixed + "\n", encoding="utf-8")
    monkeypatch.setattr(harvest, "_dedup_judge", lambda c, m: "Unit fraction 4/n" in c["title"])
    second = harvest.ingest()
    assert second["skipped_lines"] == 0
    assert second["added"] + second["merged"] == 2  # fixed line lands; re-run good line merges into itself
    reg = json.loads(harvest.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert "x_hunter.jsonl" in reg["ingested_inboxes"]  # now fully ingested


def test_missing_fields_reported(ingest_env, monkeypatch):
    inbox = ingest_env / "inbox" / "x_hunter.jsonl"
    inbox.write_text(json.dumps({"title": "No statement", "field": "number-theory"}) + "\n", encoding="utf-8")
    harvest.REGISTRY_PATH.write_text(json.dumps(_reg()), encoding="utf-8")
    out = harvest.ingest()
    assert out["skipped_lines"] == 1
    assert out["skipped_details"][0]["reason"] == "missing title/statement"
    reg = json.loads(harvest.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert "x_hunter.jsonl" not in reg["ingested_inboxes"]


def test_clean_inbox_marked_ingested(ingest_env, monkeypatch):
    inbox = ingest_env / "inbox" / "x_hunter.jsonl"
    inbox.write_text(_line() + "\n", encoding="utf-8")
    harvest.REGISTRY_PATH.write_text(json.dumps(_reg()), encoding="utf-8")
    monkeypatch.setattr(harvest, "_dedup_judge", lambda c, m: False)
    out = harvest.ingest()
    assert out["added"] == 1 and out["skipped_lines"] == 0
    reg = json.loads(harvest.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert "x_hunter.jsonl" in reg["ingested_inboxes"]
