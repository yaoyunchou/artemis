"""Run a compiled task-list plan with UI-tree taps. No model calls."""

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
    coin_entry_point,
    dice_left,
    dice_point,
    earn_dice_point,
    find_dismiss,
    find_exact,
    foreground_packages,
    is_home_screen,
    is_locked,
    list_titles,
    on_task_list,
    row_action,
    screen_size,
)


class PhoneLocked(RuntimeError):
    """The lock screen is up. Taps cannot reach 闲鱼 until someone unlocks."""


class Phone:
    """Hierarchy reads and taps for one device. Screenshots stay on the device."""

    def __init__(self, serial: str):
        self.serial = serial
        self._client: Any = None
        self._size: tuple[int, int] | None = None

    def dump(self) -> tuple[str, list[dict[str, Any]]]:
        xml = self._hierarchy() or ""
        elements = parse_ui_hierarchy(xml)
        if is_locked(xml, elements):
            raise PhoneLocked("手机锁屏了，解锁后再点「再次执行」")
        return xml, elements

    def _hierarchy(self) -> str:
        """Prefer the accessibility helper. UIAutomator misses 闲鱼's web task list."""
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


def enter_list(phone: Phone, package: str | None) -> bool:
    """Open the task-reward sheet from the 闲鱼 home badge.

    Stay inside 闲鱼. A missed tap used to press Back, which left the coin
    page and made the run look like it never started.
    """
    launched = False
    entry_taps = 0
    dice_taps = 0
    backs = 0
    for attempt in range(10):
        xml, elements = phone.dump()
        if on_task_list(elements):
            print("已进入任务奖励列表", flush=True)
            return True
        if _is_rules_page(elements):
            print("关掉签到规则页", flush=True)
            phone.back()
            continue
        _log_enter_attempt(attempt, elements)
        dismiss = find_dismiss(elements)
        if dismiss is not None:
            x_pos, y_pos = center(dismiss)
            phone.tap(x_pos, y_pos)
            continue
        packages = foreground_packages(xml)
        on_target = bool(package) and package in packages
        if package and packages and not on_target:
            if launched:
                phone.back()
            else:
                phone.launch(package)
                launched = True
            continue
        dice = earn_dice_point(elements)
        if dice is not None and dice_taps < 3:
            print(f"点赚骰子 ({dice[0]}, {dice[1]})", flush=True)
            phone.tap(dice[0], dice[1])
            dice_taps += 1
            time.sleep(2.0)
            continue
        if is_home_screen(elements) and entry_taps < 2:
            x_pos, y_pos = coin_entry_point(elements)
            print(f"点闲鱼币入口 ({x_pos}, {y_pos})", flush=True)
            phone.tap(x_pos, y_pos)
            entry_taps += 1
            time.sleep(3.0)
            continue
        if backs < 2 and not is_home_screen(elements):
            phone.back()
            backs += 1
            continue
        time.sleep(1.5)
    _xml, elements = phone.dump()
    return on_task_list(elements)


def _is_rules_page(elements: list[dict[str, Any]]) -> bool:
    blob = "\n".join(str(element.get("text") or "") for element in elements)
    return "玩法规则" in blob or "连续签到天数" in blob


def _log_enter_attempt(attempt: int, elements: list[dict[str, Any]]) -> None:
    seen: list[str] = []
    for element in elements:
        text = str(element.get("text") or "").strip()
        if not text:
            continue
        if any(token in text for token in ("任务奖励", "去完成", "领取奖励", "得骰子", "扔骰子", "签到")):
            if text not in seen:
                seen.append(text)
    shown = "、".join(seen[:6]) if seen else "屏幕上还没有任务奖励"
    print(f"还没进列表（第 {attempt + 1} 次）：{shown}", flush=True)


def scan_titles(phone: Phone) -> list[str]:
    for _ in range(6):
        phone.swipe_list(toward_top=True)
    seen: list[str] = []
    stagnant = 0
    for _ in range(6):
        _xml, elements = phone.dump()
        before = len(seen)
        for title in list_titles(elements):
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
    phone: Phone, title: str, labels: tuple[str, ...] | None = None
) -> tuple[str, tuple[int, int]] | None:
    for toward_top, attempts in ((False, 6), (True, 6)):
        for attempt in range(attempts):
            _xml, elements = phone.dump()
            if not on_task_list(elements):
                phone.swipe_list(toward_top=True)
                _xml, elements = phone.dump()
                if not on_task_list(elements) and not return_to_list(phone):
                    return None
                continue
            action = row_action(elements, title, labels)
            if action is not None:
                return action
            if attempt < attempts - 1:
                phone.swipe_list(toward_top=toward_top)
    return None


def open_reward_sheet(phone: Phone) -> bool:
    """Open the reward sheet from the 赚骰子 badge. The 任务奖励 label does not take taps."""
    _xml, elements = phone.dump()
    if on_task_list(elements):
        return True
    dice = earn_dice_point(elements)
    if dice is None:
        return False
    print(f"回到列表，点赚骰子 ({dice[0]}, {dice[1]})", flush=True)
    phone.tap(dice[0], dice[1])
    time.sleep(1.5)
    _xml, elements = phone.dump()
    return on_task_list(elements)


