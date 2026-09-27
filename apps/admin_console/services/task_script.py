"""Turn repeated list chores into taps on the UI tree.

Claim, wait, and back do not need a model. The model is reserved for rows
the script cannot finish, and for the case where the task list never appears.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from apps.admin_console.services.screen_intersect import title_matches

SCRIPT_GOAL = "用界面脚本执行任务列表。领取、停留和返回不调用模型。"
_QUOTED = re.compile(r"「([^」]{2,40})」")
_PACKAGE = re.compile(r"com\.[a-zA-Z0-9_.]+")
_SECONDS = re.compile(r"(\d+)\s*秒")
_FAIL_LINE = re.compile(r"^SCRIPT_FAIL:\s*(\d+)\s*$", re.MULTILINE)
_BUTTONS = ("领取奖励", "去完成", "已完成")
_DISMISS = ("关闭广告", "残忍离开", "关闭")
_SKIP_TITLES = {
    "领取奖励",
    "去完成",
    "已完成",
    "任务奖励",
    "关闭",
    "关闭广告",
    "残忍离开",
    "跳过",
    "签到",
    "继续玩",
    "开始游戏",
}
_NOT_A_TITLE = _SKIP_TITLES | {"立即领取", "去赚", "查看详情"}


@dataclass(frozen=True)
class Submission:
    """What the queue should run first for a batch of goals."""

    goals: list[str]
    followups: list[str] | None = None
    plan: dict[str, Any] | None = None


def prepare_submission(
    goals: list[str],
    *,
    screen_intersect: bool,
    allow_list_script: bool,
) -> Submission:
    """Prefer one UI script over a model scout when several rows are scriptable."""
    if screen_intersect and len(goals) >= 2:
        if allow_list_script:
            plan = compile_plan(goals)
            if plan is not None:
                return Submission([SCRIPT_GOAL], None, plan)
        from apps.admin_console.services.screen_intersect import (
            build_scout_goal,
            title_from_goal,
        )

        titles = [title_from_goal(goal) for goal in goals]
        return Submission([build_scout_goal(titles)], list(goals), None)
    return Submission(list(goals), None, None)


def compile_plan(goals: list[str]) -> dict[str, Any] | None:
    """A device plan when at least two rows have a fixed claim/wait/skip shape."""
    rows: list[dict[str, Any]] = []
    blob = "\n".join(goals)
    package_match = _PACKAGE.search(blob)
    package = package_match.group(0) if package_match else None
    if package is None and "闲鱼" in blob:
        package = "com.taobao.idlefish"
    for goal in goals:
        title, detail = split_row(goal)
        if not title:
            continue
        from apps.admin_console.services.prompt_recipe import compile_recipe

        recipe = compile_recipe(title, detail, source=goal)
        kind = recipe["kind"] if recipe else "model"
        seconds = recipe["seconds"] if recipe else wait_seconds(f"{title}\n{detail}")
        rows.append(
            {
                "index": len(rows),
                "title": title,
                "kind": kind,
                "seconds": seconds,
                "goal": goal,
                "fallback": short_goal(title, detail, kind if kind != "model" else classify(title, detail), seconds),
                "start_button": recipe["start_button"] if recipe else None,
                "done_button": recipe["done_button"] if recipe else None,
            }
        )
    scriptable = sum(1 for row in rows if row["kind"] != "model")
    if scriptable < 2:
        return None
    return {"package": package, "rows": rows}


def split_row(goal: str) -> tuple[str, str]:
    """Title plus the row-only instructions, without the shared preamble."""
    lines = [line.strip() for line in goal.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.startswith("只执行下面这一条") and index + 1 < len(lines):
            raw = lines[index + 1]
            title, _, rest = raw.partition("。")
            parts = [rest.strip()] if rest.strip() else []
            parts.extend(lines[index + 2 :])
            return title.strip(), "\n".join(part for part in parts if part).strip()
    for match in _QUOTED.finditer(goal):
        name = match.group(1).strip()
        if name and name not in _NOT_A_TITLE and "领取" not in name:
            return name, goal
    from apps.admin_console.services.screen_intersect import title_from_goal

    return title_from_goal(goal), goal


def classify(title: str, detail: str) -> str:
    """Fixed row shape. Shared preamble must not be included in ``detail``."""
    text = f"{title}\n{detail}"
    if any(token in text for token in ("直接跳过", "是去完成则跳过", "是去完成就直接结束")):
        if "不要打开发布" in text or "不要打开草稿" in text or "才点" in text:
            return "claim_only"
        return "skip"
    if "只有按钮已经是" in text and "领取" in text:
        return "claim_only"
    if any(token in text for token in ("关闭广告", "看视频", "「跳过」", "出现跳过", "等到出现")):
        return "video"
    if "只点签到" in text or "只点「签到」" in text or "明确的「签到」" in text:
        return "sign"
    if any(token in text for token in ("停留", "秒", "上滑", "浏览", "逛一逛", "互动")):
        return "browse"
    return "model"


def wait_seconds(text: str) -> int:
    match = _SECONDS.search(text)
    seconds = int(match.group(1)) if match else 20
    return max(8, min(seconds, 25))


def short_goal(title: str, detail: str, kind: str, seconds: int) -> str:
    """A short model prompt for the one row a script could not finish."""
    if kind == "model":
        instruction = detail.strip() or "按这一行原来的要求做完后结束。"
    elif kind == "video":
        instruction = (
            "是去完成就等到出现跳过或关闭再点。"
            "弹出放弃奖励时点关闭广告，然后返回并领取。"
        )
    elif kind == "sign":
        instruction = "只点签到，然后返回。已是领取奖励就点一次。"
    elif kind in ("claim_only", "skip"):
        instruction = "只有按钮已经是领取奖励才点一次。是去完成就直接结束，不要进入活动。"
    else:
        instruction = (
            f"是去完成就进入后停留 {seconds} 秒，不要点商品，然后返回列表；"
            "返回后若变成领取奖励再点一次。"
        )
    return (
        f"当前应在任务奖励列表。只处理「{title}」。{instruction}"
        "没有这一行就结束。不要做其他行，不要重新打开应用。"
    )


def parse_script_log(text: str) -> tuple[str, list[int]]:
    """``ran`` with fail indexes, or ``blocked`` when the list was never reached."""
    if "SCRIPT_RAN" in text:
        fails = [int(match.group(1)) for match in _FAIL_LINE.finditer(text)]
        return "ran", fails
    return "blocked", []


def foreground_packages(xml: str) -> set[str]:
    return set(re.findall(r'package="([^"]+)"', xml or ""))


def on_task_list(elements: list[dict[str, Any]]) -> bool:
    """True once the reward sheet is up, even if the tab label is already gone.

    Opening the orange badge often lands directly on 「得骰子赚闲鱼币」. That
    sheet is the list: it has 「去完成」 / 「领取奖励」 and does not always keep
    a separate 「任务奖励」 tab on screen.
    """
    texts = [_text(element) for element in elements]
    has_action = any(text in ("领取奖励", "去完成", "已完成") for text in texts)
    has_sheet = any(
        any(token in text for token in ("任务奖励", "得骰子赚闲鱼币", "累积任务奖励"))
        for text in texts
    )
    return has_action and has_sheet


def coin_entry_point(elements: list[dict[str, Any]]) -> tuple[int, int]:
    """Where to tap the orange 领 badge on the 闲鱼 home header.

    The badge is a drawing above the search button and often has no text.
    Past runs opened it at about 92.5% of the width and 5.5% of the height,
    which sits just above ``search_btn``.
    """
    badge = find_badge(elements)
    if badge is not None:
        return center(badge)
    width, height = screen_size(elements)
    for element in elements:
        resource_id = str(element.get("resource_id") or "")
        if resource_id.endswith("search_btn"):
            bounds = element.get("bounds") or [0, 0, 0, 0]
            button_height = max(1, int(bounds[3]) - int(bounds[1]))
            return int(bounds[2]) - button_height // 2, max(0, int(bounds[1]) - button_height)
    return int(width * 0.925), int(height * 0.055)


_DICE_BADGE = re.compile(r"^[×xX✕](\d+)$")


def dice_badge(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The ×N badge on the dice. The dice itself is an image with no text."""
    for element in elements:
        if _DICE_BADGE.match(_text(element).replace(" ", "")):
            return element
    return None


