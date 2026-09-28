"""闲鱼币任务奖励列表的界面规则。

这个文件由 AI 按运行日志维护。只能导入 time、re、typing 和 artemis_app_helpers；
点按只能通过 runner 传进来的 phone（dump、tap、back、launch、swipe_list）。
遇到脚本自己处理不了的画面，用 phone.note(标签, 说明) 记下，runner 会保存当时的界面。
"""

from __future__ import annotations

import re
import time
from typing import Any

import artemis_app_helpers as h

PACKAGE = "com.taobao.idlefish"

_BUTTON_LABELS = {"领取奖励", "去完成", "已完成"}
_SHEET_TOKENS = ("任务奖励", "得骰子赚闲鱼币", "累积任务奖励")
_NOT_A_ROW = ("闲鱼币兑曝光", "闲鱼币抵扣", "现金夺宝", "急速卖", "玩法规则", "签到玩法", "收益+", "可得")
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
_GAME_ROW = ("玩1关", "小游戏", "合成", "升级火炉", "消不停", "还想消")
_DICE_BADGE = re.compile(r"^[×xX✕](\d+)$")


# ---------------------------------------------------------------- 读屏


def on_task_list(elements: list[dict[str, Any]]) -> bool:
    """任务奖励弹层已经打开。弹层标题「得骰子赚闲鱼币」有时替代「任务奖励」。"""
    texts = [h.text_of(element) for element in elements]
    has_action = any(text in _BUTTON_LABELS for text in texts)
    has_sheet = any(any(token in text for token in _SHEET_TOKENS) for text in texts)
    return has_action and has_sheet


def row_titles(elements: list[dict[str, Any]]) -> list[str]:
    """屏幕上按钮是「去完成」的任务行。不含网页容器、棋盘下的商店入口和规则页。

    按钮必须和这一个节点在同一行。棋盘下的小游戏名「消了还想消」是任务行
    「去消了还想消玩1关」的一部分，按名字找按钮会把它也算成一行。
    """
    width, height = h.screen_size(elements)
    buttons = [element for element in elements if h.text_of(element) == "去完成"]
    rewards = [element for element in elements if h.text_of(element) == "+"]
    titles: list[str] = []
    for element in elements:
        text = h.text_of(element)
        if text in titles or not _is_row_title(element, text, width, height):
            continue
        if not _has_reward_line(element, rewards):
            continue
        right = int((element.get("bounds") or [0, 0, 0, 0])[2])
        for button in buttons:
            left = int((button.get("bounds") or [0, 0, 0, 0])[0])
            if left >= right - 10 and _same_band(element, button):
                titles.append(text)
                break
    return titles


def _has_reward_line(title: dict[str, Any], rewards: list[dict[str, Any]]) -> bool:
    """真任务行下面紧跟一行「+160 +2」。列表叠在棋盘上，棋盘文字没有这一行。"""
    a = title.get("bounds") or [0, 0, 0, 0]
    for reward in rewards:
        b = reward.get("bounds") or [0, 0, 0, 0]
        below = int(a[3]) - 10 <= int(b[1]) <= int(a[3]) + 40
        aligned = int(a[0]) - 20 <= int(b[0]) <= int(a[0]) + 120
        if below and aligned:
            return True
    return False


def _same_band(left: dict[str, Any], right: dict[str, Any]) -> bool:
    a = left.get("bounds") or [0, 0, 0, 0]
    b = right.get("bounds") or [0, 0, 0, 0]
    overlap = max(0, min(int(a[3]), int(b[3])) - max(int(a[1]), int(b[1])))
    shorter = max(1, min(int(a[3]) - int(a[1]), int(b[3]) - int(b[1])))
    return overlap / shorter >= 0.3


def is_skip_row(title: str) -> bool:
    """要玩一局小游戏的行，按提示词直接跳过。"""
    return any(token in title for token in _GAME_ROW)


def _is_row_title(element: dict[str, Any], text: str, width: int, height: int) -> bool:
    if len(text) < 4 or len(text) > 40 or text in _SKIP_TITLES:
        return False
    if any(token in text for token in _NOT_A_ROW):
        return False
    if h.looks_like_class_name(text) or h.covers_screen(element, width, height):
        return False
    return True


