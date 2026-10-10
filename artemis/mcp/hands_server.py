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

"""Stdio/SSE MCP server that lets an external agent drive one phone.

The server lists devices, locks one, and forwards ``look`` plus physical
actions. It does not plan, check, or call a model.
"""

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

from artemis.mcp.action_types import ActionCode, ActionResult
from artemis.mcp.actuators.adb import AdbActuator
from artemis.mcp.hands_session import HANDS_TOOL_NAMES, HandsSession
from artemis.mcp.hands_trace import HandsTrace, RowBusy
from artemis.mcp.row_steps import (
    catalog_path,
    compress_steps,
    find_catalog_row,
    find_tap,
    infer_role,
    judge_row,
    load_catalog,
    note_replay_failure,
    note_replay_success,
    store_steps,
    texts_of,
)
from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock

__all__ = ["HANDS_TOOL_NAMES", "HandsBindError", "HandsRuntime", "mcp", "main"]

mcp = FastMCP(os.environ.get("ARTEMIS_HANDS_NAME", "artemis_hands"))


class HandsBindError(RuntimeError):
    """The requested device cannot be listed or locked."""


class HandsRuntime:
    """Process-wide binding of one device to a :class:`HandsSession`."""

    def __init__(self) -> None:
        self.session: HandsSession | None = None
        self.lock: DeviceExecutionLock | None = None
        self.serial: str | None = None
        self.trace = HandsTrace(Path(__file__).resolve().parents[2])
        #: Last foreground package seen by ``look``. Stamped onto later action
        #: records so the log shows which app an action was fired from -- and so
        #: a cross-app jump shows up as a change between two consecutive stamps.
        #: Read-only bookkeeping: nothing in the decision path consults it.
        self.last_package: str | None = None

    def list_devices(self) -> list[str]:
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
            return [os.environ.get("ARTEMIS_DEVICE_ID") or "cloud_device"]
        from adbutils import AdbClient

        host = os.environ.get("ADB_HOST", "localhost")
        port_str = os.environ.get("ADB_PORT", "5037")
        port = int(port_str) if port_str.isdigit() else 5037
        return [device.serial for device in AdbClient(host=host, port=port).device_list()]

    def release(self) -> None:
        if self.lock is not None:
            self.lock.release()
            self.lock = None

    def use_device(self, serial: str | None = None) -> str:
        """Locks ``serial`` (or the env / first device) and builds a session."""
        chosen = (serial or "").strip() or (
            os.environ.get("ARTEMIS_DEVICE_ID") or os.environ.get("ADB_DEVICE_SERIAL") or ""
        ).strip()
        available = self.list_devices()
        if not chosen:
            if not available:
                raise HandsBindError("No Android devices found.")
            chosen = available[0]
        elif available and chosen not in available:
            raise HandsBindError(f"Device '{chosen}' not found. Connected: {', '.join(available)}.")
        if self.session is not None and self.serial == chosen and self.lock is not None:
            return chosen

        new_lock = DeviceExecutionLock(
            device_id=chosen,
            description="hands mcp",
            ingress="mcp-hands",
        )
        try:
            new_lock.acquire(blocking=False)
        except DeviceBusyError as exc:
            raise HandsBindError(str(exc)) from exc

        try:
            from artemis.mcp.adb_server import _get_controller

            controller = _get_controller(chosen)
            session = HandsSession(AdbActuator(controller.ctx, controller))
        except Exception as exc:
            new_lock.release()
            raise HandsBindError(f"Failed to open device '{chosen}': {exc}") from exc

        self.release()
        self.lock = new_lock
        self.session = session
        self.serial = chosen
        self.trace.bind(chosen)
        self.trace.append(
            {"op": "use_device", "serial": chosen, "available": available}
        )
        return chosen

    def ensure(self) -> HandsSession:
        if self.session is None:
            self.use_device(None)
        assert self.session is not None
        return self.session


runtime = HandsRuntime()


def _failure(action: str, message: str) -> CallToolResult:
    result = ActionResult.failure(action, message, code=ActionCode.DEVICE_ERROR)
    return CallToolResult(
        content=[TextContent(type="text", text=result.message)],
        structuredContent=result.model_dump(mode="json"),
        isError=False,
    )


def _ok_result(result: ActionResult) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=result.message)],
        structuredContent=result.model_dump(mode="json"),
        isError=False,
    )


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _look_texts(session: HandsSession) -> list[str]:
    return texts_of(session.elements)[:80]


def _click_text(session: HandsSession, target: Any) -> str:
    if isinstance(target, str) and target.strip().isdigit():
        target = int(target.strip())
    if isinstance(target, int) and 1 <= target <= len(session.elements):
        return str(session.elements[target - 1].get("text") or "").strip()
    return ""


# ---------------------------------------------------------------- log detail
#
# Everything below only enriches what lands in the daily jsonl. It never changes
# what a tool does, what it returns, or how a row is judged -- a log that could
# steer the run would be worse than no log at all.

#: Stamping the foreground package needs one extra ``dumpsys`` per look. On by
#: default; set to 0 to skip it entirely if adb is being slow or flaky.
PKG_LOG_ENV = "ARTEMIS_HANDS_LOG_PKG"