def dice_left(elements: list[dict[str, Any]]) -> int | None:
    badge = dice_badge(elements)
    if badge is None:
        return None
    match = _DICE_BADGE.match(_text(badge).replace(" ", ""))
    return int(match.group(1)) if match else None


def dice_point(elements: list[dict[str, Any]]) -> tuple[int, int] | None:
    """Center of the 扔骰子寻宝 button, which sits under its ×N badge."""
    badge = dice_badge(elements)
    if badge is None:
        return None
    width, _height = screen_size(elements)
    bounds = badge.get("bounds") or [0, 0, 0, 0]
    return width // 2, int(bounds[3]) + 80


def earn_dice_point(elements: list[dict[str, Any]]) -> tuple[int, int] | None:
    """The 赚骰子 icon, which opens the reward sheet.

    The 任务奖励 label is visible to accessibility, but injected taps do not
    activate that web button. 赚骰子 does. Its 领 badge only shows while
    something is claimable, so fall back to its place beside the dice badge.
    """
    width, height = screen_size(elements)
    best: tuple[int, int] | None = None
    best_area = 10**12
    for element in elements:
        if _text(element) != "领":
            continue
        x_pos, y_pos = center(element)
        if y_pos < height * 0.35 or y_pos > height * 0.58 or x_pos < width * 0.55:
            continue
        area = _area(element)
        if area < best_area:
            best = (x_pos, y_pos)
            best_area = area
    if best is not None:
        return best
    badge = dice_badge(elements)
    if badge is None:
        return None
    x_pos, y_pos = center(badge)
    return x_pos + 199, y_pos + 110


def is_home_screen(elements: list[dict[str, Any]]) -> bool:
    return any(str(element.get("resource_id") or "").endswith("search_btn") for element in elements)


