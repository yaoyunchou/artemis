"""Turn one hands trace into text steps that can be replayed without a model.

Steps name the words that were tapped. Coordinates stay in the trace as notes
and are not replayed. A row is learned only while the task sheet is open and
its button is already 「领取奖励」, or the title is gone from that open sheet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SHEET_TOKENS = ("任务奖励", "得骰子赚闲鱼币", "累积任务奖励")
BUTTONS = ("领取奖励", "去完成", "已完成")
OPTIONAL_TEXTS = {"取消", "关闭", "关闭广告", "残忍离开", "跳过"}
CATALOG_NAME = "闲鱼币任务清单.json"


def element_text(element: dict[str, Any]) -> str:
    return str(element.get("text") or "").strip()


def texts_of(elements: list[dict[str, Any]]) -> list[str]:
    return [element_text(element) for element in elements if element_text(element)]


def sheet_open(elements: list[dict[str, Any]]) -> bool:
    """The coin-task sheet is on screen, not merely scrolled off or left behind."""
    texts = texts_of(elements)
    has_sheet = any(any(token in text for token in SHEET_TOKENS) for text in texts)
    has_button = any(text in BUTTONS for text in texts)
    return has_sheet and has_button


def same_band(left: dict[str, Any], right: dict[str, Any]) -> bool:
    a = left.get("bounds") or [0, 0, 0, 0]
    b = right.get("bounds") or [0, 0, 0, 0]
    a_mid = (int(a[1]) + int(a[3])) / 2
    b_mid = (int(b[1]) + int(b[3])) / 2
    return abs(a_mid - b_mid) <= 80


def row_button(elements: list[dict[str, Any]], title: str) -> str | None:
    """The action button sitting on the same row as ``title``, if both are visible."""
    anchors = [element for element in elements if element_text(element) == title]
    if not anchors:
        return None
    anchor = anchors[0]
    right_edge = int((anchor.get("bounds") or [0, 0, 0, 0])[2])
    buttons = [
        element
        for element in elements
        if element_text(element) in BUTTONS and same_band(element, anchor)
    ]
    beside = [
        element
        for element in buttons
        if int((element.get("bounds") or [0, 0, 0, 0])[0]) >= right_edge - 40
    ]
    chosen = (beside or buttons)
    if not chosen:
        return None
    return element_text(chosen[0])


def judge_row(elements: list[dict[str, Any]], title: str) -> dict[str, Any]:
    """Whether this look is allowed to store steps for ``title``."""
    if not sheet_open(elements):
        return {"learned": False, "reason": "任务弹层没开", "button": None}
    button = row_button(elements, title)
    if button == "领取奖励":
        return {"learned": True, "reason": "", "button": button}
    if button == "已完成":
        return {"learned": True, "reason": "", "button": button}
    if button == "去完成":
        return {"learned": False, "reason": "按钮仍是去完成", "button": button}
    if not any(element_text(element) == title for element in elements):
        return {"learned": True, "reason": "弹层开着但这一行不在了", "button": None}
    return {"learned": False, "reason": "没看到这一行的按钮", "button": button}


def compress_steps(events: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    """Keep the successful suffix and drop taps that left the screen unchanged.

    The suffix starts at the last tap on this row's 「去完成」 or 「领取奖励」.
    A lone claim tap stays as that one step.
    """
    reduced: list[dict[str, Any]] = []
    previous: tuple[str, ...] | None = None
    pending: list[dict[str, Any]] = []
    for event in events:
        if event.get("op") == "look":
            signature = tuple(event.get("texts") or [])
            if previous is not None and signature == previous:
                pending = [item for item in pending if item.get("op") == "wait"]
            reduced.extend(pending)
            pending = []
            previous = signature
        elif event.get("op") in {"click", "press_key", "wait", "swipe"}:
            pending.append(event)
    reduced.extend(pending)

    start = None
    for index, event in enumerate(reduced):
        if event.get("op") == "click" and event.get("text") in {"去完成", "领取奖励"}:
            start = index
    if start is None:
        return []
    reduced = reduced[start:]

    steps: list[dict[str, Any]] = []
    for event in reduced:
        op = event.get("op")
        if op == "click":
            text = str(event.get("text") or "").strip()
            if not text:
                continue
            step: dict[str, Any] = {"op": "tap_text", "text": text}
            if text in {"去完成", "领取奖励"}:
                step["near"] = title
            if text in OPTIONAL_TEXTS:
                step["optional"] = True
            steps.append(step)
        elif op == "wait":
            millis = int(event.get("ms") or 0)
            if millis > 0:
                steps.append({"op": "wait", "ms": millis})
        elif op == "press_key" and event.get("key"):
            steps.append({"op": "key", "key": str(event["key"])})
    taps = [step for step in steps if step.get("op") == "tap_text"]
    if len(taps) == 1 and taps[0].get("text") == "领取奖励":
        return [{"op": "tap_text", "text": "领取奖励", "near": title}]
    return steps


def _tap_texts(step: dict[str, Any]) -> list[str]:
    if isinstance(step.get("any"), list):
        return [str(item) for item in step["any"] if str(item).strip()]
    text = str(step.get("text") or "").strip()
    return [text] if text else []


def merge_steps(previous: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union tap words at the same index. Never drop a step that already worked."""
    if not previous:
        return fresh
    if not fresh:
        return previous
    merged: list[dict[str, Any]] = []
    for index in range(max(len(previous), len(fresh))):
        if index >= len(previous):
            merged.append(fresh[index])
            continue
        if index >= len(fresh):
            merged.append(previous[index])
            continue
        old = previous[index]
        new = fresh[index]
        if old.get("op") == "tap_text" and new.get("op") == "tap_text":
            texts: list[str] = []
            for text in _tap_texts(old) + _tap_texts(new):
                if text not in texts:
                    texts.append(text)
            step = {"op": "tap_text"}
            if len(texts) == 1:
                step["text"] = texts[0]
            elif texts:
                step["any"] = texts
            near = old.get("near") or new.get("near")
            if near:
                step["near"] = near
            if old.get("optional") or new.get("optional"):
                step["optional"] = True
            merged.append(step)
        else:
            merged.append(old)
    return merged


