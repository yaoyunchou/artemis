"""Split a saved prompt into subtasks and compile a script only when the text supports one.

Button labels come from that subtask's own wording. A prompt that never names
its start and done buttons does not get a script.
"""

from __future__ import annotations

import re
from typing import Any

from apps.admin_console.services.task_script import (
    classify,
    split_row,
    wait_seconds,
)

_NUMBERED = re.compile(r"^[ \t]*(?:(\d{1,3})[.、．)]|（(\d{1,3})）)\s*(\S.*?)\s*$")
_PACKAGE = re.compile(r"com\.[a-zA-Z0-9_.]+")
_STOP_LINE = "只执行下面这一条，做完就结束。不要做清单里的其他事项。迷路时只按返回，不要重新打开应用。"

_DONE_PATTERNS = (
    re.compile(r"变成「([^」]{1,16})」"),
    re.compile(r"已经是「([^」]{1,16})」"),
    re.compile(r"完成后点「([^」]{1,16})」"),
)
_START_PATTERNS = (
    re.compile(r"按钮是「([^」]{1,16})」"),
    re.compile(r"点「([^」]{1,16})」后"),
    re.compile(r"是「([^」]{1,16})」就"),
)

SCRIPT_LIMIT_SECONDS = 60
AI_LIMIT_SECONDS = 120


def extract_buttons(text: str) -> tuple[str | None, str | None]:
    """Start and done labels written in ``text``. Missing either side yields no pair."""
    done = _first(text, _DONE_PATTERNS)
    start = _first(text, _START_PATTERNS)
    if start and done and start != done:
        return start, done
    bare_start = "去完成" if "去完成" in text else None
    bare_done = "领取奖励" if "领取奖励" in text else None
    start = start or bare_start
    done = done or bare_done
    if start and done and start != done:
        return start, done
    return None, None


def compile_recipe(title: str, detail: str, *, source: str = "") -> dict[str, Any] | None:
    """A script for one subtask, or None when the wording is not a fixed shape."""
    text = f"{title}\n{detail}".strip()
    kind = classify(title, detail)
    if kind == "model":
        return None
    start_button, done_button = extract_buttons(text)
    if not start_button or not done_button:
        # The coin-task popup only has these two buttons. A row that already
        # has a fixed shape still gets a script when the prompt does not name
        # a different pair. "没有按钮" is an explicit opt-out.
        if "没有按钮" in text or not _is_task_list_row(title, detail):
            return None
        start_button, done_button = "去完成", "领取奖励"
    package_match = _PACKAGE.search(source or text)
    package = package_match.group(0) if package_match else None
    if package is None and "闲鱼" in (source or text):
        package = "com.taobao.idlefish"
    constraints = [
        sentence.strip()
        for sentence in re.split(r"[。\n]", detail)
        if "不要" in sentence and sentence.strip()
    ]
    done_hint = "任务完成" if "任务完成" in text else None
    seconds = wait_seconds(text) if kind in ("browse", "video", "sign") else 0
    return {
        "kind": kind,
        "seconds": seconds,
        "start_button": start_button,
        "done_button": done_button,
        "package": package,
        "constraints": constraints,
        "done_hint": done_hint,
        "limit_seconds": SCRIPT_LIMIT_SECONDS,
    }


_DROP_MARKERS = (
    "列表里没有",
    "列表中不存在",
    "不存在",
    "没有这一行",
    "未安装",
    "开始下载",
    "循环",
)


def should_drop_row(status: str, reason: str, log_text: str) -> bool:
    """A failed row the screen does not offer should leave the saved prompt.

    The model transcript is not evidence. A long log often says 「不存在」 about
    some other popup and would mark a real row as 直接跳过.
    """
    if status == "completed":
        return False
    text = reason.split("最后输出", 1)[0]
    return any(marker in text for marker in _DROP_MARKERS)


def mark_row_skip(prompt: str, title: str) -> str:
    """Append 「直接跳过」 to the numbered row for ``title``."""
    lines = prompt.splitlines()
    rewritten: list[str] = []
    for line in lines:
        match = _NUMBERED.match(line)
        if match:
            raw_title = (match.group(3) or "").strip()
            name = raw_title.partition("。")[0].strip()
            if (title == name or title in raw_title or name in title) and "直接跳过" not in raw_title:
                prefix = line[: match.start(3)]
                rewritten.append(f"{prefix}{raw_title}。直接跳过。")
                continue
        rewritten.append(line)
    return "\n".join(rewritten).strip() + "\n"


