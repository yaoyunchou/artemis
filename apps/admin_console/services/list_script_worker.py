"""Device access for UI scripts, and the plan runner for the homepage task list.

App-specific rules (how to open a list, where the dice are, which rows are
real) live in ``tasks/scripts/<app>.py`` and are loaded by ``app_script_host``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from artemis.drivers.android.hierarchy import parse_ui_hierarchy
from artemis.runtime.adb_endpoint import adb_command
from artemis.runtime.device_lock import DeviceExecutionLock

from apps.admin_console.services.screen_intersect import title_matches
from apps.admin_console.services.task_script import (
    center,
    find_exact,
    is_locked,
    row_action,
    screen_size,
)


class PhoneLocked(RuntimeError):
    """The lock screen is up. Taps cannot reach the app until someone unlocks."""


class Phone:
    """Hierarchy reads and taps for one device. Screenshots stay on the device."""

    def __init__(self, serial: str):
        self.serial = serial
        self.last_xml = ""
        self.notes: list[tuple[str, str, str]] = []
        self._client: Any = None
        self._size: tuple[int, int] | None = None

    def dump(self) -> tuple[str, list[dict[str, Any]]]:
        xml = self._hierarchy() or ""
        elements = parse_ui_hierarchy(xml)
        self.last_xml = xml
        if is_locked(xml, elements):
            raise PhoneLocked("手机锁屏了，解锁后再点「再次执行」")
        return xml, elements

    def _hierarchy(self) -> str:
        """Prefer the accessibility helper. UIAutomator misses web task lists."""
        from artemis.clients.accessibility_client import AccessibilityClient, HelperUnavailable
        from artemis.clients.ui_automator_client import UIAutomatorClient

        if self._client is None:
            try:
                helper = AccessibilityClient(self.serial)
                helper.get_hierarchy()
                self._client = helper
            except (HelperUnavailable, OSError, ValueError):
                self._client = UIAutomatorClient(self.serial)
        try:
            return self._client.get_hierarchy() or ""
        except (OSError, ValueError):
            if not isinstance(self._client, UIAutomatorClient):
                self._client = UIAutomatorClient(self.serial)
                return self._client.get_hierarchy() or ""
            return ""

    def note(self, tag: str, text: str = "") -> None:
        """Let an App script flag a problem. The runner saves the screen for the maintainer."""
        print(f"[脚本提示] {tag} {text}".rstrip(), flush=True)
        self.notes.append((tag, text, self.last_xml))

    def tap(self, x_pos: int, y_pos: int) -> None:
        self._shell("input", "tap", str(x_pos), str(y_pos))
        time.sleep(1.0)

    def keep_awake(self) -> None:
        """Keep the screen on while USB is plugged in, so a long run does not hit the PIN screen."""
        self._shell("svc", "power", "stayon", "usb")
        self._shell("input", "keyevent", "KEYCODE_WAKEUP")

    def back(self) -> None:
        self._shell("input", "keyevent", "4")
        time.sleep(0.8)

    def launch(self, package: str) -> None:
        self._shell(
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )
        time.sleep(3.0)

    def swipe_list(self, *, toward_top: bool) -> None:
        if self._size is None:
            _xml, elements = self.dump()
            self._size = screen_size(elements)
        width, height = self._size
        x_pos = width // 2
        start_y = int(height * (0.30 if toward_top else 0.78))
        end_y = int(height * (0.78 if toward_top else 0.30))
        self._shell(
            "input",
            "swipe",
            str(x_pos),
            str(start_y),
            str(x_pos),
            str(end_y),
            "400",
        )
        time.sleep(0.8)

    def _shell(self, *args: str) -> None:
        subprocess.run(
            adb_command(["-s", self.serial, "shell", *args]),
            check=False,
            timeout=20,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def scan_titles(phone: Phone, script: Any) -> list[str]:
    for _ in range(6):
        phone.swipe_list(toward_top=True)
    seen: list[str] = []
    stagnant = 0
    for _ in range(6):
        _xml, elements = phone.dump()
        before = len(seen)
        for title in script.row_titles(elements):
            if title not in seen:
                seen.append(title)
        if len(seen) == before:
            stagnant += 1
            if stagnant >= 2:
                break
        else:
            stagnant = 0
        phone.swipe_list(toward_top=False)
    for _ in range(6):
        phone.swipe_list(toward_top=True)
    return seen


def seek_row(
    phone: Phone, script: Any, title: str, labels: tuple[str, ...] | None = None
) -> tuple[str, tuple[int, int]] | None:
    for toward_top, attempts in ((False, 4), (True, 4)):
        for attempt in range(attempts):
            _xml, elements = phone.dump()
            if not script.on_task_list(elements):
                if not script.return_to_list(phone):
                    return None
                continue
            action = row_action(elements, title, labels)
            if action is not None:
                return action
            if attempt < attempts - 1:
                phone.swipe_list(toward_top=toward_top)
    return None


def claim_ready(phone: Phone, script: Any, title: str, done_button: str) -> bool:
    action = seek_row(phone, script, title, (done_button,))
    if action is None:
        return False
    label, (x_pos, y_pos) = action
    if label == done_button:
        phone.tap(x_pos, y_pos)
        return True
    return False


def perform(phone: Phone, script: Any, row: dict[str, Any]) -> str | None:
    """Finish one row. ``None`` means the model should take this row."""
    title = str(row["title"])
    kind = str(row["kind"])
    done_button = str(row.get("done_button") or "")
    start_button = str(row.get("start_button") or "")
    if not done_button or not start_button:
        return None
    script.claim_visible(phone)
    script.roll_dice(phone, leave_sheet=True)
    action = seek_row(phone, script, title, (start_button, done_button))
    if action is None:
        return None
    label, (x_pos, y_pos) = action
    if label == done_button or kind in ("claim_only", "skip"):
        if label == done_button:
            phone.tap(x_pos, y_pos)
            return "已领取"
        return "跳过"
    phone.tap(x_pos, y_pos)
    if kind == "video":
        _wait_for_skip(phone, script, int(row["seconds"]))
    elif kind == "sign":
        time.sleep(2.0)
        _xml, elements = phone.dump()
        sign = find_exact(elements, "签到")
        if sign is not None:
            sx, sy = center(sign)
            phone.tap(sx, sy)
            time.sleep(1.0)
    else:
        _wait_for_done(phone, int(row["seconds"]))
    if not script.return_to_list(phone):
        return None
    claimed = bool(script.claim_visible(phone))
    script.roll_dice(phone, leave_sheet=True)
    if claimed or claim_ready(phone, script, title, done_button):
        return "已领取"
    return None


def _wait_for_done(phone: Phone, seconds: int) -> None:
    """Stay on the page until the dwell time ends or the page says the task is done."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _xml, elements = phone.dump()
        if any("任务完成" in str(element.get("text") or "") for element in elements):
            return
        time.sleep(2.0)


