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
    assert _in_row_field() == {"in_row": True}
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
    assert "purpose" not in presses[-1]


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