def return_to_list(phone: Phone) -> bool:
    """Get back onto the reward sheet. Stop pressing Back once the coin board shows."""
    backs = 0
    for _ in range(6):
        _xml, elements = phone.dump()
        if on_task_list(elements):
            return True
        if dice_badge_visible(elements):
            if open_reward_sheet(phone):
                return True
            continue
        dismiss = find_dismiss(elements)
        if dismiss is not None:
            x_pos, y_pos = center(dismiss)
            phone.tap(x_pos, y_pos)
            continue
        if is_home_screen(elements) or backs >= 3:
            return enter_list(phone, "com.taobao.idlefish")
        phone.back()
        backs += 1
    _xml, elements = phone.dump()
    return on_task_list(elements)


def dice_badge_visible(elements: list[dict[str, Any]]) -> bool:
    return dice_left(elements) is not None and not on_task_list(elements)


def close_sheet(phone: Phone) -> bool:
    """Drop the reward sheet so the dice button underneath takes taps."""
    _xml, elements = phone.dump()
    if not on_task_list(elements):
        return True
    phone.back()
    _xml, elements = phone.dump()
    if dice_badge_visible(elements):
        return True
    if not on_task_list(elements) and not dice_badge_visible(elements):
        enter_list(phone, "com.taobao.idlefish")
    return False


def claim_ready(phone: Phone, title: str, done_button: str) -> bool:
    action = seek_row(phone, title, (done_button,))
    if action is None:
        return False
    label, (x_pos, y_pos) = action
    if label == done_button:
        phone.tap(x_pos, y_pos)
        return True
    return False


def roll_dice(phone: Phone, limit: int = 20, *, leave_sheet: bool = False) -> int:
    """Roll until the ×N badge reaches 0. Close 限时惊喜; do not play a round.

    The dice button is behind the reward sheet but still in the accessibility
    tree, so a tap while the sheet is up lands on a task row. Close the sheet
    first, and only when ``leave_sheet`` allows it.
    """
    _xml, elements = phone.dump()
    if on_task_list(elements):
        if not leave_sheet or not close_sheet(phone):
            return 0
    rolled = 0
    stuck = 0
    for _ in range(limit * 2):
        _xml, elements = phone.dump()
        if on_task_list(elements):
            break
        surprise = find_exact(elements, "限时惊喜")
        dismiss = find_dismiss(elements)
        if surprise is not None or (dismiss is not None and dice_left(elements) is None):
            if dismiss is None:
                break
            x_pos, y_pos = center(dismiss)
            phone.tap(x_pos, y_pos)
            continue
        left = dice_left(elements)
        point = dice_point(elements)
        if point is None or not left or rolled >= limit:
            break
        phone.tap(point[0], point[1])
        time.sleep(2.5)
        _xml, after = phone.dump()
        if dice_left(after) == left:
            stuck += 1
            if stuck >= 2:
                print(f"骰子点了两次仍是 ×{left}，先停下掷骰子", flush=True)
                break
            continue
        stuck = 0
        rolled += 1
    if rolled:
        print(f"掷骰子 {rolled} 次，剩 ×{dice_left(phone.dump()[1]) or 0}", flush=True)
    open_reward_sheet(phone)
    return rolled


def claim_visible(phone: Phone, done_button: str = "领取奖励") -> int:
    """Tap claim buttons on the current screen so finished rows do not pile at the top."""
    tapped = 0
    previous: tuple[int, int] | None = None
    for _ in range(6):
        _xml, elements = phone.dump()
        if not on_task_list(elements):
            break
        target = find_exact(elements, done_button)
        if target is None:
            break
        point = center(target)
        if point == previous:
            break
        previous = point
        phone.tap(point[0], point[1])
        tapped += 1
    return tapped


def rows_to_start(elements: list[dict[str, Any]]) -> list[str]:
    """Titles on this screen whose row button is 「去完成」."""
    titles: list[str] = []
    for text in list_titles(elements):
        action = row_action(elements, text, ("去完成",))
        if action is not None and action[0] == "去完成" and text not in titles:
            titles.append(text)
    return titles


def perform(phone: Phone, row: dict[str, Any]) -> str | None:
    """Finish one row. ``None`` means the model should take this row."""
    title = str(row["title"])
    kind = str(row["kind"])
    done_button = str(row.get("done_button") or "")
    start_button = str(row.get("start_button") or "")
    if not done_button or not start_button:
        return None
    claim_visible(phone, done_button)
    rolled = roll_dice(phone, leave_sheet=True)
    if rolled:
        print(f"[脚本] 掷骰子 {rolled} 次", flush=True)
    action = seek_row(phone, title, (start_button, done_button))
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
        _wait_for_skip(phone, int(row["seconds"]))
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
    if not return_to_list(phone):
        return None
    claimed = bool(claim_visible(phone, done_button))
    rolled = roll_dice(phone, leave_sheet=True)
    if rolled:
        print(f"[脚本] 做完后掷骰子 {rolled} 次", flush=True)
    if claimed or claim_ready(phone, title, done_button):
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


def _wait_for_skip(phone: Phone, seconds: int) -> None:
    deadline = time.monotonic() + max(seconds, 12)
    while time.monotonic() < deadline:
        _xml, elements = phone.dump()
        if on_task_list(elements):
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
    phone = Phone(serial)
    if not enter_list(phone, plan.get("package")):
        print("脚本未能进入任务奖励列表，这一次改用模型核对名单。", flush=True)
        print("SCRIPT_BLOCKED", flush=True)
        return
    seen = scan_titles(phone)
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
            outcome = perform(phone, row)
        except Exception as exc:
            print(f"[脚本] {title} → 中断 {exc}", flush=True)
            outcome = None
        if outcome is None:
            print(f"[脚本] {title} → 交给模型", flush=True)
            fails.append(index)
            if not return_to_list(phone):
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
