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

mcp = FastMCP("artemis_hands")


class HandsBindError(RuntimeError):
    """The requested device cannot be listed or locked."""


class HandsRuntime:
    """Process-wide binding of one device to a :class:`HandsSession`."""

    def __init__(self) -> None:
        self.session: HandsSession | None = None
        self.lock: DeviceExecutionLock | None = None
        self.serial: str | None = None
        self.trace = HandsTrace(Path(__file__).resolve().parents[2])

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
        self.trace.device = chosen
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


def _in_row_field() -> dict[str, Any]:
    title = runtime.trace.title
    if not title:
        return {"in_row": False}
    return {"in_row": True, "title": title}


def _role_field(*, text: str = "", role: str = "", key: str = "") -> dict[str, Any]:
    chosen = role.strip()
    if not chosen and (text or key):
        chosen = infer_role(text, key or None)
    if chosen:
        return {"role": chosen}
    if runtime.trace.title:
        return {"role": "task"}
    return {}


def _trace_done(event: dict[str, Any], *, text: str = "", role: str = "", key: str = "") -> None:
    runtime.trace.note(
        {
            "ok": True,
            **event,
            **_in_row_field(),
            **_role_field(text=text, role=role, key=key),
        }
    )


def _trace_missed(
    op: str,
    *,
    text: str = "",
    role: str = "",
    key: str = "",
    **extra: Any,
) -> None:
    runtime.trace.append(
        {
            "op": f"{op}_failed",
            "ok": False,
            **_in_row_field(),
            **_role_field(text=text, role=role, key=key),
            **extra,
        }
    )


def _note_look(session: HandsSession) -> None:
    _trace_done({"op": "look", "texts": _look_texts(session)})


def _look_text(obs) -> str:
    if not obs.ok:
        return obs.message or "Failed to capture the screen."
    text = (obs.elements_text or "").strip()
    if text:
        return text
    return (
        "No indexed elements on this screen. Decide from the screenshot and pass"
        " normalized [x, y] coordinates (0-1000)."
    )


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
async def look(include_image: bool = True, settle_ms: int = 400) -> CallToolResult:
    """Capture the current screen for the agent to decide the next action.

    Returns the numbered element list as text and, by default, the screenshot
    as an image. Element indexes start at 1 and stay valid until the next
    successful look. This server does not interpret the screen.
    """
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("look", str(exc))
    try:
        obs, image = await session.look(include_image=include_image, settle_ms=settle_ms)
    except Exception as exc:
        return _failure("look", f"Error during look: {exc}")
    if obs.ok and obs.hierarchy_ok:
        _note_look(session)
    content: list[Any] = [TextContent(type="text", text=_look_text(obs))]
    if include_image and image:
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(image).decode("utf-8"),
                mimeType="image/jpeg",
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
    role: str = "",
) -> CallToolResult:
    """Tap an element index from the latest look, or a normalized [x, y] point."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("click", str(exc))
    text = _click_text(session, target)
    try:
        result = await session.click(target, times=times, delay_ms=delay_ms)
    except Exception as exc:
        _trace_missed("click", text=text, role=role)
        return _failure("click", f"Error during click: {exc}")
    if result.ok:
        _trace_done(
            {"op": "click", "text": text, "xy": result.normalized_coordinates},
            text=text,
            role=role,
        )
    else:
        _trace_missed("click", text=text, role=role, reason=result.message)
    return _ok_result(result)


@mcp.tool(name="long_press")
async def long_press(target: int | list[int], duration_ms: int = 1000) -> CallToolResult:
    """Long-press an element index from the latest look, or a normalized [x, y] point."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("long_press", str(exc))
    try:
        return _ok_result(await session.long_press(target, duration_ms=duration_ms))
    except Exception as exc:
        return _failure("long_press", f"Error during long_press: {exc}")


@mcp.tool(name="swipe")
async def swipe(start: list[int], end: list[int], duration_ms: int = 800) -> CallToolResult:
    """Swipe between two normalized [x, y] points (0-1000)."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("swipe", str(exc))
    try:
        result = await session.swipe(start, end, duration_ms=duration_ms)
    except Exception as exc:
        return _failure("swipe", f"Error during swipe: {exc}")
    if result.ok:
        _trace_done({"op": "swipe", "start": start, "end": end})
    else:
        _trace_missed("swipe", start=start, end=end)
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
    try:
        return _ok_result(await session.input_text(text, target=target, clear_exist=clear_exist))
    except Exception as exc:
        return _failure("input_text", f"Error during input_text: {exc}")


@mcp.tool(name="press_key")
async def press_key(key: str, role: str = "") -> CallToolResult:
    """Press a key such as back, home, enter, or an Android KEYCODE_* name."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("press_key", str(exc))
    try:
        result = await session.press_key(key)
    except Exception as exc:
        _trace_missed("press_key", key=key, role=role)
        return _failure("press_key", f"Error during press_key: {exc}")
    if result.ok:
        _trace_done({"op": "press_key", "key": key}, role=role, key=key)
    else:
        _trace_missed("press_key", key=key, role=role, reason=result.message)
    return _ok_result(result)


