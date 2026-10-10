"""Hands trace enriches open-row actions with title and in_row."""

from __future__ import annotations

import json
from pathlib import Path

from artemis.mcp.hands_trace import HandsTrace


def _read_events(tmp_path: Path) -> list[dict]:
    log = next((tmp_path / "logs" / "hands").rglob("*.jsonl"))
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_note_and_append_inherit_title_and_in_row(tmp_path: Path):
    trace = HandsTrace(tmp_path, device="dev1")
    trace.begin("去蚂蚁庄园逛一逛", extra={"how": "script"})
    trace.note({"op": "click", "text": "去完成", "ok": True})
    trace.append({"op": "click_failed", "ok": False, "reason": "miss"})
    trace.close_span(learned=True, outcome="done")
    events = _read_events(tmp_path)
    begin = events[0]
    assert begin["op"] == "begin_row"
    assert begin["title"] == "去蚂蚁庄园逛一逛"
    assert begin["how"] == "script"
    click = events[1]
    assert click["title"] == "去蚂蚁庄园逛一逛"
    assert click["in_row"] is True
    failed = events[2]
    assert failed["op"] == "click_failed"
    assert failed["title"] == "去蚂蚁庄园逛一逛"
    assert failed["in_row"] is True
    end = events[3]
    assert end["op"] == "end_row"
    assert end["outcome"] == "done"


def test_boundary_ops_do_not_inherit_open_title(tmp_path: Path):
    trace = HandsTrace(tmp_path)
    trace.begin("甲")
    trace.append({"op": "replay_start", "title": "乙"})
    events = _read_events(tmp_path)
    replay = events[-1]
    assert replay["title"] == "乙"
    assert "in_row" not in replay