def _dice_badge(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    """骰子是图片，界面树里只有 ×N 角标。N 就是剩余次数。"""
    for element in elements:
        if _DICE_BADGE.match(h.text_of(element).replace(" ", "")):
            return element
    return None


def _dice_left(elements: list[dict[str, Any]]) -> int | None:
    badge = _dice_badge(elements)
    if badge is None:
        return None
    match = _DICE_BADGE.match(h.text_of(badge).replace(" ", ""))
    return int(match.group(1)) if match else None


def _dice_point(elements: list[dict[str, Any]]) -> tuple[int, int] | None:
    badge = _dice_badge(elements)
    if badge is None:
        return None
    width, _height = h.screen_size(elements)
    bounds = badge.get("bounds") or [0, 0, 0, 0]
    return width // 2, int(bounds[3]) + 80


def _earn_dice_point(elements: list[dict[str, Any]]) -> tuple[int, int] | None:
    """「赚骰子」能打开任务列表；「任务奖励」文字点了没反应。

    「领」角标只在有奖励可领时出现，没有时按骰子角标旁边的位置点。
    """
    width, height = h.screen_size(elements)
    best: tuple[int, int] | None = None
    best_area = 10**12
    for element in elements:
        if h.text_of(element) != "领":
            continue
        x_pos, y_pos = h.center(element)
        if y_pos < height * 0.35 or y_pos > height * 0.58 or x_pos < width * 0.55:
            continue
        size = h.area(element)
        if size < best_area:
            best = (x_pos, y_pos)
            best_area = size
    if best is not None:
        return best
    badge = _dice_badge(elements)
    if badge is None:
        return None
    x_pos, y_pos = h.center(badge)
    return x_pos + 199, y_pos + 110


def _is_home(elements: list[dict[str, Any]]) -> bool:
    return any(str(element.get("resource_id") or "").endswith("search_btn") for element in elements)


def _home_badge_point(elements: list[dict[str, Any]]) -> tuple[int, int]:
    """闲鱼首页右上角橙色「领」，在搜索按钮正上方，没有文字。"""
    width, height = h.screen_size(elements)
    for element in elements:
        if str(element.get("resource_id") or "").endswith("search_btn"):
            bounds = element.get("bounds") or [0, 0, 0, 0]
            button_height = max(1, int(bounds[3]) - int(bounds[1]))
            return int(bounds[2]) - button_height // 2, max(0, int(bounds[1]) - button_height)
    return int(width * 0.925), int(height * 0.055)


def _is_rules_page(elements: list[dict[str, Any]]) -> bool:
    blob = "\n".join(h.text_of(element) for element in elements)
    return "玩法规则" in blob or "连续签到天数" in blob


def _on_board(elements: list[dict[str, Any]]) -> bool:
    return _dice_left(elements) is not None and not on_task_list(elements)


_AD_TOKENS = ("点击广告拿奖励", "下载第三方应用", "广告")
_AD_REWARD_TOKENS = ("秒拿奖励", "更快拿奖")
_AD_ACTION_TOKENS = ("点击浏览商品", "浏览商品", "去逛逛")
_AD_TEXT_TOKENS = ("我猜你没看过",)


def _is_ad(elements: list[dict[str, Any]]) -> bool:
    """看视频任务打开的全屏广告、浏览商品跳转页等。

    关闭按钮常常是图片，界面树里没有文字；顶部倒计时和底部 CTA 会分开成多个节点。
    """
    if on_task_list(elements) or _on_board(elements) or _is_home(elements):
        return False
    texts = [h.text_of(element) for element in elements]
    if any(any(token in text for token in _AD_TOKENS) for text in texts):
        return True
    if any(any(token in text for token in _AD_TEXT_TOKENS) for text in texts):
        return True
    has_reward = any(any(token in text for token in _AD_REWARD_TOKENS) for text in texts)
    has_action = any(any(token in text for token in _AD_ACTION_TOKENS) for text in texts)
    return has_reward and has_action


def _ad_close_point(elements: list[dict[str, Any]]) -> tuple[int, int] | None:
    """倒计时右侧的关闭。关闭本身没文字，界面树里不出现，按「秒拿奖励」右边推算。"""
    width, height = h.screen_size(elements)
    right: int | None = None
    y_pos: int | None = None
    for element in elements:
        text = h.text_of(element)
        if "拿奖励" not in text:
            continue
        _x_pos, row_y = h.center(element)
        if row_y > height * 0.25:
            continue
        edge = int((element.get("bounds") or [0, 0, 0, 0])[2])
        if right is None or edge > right:
            right = edge
            y_pos = row_y
    if right is None or y_pos is None:
        return None
    return min(width - 48, right + 80), y_pos


def _leave_ad(phone: Any) -> bool:
    """等倒计时走完，再按返回；返回不管用就点右上角、左上角的关闭。不点广告本身。"""
    waited = 0.0
    backs = 0
    corner = 0
    launched = 0
    claims = 0
    dismisses = 0
    closes = 0
    while waited < 45:
        _xml, elements = phone.dump()
        if not _is_ad(elements):
            return True

        claim = h.find_exact(elements, "领取奖励")
        if claim is not None and claims < 2:
            x_pos, y_pos = h.center(claim)
            phone.tap(x_pos, y_pos)
            claims += 1
            waited += 1.0
            time.sleep(1.0)
            continue

        dismiss = h.find_dismiss(elements) or h.find_exact(elements, "跳过", "关闭")
        if dismiss is not None and dismisses < 3:
            x_pos, y_pos = h.center(dismiss)
            phone.tap(x_pos, y_pos)
            dismisses += 1
            waited += 1.0
            continue

        close = _ad_close_point(elements)
        if close is not None and closes < 2:
            print(f"点广告关闭 {close}", flush=True)
            phone.tap(close[0], close[1])
            closes += 1
            waited += 1.0
            time.sleep(1.0)
            continue

        if closes == 0 and waited < 18:
            time.sleep(3.0)
            waited += 3.0
            continue

        if backs < 2:
            phone.back()
            backs += 1
            waited += 1.0
            continue

        width, _height = h.screen_size(elements)
        points = [(width - 70, 150), (70, 150), (width - 70, 230)]
        if corner < len(points):
            print(f"广告页按返回没关掉，点关闭 {points[corner]}", flush=True)
            phone.tap(points[corner][0], points[corner][1])
            corner += 1
            waited += 1.0
            continue

        if launched < 2:
            print("广告页按返回和点角没关掉，强制回到闲鱼", flush=True)
            phone.launch(PACKAGE)
            launched += 1
            time.sleep(2.0)
            continue

        phone.note("ad_stuck", "广告页等了 45 秒、按返回、点角和强制回闲鱼仍未离开")
        return False

    _xml, elements = phone.dump()
    if not _is_ad(elements):
        return True
    phone.launch(PACKAGE)
    time.sleep(2.0)
    _xml, elements = phone.dump()
    return not _is_ad(elements)


# ---------------------------------------------------------------- 动作


def enter_list(phone: Any) -> bool:
    """从闲鱼任意页面进到任务奖励列表。点不中不按返回离开闲鱼。"""
    launched = False
    entry_taps = 0
    dice_taps = 0
    backs = 0
    force_launches = 0
    for attempt in range(12):
        xml, elements = phone.dump()
        if on_task_list(elements):
            print("已进入任务奖励列表", flush=True)
            return True
        if _is_rules_page(elements):
            print("关掉签到规则页", flush=True)
            phone.back()
            continue
        if _is_ad(elements):
            if not _leave_ad(phone):
                phone.note("enter_ad_stuck", "进入列表时广告页没关掉")
                if force_launches < 2:
                    print("广告页没关掉，强制回到闲鱼首页", flush=True)
                    phone.launch(PACKAGE)
                    force_launches += 1
                    time.sleep(2.0)
                    continue
                return False
            continue
        dismiss = h.find_dismiss(elements)
        if dismiss is not None:
            x_pos, y_pos = h.center(dismiss)
            phone.tap(x_pos, y_pos)
            continue
        packages = h.foreground_packages(xml)
        if packages and PACKAGE not in packages:
            if force_launches < 2:
                print("不在闲鱼前台，强制回闲鱼", flush=True)
                phone.launch(PACKAGE)
                force_launches += 1
                launched = True
                time.sleep(2.0)
                continue
            if launched:
                phone.back()
                backs += 1
                continue
            phone.launch(PACKAGE)
            launched = True
            continue
        point = _earn_dice_point(elements)
        if point is not None and dice_taps < 3:
            print(f"点赚骰子 {point}", flush=True)
            phone.tap(point[0], point[1])
            dice_taps += 1
            time.sleep(2.0)
            continue
        if _is_home(elements) and entry_taps < 2:
            point = _home_badge_point(elements)
            print(f"点闲鱼币入口 {point}", flush=True)
            phone.tap(point[0], point[1])
            entry_taps += 1
            time.sleep(3.0)
            continue
        if backs < 2 and not _is_home(elements):
            phone.back()
            backs += 1
            continue
        if force_launches < 2 and not _is_home(elements):
            print("强制回到闲鱼首页", flush=True)
            phone.launch(PACKAGE)
            force_launches += 1
            time.sleep(2.0)
            continue
        print(f"还没进列表（第 {attempt + 1} 次）", flush=True)
        time.sleep(1.5)
    _xml, elements = phone.dump()
    if on_task_list(elements):
        return True
    phone.note("enter_list_stuck", "多次尝试仍未进入任务奖励列表")
    return False


def _open_sheet(phone: Any) -> bool:
    _xml, elements = phone.dump()
    if on_task_list(elements):
        return True
    point = _earn_dice_point(elements)
    if point is None:
        return False
    print(f"回到列表，点赚骰子 {point}", flush=True)
    phone.tap(point[0], point[1])
    time.sleep(1.5)
    _xml, elements = phone.dump()
    return on_task_list(elements)


def return_to_list(phone: Any) -> bool:
    """做完一条后回到列表。回到闲鱼币页就不再按返回，改点赚骰子。"""
    backs = 0
    for _ in range(8):
        xml, elements = phone.dump()
        if on_task_list(elements):
            return True
        if _on_board(elements):
            if _open_sheet(phone):
                return True
            continue
        if _is_ad(elements):
            if not _leave_ad(phone):
                phone.note("return_ad_stuck", "做完任务后广告页没关掉")
                return enter_list(phone)
            continue
        dismiss = h.find_dismiss(elements)
        if dismiss is not None:
            x_pos, y_pos = h.center(dismiss)
            phone.tap(x_pos, y_pos)
            continue
        packages = h.foreground_packages(xml)
        if packages and PACKAGE not in packages:
            return enter_list(phone)
        if _is_home(elements) or backs >= 3:
            return enter_list(phone)
        phone.back()
        backs += 1
    _xml, elements = phone.dump()
    if on_task_list(elements):
        return True
    return enter_list(phone)


def _close_sheet(phone: Any) -> bool:
    _xml, elements = phone.dump()
    if not on_task_list(elements):
        return True
    phone.back()
    _xml, elements = phone.dump()
    if _on_board(elements):
        return True
    if not on_task_list(elements):
        enter_list(phone)
    return False


def roll_dice(phone: Any, *, leave_sheet: bool) -> int:
    """掷到 ×0 为止。列表盖着骰子时先关列表，否则会点到列表里的行。"""
    _xml, elements = phone.dump()
    if on_task_list(elements):
        if not leave_sheet or not _close_sheet(phone):
            return 0
    elif _dice_left(elements) is None:
        return 0
    rolled = 0
    stuck = 0
    for _ in range(40):
        _xml, elements = phone.dump()
        if on_task_list(elements):
            break
        surprise = h.find_exact(elements, "限时惊喜")
        dismiss = h.find_dismiss(elements)
        if surprise is not None or (dismiss is not None and _dice_left(elements) is None):
            if dismiss is None:
                break
            x_pos, y_pos = h.center(dismiss)
            phone.tap(x_pos, y_pos)
            continue
        left = _dice_left(elements)
        point = _dice_point(elements)
        if point is None or not left or rolled >= 20:
            break
        phone.tap(point[0], point[1])
        time.sleep(2.5)
        _xml, after = phone.dump()
        if _dice_left(after) == left:
            stuck += 1
            if stuck >= 2:
                phone.note("dice_stuck", f"骰子点了两次仍是 ×{left}")
                break
            continue
        stuck = 0
        rolled += 1
    if rolled:
        print(f"掷骰子 {rolled} 次，剩 ×{_dice_left(phone.dump()[1]) or 0}", flush=True)
    _open_sheet(phone)
    return rolled


def claim_visible(phone: Any) -> int:
    """点掉当前屏幕上的「领取奖励」，免得完成行堆在列表顶部。"""
    tapped = 0
    previous: tuple[int, int] | None = None
    for _ in range(6):
        _xml, elements = phone.dump()
        if not on_task_list(elements):
            break
        target = h.find_exact(elements, "领取奖励")
        if target is None:
            break
        point = h.center(target)
        if point == previous:
            break
        previous = point
        phone.tap(point[0], point[1])
        tapped += 1
    return tapped