def _pkg_log_enabled() -> bool:
    return os.environ.get(PKG_LOG_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


async def _refresh_package(session: HandsSession) -> str | None:
    """Read the foreground package and cache it for the next action records.

    Best-effort only: a missing driver, a slow shell, or any error keeps the
    previous stamp and never raises. Bounded so a hung adb cannot stall a run.
    """
    if not _pkg_log_enabled():
        return runtime.last_package
    try:
        driver = getattr(session.actuator.controller, "driver", None)
        getter = getattr(driver, "get_current_package", None)
        if getter is None:
            return runtime.last_package
        package = await asyncio.wait_for(getter(), timeout=3.0)
    except Exception:
        return runtime.last_package
    if package:
        runtime.last_package = str(package)
    return runtime.last_package


def _pkg_field() -> dict[str, Any]:
    """The cached foreground package. Cheap: no device round-trip."""
    return {"pkg": runtime.last_package} if runtime.last_package else {}


def _target_source(session: HandsSession, target: Any) -> dict[str, Any]:
    """How a tap addressed its target: an element index (tree) or a raw point.

    A coordinate tap also gets ``near_*`` fields naming the closest element, so
    the log can tell a blind guess from a tap on a known element. Purely
    additive: ``source`` still reads ``"tree"`` or ``"coord"`` exactly as before.
    """
    if isinstance(target, str) and target.strip().isdigit():
        target = int(target.strip())
    if isinstance(target, int) and not isinstance(target, bool):
        info: dict[str, Any] = {"source": "tree", "index": target}
        if 1 <= target <= len(session.elements):
            element = session.elements[target - 1]
            if element.get("bounds"):
                info["bounds"] = element["bounds"]
            if element.get("class"):
                info["element_class"] = element["class"]
        return info
    info = {"source": "coord"}
    if (
        isinstance(target, (list, tuple))
        and len(target) == 2
        and all(
            isinstance(part, (int, float)) and not isinstance(part, bool)
            for part in target
        )
    ):
        near = _nearest_element(
            session.elements,
            float(target[0]),
            float(target[1]),
            getattr(session, "width", None),
            getattr(session, "height", None),
        )
        if near is not None:
            info.update(near)
    return info


#: A coordinate tap whose point lands within this many normalized units (0-1000)
#: of an element's bounds-center is close enough to name that element in the log.
#: It is a 0-1000 space quantity, so the element side must be normalized first.
NEAR_BOUNDS_RADIUS = 80


def _bounds_center(element: dict[str, Any]) -> tuple[float, float] | None:
    """Pixel bounds-center of an element, or ``None``.

    Bounds are device pixels: ``artemis/utils/visualization.py:425`` writes
    ``[left, top, right, bottom]`` straight from the accessibility dump, no
    normalizing. So this is a pixel center -- ``_nearest_element`` normalizes it
    before any distance is compared. Defensive on purpose: ``bounds`` may be
    missing, the wrong length, or hold non-numeric parts. Any of those is
    skipped, never raised -- this only feeds a log field.
    """
    bounds = element.get("bounds")
    if not (isinstance(bounds, (list, tuple)) and len(bounds) == 4):
        return None
    try:
        left, top, right, bottom = (float(bounds[index]) for index in range(4))
    except (TypeError, ValueError):
        return None
    return ((left + right) / 2.0, (top + bottom) / 2.0)


def _to_normalized(value: float, extent: Any) -> int | None:
    """Map one pixel value onto the 0-1000 space, or ``None`` if impossible.

    Reuses the exact formula ``resolve_target`` already uses
    (``artemis/mcp/hands_session.py:92-93``):
    ``int(max(0, min(1000, round(value * 1000 / max(1, extent)))))`` -- the same
    convention ``row_steps.find_tap`` also follows. ``None`` is returned when
    ``extent`` is missing, zero, non-numeric, or a bool, so the caller declines to
    guess rather than dividing by a bad size.
    """
    if isinstance(extent, bool):
        return None
    try:
        size = float(extent)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    return int(max(0, min(1000, round(value * 1000 / max(1.0, size)))))


def _nearest_element(
    elements: list[dict[str, Any]],
    x: float,
    y: float,
    width: Any,
    height: Any,
) -> dict[str, Any] | None:
    """Describe the element nearest a normalized tap, or ``None`` if none is close.

    The tap (``x``/``y``) is normalized 0-1000 -- the ``click`` convention. Element
    bounds are device pixels, so each bounds-center is normalized first with the
    same formula ``resolve_target`` uses (``artemis/mcp/hands_session.py:92-93``);
    only then is a Euclidean distance between them meaningful. A missing, zero, or
    non-numeric ``width``/``height`` makes that conversion impossible, so the tap
    is left unnamed rather than guessed. Returns log fields only (``near_*``); it
    never decides anything.
    """
    best: dict[str, Any] | None = None
    best_distance: float | None = None
    for element in elements:
        center = _bounds_center(element)
        if center is None:
            continue
        nx = _to_normalized(center[0], width)
        ny = _to_normalized(center[1], height)
        if nx is None or ny is None:
            return None
        distance = ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = element
    if best is None or best_distance is None or best_distance > NEAR_BOUNDS_RADIUS:
        return None
    info: dict[str, Any] = {
        "near_text": str(best.get("text") or "").strip(),
        "near_distance": round(best_distance, 1),
    }
    if best.get("class"):
        info["near_class"] = best["class"]
    if best.get("bounds"):
        info["near_bounds"] = best["bounds"]
    return info


def _in_row_field() -> dict[str, Any]:
    """Whether an action lands inside an open row, and which row that is.

    Only ~39% of actions today fall between ``begin_row`` and ``end_row``. This
    field makes that ratio countable per action instead of inferred. The title
    is repeated on the action so a miner can group the span without walking
    backwards to ``begin_row``. Read-only: it never changes what a tool does.
    """
    title = runtime.trace.title
    if not title:
        return {"in_row": False}
    return {"in_row": True, "title": title}


def _role_field(*, text: str = "", role: str = "", key: str = "") -> dict[str, Any]:
    """Learning bucket for one action. An explicit ``role`` always wins.

    Navigation / permission / cold-start are inferred from the blacklist even
    outside a row. The residual bucket ``task`` is written only while a row is
    open, so an out-of-row tap is not pretended to be a task step.
    """
    chosen = role.strip()
    if chosen:
        return {"role": chosen}
    if text or key:
        inferred = infer_role(text, key or None)
        if inferred != "task":
            return {"role": inferred}
        if runtime.trace.title:
            return {"role": "task"}
        return {}
    if runtime.trace.title:
        return {"role": "task"}
    return {}


def _trace_done(
    event: dict[str, Any],
    *,
    text: str = "",
    role: str = "",
    key: str = "",
) -> None:
    """Log a successful action. Stays in the row's event stream, as before."""
    runtime.trace.note(
        {
            "ok": True,
            **event,
            **_in_row_field(),
            **_role_field(text=text, role=role, key=key),
        }
    )


def _trace_missed(
    action: str,
    result: ActionResult,
    event: dict[str, Any] | None = None,
    *,
    text: str = "",
    role: str = "",
    key: str = "",
) -> None:
    """Log a failed action to the daily jsonl only.

    Uses ``append`` rather than ``note`` on purpose: a failure must never enter
    the open row's event stream, where ``compress_steps`` could mistake it for a
    step that is safe to replay. The open row's title is still stamped on the
    line so offline stats can count misses per title.
    """
    runtime.trace.append(
        {
            "op": f"{action}_failed",
            "ok": False,
            "reason": result.message,
            "code": getattr(result.code, "value", str(result.code)),
            **(event or {}),
            **_in_row_field(),
            **_role_field(text=text, role=role, key=key),
        }
    )


def _note_return(via: str, detail: str) -> None:
    """Record one deliberate return (go home, then reopen the target app).

    The return leg is the highest-frequency failure in the field and no stored
    ``steps`` can express it, so it is structured here for later mining. It is a
    meta record only: written with ``append`` (never the row's event stream).
    ``role`` is ``nav`` so miners can drop it. An open row may still stamp
    ``title``; the op is not ``begin_row`` / ``end_row``, so recovery will not
    pair this line as a span boundary.
    """
    runtime.trace.append(
        {"op": "return", "via": via, "detail": detail, "role": "nav", **_pkg_field()}
    )


def _step_expect(step: dict[str, Any]) -> dict[str, Any]:
    """What a stored replay step was looking for, for the log line."""
    op = step.get("op")
    expect: dict[str, Any] = {}
    if op == "tap_text":
        if step.get("text"):
            expect["text"] = step["text"]
        if step.get("any"):
            expect["any"] = step["any"]
        if step.get("near"):
            expect["near"] = step["near"]
    elif op == "swipe":
        expect["start"] = step.get("start")
        expect["end"] = step.get("end")
    elif op == "wait":
        expect["ms"] = step.get("ms")
    elif op == "key":
        expect["key"] = step.get("key")
    return expect


def _steps_summary(steps: list[dict[str, Any]]) -> dict[str, int]:
    """Count stored steps by kind, so the log shows the shape of a script."""
    summary: dict[str, int] = {}
    for step in steps:
        key = str(step.get("op") or "?")
        summary[key] = summary.get(key, 0) + 1
    return summary


def _note_look(session: HandsSession, obs: Any, settle_ms: int | None = None) -> None:
    texts = _look_texts(session)
    event: dict[str, Any] = {
        "op": "look",
        "texts": texts,
        "image": obs.screenshot_path,
        "elements": len(session.elements),
        "width": session.width,
        "height": session.height,
        "hierarchy_ok": bool(getattr(obs, "hierarchy_ok", True)),
        **_pkg_field(),
        **_in_row_field(),
    }
    if settle_ms is not None:
        event["settle_ms"] = settle_ms
    runtime.trace.note(event)
    runtime.trace.set_before({"texts": texts, **_pkg_field()})


#: The screenshot is persisted to disk on every ``look``; only its path travels
#: back to the agent. Inlining the pixels is opt-in because one JPEG is
#: ~0.5-0.7 MB of base64 that the host re-sends on every later turn -- a long
#: run stacks up hundreds and drives the host process into an abort.
LOOK_IMAGE_ENV = "ARTEMIS_HANDS_LOOK_IMAGE"


def _inline_image_enabled() -> bool:
    return os.environ.get(LOOK_IMAGE_ENV, "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _look_text(obs) -> str:
    if not obs.ok:
        return obs.message or "Failed to capture the screen."
    text = (obs.elements_text or "").strip()
    if not text:
        text = (
            "No indexed elements on this screen. Decide from the screenshot and pass"
            " normalized [x, y] coordinates (0-1000)."
        )
    if obs.screenshot_path:
        text += f"\n[screenshot] {obs.screenshot_path}"
    return text


@mcp.tool(name="list_devices")
async def list_devices() -> str:
    """List connected Android device serials. Does not lock a device."""
    try:
        serials = runtime.list_devices()
    except Exception as exc:
        return f"Failed to list devices: {exc}"
    if not serials:
        return "No Android devices found."
    return "\n".join(serials)


@mcp.tool(name="use_device")
async def use_device(serial: str | None = None) -> str:
    """Lock one device for later look/action calls.

    Omit serial to use ARTEMIS_DEVICE_ID / ADB_DEVICE_SERIAL, or the first
    connected device. Fails when that device is already running a task.
    """
    try:
        chosen = runtime.use_device(serial)
    except HandsBindError as exc:
        return f"Error: {exc}"
    return f"Bound to {chosen}."


@mcp.tool(name="look")
async def look(include_image: bool = False, settle_ms: int = 400) -> CallToolResult:
    """Capture the current screen for the agent to decide the next action.

    Returns the numbered element list as text, plus the absolute path of the
    screenshot persisted for this observation -- open that file if you need the
    pixels. The image is never inlined unless the server opts in with
    ``ARTEMIS_HANDS_LOOK_IMAGE=1``.

    Rationale: one screenshot is ~0.5-0.7 MB of base64, which lands in the
    session transcript on every call. A long run accumulates hundreds of them
    and drives the host agent process into an abort. The element list is the
    signal that actually drives the clicks. Element indexes start at 1 and stay
    valid until the next successful look. This server does not interpret the
    screen.
    """
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("look", str(exc))
    inline = include_image and _inline_image_enabled()
    try:
        obs, image = await session.look(include_image=inline, settle_ms=settle_ms)
    except Exception as exc:
        return _failure("look", f"Error during look: {exc}")
    if obs.ok and obs.hierarchy_ok:
        await _refresh_package(session)
        _note_look(session, obs, settle_ms=settle_ms)
    else:
        # A look that captured nothing used to leave no trace at all. Record the
        # miss under its own op so it is visible without perturbing learning:
        # ``compress_steps`` only ever reads ``op == "look"`` events.
        runtime.trace.append(
            {
                "op": "look_failed",
                "ok": False,
                "hierarchy_ok": bool(getattr(obs, "hierarchy_ok", False)),
                "message": obs.message,
                "settle_ms": settle_ms,
                **_pkg_field(),
                **_in_row_field(),
            }
        )
    content: list[Any] = [TextContent(type="text", text=_look_text(obs))]
    if inline and image:
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(image).decode("utf-8"),
                mimeType="image/jpeg",
            )
        )
    elif include_image:
        # Say so explicitly: a silent drop would read as a failed capture.
        content.append(
            TextContent(
                type="text",
                text=(
                    "[screenshot not inlined] open this file for the pixels:"
                    f" {obs.screenshot_path}\n"
                    f"(the server only inlines images when {LOOK_IMAGE_ENV}=1)"
                ),
            )
        )
    return CallToolResult(
        content=content,
        structuredContent=obs.model_dump(mode="json"),
        isError=False,
    )