@mcp.tool(name="manage_app")
async def manage_app(action: str, app_name: str) -> CallToolResult:
    """Launch or stop an app by name or package. action is 'launch' or 'stop'."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("manage_app", str(exc))
    try:
        return _ok_result(await session.manage_app(action, app_name))
    except Exception as exc:
        return _failure("manage_app", f"Error during manage_app: {exc}")


@mcp.tool(name="open_link")
async def open_link(url: str) -> CallToolResult:
    """Open a URL or deep link on the device."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("open_link", str(exc))
    try:
        return _ok_result(await session.open_link(url))
    except Exception as exc:
        return _failure("open_link", f"Error during open_link: {exc}")


@mcp.tool(name="erase_one_char")
async def erase_one_char() -> CallToolResult:
    """Erase one character in the focused field."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("erase_one_char", str(exc))
    try:
        return _ok_result(await session.erase_one_char())
    except Exception as exc:
        return _failure("erase_one_char", f"Error during erase_one_char: {exc}")


@mcp.tool(name="focus_and_clear_text")
async def focus_and_clear_text(target: int | list[int]) -> CallToolResult:
    """Focus an element index or normalized [x, y] and clear its text."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("focus_and_clear_text", str(exc))
    try:
        return _ok_result(await session.focus_and_clear_text(target))
    except Exception as exc:
        return _failure("focus_and_clear_text", f"Error during focus_and_clear_text: {exc}")


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
        _trace_done({"op": "wait", "ms": time_in_ms})
    else:
        _trace_missed("wait", ms=time_in_ms)
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
        return _ok_result(
            await session.wait_for_text(text, wait_state=wait_state, timeout_ms=timeout_ms)
        )
    except Exception as exc:
        return _failure("wait_for_text", f"Error during wait_for_text: {exc}")


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
        return _ok_result(await session.click_sequence(sequence, delay_ms=delay_ms))
    except Exception as exc:
        return _failure("click_sequence", f"Error during click_sequence: {exc}")


def _repo_catalog() -> Path:
    return catalog_path(Path(__file__).resolve().parents[2])


@mcp.tool(name="begin_row")
async def begin_row(title: str, how: str = "") -> str:
    """Open a task-row span in the hands log. Refuses while another row is still open."""
    name = title.strip()
    if not name:
        return _json({"ok": False, "reason": "行名是空的"})
    extra: dict[str, Any] | None = None
    how_name = how.strip()
    if how_name:
        extra = {"how": how_name}
    try:
        runtime.trace.begin(name, extra=extra)
    except RowBusy as exc:
        return _json({"ok": False, "reason": str(exc), "open_title": exc.title})
    payload: dict[str, Any] = {"ok": True, "title": name}
    if how_name:
        payload["how"] = how_name
    return _json(payload)


@mcp.tool(name="close_row")
async def close_row(outcome: str, reason: str = "", abandon: bool = False) -> str:
    """Close the open row span without looking at the screen or writing the catalog."""
    if not runtime.trace.title:
        return _json({"ok": False, "reason": "没有进行中的行"})
    title = runtime.trace.title
    outcome_name = outcome.strip() or ("skip" if abandon else "fail")
    if abandon:
        runtime.trace.abandon(outcome=outcome_name, reason=reason.strip())
    else:
        learned = outcome_name == "done"
        runtime.trace.close_span(
            learned=learned,
            outcome=outcome_name,
            reason=reason.strip(),
        )
    return _json(
        {
            "ok": True,
            "title": title,
            "outcome": outcome_name,
            "learned": outcome_name == "done",
            "reason": reason.strip(),
            "abandon": abandon,
        }
    )


@mcp.tool(name="end_row")
async def end_row(abandon: bool = False) -> str:
    """Look once and log whether this row looks done. Does not write steps to the catalog by default.

    ``learned`` only means the sheet looked finished, not that steps were stored.
    Set ``ARTEMIS_HANDS_END_ROW_WRITE=1`` to restore writing steps into the catalog.
    ``abandon`` closes the row as skipped. A row that is not done stays open.
    Prefer ``close_row`` when the caller already knows the outcome.
    """
    if not runtime.trace.title:
        return _json({"ok": False, "learned": False, "reason": "没有进行中的行", "outcome": "fail"})
    title = runtime.trace.title
    if abandon:
        runtime.trace.abandon(outcome="skip")
        return _json(
            {
                "ok": True,
                "learned": False,
                "title": title,
                "outcome": "skip",
                "abandon": True,
            }
        )
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _json({"ok": False, "learned": False, "reason": str(exc), "outcome": "fail"})
    try:
        obs, _image = await session.look(include_image=False, settle_ms=300)
    except Exception as exc:
        return _json({"ok": False, "learned": False, "reason": f"看屏失败：{exc}", "outcome": "fail"})
    if not (obs.ok and obs.hierarchy_ok):
        return _json({"ok": False, "learned": False, "reason": "看屏失败", "outcome": "fail"})
    _note_look(session)
    judged = judge_row(session.elements, title)
    if not judged["learned"]:
        return _json(
            {
                "ok": True,
                "learned": False,
                "title": title,
                "reason": judged["reason"],
                "outcome": "fail",
                "button": judged.get("button"),
            }
        )
    steps = compress_steps(runtime.trace.events, title)
    if not steps:
        return _json(
            {
                "ok": True,
                "learned": False,
                "title": title,
                "reason": "没有可按文字重放的点击",
                "outcome": "fail",
            }
        )
    write_catalog = os.environ.get("ARTEMIS_HANDS_END_ROW_WRITE") == "1"
    stored_steps: list[dict[str, Any]] | None = None
    if write_catalog:
        try:
            row = store_steps(_repo_catalog(), title, steps)
        except ValueError as exc:
            return _json({"ok": False, "learned": False, "title": title, "reason": str(exc), "outcome": "fail"})
        stored_steps = list(row.get("steps") or [])
    runtime.trace.finish(learned=True, outcome="done", reason=str(judged.get("reason") or ""))
    payload: dict[str, Any] = {
        "ok": True,
        "learned": True,
        "title": title,
        "outcome": "done",
        "button": judged["button"],
        "catalog_written": write_catalog,
    }
    if stored_steps is not None:
        payload["steps"] = stored_steps
    return _json(payload)


