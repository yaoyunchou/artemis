"""Row steps are learned from a hands trace and replayed by text, not coordinates."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

from artemis.mcp.hands_trace import HandsTrace, RowBusy
from artemis.mcp.row_steps import (
    compress_steps,
    infer_role,
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


def test_infer_role_marks_navigation_permission_and_cold_start():
    assert infer_role("闲鱼币") == "nav"
    assert infer_role("始终允许") == "permission"
    assert infer_role("去完成") == "task"
    assert infer_role("", key="HOME") == "cold_start"
    assert infer_role("返回", key="home") == "nav"


def test_replay_environment_failure_does_not_increment_script_fails(tmp_path: Path):
    catalog = tmp_path / "tasks" / "闲鱼币任务清单.json"
    catalog.parent.mkdir()
    catalog.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "title": "去百度逛一逛",
                        "run": "yes",
                        "script_fails": 2,
                        "steps": [{"op": "wait", "ms": 1}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    failed = note_replay_failure(catalog, "去百度逛一逛", reason="看屏失败：adb")
    assert failed["script_fails"] == 2
    assert failed["cleared"] is False
    assert failed["env_failure"] is True
    saved = json.loads(catalog.read_text(encoding="utf-8"))
    row = saved["rows"][0]
    assert row["script_fails"] == 2
    assert row["steps"]
    counted = note_replay_failure(catalog, "去百度逛一逛", reason="屏幕上没有这一步的字")
    assert counted["script_fails"] == 3
    assert counted["cleared"] is True


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


def test_settle_blocks_on_an_open_row_but_only_records_learning_debts():
    data = {"rows": [{"title": "甲", "run": "yes", "steps": []}, {"title": "乙", "run": "skip", "steps": []}]}
    # 行开着没有 end_row：阻塞
    open_row = settle([{"op": "begin_row", "title": "甲"}], data, None)
    assert open_row["ok"] is False
    assert any("end_row" in item for item in open_row["problems"])
    # 碰过但没学会：只记债务，不阻塞（学习是债务，不是锁）
    missed = settle(
        [{"op": "begin_row", "title": "甲"}, {"op": "end_row", "title": "甲", "learned": False}],
        data,
        None,
    )
    assert missed["ok"] is True
    assert missed["debts"] == ["甲"]
    # 学成了且有步骤：通过
    learned = settle(
        [{"op": "begin_row", "title": "甲"}, {"op": "end_row", "title": "甲", "learned": True}],
        {"rows": [{"title": "甲", "run": "yes", "steps": [{"op": "key", "key": "back"}]}]},
        2,
    )
    assert learned["ok"] is True
    # 学成了但没有步骤：阻塞（数据不一致）
    inconsistent = settle(
        [{"op": "begin_row", "title": "甲"}, {"op": "end_row", "title": "甲", "learned": True}],
        {"rows": [{"title": "甲", "run": "yes", "steps": []}]},
        0,
    )
    assert inconsistent["ok"] is False
    # 未学会的可做行变多：只警告，不阻塞
    grew = settle([], {"rows": [{"title": "甲", "run": "yes", "steps": []}]}, 0)
    assert grew["ok"] is True
    assert grew["warnings"]


def test_finish_always_closes_the_row_so_one_miss_cannot_stall_the_run(tmp_path: Path):
    trace = HandsTrace(tmp_path)
    trace.begin("甲")
    try:
        trace.begin("乙")
        raised = False
    except RowBusy:
        raised = True
    assert raised
    trace.note({"op": "click", "text": "去完成"})
    events = trace.finish(learned=False, reason="按钮仍是去完成")
    assert trace.title is None
    assert events[0]["text"] == "去完成"
    assert events[0]["title"] == "甲"
    assert events[0]["in_row"] is True
    assert "甲" in trace.pending_learn
    trace.begin("乙")
    assert trace.title == "乙"
    trace.finish(learned=True)
    assert trace.title is None
    lines = next((tmp_path / "logs" / "hands").rglob("*.jsonl")).read_text(encoding="utf-8")
    assert "begin_row" in lines
    assert "end_row" in lines


def test_a_reloaded_process_recovers_the_open_row_instead_of_blocking(tmp_path: Path):
    first = HandsTrace(tmp_path, device="dev1")
    first.begin("甲")
    assert first.title == "甲"

    second = HandsTrace(tmp_path, device="dev1")
    assert second.title is None
    assert "甲" in second.pending_learn
    assert "甲" in second.recovered
    second.begin("乙")
    assert second.title == "乙"
    log = next((tmp_path / "logs" / "hands").rglob("*.jsonl")).read_text(encoding="utf-8")
    assert "进程重启" in log
    assert '"outcome": "lost"' in log


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


def test_recovery_falls_back_to_the_log_when_no_session_file_exists(tmp_path: Path):
    """旧进程从没写过 .session.json，但日志里留着没配对的 begin_row。"""
    day = datetime.now().strftime("%Y%m%d")
    log = tmp_path / "logs" / "hands" / "dev2" / f"{day}.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps(
            {"date": day, "device": "dev2", "op": "begin_row", "title": "去玩闲鱼游戏15s"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    trace = HandsTrace(tmp_path, device="dev2")
    assert trace.title is None
    assert "去玩闲鱼游戏15s" in trace.pending_learn
    trace.begin("乙")
    assert trace.title == "乙"
    assert "进程重启" in log.read_text(encoding="utf-8")


def test_bind_recovers_the_open_row_for_the_device_it_locks(tmp_path: Path):
    """运行时先建 trace 再锁设备，恢复扫描必须发生在 bind 之后。"""
    day = datetime.now().strftime("%Y%m%d")
    log = tmp_path / "logs" / "hands" / "dev3" / f"{day}.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps(
            {"date": day, "device": "dev3", "op": "begin_row", "title": "去玩闲鱼游戏15s"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    trace = HandsTrace(tmp_path)
    assert trace.title is None
    assert trace.pending_learn == []

    trace.bind("dev3")
    assert "去玩闲鱼游戏15s" in trace.pending_learn
    assert "去玩闲鱼游戏15s" in trace.recovered
    trace.begin("乙")
    assert trace.title == "乙"
    assert "进程重启" in log.read_text(encoding="utf-8")


def test_recovery_closes_every_orphan_even_when_a_later_row_closed_normally(tmp_path: Path):
    """真实场景：重载丢了内存里的行，下一行 begin 后正常 end，老的 begin 仍是孤儿。"""
    day = datetime.now().strftime("%Y%m%d")
    log = tmp_path / "logs" / "hands" / "dev4" / f"{day}.jsonl"
    log.parent.mkdir(parents=True)
    rows = [
        {"op": "begin_row", "title": "去玩闲鱼游戏15s"},
        {"op": "click", "text": "去完成"},
        {"op": "begin_row", "title": "去消了还想消玩1关"},
        {"op": "end_row", "title": "去消了还想消玩1关", "learned": False},
    ]
    log.write_text(
        "".join(
            json.dumps({"date": day, "device": "dev4", **row}, ensure_ascii=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    trace = HandsTrace(tmp_path, device="dev4")
    assert trace.recovered == ["去玩闲鱼游戏15s"]
    assert trace.pending_learn == ["去玩闲鱼游戏15s"]
    trace.begin("乙")
    assert trace.title == "乙"


def test_compress_keeps_swipes_so_scroll_driven_rows_can_replay():
    title = "搜一搜推荐好物"
    events = [
        {"op": "look", "texts": ["得骰子赚闲鱼币", title, "去完成"]},
        {"op": "click", "text": "去完成"},
        {"op": "look", "texts": ["浏览15秒获得奖励"]},
        {"op": "swipe", "start": [500, 850], "end": [500, 450], "ms": 3000},
        {"op": "look", "texts": ["得骰子赚闲鱼币", title, "领取奖励"]},
    ]
    steps = compress_steps(events, title)
    assert [s for s in steps if s.get("op") == "swipe"] == [
        {"op": "swipe", "start": [500, 850], "end": [500, 450], "ms": 3000}
    ]


def test_compress_keeps_a_swipe_even_when_the_tree_did_not_change():
    """倒计时跟着滑动走，界面树可能不动 —— swipe 不能被当成无效操作丢掉。"""
    title = "去浏览福利好物"
    events = [
        {"op": "look", "texts": ["得骰子赚闲鱼币", title, "去完成"]},
        {"op": "click", "text": "去完成"},
        {"op": "look", "texts": ["同一屏"]},
        {"op": "swipe", "start": [500, 850], "end": [500, 450]},
        {"op": "click", "text": "没用的点击"},
        {"op": "look", "texts": ["同一屏"]},
    ]
    steps = compress_steps(events, title)
    assert any(step.get("op") == "swipe" for step in steps)
    assert not any(step.get("text") == "没用的点击" for step in steps)


def test_merge_replaces_steps_recorded_before_swipes_were_captured():
    old = [
        {"op": "tap_text", "text": "去完成", "near": "搜一搜推荐好物"},
        {"op": "wait", "ms": 2000},
        {"op": "tap_text", "text": "领取奖励", "near": "搜一搜推荐好物"},
    ]
    fresh = [
        {"op": "tap_text", "text": "去完成", "near": "搜一搜推荐好物"},
        {"op": "swipe", "start": [500, 850], "end": [500, 450], "ms": 3000},
        {"op": "tap_text", "text": "领取奖励", "near": "搜一搜推荐好物"},
    ]
    assert merge_steps(old, fresh) == fresh


def test_merge_still_unions_taps_when_both_sides_have_swipes():
    old = [
        {"op": "tap_text", "text": "去完成"},
        {"op": "swipe", "start": [500, 850], "end": [500, 450]},
    ]
    fresh = [
        {"op": "tap_text", "text": "去逛逛"},
        {"op": "swipe", "start": [500, 850], "end": [500, 450]},
    ]
    merged = merge_steps(old, fresh)
    assert merged[0] == {"op": "tap_text", "any": ["去完成", "去逛逛"]}
    assert merged[1]["op"] == "swipe"