@mcp.tool(name="click")
async def click(
    target: int | list[int],
    times: int = 1,
    delay_ms: int = 100,
    intent: str = "",
    role: str = "",
) -> CallToolResult:
    """Tap an element index from the latest look, or a normalized [x, y] point.

    ``intent`` is the caller's short name for why this tap happens -- 关广告 /
    找入口 / 停留 / 确认 / 跳过 / 领取 / 回程. It upgrades a pixel tap into a
    semantic action so runs can be mined for stable steps. Empty is the default
    and writes no ``intent`` key, keeping older log lines unchanged.

    ``role`` is the learning bucket (``task`` / ``nav`` / ``permission`` /
    ``cold_start``). Empty lets the server infer one from the tapped text.
    An explicit value always wins, including a task-step 「返回」.
    """
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("click", str(exc))
    text = _click_text(session, target)
    source = _target_source(session, target)
    # An index tap already has ``text``. A coordinate tap does not, so the
    # blacklist can still see the nearest element's words.
    named = text or str(source.get("near_text") or "")
    try:
        result = await session.click(target, times=times, delay_ms=delay_ms)
    except Exception as exc:
        missed = {"text": text, **source, **_pkg_field()}
        if intent:
            missed["intent"] = intent
        _trace_missed(
            "click",
            ActionResult.failure("click", f"Error during click: {exc}"),
            missed,
            text=named,
            role=role,
        )
        return _failure("click", f"Error during click: {exc}")
    if result.ok:
        done = {
            "op": "click",
            "text": text,
            "xy": result.normalized_coordinates,
            "times": times,
            **source,
            **_pkg_field(),
        }
        if intent:
            done["intent"] = intent
        _trace_done(done, text=named, role=role)
    else:
        missed = {"text": text, **source, **_pkg_field()}
        if intent:
            missed["intent"] = intent
        _trace_missed("click", result, missed, text=named, role=role)
    return _ok_result(result)


