"""Keep only the provided tasks that the phone's task list actually shows."""

from __future__ import annotations

import re

_VISIBLE_MARK = re.compile(r"VISIBLE\s*[:：]\s*(.+)", re.IGNORECASE)
_SPLIT = re.compile(r"[|、，,]")
_DROP = ("秒", "去", "一次", " ", "\t")


def title_from_goal(goal: str) -> str:
    """The task name is the line after the per-item stop sentence, else the last line."""
    lines = [line.strip() for line in goal.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.startswith("只执行下面这一条") and index + 1 < len(lines):
            return lines[index + 1].split("。", 1)[0].strip()
    if not lines:
        return ""
    return lines[-1].split("。", 1)[0].strip()


def build_scout_goal(titles: list[str]) -> str:
    """A single run that reads the on-screen list and does not start any row."""
    names = "\n".join(f"- {title}" for title in titles if title)
    return (
        "进入闲鱼币的任务奖励列表后停在列表上。这一步只核对名单，不要点「去完成」或「领取奖励」，"
        "不要掷骰子，不要进入任何活动。在列表内部最多上滑 4 屏，把每一行任务名称记下来。"
        "滑过 4 屏或已经看到列表底部就停止，不要来回滑。\n"
        "结束时 explanation 必须包含一行，以 VISIBLE: 开头，名称用 | 分隔，只写屏幕上真实出现的名称。\n"
        "例如：VISIBLE: 浏览推荐的国补商品 | 去蚂蚁庄园逛一逛\n"
        "用户准备做的名称如下，屏幕上没有的不要写进 VISIBLE：\n"
        f"{names}"
    )


def parse_visible(text: str) -> list[str]:
    """Names from a `VISIBLE:` line. Empty when the scout did not report one."""
    names: list[str] = []
    for line in text.splitlines():
        match = _VISIBLE_MARK.search(line)
        if not match:
            continue
        for part in _SPLIT.split(match.group(1)):
            name = part.strip().strip("。.")
            if name:
                names.append(name)
    return names


def _norm(value: str) -> str:
    cleaned = re.sub(r"\d+", "", value)
    for token in _DROP:
        cleaned = cleaned.replace(token, "")
    return cleaned


def title_matches(title: str, visible: list[str]) -> bool:
    key = _norm(title.split("。", 1)[0])
    if len(key) < 2:
        return False
    for name in visible:
        other = _norm(name)
        if not other:
            continue
        if key in other or other in key:
            return True
    return False


def matching_goals(goals: list[str], visible: list[str]) -> list[str]:
    """Goals whose task name appears on the screen list."""
    if not visible:
        return []
    kept: list[str] = []
    for goal in goals:
        title = title_from_goal(goal)
        if title_matches(title, visible):
            kept.append(goal)
    return kept