def is_locked(xml: str, elements: list[dict[str, Any]]) -> bool:
    packages = foreground_packages(xml)
    if packages and packages != {"com.android.systemui"}:
        return False
    return any("解锁" in _text(element) or "PIN 码" in _text(element) for element in elements)


_CLASS_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _covers_screen(element: dict[str, Any], width: int, height: int) -> bool:
    bounds = element.get("bounds") or []
    if len(bounds) != 4:
        return False
    box_width = int(bounds[2]) - int(bounds[0])
    box_height = int(bounds[3]) - int(bounds[1])
    return box_width >= width * 0.85 and box_height >= height * 0.7


_NOT_A_ROW = ("闲鱼币兑曝光", "闲鱼币抵扣", "现金夺宝", "急速卖", "玩法规则", "签到玩法")
_GAME_ROW = ("玩1关", "小游戏", "合成", "升级火炉", "消不停", "还想消")


def is_game_row(title: str) -> bool:
    """Rows that need a round of a mini game. The prompt says to skip those."""
    return any(token in title for token in _GAME_ROW)


def is_row_title(element: dict[str, Any], width: int, height: int) -> bool:
    """A task-list row, not the web view, the shop block, or a rules page."""
    text = _text(element)
    if len(text) < 4 or len(text) > 40 or text in _SKIP_TITLES:
        return False
    if any(token in text for token in _NOT_A_ROW):
        return False
    if "WebView" in text or "webview" in text or _CLASS_NAME.match(text):
        return False
    if _covers_screen(element, width, height):
        return False
    return True


def list_titles(elements: list[dict[str, Any]]) -> list[str]:
    width, height = screen_size(elements)
    titles: list[str] = []
    for element in elements:
        text = _text(element)
        if not is_row_title(element, width, height):
            continue
        if text not in titles:
            titles.append(text)
    return titles


def find_exact(elements: list[dict[str, Any]], *labels: str) -> dict[str, Any] | None:
    for label in labels:
        for element in elements:
            if _text(element) == label:
                return element
    return None


def find_dismiss(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    return find_exact(elements, *_DISMISS)


def find_label(elements: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    matches = [element for element in elements if label in _text(element)]
    if not matches:
        return None
    return min(matches, key=_area)


def find_badge(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Top-right 领 / 签到 icon. Ignores the task rows lower on the page."""
    width, height = screen_size(elements)
    best: dict[str, Any] | None = None
    best_area = 10**12
    for element in elements:
        text = _text(element)
        if "领" not in text and "签到" not in text:
            continue
        if "任务奖励" in text or "领取奖励" in text:
            continue
        x_pos, y_pos = center(element)
        if y_pos > height * 0.22 or x_pos < width * 0.62:
            continue
        area = _area(element)
        if area < best_area:
            best = element
            best_area = area
    return best


def row_action(
    elements: list[dict[str, Any]],
    title: str,
    labels: tuple[str, ...] | None = None,
) -> tuple[str, tuple[int, int]] | None:
    """The button sitting on the same row as ``title``.

    ``labels`` are the button texts named by the current prompt. The default
    pair is only for callers that already know they are on that wording.
    """
    allowed = labels or _BUTTONS
    titles = [
        element
        for element in elements
        if _text(element) and title_matches(title, [_text(element)])
    ]
    if not titles:
        return None
    title_node = max(titles, key=lambda element: len(_text(element)))
    buttons: list[dict[str, Any]] = []
    title_y = center(title_node)[1]
    for element in elements:
        label = _text(element)
        if label not in allowed:
            continue
        if _vertical_overlap(title_node, element) >= 0.4:
            buttons.append(element)
    if not buttons:
        return None
    button = min(buttons, key=lambda element: abs(center(element)[1] - title_y))
    return _text(button), center(button)


def screen_size(elements: list[dict[str, Any]]) -> tuple[int, int]:
    width, height = 1080, 2400
    for element in elements:
        bounds = element.get("bounds") or []
        if len(bounds) == 4:
            width = max(width, int(bounds[2]))
            height = max(height, int(bounds[3]))
    return width, height


def center(element: dict[str, Any]) -> tuple[int, int]:
    bounds = element.get("bounds") or [0, 0, 0, 0]
    return (int(bounds[0]) + int(bounds[2])) // 2, (int(bounds[1]) + int(bounds[3])) // 2


def _text(element: dict[str, Any]) -> str:
    return str(element.get("text") or "").strip()


def _area(element: dict[str, Any]) -> int:
    bounds = element.get("bounds") or [0, 0, 0, 0]
    return max(0, int(bounds[2]) - int(bounds[0])) * max(0, int(bounds[3]) - int(bounds[1]))


def _vertical_overlap(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_bounds = left.get("bounds") or [0, 0, 0, 0]
    right_bounds = right.get("bounds") or [0, 0, 0, 0]
    top = max(int(left_bounds[1]), int(right_bounds[1]))
    bottom = min(int(left_bounds[3]), int(right_bounds[3]))
    overlap = max(0, bottom - top)
    left_height = max(1, int(left_bounds[3]) - int(left_bounds[1]))
    right_height = max(1, int(right_bounds[3]) - int(right_bounds[1]))
    return overlap / min(left_height, right_height)
