"""Rewrite an App script from one run's logs, and undo a rewrite that ran worse.

After every run: record the result on the script version that ran. A new
version whose first run is worse than its parent's last run goes back to the
parent. Otherwise, if the run got stuck, the model reads the script, the logs
and the saved screens and returns a whole new file. It goes live only if it
passes ``app_script_host.validate``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.admin_console.services.app_script_host import (
    REQUIRED,
    ScriptRejected,
    validate,
    write_current_file,
)
from apps.admin_console.services.personal_task_store import PersonalTaskStore

_CODE_BLOCK = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
_REASON = re.compile(r"^REASON[:：]\s*(.+)$", re.MULTILINE)

_CONTRACT = """\
接口（全部必须保留，签名不变）：
- on_task_list(elements) -> bool：任务奖励列表是否已打开
- row_titles(elements) -> list[str]：屏幕上按钮是「去完成」的真实任务行
- is_skip_row(title) -> bool：按规则直接跳过的行
- enter_list(phone) -> bool：从 App 任意页面进到任务列表
- return_to_list(phone) -> bool：做完一行后回到任务列表
- roll_dice(phone, *, leave_sheet) -> int：掷完剩余骰子，返回掷了几次
- claim_visible(phone) -> int：点掉当前屏幕上的「领取奖励」
elements 是节点列表，每个节点有 text、resource_id、class、clickable、bounds [左,上,右,下]、center。
phone 只有 dump() -> (xml, elements)、tap(x, y)、back()、launch(package)、swipe_list(toward_top=bool)、note(标签, 说明)。
只能 import time、re、typing、artemis_app_helpers（as h）。不能用 open、exec、eval、getattr，也不能访问双下划线属性。
h 里有：text_of、center、area、screen_size、find_exact、find_label、find_dismiss、row_action、
foreground_packages、covers_screen、looks_like_class_name、title_matches。
"""


def chat(prompt: str, *, max_tokens: int, timeout: float) -> str:
    """One OpenAI-compatible chat call with the key and model from .env."""
    from artemis.config.settings import settings

    secret = settings.OPENAI_API_KEY
    key = (secret.get_secret_value() if secret else "") or os.environ.get("OPENAI_API_KEY", "")
    key = key.strip()
    if not key:
        raise RuntimeError("没有配置 OPENAI_API_KEY")
    base = (settings.OPENAI_BASE_URL or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = (settings.ARTEMIS_MODEL or os.environ.get("ARTEMIS_MODEL") or "qwen3.8-flash").strip()
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload["choices"][0]["message"]["content"])


def problems_of(report: dict[str, Any], evidence: list[dict[str, str]]) -> list[str]:
    """What went wrong in this run that a script change could fix."""
    found: list[str] = []
    if not report.get("entered"):
        found.append("没有进入任务奖励列表")
    for item in report.get("script_failures") or []:
        found.append(f"脚本小任务「{item.get('title')}」失败：{item.get('reason')}")
    if any(item.get("reason") == "dice_stuck" for item in evidence):
        found.append("骰子点了没反应")
    pending = report.get("stopped_with_pending") or []
    if pending:
        found.append(f"还有未完成的收录就停止了：{'、'.join(pending[:8])}")
    return found


def _screen_text(path: str, limit: int = 60) -> str:
    """Only nodes with text, so a dump fits in the prompt."""
    from artemis.drivers.android.hierarchy import parse_ui_hierarchy

    try:
        elements = parse_ui_hierarchy(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return "（读不到快照）"
    lines: list[str] = []
    for element in elements:
        text = str(element.get("text") or "").strip().replace("\n", " ")
        rid = str(element.get("resource_id") or "").rsplit("/", 1)[-1]
        if not text and not rid.endswith("search_btn"):
            continue
        lines.append(f"{element.get('bounds')} {text[:30]!r} {rid}".rstrip())
        if len(lines) >= limit:
            break
    return "\n".join(lines) or "（没有文字节点）"


def build_prompt(
    *,
    code: str,
    problems: list[str],
    evidence: list[dict[str, str]],
    stdout_tail: str,
    subtask_logs: list[dict[str, Any]],
) -> str:
    screens = []
    for item in evidence[-5:]:
        note = f"（{item['note']}）" if item.get("note") else ""
        screens.append(f"### {item['reason']}{note}\n{_screen_text(item['path'])}")
    logs = []
    for item in subtask_logs[-12:]:
        logs.append(f"- {item.get('title')}：{item.get('status')} {str(item.get('reason') or '')[:160]}")
    return (
        "你在维护一个 Android 界面脚本，它让手机自动完成闲鱼币任务列表。"
        "根据这一轮的问题、日志和出问题时的界面，改写整个脚本文件。"
        "只改和问题有关的地方，其余规则保持原样。\n\n"
        f"{_CONTRACT}\n"
        "## 这一轮的问题\n" + "\n".join(f"- {line}" for line in problems) + "\n\n"
        "## 小任务日志\n" + ("\n".join(logs) or "（无）") + "\n\n"
        "## 运行输出结尾\n```\n" + stdout_tail[-3000:] + "\n```\n\n"
        "## 出问题时的界面（bounds 文字 resource_id）\n" + ("\n\n".join(screens) or "（无）") + "\n\n"
        "## 当前脚本\n```python\n" + code + "\n```\n\n"
        "回答格式：第一行写 `REASON: 一句话说明改了什么`，然后给出完整的新文件，放在一个 ```python 代码块里。"
    )


def parse_answer(answer: str) -> tuple[str, str]:
    blocks = _CODE_BLOCK.findall(answer)
    if not blocks:
        raise ValueError("回答里没有代码块")
    code = max(blocks, key=len).strip() + "\n"
    match = _REASON.search(answer)
    reason = match.group(1).strip() if match else "按本轮日志修改"
    return reason[:200], code


def _evidence_cases(evidence: list[dict[str, str]]) -> list[dict[str, Any]]:
    """This run's screens as unlabeled replay cases: the new script must not crash on them."""
    from artemis.drivers.android.hierarchy import parse_ui_hierarchy

    cases: list[dict[str, Any]] = []
    for item in evidence:
        try:
            xml = Path(item["path"]).read_text(encoding="utf-8")
        except OSError:
            continue
        cases.append({"name": Path(item["path"]).stem, "elements": parse_ui_hierarchy(xml), "expect": {}})
    return cases