def append_catalog_row(prompt: str, title: str, detail: str) -> str:
    """Add one numbered row the screen showed and the catalog did not know."""
    lines = [line for line in prompt.splitlines()]
    last_number = 0
    for line in lines:
        match = _NUMBERED.match(line)
        if match:
            last_number = int(match.group(1) or match.group(2) or last_number)
    body = detail.strip()
    row = f"{last_number + 1}. {title}。{body}" if body else f"{last_number + 1}. {title}"
    text = "\n".join(lines).strip()
    return f"{text}\n{row}\n" if text else f"{row}\n"


def drop_numbered_rows(prompt: str, titles: list[str]) -> str:
    """Remove numbered items whose title matches one of ``titles``. Preamble stays."""
    if not titles:
        return prompt
    keys = [title.strip() for title in titles if title.strip()]
    lines = prompt.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        match = _NUMBERED.match(line)
        if match:
            raw_title = (match.group(3) or "").strip()
            title = raw_title.partition("。")[0].strip()
            skipping = any(key == title or key in raw_title or title in key for key in keys)
            if skipping:
                continue
        elif skipping:
            continue
        kept.append(line)
    return "\n".join(kept).strip() + "\n"


def split_saved_prompt(raw: str) -> list[dict[str, Any]]:
    """Numbered rows become subtasks. A prompt without a list stays one subtask."""
    lines = raw.splitlines()
    preamble_lines: list[str] = []
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        match = _NUMBERED.match(line)
        if match:
            if current is not None:
                items.append(current)
            current = {"title": (match.group(3) or "").strip(), "body": []}
            continue
        if current is None:
            preamble_lines.append(line)
        else:
            current["body"].append(line)
    if current is not None:
        items.append(current)
    if len(items) < 2:
        title = _single_title(raw)
        recipe = compile_recipe(title, raw, source=raw)
        return [_subtask(title, raw, raw, recipe)]

    preamble = "\n".join(preamble_lines).strip()
    built: list[dict[str, Any]] = []
    for item in items:
        raw_title = str(item["title"])
        title, _, rest = raw_title.partition("。")
        body = "\n".join(part for part in [rest.strip(), *item["body"]] if str(part).strip())
        title = title.strip() or raw_title.strip()
        detail = body.strip()
        prompt_parts = [preamble, _STOP_LINE, title, detail]
        prompt = "\n".join(part for part in prompt_parts if part)
        shown = title if len(title) <= 72 else f"{title[:72]}…"
        recipe = compile_recipe(title, detail, source=raw)
        built.append(_subtask(shown, prompt, detail, recipe))
    return built


def budget_result(
    *,
    elapsed: float,
    limit: float,
    done: bool,
    stage: str,
    done_button: str,
) -> tuple[str, str]:
    """Completed, failed, or still running for one subtask budget."""
    if done:
        return "completed", f"已点「{done_button}」"
    if elapsed >= limit:
        return "failed", f"超过 {int(limit)} 秒，按钮仍不是「{done_button}」（停在{stage}）"
    return "running", ""


def _subtask(title: str, prompt: str, detail: str, recipe: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "title": title or "未命名任务",
        "prompt": prompt,
        "detail": detail,
        "runner": "script" if recipe else "ai",
        "script": recipe,
    }


def _single_title(raw: str) -> str:
    title, _detail = split_row(raw)
    if title:
        return title if len(title) <= 72 else f"{title[:72]}…"
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= 72 else f"{stripped[:72]}…"
    return "未命名任务"


def _is_task_list_row(title: str, detail: str) -> bool:
    """A named row on the reward list, not a generic instruction such as opening Settings."""
    name = title.strip()
    if name.startswith(("去", "浏览", "看视频", "发布", "逛")):
        return True
    text = f"{title}\n{detail}"
    return any(token in text for token in ("逛一逛", "签到", "领奖励", "小游戏", "领取奖励"))


def _first(text: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None
