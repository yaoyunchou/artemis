# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The A-list log fields: added only, never steering the decision path.

Scope, per 闲鱼币任务_日志补全要求与闭环设计.md §4 A: ``intent`` / ``run_id`` +
``run_seq`` / coordinate ``near_*`` / ``in_row`` / ``op: return`` / ``done`` +
``reward``. The learning behaviour (``compress_steps``) must be byte-for-byte
unchanged.

Coordinate note: a ``click`` tap is normalized 0-1000, but element ``bounds`` are
device pixels (``artemis/utils/visualization.py:425``). The ``near_*`` tests below
therefore use pixel bounds *and* normalized taps, at both a small screen and the
real 1080x2400 scale -- passing a pixel center as the tap is exactly the bug this
suite is meant to catch.

These tests use a private temp dir rather than pytest's ``tmp_path``: this
sandbox brokers ``mkdir`` for ``pytest-of-*`` and fails it, so ``tmp_path`` is
unusable here.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from artemis.mcp import hands_server
from artemis.mcp.action_types import ActionResult, ObserveResult
from artemis.mcp.hands_server import _in_row_field, _target_source
from artemis.mcp.hands_trace import HandsTrace
from artemis.mcp.row_steps import compress_steps


@pytest.fixture
def workdir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="hands-trace-fields-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _records(trace: HandsTrace) -> list[dict]:
    path = trace.path_for()
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _FakeSession:
    """Just enough of HandsSession for the logging path; no device, no I/O."""

    def __init__(
        self,
        elements: list[dict] | None = None,
        width: Any = 1080,
        height: Any = 2400,
    ) -> None:
        self.elements = elements or []
        self.width = width
        self.height = height

    async def click(self, target, times: int = 1, delay_ms: int = 100) -> ActionResult:
        return ActionResult.success("click", "tapped", normalized_coordinates=[1, 2])

    async def press_key(self, key: str) -> ActionResult:
        return ActionResult.success("press_key", "pressed")


class _FakeRuntime:
    def __init__(self, trace: HandsTrace, session: _FakeSession) -> None:
        self.trace = trace
        self.session = session
        self.last_package: str | None = None

    def ensure(self) -> _FakeSession:
        return self.session


# ----------------------------------------------------------------- run fields


def test_begin_run_rotates_run_id_and_leaves_the_open_row_untouched(workdir: Path) -> None:
    trace = HandsTrace(workdir, device="testdev")
    trace.begin("甲")
    original_run_id = trace.run_id
    original_events = trace.events

    tokens = trace.begin_run("闲鱼币一轮")

    assert trace.run_id != original_run_id
    assert tokens["run_id"] == trace.run_id
    assert trace.run_seq == 1
    assert tokens["run_seq"] == 1
    # A run boundary must never disturb what is being learned.
    assert trace.title == "甲"
    assert trace.events is original_events

    starts = [record for record in _records(trace) if record.get("op") == "run_start"]
    assert starts[-1]["label"] == "闲鱼币一轮"
    assert starts[-1]["run_id"] == trace.run_id
    # No ``title``: otherwise ``_open_rows_from_log`` would try to pair it.
    assert "title" not in starts[-1]


def test_every_record_carries_run_seq(workdir: Path) -> None:
    trace = HandsTrace(workdir, device="testdev")
    trace.begin("甲")
    trace.note({"op": "click", "text": "去完成"})
    trace.begin_run("r1")
    trace.begin_run("r2")
    trace.finish(learned=False, reason="没做成")

    records = _records(trace)
    assert records
    assert all("run_seq" in record for record in records)
    assert records[-1]["op"] == "end_row"
    assert records[-1]["run_seq"] == 2
    assert trace.snapshot()["run_seq"] == 2


# --------------------------------------------------------------- coordinate tap
#
# Bounds are pixels, taps are normalized 0-1000; each case below converts on the
# element side only, mirroring hands_session.resolve_target (L92-93).


def test_coordinate_tap_names_the_nearest_element_on_a_small_screen() -> None:
    session = _FakeSession(
        [
            {"text": "关闭广告", "bounds": [100, 200, 300, 400], "class": "android.widget.Button"},
            {"text": "领取奖励", "bounds": [800, 400, 1000, 460], "class": "android.widget.TextView"},
        ],
        width=1000,
        height=2000,
    )
    # Pixel center (200, 300) -> normalized (200, 150). Tapping there must name it.
    info = _target_source(session, [200, 150])
    assert info["source"] == "coord"
    assert info["near_text"] == "关闭广告"
    assert info["near_class"] == "android.widget.Button"
    assert info["near_bounds"] == [100, 200, 300, 400]
    assert info["near_distance"] == 0.0