def _ran_worse(version: dict[str, Any], parent: dict[str, Any]) -> str | None:
    if parent.get("last_run_at") is None:
        return None
    if int(parent.get("last_entered") or 0) and not int(version.get("last_entered") or 0):
        return "新版本没进任务列表，上一版能进"
    if int(version.get("last_completed") or 0) < int(parent.get("last_completed") or 0):
        return (
            f"新版本完成 {version.get('last_completed') or 0} 条，"
            f"少于上一版的 {parent.get('last_completed') or 0} 条"
        )
    return None


def maintain(
    store: PersonalTaskStore,
    *,
    package: str,
    version_id: str,
    report: dict[str, Any],
    evidence: list[dict[str, str]],
    stdout_tail: str,
    subtask_logs: list[dict[str, Any]],
    ask: Callable[[str], str] | None = None,
) -> str:
    """Record, roll back, or rewrite. Returns one line for the run log, or ''."""
    version = store.record_script_result(
        version_id,
        completed=int(report.get("completed") or 0),
        failed=int(report.get("failed") or 0),
        entered=bool(report.get("entered")),
    )
    parent_id = version.get("parent_id")
    if int(version.get("run_count") or 0) == 1 and parent_id:
        parent = store.get_script_version(str(parent_id))
        worse = _ran_worse(version, parent)
        if worse:
            store.set_current_script(str(parent_id), previous_status="rolled_back")
            store.mark_script(version_id, "rolled_back", f"{version.get('reason')}（已退回：{worse}）")
            write_current_file(package, str(parent["code"]))
            return f"App 脚本退回第 {parent['version']} 版：{worse}"

    problems = problems_of(report, evidence)
    if not problems:
        return ""
    prompt = build_prompt(
        code=str(version["code"]),
        problems=problems,
        evidence=evidence,
        stdout_tail=stdout_tail,
        subtask_logs=subtask_logs,
    )
    asker = ask or (lambda text: chat(text, max_tokens=8000, timeout=420))
    try:
        reason, code = parse_answer(asker(prompt))
    except Exception as exc:
        return f"App 脚本没有修改：模型没给出可用的脚本（{exc}）"
    if code.strip() == str(version["code"]).strip():
        return "App 脚本没有修改：模型给回了原样的脚本"
    try:
        validate(code, package, _evidence_cases(evidence))
    except ScriptRejected as exc:
        store.add_script_version(
            package,
            code,
            f"{reason}（未通过检查：{exc}）"[:500],
            status="rejected",
            parent_id=version_id,
        )
        return f"App 脚本改写没通过检查，继续用第 {version['version']} 版：{exc}"
    fresh = store.add_script_version(package, code, reason, status="current", parent_id=version_id)
    write_current_file(package, code)
    return f"App 脚本已更新到第 {fresh['version']} 版：{reason}"


__all__ = ["REQUIRED", "build_prompt", "chat", "maintain", "parse_answer", "problems_of"]