@mcp.tool(name="long_press")
async def long_press(
    target: int | list[int],
    duration_ms: int = 1000,
    intent: str = "",
) -> CallToolResult:
    """Long-press an element index from the latest look, or a normalized [x, y] point.

    ``intent`` carries the same meaning as on ``click``. Empty by default.
    """
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("long_press", str(exc))
    text = _click_text(session, target)
    source = _target_source(session, target)
    try:
        result = await session.long_press(target, duration_ms=duration_ms)
    except Exception as exc:
        return _failure("long_press", f"Error during long_press: {exc}")
    if result.ok:
        done = {
            "op": "long_press",
            "text": text,
            "xy": result.normalized_coordinates,
            "duration_ms": duration_ms,
            **source,
            **_pkg_field(),
        }
        if intent:
            done["intent"] = intent
        _trace_done(done)
    else:
        missed = {"text": text, **source, **_pkg_field()}
        if intent:
            missed["intent"] = intent
        _trace_missed("long_press", result, missed)
    return _ok_result(result)


@mcp.tool(name="swipe")
async def swipe(
    start: list[int],
    end: list[int],
    duration_ms: int = 800,
    intent: str = "",
) -> CallToolResult:
    """Swipe between two normalized [x, y] points (0-1000).

    ``intent`` records why the swipe happens (滚动 / 翻页 / 回程), same as on
    ``click``. Empty by default; no key is written when empty.
    """
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("swipe", str(exc))
    try:
        result = await session.swipe(start, end, duration_ms=duration_ms)
    except Exception as exc:
        return _failure("swipe", f"Error during swipe: {exc}")
    if result.ok:
        done = {"op": "swipe", "start": start, "end": end, "ms": duration_ms, **_pkg_field()}
        if intent:
            done["intent"] = intent
        _trace_done(done)
    else:
        missed = {"start": start, "end": end, "ms": duration_ms, **_pkg_field()}
        if intent:
            missed["intent"] = intent
        _trace_missed("swipe", result, missed)
    return _ok_result(result)