def _wait_for_skip(phone: Phone, script: Any, seconds: int) -> None:
    deadline = time.monotonic() + max(seconds, 12)
    while time.monotonic() < deadline:
        _xml, elements = phone.dump()
        if script.on_task_list(elements):
            return
        target = find_exact(elements, "关闭广告", "残忍离开", "跳过")
        if target is None and any("放弃" in str(element.get("text") or "") for element in elements):
            target = find_exact(elements, "关闭广告", "关闭")
        if target is not None:
            x_pos, y_pos = center(target)
            phone.tap(x_pos, y_pos)
            return
        time.sleep(2.0)


def execute(plan: dict[str, Any], serial: str) -> None:
    from apps.admin_console.services.app_script_host import load_app_script

    phone = Phone(serial)
    script = load_app_script(str(plan.get("package") or "com.taobao.idlefish"))
    if not script.enter_list(phone):
        print("脚本未能进入任务奖励列表，这一次改用模型核对名单。", flush=True)
        print("SCRIPT_BLOCKED", flush=True)
        return
    seen = scan_titles(phone, script)
    print("VISIBLE: " + " | ".join(seen), flush=True)
    fails: list[int] = []
    for row in plan.get("rows") or []:
        title = str(row.get("title") or "")
        index = int(row.get("index") or 0)
        if not title_matches(title, seen):
            print(f"[脚本] {title} → 未找到", flush=True)
            continue
        if row.get("kind") == "model":
            print(f"[脚本] {title} → 交给模型", flush=True)
            fails.append(index)
            continue
        try:
            outcome = perform(phone, script, row)
        except PhoneLocked:
            raise
        except Exception as exc:
            print(f"[脚本] {title} → 中断 {exc}", flush=True)
            outcome = None
        if outcome is None:
            print(f"[脚本] {title} → 交给模型", flush=True)
            fails.append(index)
            if not script.return_to_list(phone):
                for rest in (plan.get("rows") or [])[index + 1 :]:
                    rest_index = int(rest.get("index") or 0)
                    if rest_index not in fails:
                        fails.append(rest_index)
                break
            continue
        print(f"[脚本] {title} → {outcome}", flush=True)
    print("SCRIPT_RAN", flush=True)
    for index in fails:
        print(f"SCRIPT_FAIL: {index}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a task list from the UI tree.")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--device-serial", default="")
    args = parser.parse_args(argv)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    serial = (args.device_serial or "").strip()
    if not serial:
        print("脚本没有设备串号。", flush=True)
        print("SCRIPT_BLOCKED", flush=True)
        return 0
    lock = DeviceExecutionLock(
        serial,
        description="list script",
        session_id=None,
    )
    try:
        lock.acquire(timeout=60)
        execute(plan, serial)
    except Exception as exc:
        print(f"脚本中断：{exc}", flush=True)
        print("SCRIPT_BLOCKED", flush=True)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