@mcp.tool(name="replay_row")
async def replay_row(title: str) -> str:
    """Play the stored steps for one row inside this session. One result, no images."""
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

    def _replay_note(event: dict[str, Any]) -> None:
        runtime.trace.note(
            {
                "ok": True,
                **event,
                "title": name,
                "in_row": True,
                "role": "replay",
            }
        )

    def _replay_fail(
        reason: str,
        *,
        step: int | None = None,
        fail_kind: str = "script",
        **extra: Any,
    ) -> str:
        failure = note_replay_failure(catalog, name, reason=reason)
        runtime.trace.append(
            {
                "op": "replay_end",
                "title": name,
                "ok": False,
                "outcome": "fail",
                "reason": reason,
                "fail_kind": fail_kind if not failure.get("env_failure") else "env",
            }
        )
        payload: dict[str, Any] = {
            "ok": False,
            "title": name,
            "reason": reason,
            "outcome": "fail",
            **failure,
            **extra,
        }
        if step is not None:
            payload["step"] = step
        return _json(payload)

    runtime.trace.append({"op": "replay_start", "title": name, "how": "replay"})
    deadline = time.monotonic() + 60
    for index, step in enumerate(steps, start=1):
        if time.monotonic() > deadline:
            return _replay_fail("超过一分钟", step=index, fail_kind="timeout")
        try:
            obs, _image = await session.look(include_image=False, settle_ms=200)
        except Exception as exc:
            return _replay_fail(str(exc), step=index, fail_kind="look")
        if not (obs.ok and obs.hierarchy_ok):
            return _replay_fail("看屏失败", step=index, fail_kind="look")
        _note_look(session)
        op = step.get("op")
        if op == "tap_text":
            point = find_tap(session.elements, step, session.width, session.height)
            if point is None:
                if step.get("optional"):
                    continue
                return _replay_fail(
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
                    }
                )
            elif not step.get("optional"):
                return _replay_fail(result.message, step=index, fail_kind="script")
        elif op == "wait":
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            millis = min(int(step.get("ms") or 0), max(0, remaining_ms))
            if millis:
                await session.wait_for_delay(millis)
                _replay_note({"op": "wait", "ms": millis})
        elif op == "key":
            result = await session.press_key(str(step.get("key") or "back"))
            if result.ok:
                _replay_note({"op": "press_key", "key": step.get("key") or "back"})
            else:
                return _replay_fail(result.message, step=index, fail_kind="script")
    try:
        obs, _image = await session.look(include_image=False, settle_ms=300)
    except Exception as exc:
        return _replay_fail(str(exc), fail_kind="look")
    if not (obs.ok and obs.hierarchy_ok):
        return _replay_fail("看屏失败", fail_kind="look")
    _note_look(session)
    judged = judge_row(session.elements, name)
    if not judged["learned"]:
        fail_kind = "env" if judged["reason"] == "任务弹层没开" else "script"
        failure = note_replay_failure(catalog, name, reason=str(judged["reason"]))
        runtime.trace.append(
            {
                "op": "replay_end",
                "title": name,
                "ok": False,
                "outcome": "fail",
                "reason": judged["reason"],
                "fail_kind": fail_kind,
            }
        )
        return _json(
            {
                "ok": False,
                "title": name,
                "reason": judged["reason"],
                "button": judged["button"],
                "texts": _look_texts(session),
                "outcome": "fail",
                **failure,
            }
        )
    note_replay_success(catalog, name)
    runtime.trace.append(
        {
            "op": "replay_end",
            "title": name,
            "ok": True,
            "outcome": "done",
            "button": judged["button"],
            "reason": judged["reason"],
        }
    )
    return _json(
        {
            "ok": True,
            "title": name,
            "button": judged["button"],
            "reason": judged["reason"],
            "outcome": "done",
        }
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