@mcp.tool(name="input_text")
async def input_text(
    text: str,
    target: int | list[int] | None = None,
    clear_exist: bool = True,
) -> CallToolResult:
    """Type text. Optional target focuses an element index or normalized [x, y] first."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("input_text", str(exc))
    target_info = _target_source(session, target) if target is not None else {}
    try:
        result = await session.input_text(text, target=target, clear_exist=clear_exist)
    except Exception as exc:
        return _failure("input_text", f"Error during input_text: {exc}")
    if result.ok:
        _trace_done(
            {
                "op": "input_text",
                "text": text,
                "chars": len(text),
                "clear_exist": clear_exist,
                **target_info,
                **_pkg_field(),
            }
        )
    else:
        _trace_missed("input_text", result, {"chars": len(text), **target_info, **_pkg_field()})
    return _ok_result(result)


@mcp.tool(name="press_key")
async def press_key(key: str, purpose: str = "", role: str = "") -> CallToolResult:
    """Press a key such as back, home, enter, or an Android KEYCODE_* name.

    ``purpose`` records why the key is pressed. When it is ``"return"`` the run
    is going home to reopen the target app -- the most common failure mode in the
    field, and one the stored ``steps`` cannot express -- so a dedicated
    ``op: "return"`` record is written too. Empty by default.

    ``role`` overrides the inferred bucket. Home / recents infer ``cold_start``
    unless the caller passes ``role`` (a task-step back must stay ``task``).
    """
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("press_key", str(exc))
    try:
        result = await session.press_key(key)
    except Exception as exc:
        missed = {"key": key, **_pkg_field()}
        if purpose:
            missed["purpose"] = purpose
        _trace_missed(
            "press_key",
            ActionResult.failure("press_key", f"Error during press_key: {exc}"),
            missed,
            role=role,
            key=key,
        )
        return _failure("press_key", f"Error during press_key: {exc}")
    if result.ok:
        done = {"op": "press_key", "key": key, **_pkg_field()}
        if purpose:
            done["purpose"] = purpose
        _trace_done(done, role=role, key=key)
        if purpose == "return":
            _note_return("press_key", key)
    else:
        missed = {"key": key, **_pkg_field()}
        if purpose:
            missed["purpose"] = purpose
        _trace_missed("press_key", result, missed, role=role, key=key)
    return _ok_result(result)


@mcp.tool(name="manage_app")
async def manage_app(action: str, app_name: str, purpose: str = "") -> CallToolResult:
    """Launch or stop an app by name or package. action is 'launch' or 'stop'.

    ``purpose="return"`` also writes the dedicated return record (see
    ``press_key``). Empty by default.
    """
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("manage_app", str(exc))
    try:
        result = await session.manage_app(action, app_name)
    except Exception as exc:
        return _failure("manage_app", f"Error during manage_app: {exc}")
    if result.ok:
        done = {"op": "manage_app", "action": action, "app_name": app_name, **_pkg_field()}
        if purpose:
            done["purpose"] = purpose
        _trace_done(done)
        if purpose == "return":
            _note_return("manage_app", app_name)
    else:
        missed = {"action": action, "app_name": app_name, **_pkg_field()}
        if purpose:
            missed["purpose"] = purpose
        _trace_missed("manage_app", result, missed)
    return _ok_result(result)


@mcp.tool(name="open_link")
async def open_link(url: str, purpose: str = "") -> CallToolResult:
    """Open a URL or deep link on the device.

    ``purpose="return"`` also writes the dedicated return record (see
    ``press_key``). Empty by default.
    """
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("open_link", str(exc))
    try:
        result = await session.open_link(url)
    except Exception as exc:
        return _failure("open_link", f"Error during open_link: {exc}")
    if result.ok:
        done = {"op": "open_link", "url": url, **_pkg_field()}
        if purpose:
            done["purpose"] = purpose
        _trace_done(done)
        if purpose == "return":
            _note_return("open_link", url)
    else:
        missed = {"url": url, **_pkg_field()}
        if purpose:
            missed["purpose"] = purpose
        _trace_missed("open_link", result, missed)
    return _ok_result(result)


@mcp.tool(name="erase_one_char")
async def erase_one_char() -> CallToolResult:
    """Erase one character in the focused field."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("erase_one_char", str(exc))
    try:
        result = await session.erase_one_char()
    except Exception as exc:
        return _failure("erase_one_char", f"Error during erase_one_char: {exc}")
    if result.ok:
        _trace_done({"op": "erase_one_char", **_pkg_field()})
    else:
        _trace_missed("erase_one_char", result, _pkg_field())
    return _ok_result(result)


@mcp.tool(name="focus_and_clear_text")
async def focus_and_clear_text(target: int | list[int]) -> CallToolResult:
    """Focus an element index or normalized [x, y] and clear its text."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("focus_and_clear_text", str(exc))
    source = _target_source(session, target)
    try:
        result = await session.focus_and_clear_text(target)
    except Exception as exc:
        return _failure("focus_and_clear_text", f"Error during focus_and_clear_text: {exc}")
    if result.ok:
        _trace_done({"op": "focus_and_clear_text", **source, **_pkg_field()})
    else:
        _trace_missed("focus_and_clear_text", result, {**source, **_pkg_field()})
    return _ok_result(result)


@mcp.tool(name="wait_for_delay")
async def wait_for_delay(time_in_ms: int) -> CallToolResult:
    """Wait for a fixed number of milliseconds. Does not decide what to do next."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("wait_for_delay", str(exc))
    try:
        result = await session.wait_for_delay(time_in_ms)
    except Exception as exc:
        return _failure("wait_for_delay", f"Error during wait_for_delay: {exc}")
    if result.ok:
        _trace_done({"op": "wait", "ms": time_in_ms, **_pkg_field()})
    else:
        _trace_missed("wait_for_delay", result, {"ms": time_in_ms, **_pkg_field()})
    return _ok_result(result)