def test_coordinate_tap_names_a_button_at_real_device_scale() -> None:
    # 1080x2400: pixel center (950, 80) -> normalized (880, 33). Before the
    # normalization fix this exact tap returned no ``near_*`` at all.
    session = _FakeSession(
        [{"text": "关闭广告", "bounds": [900, 40, 1000, 120]}],
        width=1080,
        height=2400,
    )
    info = _target_source(session, [879.6, 33.3])
    assert info["near_text"] == "关闭广告"
    assert info["near_bounds"] == [900, 40, 1000, 120]
    assert info["near_distance"] <= 1.0


def test_coordinate_tap_keeps_the_threshold_boundary_and_drops_beyond() -> None:
    # Pixel center (100, 400) -> normalized (100, 200) on a 1000x2000 screen.
    session = _FakeSession(
        [{"text": "按钮", "bounds": [0, 400, 200, 400]}],
        width=1000,
        height=2000,
    )
    on = _target_source(session, [20, 200])  # distance exactly 80 -> kept
    assert on["near_text"] == "按钮"
    assert on["near_distance"] == 80.0
    beyond = _target_source(session, [19, 200])  # distance 81 -> dropped
    assert beyond == {"source": "coord"}


def test_coordinate_tap_far_from_every_element_has_no_near_keys() -> None:
    session = _FakeSession(
        [{"text": "关闭广告", "bounds": [100, 200, 300, 400]}],
        width=1000,
        height=2000,
    )
    info = _target_source(session, [200, 900])
    assert info == {"source": "coord"}
    for key in ("near_text", "near_class", "near_bounds", "near_distance"):
        assert key not in info


def test_coordinate_tap_tolerates_missing_or_malformed_bounds() -> None:
    session = _FakeSession(
        [
            {"text": "无 bounds"},
            {"text": "字符串 bounds", "bounds": "[0,0][10,10]"},
            {"text": "坏数值", "bounds": [1, 2, "x", 4]},
        ],
        width=1000,
        height=2000,
    )
    assert _target_source(session, [5, 5]) == {"source": "coord"}


def test_coordinate_tap_without_a_usable_screen_size_stays_unnamed() -> None:
    elements = [{"text": "按钮", "bounds": [100, 200, 300, 400]}]
    for width, height in ((None, None), (0, 0), (0, 2000), (1000, "x")):
        session = _FakeSession(elements, width=width, height=height)
        info = _target_source(session, [200, 150])
        assert info == {"source": "coord"}
        assert "near_text" not in info


# ------------------------------------------------------------------- in_row