def find_tap(
    elements: list[dict[str, Any]],
    step: dict[str, Any],
    width: int,
    height: int,
) -> list[int] | None:
    """Normalized [x, y] for a text step, preferring the named row's band."""
    wanted = _tap_texts(step)
    if not wanted:
        return None
    exact = [element for element in elements if element_text(element) in wanted]
    if not exact:
        exact = [
            element
            for element in elements
            if any(word and word in element_text(element) for word in wanted)
        ]
    near = str(step.get("near") or "")
    if near and exact:
        anchors = [element for element in elements if element_text(element) == near]
        if anchors:
            band = [element for element in exact if same_band(element, anchors[0])]
            if band:
                exact = band
    if not exact:
        return None
    center = exact[0].get("center")
    if not (isinstance(center, (list, tuple)) and len(center) == 2):
        return None
    nx = int(max(0, min(1000, round(float(center[0]) * 1000 / max(1, width)))))
    ny = int(max(0, min(1000, round(float(center[1]) * 1000 / max(1, height)))))
    return [nx, ny]


def catalog_path(root: Path) -> Path:
    return root / "tasks" / CATALOG_NAME


def load_catalog(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("rows", [])
    return data


def save_catalog(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find_catalog_row(data: dict[str, Any], title: str) -> dict[str, Any] | None:
    for row in data["rows"]:
        if row.get("title") == title:
            return row
    return None


def store_steps(path: Path, title: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge ``steps`` onto the catalog row. Skip rows are left unchanged."""
    data = load_catalog(path)
    row = find_catalog_row(data, title)
    if row is not None and row.get("run") == "skip":
        raise ValueError(f"{title} 已标记不能运行，不写步骤")
    if row is None:
        row = {
            "title": title,
            "run": "yes",
            "script": None,
            "script_fails": 0,
            "seconds": 15,
            "reason": "",
            "steps": [],
        }
        data["rows"].append(row)
    row["steps"] = merge_steps(list(row.get("steps") or []), steps)
    row["script_fails"] = 0
    save_catalog(path, data)
    return row


def note_replay_failure(path: Path, title: str) -> dict[str, Any]:
    """Count one replay miss. The third miss drops steps so the model can relearn."""
    data = load_catalog(path)
    row = find_catalog_row(data, title)
    if row is None:
        raise ValueError(f"清单里没有 {title}")
    fails = int(row.get("script_fails") or 0) + 1
    row["script_fails"] = fails
    cleared = False
    if fails >= 3:
        row["steps"] = []
        cleared = True
    save_catalog(path, data)
    return {"title": title, "script_fails": fails, "cleared": cleared}


def note_replay_success(path: Path, title: str) -> None:
    """A replay that reached the reward resets the miss count and keeps steps."""
    data = load_catalog(path)
    row = find_catalog_row(data, title)
    if row is None:
        return
    row["script_fails"] = 0
    save_catalog(path, data)


def unlearned_count(data: dict[str, Any]) -> int:
    total = 0
    for row in data.get("rows") or []:
        if row.get("run") == "skip":
            continue
        if not (row.get("steps") or []):
            total += 1
    return total


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def settle(events: list[dict[str, Any]], data: dict[str, Any], previous_unlearned: int | None) -> dict[str, Any]:
    """Fail when a touched row was not learned, or the unlearned set grew."""
    touched: dict[str, dict[str, bool]] = {}
    for event in events:
        title = str(event.get("title") or "")
        if event.get("op") == "begin_row" and title:
            touched[title] = {"learned": False}
        elif event.get("op") == "end_row" and title:
            state = touched.setdefault(title, {"learned": False})
            if event.get("learned"):
                state["learned"] = True
    rows = {str(row.get("title")): row for row in data.get("rows") or []}
    problems: list[str] = []
    for title, state in touched.items():
        row = rows.get(title) or {}
        if row.get("run") == "skip":
            continue
        has_steps = bool(row.get("steps"))
        if state.get("learned") and not has_steps:
            problems.append(f"{title} 已做成但没有步骤")
        elif not state.get("learned") and not has_steps:
            problems.append(f"{title} 碰过但没学会")
    unlearned = unlearned_count(data)
    if previous_unlearned is not None and unlearned > previous_unlearned:
        problems.append(f"没有步骤的可做行从 {previous_unlearned} 增到 {unlearned}")
    return {"ok": not problems, "problems": problems, "unlearned": unlearned}