@mcp.tool(name="wait_for_text")
async def wait_for_text(
    text: str, wait_state: str = "appear", timeout_ms: int = 5000
) -> CallToolResult:
    """Wait until text appears in or disappears from the UI tree. wait_state is appear or disappear."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("wait_for_text", str(exc))
    try:
        result = await session.wait_for_text(text, wait_state=wait_state, timeout_ms=timeout_ms)
    except Exception as exc:
        return _failure("wait_for_text", f"Error during wait_for_text: {exc}")
    detail = {"text": text, "wait_state": wait_state, "timeout_ms": timeout_ms, **_pkg_field()}
    if result.ok:
        _trace_done({"op": "wait_for_text", **detail})
    else:
        _trace_missed("wait_for_text", result, detail)
    return _ok_result(result)


@mcp.tool(name="click_sequence")
async def click_sequence(sequence: list[list[int]], delay_ms: int = 50) -> CallToolResult:
    """Tap a series of normalized [x, y] points without looking between them.

    Element indexes are refused: the list would be stale after the first tap.
    """
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("click_sequence", str(exc))
    try:
        result = await session.click_sequence(sequence, delay_ms=delay_ms)
    except Exception as exc:
        return _failure("click_sequence", f"Error during click_sequence: {exc}")
    detail = {"points": sequence, "delay_ms": delay_ms, "count": len(sequence), **_pkg_field()}
    if result.ok:
        _trace_done({"op": "click_sequence", **detail})
    else:
        _trace_missed("click_sequence", result, detail)
    return _ok_result(result)


def _repo_catalog() -> Path:
    return catalog_path(Path(__file__).resolve().parents[2])


def _catalog_write_enabled() -> bool:
    """Old ``end_row`` wrote steps into the catalog. That is opt-in now."""
    return os.environ.get("ARTEMIS_HANDS_END_ROW_WRITE") == "1"


def _replay_fail_kind(reason: str, hinted: str) -> str:
    """``script`` / ``env`` / ``timeout`` / ``look``. Environment phrases win."""
    if "任务弹层没开" in reason:
        return "env"
    if "看屏失败" in reason:
        return "look"
    return hinted


@mcp.tool(name="begin_run")
async def begin_run(label: str = "") -> str:
    """Start a new run: rotate run_id and bump run_seq so every task run is separable."""
    try:
        runtime.ensure()
    except HandsBindError as exc:
        return _json({"ok": False, "reason": str(exc)})
    tokens = runtime.trace.begin_run(label)
    return _json(
        {
            "ok": True,
            "run_id": tokens["run_id"],
            "run_seq": tokens["run_seq"],
            "label": label,
        }
    )


@mcp.tool(name="begin_row")
async def begin_row(title: str, how: str = "") -> str:
    """Start learning one task row. Refuses while the previous row is still open.

    ``how`` is who drives the row (``script`` / ``ai`` / ``replay`` / ``manual``).
    It is logged on ``begin_row`` next to ``pkg`` and does not change learning.
    """
    name = title.strip()
    if not name:
        return _json({"ok": False, "reason": "行名是空的"})
    extra: dict[str, Any] = dict(_pkg_field())
    how_name = how.strip()
    if how_name:
        extra["how"] = how_name
    try:
        runtime.trace.begin(name, extra=extra or None)
    except RowBusy as exc:
        return _json({"ok": False, "reason": str(exc), "open_title": exc.title})
    payload: dict[str, Any] = {"ok": True, "title": name}
    if how_name:
        payload["how"] = how_name
    return _json(payload)


@mcp.tool(name="close_row")
async def close_row(outcome: str = "", reason: str = "", abandon: bool = False) -> str:
    """Close the open row without looking and without writing the catalog.

    Use this when the caller already knows the outcome. ``outcome`` is
    ``done`` / ``fail`` / ``skip`` / ``lost``. Empty becomes ``skip`` when
    ``abandon`` is set, otherwise ``fail``. ``abandon`` marks the row as not
    worth learning, same as ``end_row(abandon=True)``.
    """
    if not runtime.trace.title:
        return _json(
            {"ok": False, "learned": False, "reason": "没有进行中的行", "outcome": "fail"}
        )
    title = runtime.trace.title
    outcome_name = outcome.strip() or ("skip" if abandon else "fail")
    note = reason.strip()
    extra = dict(_pkg_field()) or None
    if abandon:
        runtime.trace.abandon(outcome=outcome_name, reason=note, extra=extra)
        learned = False
    else:
        learned = outcome_name == "done"
        runtime.trace.finish(learned=learned, outcome=outcome_name, reason=note, extra=extra)
    return _json(
        {
            "ok": True,
            "title": title,
            "outcome": outcome_name,
            "learned": learned,
            "reason": note,
            "abandon": abandon,
            "closed": True,
            "catalog_written": False,
        }
    )


@mcp.tool(name="end_row")
async def end_row(abandon: bool = False, reward: str = "") -> str:
    """Look once, log whether this row looks done, and close the span.

    A miss still closes the row: it becomes an offline debt, not a lock on
    later rows. ``learned`` means the sheet looked finished. It does **not**
    mean steps were written into the catalog. Set
    ``ARTEMIS_HANDS_END_ROW_WRITE=1`` to restore that write.

    ``abandon`` closes the row as skipped without looking. ``reward`` is an
    optional payout note (e.g. ``+100``) stored on the ``end_row`` line.

    Prefer ``close_row`` when the outcome is already known and another look
    would judge the wrong screen.
    """
    if not runtime.trace.title:
        return _json(
            {"ok": False, "learned": False, "reason": "没有进行中的行", "outcome": "fail"}
        )
    title = runtime.trace.title
    if abandon:
        extra: dict[str, Any] = {"done": False, "catalog_written": False, **_pkg_field()}
        if reward:
            extra["reward"] = reward
        runtime.trace.abandon(extra=extra, outcome="skip")
        return _json(
            {
                "ok": True,
                "learned": False,
                "title": title,
                "abandon": True,
                "outcome": "skip",
                "closed": True,
                "catalog_written": False,
            }
        )
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _json(
            {
                "ok": False,
                "learned": False,
                "reason": str(exc),
                "outcome": "fail",
                "closed": False,
            }
        )
    try:
        obs, _image = await session.look(include_image=False, settle_ms=300)
    except Exception as exc:
        return _json(
            {
                "ok": False,
                "learned": False,
                "reason": f"看屏失败：{exc}",
                "outcome": "fail",
                "closed": False,
            }
        )
    if not (obs.ok and obs.hierarchy_ok):
        return _json(
            {
                "ok": False,
                "learned": False,
                "reason": "看屏失败",
                "outcome": "fail",
                "closed": False,
            }
        )
    await _refresh_package(session)
    _note_look(session, obs, settle_ms=300)
    judged = judge_row(session.elements, title)
    # Diagnostics for the end_row line: which button decided the outcome, how
    # much of the screen the tree exposed, and whether the row counts as done.
    # ``done`` is ``judged["learned"]`` -- the exact signal that gates learning,
    # so a log reader gets the same verdict the row was closed on. These fields
    # are never consulted by the judgement itself. ``catalog_written`` says
    # whether this call also stored steps; ``learned`` alone does not.
    detail: dict[str, Any] = {
        "button": judged["button"],
        "elements": len(session.elements),
        "done": judged["learned"],
        "catalog_written": False,
        **_pkg_field(),
    }
    if reward:
        detail["reward"] = reward
    if not judged["learned"]:
        runtime.trace.finish(
            learned=False, reason=judged["reason"], outcome="fail", extra=detail
        )
        return _json(
            {
                "ok": True,
                "learned": False,
                "title": title,
                "reason": judged["reason"],
                "outcome": "fail",
                "closed": True,
                "catalog_written": False,
            }
        )
    steps = compress_steps(runtime.trace.events, title)
    if not steps:
        runtime.trace.finish(
            learned=False,
            reason="没有可按文字重放的点击",
            outcome="fail",
            extra=detail,
        )
        return _json(
            {
                "ok": True,
                "learned": False,
                "title": title,
                "reason": "没有可按文字重放的点击",
                "outcome": "fail",
                "closed": True,
                "catalog_written": False,
            }
        )
    stored_steps: list[dict[str, Any]] | None = None
    if _catalog_write_enabled():
        try:
            row = store_steps(_repo_catalog(), title, steps)
        except ValueError as exc:
            runtime.trace.finish(learned=False, reason=str(exc), outcome="fail", extra=detail)
            return _json(
                {
                    "ok": False,
                    "learned": False,
                    "title": title,
                    "reason": str(exc),
                    "outcome": "fail",
                    "closed": True,
                    "catalog_written": False,
                }
            )
        stored_steps = list(row.get("steps") or [])
        detail = {**detail, "catalog_written": True, "steps": stored_steps}
    runtime.trace.finish(
        learned=True,
        outcome="done",
        reason=str(judged.get("reason") or ""),
        extra=detail,
    )
    payload: dict[str, Any] = {
        "ok": True,
        "learned": True,
        "title": title,
        "outcome": "done",
        "button": judged["button"],
        "closed": True,
        "catalog_written": stored_steps is not None,
    }
    if stored_steps is not None:
        payload["steps"] = stored_steps
    return _json(payload)


@mcp.tool(name="replay_row")
async def replay_row(title: str) -> str:
    """Play the stored steps for one row inside this session. One result, no images.

    Does not open a nested ``begin_row``. ``replay_end`` carries ``outcome`` and
    ``fail_kind`` (``script`` / ``env`` / ``timeout`` / ``look``). A reason of
    「任务弹层没开」 or 「看屏失败」 does not increment ``script_fails``.
    """
    name = title.strip()
    if runtime.trace.title:
        return _json(
            {
                "ok": False,
                "reason": f"上一行「{runtime.trace.title}」还没 end_row",
                "open_title": runtime.trace.title,
            }
        )
    catalog = _repo_catalog()
    try:
        row = find_catalog_row(load_catalog(catalog), name)
    except (OSError, json.JSONDecodeError) as exc:
        return _json({"ok": False, "reason": f"读清单失败：{exc}"})
    steps = list((row or {}).get("steps") or [])
    if not steps:
        return _json({"ok": False, "title": name, "reason": "没有步骤"})
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _json({"ok": False, "reason": str(exc)})

    # ``replay_*`` lines make a replay countable. Action notes keep ``replay`` /
    # ``step`` / ``pkg`` and also carry the row title, because replay does not
    # open a nested ``begin_row`` (an open row is refused above).
    def _end(ok: bool, payload: dict[str, Any]) -> str:
        runtime.trace.append(
            {"op": "replay_end", "title": name, "ok": ok, **_pkg_field(), **payload}
        )
        return _json(payload)

    def _fail(
        reason: str,
        *,
        step: int | None = None,
        fail_kind: str = "script",
        **extra: Any,
    ) -> str:
        failure = note_replay_failure(catalog, name, reason=reason)
        body: dict[str, Any] = {
            "ok": False,
            "title": name,
            "reason": reason,
            "outcome": "fail",
            **failure,
            "fail_kind": _replay_fail_kind(reason, fail_kind),
            **extra,
        }
        if step is not None:
            body["step"] = step
        return _end(False, body)

    def _replay_note(event: dict[str, Any]) -> None:
        runtime.trace.note(
            {
                "ok": True,
                "title": name,
                "in_row": True,
                "role": "replay",
                **event,
                **_pkg_field(),
            }
        )

    await _refresh_package(session)
    runtime.trace.append(
        {
            "op": "replay_start",
            "title": name,
            "how": "replay",
            "steps": len(steps),
            "summary": _steps_summary(steps),
            "script_fails": int((row or {}).get("script_fails") or 0),
            **_pkg_field(),
        }
    )
    deadline = time.monotonic() + 60
    for index, step in enumerate(steps, start=1):
        if time.monotonic() > deadline:
            return _fail("超过一分钟", step=index, fail_kind="timeout")
        try:
            obs, _image = await session.look(include_image=False, settle_ms=200)
        except Exception as exc:
            return _fail(str(exc), step=index, fail_kind="look")
        if not (obs.ok and obs.hierarchy_ok):
            return _fail("看屏失败", step=index, fail_kind="look")
        _note_look(session, obs)
        op = step.get("op")
        runtime.trace.append(
            {
                "op": "replay_step",
                "title": name,
                "step": index,
                "step_op": op,
                "expect": _step_expect(step),
                "optional": bool(step.get("optional")),
                **_pkg_field(),
            }
        )
        if op == "tap_text":
            point = find_tap(session.elements, step, session.width, session.height)
            if point is None:
                if step.get("optional"):
                    continue
                return _fail(
                    "屏幕上没有这一步的字",
                    step=index,
                    fail_kind="script",
                    texts=_look_texts(session),
                )
            result = await session.click(point)
            if result.ok:
                _replay_note(
                    {
                        "op": "click",
                        "text": step.get("text") or "",
                        "xy": result.normalized_coordinates,
                        "replay": name,
                        "step": index,
                    }
                )
            elif not step.get("optional"):
                return _fail(result.message, step=index, fail_kind="script")
        elif op == "swipe":
            start = step.get("start")
            end = step.get("end")
            if not (
                isinstance(start, list)
                and len(start) == 2
                and isinstance(end, list)
                and len(end) == 2
            ):
                if step.get("optional"):
                    continue
                return _fail("这一步的 swipe 坐标不完整", step=index, fail_kind="script")
            millis = int(step.get("ms") or 800)
            point_from = [int(start[0]), int(start[1])]
            point_to = [int(end[0]), int(end[1])]
            result = await session.swipe(point_from, point_to, duration_ms=millis)
            if result.ok:
                _replay_note(
                    {
                        "op": "swipe",
                        "start": point_from,
                        "end": point_to,
                        "ms": millis,
                        "replay": name,
                        "step": index,
                    }
                )
            elif not step.get("optional"):
                return _fail(result.message, step=index, fail_kind="script")
        elif op == "wait":
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            millis = min(int(step.get("ms") or 0), max(0, remaining_ms))
            if millis:
                await session.wait_for_delay(millis)
                _replay_note(
                    {"op": "wait", "ms": millis, "replay": name, "step": index}
                )
        elif op == "key":
            result = await session.press_key(str(step.get("key") or "back"))
            if result.ok:
                _replay_note(
                    {
                        "op": "press_key",
                        "key": step.get("key") or "back",
                        "replay": name,
                        "step": index,
                    }
                )
            else:
                return _fail(result.message, step=index, fail_kind="script")
    try:
        obs, _image = await session.look(include_image=False, settle_ms=300)
    except Exception as exc:
        return _fail(str(exc), fail_kind="look")
    if not (obs.ok and obs.hierarchy_ok):
        return _fail("看屏失败", fail_kind="look")
    _note_look(session, obs, settle_ms=300)
    judged = judge_row(session.elements, name)
    if not judged["learned"]:
        hinted = "env" if judged["reason"] == "任务弹层没开" else "script"
        return _fail(
            str(judged["reason"]),
            fail_kind=hinted,
            button=judged["button"],
            texts=_look_texts(session),
        )
    note_replay_success(catalog, name)
    return _end(
        True,
        {
            "ok": True,
            "title": name,
            "button": judged["button"],
            "reason": judged["reason"],
            "outcome": "done",
        },
    )


def _registered_tool_names() -> set[str]:
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if isinstance(tools, dict):
        return set(tools)
    return set()


def main(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8001,
    manage_awake: bool = True,
) -> None:
    """Run the hands MCP server. Stdio mode keeps logs off the JSON-RPC stream.

    ``manage_awake`` is false when the Artemis CLI already started the awake
    service for this process.
    """
    if transport.lower() != "sse":
        from artemis.mcp.adb_server import configure_stdio_mode

        configure_stdio_mode()
    if manage_awake:
        from artemis.runtime import start_awake_service

        start_awake_service()
    try:
        if transport.lower() == "sse":
            mcp.run(transport="sse", host=host, port=port)
        else:
            mcp.run(transport="stdio")
    finally:
        runtime.release()
        if manage_awake:
            from artemis.runtime import shutdown_awake_service

            shutdown_awake_service()


if __name__ == "__main__":
    main()