def test_in_row_field_tracks_the_open_row(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trace = HandsTrace(workdir, device="testdev")
    monkeypatch.setattr(hands_server, "runtime", _FakeRuntime(trace, _FakeSession()))

    assert _in_row_field() == {"in_row": False}
    trace.begin("甲")
    assert _in_row_field() == {"in_row": True, "title": "甲"}
    trace.finish(learned=False, reason="没做成")
    assert _in_row_field() == {"in_row": False}


@pytest.mark.asyncio
async def test_look_records_carry_in_row(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``look`` is the single most frequent op; it must carry ``in_row`` too."""
    trace = HandsTrace(workdir, device="testdev")
    session = _FakeSession([{"text": "首页", "bounds": [0, 0, 200, 100]}])

    async def _look(include_image: bool = False, settle_ms: int = 0):
        obs = ObserveResult(
            ok=True,
            screenshot_path="/tmp/x.jpg",
            width=1080,
            height=2400,
            elements=list(session.elements),
            hierarchy_ok=True,
            elements_text="[1] Text: '首页'",
        )
        return obs, None

    session.look = _look  # type: ignore[method-assign]
    monkeypatch.setattr(hands_server, "runtime", _FakeRuntime(trace, session))

    await hands_server.look()
    looks = [record for record in _records(trace) if record.get("op") == "look"]
    assert looks and looks[-1]["in_row"] is False

    trace.begin("甲")
    await hands_server.look()
    looks = [record for record in _records(trace) if record.get("op") == "look"]
    assert looks[-1]["in_row"] is True
    assert looks[-1]["title"] == "甲"
    assert looks[-1]["run_id"] == trace.run_id


# -------------------------------------------------------- intent / return (tools)


@pytest.mark.asyncio
async def test_click_writes_intent_only_when_non_empty(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = HandsTrace(workdir, device="testdev")
    elements = [{"text": "关闭广告", "bounds": [900, 40, 980, 120]}]
    monkeypatch.setattr(hands_server, "runtime", _FakeRuntime(trace, _FakeSession(elements)))

    await hands_server.click(target=1, intent="关广告")
    await hands_server.click(target=1)

    clicks = [record for record in _records(trace) if record.get("op") == "click"]
    assert clicks[0]["intent"] == "关广告"
    assert "intent" not in clicks[1]


@pytest.mark.asyncio
async def test_press_key_return_writes_a_titleless_return_record(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = HandsTrace(workdir, device="testdev")
    monkeypatch.setattr(hands_server, "runtime", _FakeRuntime(trace, _FakeSession()))

    await hands_server.press_key("home", purpose="return")
    await hands_server.press_key("back")  # no purpose -> no return record

    records = _records(trace)
    returns = [record for record in records if record.get("op") == "return"]
    assert len(returns) == 1
    assert returns[0]["via"] == "press_key"
    assert returns[0]["detail"] == "home"
    assert "title" not in returns[0]

    presses = [record for record in records if record.get("op") == "press_key"]
    assert presses[0]["purpose"] == "return"
    assert presses[0]["role"] == "cold_start"
    assert "purpose" not in presses[-1]
    assert "role" not in presses[-1]
    assert returns[0]["role"] == "nav"


# ----------------------------------------------------- learning is untouched


def test_new_ops_do_not_change_compress_steps() -> None:
    title = "去蚂蚁庄园逛一逛"
    enriched = [
        {"op": "run_start", "label": "r"},  # new op
        {"op": "look", "texts": ["得骰子赚闲鱼币", title, "去完成"], "in_row": True},
        {"op": "click", "text": "去完成"},
        {"op": "look", "texts": ["蚂蚁庄园"], "in_row": True},
        {"op": "click", "text": "取消"},
        {"op": "click_failed", "ok": False, "reason": "miss"},  # new op
        {"op": "look", "texts": ["蚂蚁庄园"]},
        {"op": "wait", "ms": 15000},
        {"op": "swipe", "start": [500, 850], "end": [500, 450], "ms": 3000},
        {"op": "click", "xy": [940, 80], "source": "coord"},
        {"op": "return", "via": "press_key", "detail": "home"},  # new op
        {"op": "look", "texts": ["得骰子赚闲鱼币", title, "领取奖励"]},
    ]
    new_ops = {"run_start", "return", "click_failed"}
    plain = [event for event in enriched if event.get("op") not in new_ops]

    result = compress_steps(enriched, title)
    assert result == compress_steps(plain, title)
    # And the shared result is still the real thing, not an empty fallback.
    assert result[0] == {"op": "tap_text", "text": "去完成", "near": title}
    assert any(step.get("op") == "swipe" for step in result)


# ------------------------------------------------------- learnable row span


def test_open_row_actions_inherit_title_and_boundary_ops_do_not(workdir: Path) -> None:
    trace = HandsTrace(workdir, device="testdev")
    trace.begin("去蚂蚁庄园逛一逛", extra={"how": "script"})
    trace.note({"op": "click", "text": "去完成", "ok": True})
    trace.append({"op": "click_failed", "ok": False, "reason": "miss"})
    trace.append({"op": "replay_start", "title": "另一行"})
    trace.begin_run("一轮")
    events = trace.finish(learned=False, reason="按钮仍是去完成")

    assert events[0]["title"] == "去蚂蚁庄园逛一逛"
    assert events[0]["in_row"] is True
    assert trace.title is None
    assert "去蚂蚁庄园逛一逛" in trace.pending_learn

    records = _records(trace)
    begin = records[0]
    assert begin["op"] == "begin_row"
    assert begin["how"] == "script"
    assert begin["run_id"]
    click = next(record for record in records if record.get("op") == "click")
    assert click["title"] == "去蚂蚁庄园逛一逛"
    assert click["in_row"] is True
    assert click["run_seq"] == 0
    failed = next(record for record in records if record.get("op") == "click_failed")
    assert failed["title"] == "去蚂蚁庄园逛一逛"
    assert failed["in_row"] is True
    replay = next(record for record in records if record.get("op") == "replay_start")
    assert replay["title"] == "另一行"
    assert "in_row" not in replay
    started = next(record for record in records if record.get("op") == "run_start")
    assert "title" not in started
    end = records[-1]
    assert end["op"] == "end_row"
    assert end["outcome"] == "fail"
    assert end["learned"] is False
    assert end["run_id"]
    assert end["run_seq"] == 1
    assert end["run_id"] != begin["run_id"]


def test_close_span_records_outcome_and_a_miss_still_closes(workdir: Path) -> None:
    trace = HandsTrace(workdir, device="testdev")
    trace.begin("甲")
    trace.close_span(learned=True, outcome="done", reason="")
    assert trace.title is None
    assert trace.completed == ["甲"]
    trace.begin("乙")
    trace.abandon(outcome="skip", reason="不做")
    assert trace.title is None
    assert "乙" not in trace.pending_learn
    outcomes = {
        record["title"]: record["outcome"]
        for record in _records(trace)
        if record.get("op") == "end_row"
    }
    assert outcomes == {"甲": "done", "乙": "skip"}


@pytest.mark.asyncio
async def test_click_role_infers_nav_and_explicit_role_wins(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = HandsTrace(workdir, device="testdev")
    elements = [{"text": "闲鱼币", "bounds": [0, 0, 200, 80]}]
    monkeypatch.setattr(hands_server, "runtime", _FakeRuntime(trace, _FakeSession(elements)))

    await hands_server.click(target=1)
    trace.begin("做一行")
    await hands_server.click(target=1, role="task")

    clicks = [record for record in _records(trace) if record.get("op") == "click"]
    assert clicks[0]["role"] == "nav"
    assert "title" not in clicks[0]
    assert clicks[1]["role"] == "task"
    assert clicks[1]["title"] == "做一行"
    assert clicks[1]["in_row"] is True
    assert "intent" not in clicks[1]

    # Pixel center (100, 40) on 1080x2400 is normalized (93, 17).
    await hands_server.click(target=[93, 17])
    coord = [record for record in _records(trace) if record.get("op") == "click"][-1]
    assert coord["source"] == "coord"
    assert coord["role"] == "nav"
    assert coord["near_text"] == "闲鱼币"


def _sheet(title: str, button: str) -> list[dict]:
    return [
        {"text": "得骰子赚闲鱼币", "bounds": [200, 80, 800, 140]},
        {"text": title, "bounds": [80, 400, 520, 460]},
        {"text": button, "bounds": [800, 400, 1000, 460]},
    ]


def _bind(trace: HandsTrace, session: _FakeSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hands_server, "runtime", _FakeRuntime(trace, session))


@pytest.mark.asyncio
async def test_end_row_default_does_not_write_catalog_and_a_miss_still_closes(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = workdir / "tasks" / "闲鱼币任务清单.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"rows": []}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(hands_server, "_repo_catalog", lambda: catalog)
    monkeypatch.delenv("ARTEMIS_HANDS_END_ROW_WRITE", raising=False)

    trace = HandsTrace(workdir, device="testdev")
    session = _FakeSession(_sheet("去蚂蚁庄园逛一逛", "去完成"))

    async def _look(include_image: bool = False, settle_ms: int = 0):
        obs = ObserveResult(
            ok=True,
            hierarchy_ok=True,
            elements=list(session.elements),
            width=1080,
            height=2400,
            screenshot_path=str(workdir / "screen.jpg"),
        )
        return obs, None

    session.look = _look  # type: ignore[method-assign]
    _bind(trace, session, monkeypatch)

    called: list[str] = []

    def _boom(*_args, **_kwargs):
        called.append("store")
        raise AssertionError("store_steps should not run")

    monkeypatch.setattr(hands_server, "store_steps", _boom)
    trace.begin("去蚂蚁庄园逛一逛", extra={"how": "script"})
    trace.note({"op": "click", "text": "去完成", "ok": True})
    missed = json.loads(await hands_server.end_row())
    assert missed["learned"] is False
    assert missed["outcome"] == "fail"
    assert missed["closed"] is True
    assert missed["catalog_written"] is False
    assert missed["reason"] == "按钮仍是去完成"
    assert trace.title is None
    assert called == []

    trace.begin("去蚂蚁庄园逛一逛")
    trace.note({"op": "click", "text": "去完成", "ok": True})
    session.elements = _sheet("去蚂蚁庄园逛一逛", "领取奖励")
    done = json.loads(await hands_server.end_row(reward="+10"))
    assert done["learned"] is True
    assert done["outcome"] == "done"
    assert done["catalog_written"] is False
    assert "steps" not in done
    assert trace.title is None
    assert json.loads(catalog.read_text(encoding="utf-8"))["rows"] == []
    ends = [record for record in _records(trace) if record.get("op") == "end_row"]
    assert ends[-1]["outcome"] == "done"
    assert ends[-1]["catalog_written"] is False
    assert ends[-1]["reward"] == "+10"
    assert ends[-1]["button"] == "领取奖励"
    assert "run_id" in ends[-1]


@pytest.mark.asyncio
async def test_end_row_writes_catalog_only_when_opted_in(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = workdir / "tasks" / "闲鱼币任务清单.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {"rows": [{"title": "去蚂蚁庄园逛一逛", "run": "yes", "steps": [], "script_fails": 0}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(hands_server, "_repo_catalog", lambda: catalog)
    monkeypatch.setenv("ARTEMIS_HANDS_END_ROW_WRITE", "1")

    trace = HandsTrace(workdir, device="testdev")
    session = _FakeSession(_sheet("去蚂蚁庄园逛一逛", "领取奖励"))

    async def _look(include_image: bool = False, settle_ms: int = 0):
        return (
            ObserveResult(
                ok=True,
                hierarchy_ok=True,
                elements=list(session.elements),
                width=1080,
                height=2400,
            ),
            None,
        )

    session.look = _look  # type: ignore[method-assign]
    _bind(trace, session, monkeypatch)
    trace.begin("去蚂蚁庄园逛一逛")
    trace.note({"op": "click", "text": "去完成", "ok": True})
    payload = json.loads(await hands_server.end_row())
    assert payload["learned"] is True
    assert payload["catalog_written"] is True
    assert payload["steps"][0]["text"] == "去完成"
    saved = json.loads(catalog.read_text(encoding="utf-8"))
    assert saved["rows"][0]["steps"][0]["text"] == "去完成"


@pytest.mark.asyncio
async def test_close_row_closes_without_a_look(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = HandsTrace(workdir, device="testdev")
    _bind(trace, _FakeSession(), monkeypatch)
    opened = json.loads(await hands_server.begin_row("甲", how="ai"))
    assert opened["how"] == "ai"
    closed = json.loads(await hands_server.close_row(outcome="lost", reason="掉线"))
    assert closed["outcome"] == "lost"
    assert closed["learned"] is False
    assert closed["catalog_written"] is False
    assert trace.title is None
    assert "甲" in trace.pending_learn
    end = [record for record in _records(trace) if record.get("op") == "end_row"][-1]
    assert end["outcome"] == "lost"
    assert end["reason"] == "掉线"
    assert "button" not in end


@pytest.mark.asyncio
async def test_replay_environment_failure_keeps_steps_and_records_fail_kind(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = workdir / "tasks" / "闲鱼币任务清单.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "title": "去百度逛一逛",
                        "run": "yes",
                        "script_fails": 2,
                        "steps": [{"op": "tap_text", "text": "去完成"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(hands_server, "_repo_catalog", lambda: catalog)
    trace = HandsTrace(workdir, device="testdev")
    session = _FakeSession([])

    async def _look(include_image: bool = False, settle_ms: int = 0):
        return ObserveResult(ok=True, hierarchy_ok=False, elements=[]), None

    session.look = _look  # type: ignore[method-assign]
    _bind(trace, session, monkeypatch)

    payload = json.loads(await hands_server.replay_row("去百度逛一逛"))
    assert payload["ok"] is False
    assert payload["outcome"] == "fail"
    assert payload["fail_kind"] == "look"
    assert payload["env_failure"] is True
    assert payload["script_fails"] == 2
    assert payload["cleared"] is False
    saved = json.loads(catalog.read_text(encoding="utf-8"))
    assert saved["rows"][0]["steps"]
    assert saved["rows"][0]["script_fails"] == 2
    records = _records(trace)
    start = next(record for record in records if record.get("op") == "replay_start")
    assert start["how"] == "replay"
    assert start["steps"] == 1
    assert start["title"] == "去百度逛一逛"
    assert "summary" in start
    end = next(record for record in records if record.get("op") == "replay_end")
    assert end["fail_kind"] == "look"
    assert end["outcome"] == "fail"
    assert "run_id" in end
    assert "in_row" not in end
