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


def _note_look(session: HandsSession) -> None:
    runtime.trace.note({"op": "look", "texts": _look_texts(session)})


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
async def click(target: int | list[int], times: int = 1, delay_ms: int = 100) -> CallToolResult:
    """Tap an element index from the latest look, or a normalized [x, y] point."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("click", str(exc))
    text = _click_text(session, target)
    try:
        result = await session.click(target, times=times, delay_ms=delay_ms)
    except Exception as exc:
        return _failure("click", f"Error during click: {exc}")
    if result.ok:
        runtime.trace.note(
            {"op": "click", "text": text, "xy": result.normalized_coordinates}
        )
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
        runtime.trace.note({"op": "swipe", "start": start, "end": end})
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
async def press_key(key: str) -> CallToolResult:
    """Press a key such as back, home, enter, or an Android KEYCODE_* name."""
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _failure("press_key", str(exc))
    try:
        result = await session.press_key(key)
    except Exception as exc:
        return _failure("press_key", f"Error during press_key: {exc}")
    if result.ok:
        runtime.trace.note({"op": "press_key", "key": key})
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
        runtime.trace.note({"op": "wait", "ms": time_in_ms})
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
async def begin_row(title: str) -> str:
    """Start learning one task row. Refuses while the previous row is still open."""
    name = title.strip()
    if not name:
        return _json({"ok": False, "reason": "行名是空的"})
    try:
        runtime.trace.begin(name)
    except RowBusy as exc:
        return _json({"ok": False, "reason": str(exc), "open_title": exc.title})
    return _json({"ok": True, "title": name})


@mcp.tool(name="end_row")
async def end_row(abandon: bool = False) -> str:
    """Look once and, if the sheet shows this row is done, store its steps.

    ``abandon`` closes the row without storing steps. Use it only after the
    catalog row has been marked skip. A row that is not done stays open.
    """
    if not runtime.trace.title:
        return _json({"ok": False, "learned": False, "reason": "没有进行中的行"})
    title = runtime.trace.title
    if abandon:
        runtime.trace.abandon()
        return _json({"ok": True, "learned": False, "title": title, "abandon": True})
    try:
        session = runtime.ensure()
    except HandsBindError as exc:
        return _json({"ok": False, "learned": False, "reason": str(exc)})
    try:
        obs, _image = await session.look(include_image=False, settle_ms=300)
    except Exception as exc:
        return _json({"ok": False, "learned": False, "reason": f"看屏失败：{exc}"})
    if not (obs.ok and obs.hierarchy_ok):
        return _json({"ok": False, "learned": False, "reason": "看屏失败"})
    _note_look(session)
    judged = judge_row(session.elements, title)
    if not judged["learned"]:
        return _json({"ok": True, "learned": False, "title": title, "reason": judged["reason"]})
    steps = compress_steps(runtime.trace.events, title)
    if not steps:
        return _json({"ok": True, "learned": False, "title": title, "reason": "没有可按文字重放的点击"})
    try:
        row = store_steps(_repo_catalog(), title, steps)
    except ValueError as exc:
        return _json({"ok": False, "learned": False, "title": title, "reason": str(exc)})
    runtime.trace.finish(learned=True)
    return _json(
        {
            "ok": True,
            "learned": True,
            "title": title,
            "button": judged["button"],
            "steps": row.get("steps") or [],
        }
    )


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
    deadline = time.monotonic() + 60
    for index, step in enumerate(steps, start=1):
        if time.monotonic() > deadline:
            failure = note_replay_failure(catalog, name)
            return _json(
                {"ok": False, "title": name, "step": index, "reason": "超过一分钟", **failure}
            )
        try:
            obs, _image = await session.look(include_image=False, settle_ms=200)
        except Exception as exc:
            failure = note_replay_failure(catalog, name)
            return _json({"ok": False, "title": name, "step": index, "reason": str(exc), **failure})
        if not (obs.ok and obs.hierarchy_ok):
            failure = note_replay_failure(catalog, name)
            return _json({"ok": False, "title": name, "step": index, "reason": "看屏失败", **failure})
        _note_look(session)
        op = step.get("op")
        if op == "tap_text":
            point = find_tap(session.elements, step, session.width, session.height)
            if point is None:
                if step.get("optional"):
                    continue
                failure = note_replay_failure(catalog, name)
                return _json(
                    {
                        "ok": False,
                        "title": name,
                        "step": index,
                        "reason": "屏幕上没有这一步的字",
                        "texts": _look_texts(session),
                        **failure,
                    }
                )
            result = await session.click(point)
            if result.ok:
                runtime.trace.note(
                    {"op": "click", "text": step.get("text") or "", "xy": result.normalized_coordinates}
                )
            elif not step.get("optional"):
                failure = note_replay_failure(catalog, name)
                return _json(
                    {"ok": False, "title": name, "step": index, "reason": result.message, **failure}
                )
        elif op == "wait":
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            millis = min(int(step.get("ms") or 0), max(0, remaining_ms))
            if millis:
                await session.wait_for_delay(millis)
                runtime.trace.note({"op": "wait", "ms": millis})
        elif op == "key":
            result = await session.press_key(str(step.get("key") or "back"))
            if result.ok:
                runtime.trace.note({"op": "press_key", "key": step.get("key") or "back"})
            else:
                failure = note_replay_failure(catalog, name)
                return _json(
                    {"ok": False, "title": name, "step": index, "reason": result.message, **failure}
                )
    try:
        obs, _image = await session.look(include_image=False, settle_ms=300)
    except Exception as exc:
        failure = note_replay_failure(catalog, name)
        return _json({"ok": False, "title": name, "reason": str(exc), **failure})
    if not (obs.ok and obs.hierarchy_ok):
        failure = note_replay_failure(catalog, name)
        return _json({"ok": False, "title": name, "reason": "看屏失败", **failure})
    _note_look(session)
    judged = judge_row(session.elements, name)
    if not judged["learned"]:
        failure = note_replay_failure(catalog, name)
        return _json(
            {
                "ok": False,
                "title": name,
                "reason": judged["reason"],
                "button": judged["button"],
                "texts": _look_texts(session),
                **failure,
            }
        )
    note_replay_success(catalog, name)
    return _json({"ok": True, "title": name, "button": judged["button"], "reason": judged["reason"]})


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
