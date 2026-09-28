"""Row steps are learned from a hands trace and replayed by text, not coordinates."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from artemis.mcp.hands_trace import HandsTrace, RowBusy
from artemis.mcp.row_steps import (
    compress_steps,
    judge_row,
    merge_steps,
    note_replay_failure,
    settle,
    store_steps,
)

_MATCH = (
    Path(__file__).resolve().parents[3]
    / ".cursor"
    / "skills"
    / "xianyu-coin-tasks"
    / "scripts"
    / "match_rows.py"
)


def _load_match(catalog: Path):
    spec = importlib.util.spec_from_file_location("match_rows", _MATCH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.CATALOG = catalog
    return module


def _element(text: str, top: int, bottom: int, left: int = 100, right: int = 400) -> dict:
    return {
        "text": text,
        "bounds": [left, top, right, bottom],
        "center": [(left + right) / 2, (top + bottom) / 2],
    }


def _sheet(title: str, button: str) -> list[dict]:
    return [
        _element("得骰子赚闲鱼币", 80, 140, 200, 800),
        _element(title, 400, 460, 80, 520),
        _element(button, 400, 460, 800, 1000),
    ]


def test_sheet_must_be_open_before_a_row_counts_as_learned():
    title = "去蚂蚁庄园逛一逛"
    assert judge_row([_element(title, 400, 460), _element("领取奖励", 400, 460, 800, 1000)], title)[
        "learned"
    ] is False
    learned = judge_row(_sheet(title, "领取奖励"), title)
    assert learned["learned"] is True
    assert learned["button"] == "领取奖励"
    assert judge_row(_sheet(title, "去完成"), title)["learned"] is False
    gone = judge_row([_element("得骰子赚闲鱼币", 80, 140, 200, 800), _element("去完成", 500, 560, 800, 1000)], title)
    assert gone["learned"] is True


def test_compress_drops_taps_that_do_not_change_the_screen_and_keeps_the_last_open():
    title = "去蚂蚁庄园逛一逛"
    events = [
        {"op": "look", "texts": ["得骰子赚闲鱼币", title, "去完成"]},
        {"op": "click", "text": "关闭"},
        {"op": "look", "texts": ["得骰子赚闲鱼币", title, "去完成"]},
        {"op": "click", "text": "去完成"},
        {"op": "look", "texts": ["蚂蚁庄园"]},
        {"op": "click", "text": "取消"},
        {"op": "look", "texts": ["蚂蚁庄园"]},
        {"op": "wait", "ms": 15000},
        {"op": "press_key", "key": "back"},
        {"op": "look", "texts": ["得骰子赚闲鱼币", title, "领取奖励"]},
    ]
    steps = compress_steps(events, title)
    assert steps[0] == {"op": "tap_text", "text": "去完成", "near": title}
    assert {"op": "wait", "ms": 15000} in steps
    assert {"op": "key", "key": "back"} in steps
    assert not any(step.get("text") == "关闭" for step in steps)
    assert not any(step.get("text") == "取消" for step in steps)


def test_claim_only_is_one_tap():
    title = "通过首页访问闲鱼币"
    steps = compress_steps(
        [
            {"op": "look", "texts": ["得骰子赚闲鱼币", title, "领取奖励"]},
            {"op": "click", "text": "领取奖励"},
            {"op": "look", "texts": ["得骰子赚闲鱼币", title, "已完成"]},
        ],
        title,
    )
    assert steps == [{"op": "tap_text", "text": "领取奖励", "near": title}]


def test_merge_keeps_old_steps_and_unions_tap_words():
    old = [{"op": "tap_text", "text": "点击浏览商品", "near": "看视频"}]
    fresh = [{"op": "tap_text", "text": "去逛逛", "near": "看视频"}]
    merged = merge_steps(old, fresh)
    assert merged == [{"op": "tap_text", "any": ["点击浏览商品", "去逛逛"], "near": "看视频"}]
    assert merge_steps(old, []) == old


def test_store_steps_refuses_skip_and_replay_failure_clears_on_the_third_miss(tmp_path: Path):
    catalog = tmp_path / "tasks" / "闲鱼币任务清单.json"
    catalog.parent.mkdir()
    catalog.write_text(
        json.dumps(
            {
                "rows": [
                    {"title": "跳过行", "run": "skip", "steps": []},
                    {"title": "去百度逛一逛", "run": "yes", "script_fails": 2, "steps": [{"op": "wait", "ms": 1}]},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        store_steps(catalog, "跳过行", [{"op": "tap_text", "text": "去完成"}])
        raised = False
    except ValueError:
        raised = True
    assert raised
    row = store_steps(catalog, "去蚂蚁庄园逛一逛", [{"op": "tap_text", "text": "去完成", "near": "去蚂蚁庄园逛一逛"}])
    assert row["steps"][0]["text"] == "去完成"
    failed = note_replay_failure(catalog, "去百度逛一逛")
    assert failed["cleared"] is True
    saved = json.loads(catalog.read_text(encoding="utf-8"))
    baidu = next(item for item in saved["rows"] if item["title"] == "去百度逛一逛")
    assert baidu["steps"] == []
    assert baidu["script_fails"] == 3


def test_settle_fails_when_a_touched_row_has_no_steps_or_the_unlearned_set_grows():
    data = {"rows": [{"title": "甲", "run": "yes", "steps": []}, {"title": "乙", "run": "skip", "steps": []}]}
    open_row = settle([{"op": "begin_row", "title": "甲"}], data, None)
    assert open_row["ok"] is False
    learned = settle(
        [{"op": "begin_row", "title": "甲"}, {"op": "end_row", "title": "甲", "learned": True}],
        {"rows": [{"title": "甲", "run": "yes", "steps": [{"op": "key", "key": "back"}]}]},
        2,
    )
    assert learned["ok"] is True
    grew = settle([], {"rows": [{"title": "甲", "run": "yes", "steps": []}]}, 0)
    assert grew["ok"] is False


def test_trace_refuses_a_second_begin_until_the_row_finishes(tmp_path: Path):
    trace = HandsTrace(tmp_path)
    trace.begin("甲")
    try:
        trace.begin("乙")
        raised = False
    except RowBusy:
        raised = True
    assert raised
    trace.note({"op": "click", "text": "去完成"})
    events = trace.finish(learned=False)
    assert trace.title == "甲"
    assert events[0]["text"] == "去完成"
    trace.finish(learned=True)
    assert trace.title is None
    lines = next((tmp_path / "logs" / "hands").rglob("*.jsonl")).read_text(encoding="utf-8")
    assert "begin_row" in lines
    assert "end_row" in lines


def test_match_rows_replays_when_steps_exist_and_record_ok_keeps_them(tmp_path: Path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "title": "去蚂蚁庄园逛一逛",
                        "run": "yes",
                        "script": "leave_and_return",
                        "script_fails": 0,
                        "seconds": 15,
                        "steps": [{"op": "tap_text", "text": "去完成"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    match = _load_match(catalog)
    action = match.action_for(match.find(match.load(), "去蚂蚁庄园逛一逛"), "去完成", "去蚂蚁庄园逛一逛")
    assert action["action"] == "replay"
    import sys

    sys.argv = ["match_rows.py", "--record-ok", "去蚂蚁庄园逛一逛"]
    match.main()
    kept = match.find(match.load(), "去蚂蚁庄园逛一逛")
    assert kept is not None
    assert kept["steps"]
    assert kept["run"] == "done"
