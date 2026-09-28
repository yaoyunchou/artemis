"""Run one collected task. Subtasks are timed; the parent task is not.

This file is the app-independent loop. How to open a list, where the dice
are and which rows are real comes from ``tasks/scripts/<app>.py``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import Any

from artemis.config import WORKSPACE_ROOT
from artemis.runtime.device_lock import DeviceExecutionLock

from apps.admin_console.services.app_script_host import load_app_script
from apps.admin_console.services.app_script_maintainer import chat, maintain
from apps.admin_console.services.list_script_worker import Phone, PhoneLocked
from apps.admin_console.services.personal_task_store import PersonalTaskStore
from apps.admin_console.services.prompt_recipe import (
    AI_LIMIT_SECONDS,
    SCRIPT_LIMIT_SECONDS,
    append_catalog_row,
    budget_result,
    mark_row_skip,
    should_drop_row,
)
from apps.admin_console.services.screen_intersect import title_matches
from apps.admin_console.services.task_script import center, find_exact, row_action

DEFAULT_PACKAGE = "com.taobao.idlefish"


class Evidence:
    """UI dumps saved at the moments a run went wrong. The script maintainer reads these."""

    def __init__(self, run_id: str):
        session = (os.environ.get("ARTEMIS_SESSION_ID") or "").strip()
        base = WORKSPACE_ROOT / "traces" / (session or f"personal_{run_id}")
        self.folder = base / "dumps"
        self.stdout_path = base / "stdout.log"
        self.items: list[dict[str, str]] = []

    def save(self, phone: Any, reason: str, note: str = "") -> None:
        xml = str(getattr(phone, "last_xml", "") or "")
        if not xml:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{len(self.items) + 1:02d}_{reason}.xml"
        path.write_text(xml, encoding="utf-8")
        self.items.append({"reason": reason, "note": note, "path": str(path)})

    def collect_notes(self, phone: Any) -> None:
        """Script-side events such as dice that never moved."""
        for tag, text, xml in list(getattr(phone, "notes", []) or []):
            if not xml:
                continue
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self.folder / f"{len(self.items) + 1:02d}_{tag}.xml"
            path.write_text(xml, encoding="utf-8")
            self.items.append({"reason": tag, "note": text, "path": str(path)})
        if hasattr(phone, "notes"):
            phone.notes.clear()

    def stdout_tail(self, limit: int = 6000) -> str:
        if not self.stdout_path.exists():
            return ""
        return self.stdout_path.read_text(encoding="utf-8", errors="replace")[-limit:]


def run_script_subtask(
    phone: Phone, subtask: dict[str, Any], script: Any = None
) -> tuple[str, str, str]:
    """Tap the buttons named by this subtask's script. Stop at 60 seconds."""
    recipe = subtask.get("script") or {}
    script = script or load_app_script(str(recipe.get("package") or DEFAULT_PACKAGE))
    title = str(subtask.get("title") or "")
    start_button = str(recipe.get("start_button") or "")
    done_button = str(recipe.get("done_button") or "")
    labels = tuple(label for label in (start_button, done_button) if label)
    kind = str(recipe.get("kind") or "")
    seconds = int(recipe.get("seconds") or 0)
    hint = recipe.get("done_hint")
    deadline = time.monotonic() + SCRIPT_LIMIT_SECONDS
    stage = "未找到这一行"
    lines = [f"脚本开始，限时 {SCRIPT_LIMIT_SECONDS} 秒。开始按钮「{start_button}」，完成按钮「{done_button}」。"]
    swipes = 0
    page_swipes = 0
    opened = False
    dwell_until = 0.0
    while time.monotonic() < deadline:
        _xml, elements = phone.dump()
        if not script.on_task_list(elements):
            shows = _page_shows_title(elements, title)
            if shows or opened:
                opened = True
                if dwell_until <= 0:
                    dwell_until = min(deadline, time.monotonic() + max(seconds, 0))
                target = _open_page_target(elements, str(hint or ""), done_button)
                if target is not None:
                    label = str(target.get("text") or "")
                    x_pos, y_pos = center(target)
                    phone.tap(x_pos, y_pos)
                    lines.append(f"页面上点了「{label}」")
                    stage = "活动页"
                    if label == done_button:
                        return "completed", f"已点「{done_button}」", "\n".join(lines)
                    continue
                if time.monotonic() < dwell_until:
                    stage = "活动页"
                    if kind == "browse" and page_swipes < 2:
                        phone.swipe_list(toward_top=False)
                        page_swipes += 1
                        lines.append("按页面往下看")
                    else:
                        time.sleep(2.0)
                    continue
            if script.return_to_list(phone):
                lines.append("回到列表")
                stage = "回到列表"
                continue
            stage = "没有回到列表"
            lines.append(stage)
            if not opened:
                break
            continue
        action = row_action(elements, title, labels)
        if action is None:
            stage = "未找到这一行"
            lines.append(stage)
            if swipes >= 4:
                stage = "滑过 4 屏仍没有这一行"
                lines.append(stage)
                break
            phone.swipe_list(toward_top=False)
            swipes += 1
            continue
        label, (x_pos, y_pos) = action
        if label == done_button:
            phone.tap(x_pos, y_pos)
            lines.append(f"点了「{done_button}」")
            return "completed", f"已点「{done_button}」", "\n".join(lines)
        if label == start_button and kind in ("claim_only", "skip"):
            lines.append(f"按钮仍是「{start_button}」，按提示词不进入")
            return "skipped", f"按钮仍是「{start_button}」，按提示词跳过", "\n".join(lines)
        if label == start_button:
            phone.tap(x_pos, y_pos)
            lines.append(f"点了「{start_button}」")
            opened = True
            dwell_until = min(deadline, time.monotonic() + max(seconds, 0))
            stage = "活动页"
            _dwell(phone, seconds, hint, deadline, lines)
            if not _back_until_row(phone, script, title, labels, deadline, lines):
                stage = "活动页"
                continue
            stage = "回到列表"
            continue
    _xml, elements = phone.dump()
    if done_button and script.on_task_list(elements):
        action = row_action(elements, title, (done_button,))
        if action is not None:
            phone.tap(action[1][0], action[1][1])
            lines.append(f"到时后仍点了「{done_button}」")
            return "completed", f"已点「{done_button}」", "\n".join(lines)
    _status, reason = budget_result(
        elapsed=SCRIPT_LIMIT_SECONDS,
        limit=SCRIPT_LIMIT_SECONDS,
        done=False,
        stage=stage,
        done_button=done_button or "完成按钮",
    )
    lines.append(reason)
    return "failed", reason, "\n".join(lines)


def run_model_subtask(subtask: dict[str, Any], serial: str) -> tuple[str, str, str]:
    """Run Flash for this subtask and stop it at 120 seconds."""
    title = str(subtask.get("title") or "").strip()
    detail = str(subtask.get("detail") or "").strip()
    if title:
        prompt = (
            f"完成「{title}」。{detail}\n"
            "先看当前页面上的文字和按钮。已经在这个任务的页面上，就按页面内容做完，"
            "不要因为现在不是任务列表就结束。做完再回到任务奖励列表。"
            "不要掷骰子，不要打开签到规则或玩法说明。"
        )
    else:
        prompt = str(subtask.get("prompt") or "")
    cmd = [
        sys.executable,
        "-m",
        "artemis.main",
        prompt,
        "--profile",
        "flash",
        "--standalone",
    ]
    if serial:
        cmd.extend(["--device-serial", serial])
    env = os.environ.copy()
    env["ARTEMIS_DEVICE_LOCK_BORROWED"] = "1"
    env["ARTEMIS_TASK_WORKER"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
    )
    try:
        output, _unused = proc.communicate(timeout=AI_LIMIT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        output, _unused = proc.communicate()
        reason = f"超过 {AI_LIMIT_SECONDS} 秒仍未完成"
        tail = (output or "").strip()
        if tail:
            reason = f"{reason}。最后输出：{tail[-400:]}"
        return "failed", reason, output or reason
    text = output or ""
    if proc.returncode == 0:
        return "completed", "模型执行结束", text
    tail = text.strip()[-400:]
    reason = f"模型未完成，退出码 {proc.returncode}"
    if tail:
        reason = f"{reason}。最后输出：{tail}"
    return "failed", reason, text


def _run_catalog_without_device(store: PersonalTaskStore, task: dict[str, Any], run_id: str) -> None:
    """Record every catalog row when no phone is attached. The parent run still finishes."""
    failed = 0
    for subtask in task["subtasks"]:
        started = time.time()
        if subtask.get("runner") == "script":
            status, reason, log_text = "failed", "没有设备，脚本无法点按", "没有设备"
        else:
            status, reason, log_text = run_model_subtask(subtask, "")
        if status != "completed":
            failed += 1
        store.add_subtask_log(
            run_id=run_id,
            subtask_id=str(subtask["id"]),
            position=int(subtask.get("position") or 0),
            title=str(subtask.get("title") or ""),
            runner=str(subtask.get("runner") or "ai"),
            status=status,
            reason=reason,
            log_text=log_text,
            started_at=started,
            finished_at=time.time(),
        )
    summary = f"{len(task['subtasks']) - failed} 条完成，{failed} 条未完成"
    store.finish_run(run_id, "completed" if failed == 0 else "partial", summary)


def _task_package(task: dict[str, Any]) -> str:
    for subtask in task["subtasks"]:
        script = subtask.get("script") or {}
        if script.get("package"):
            return str(script["package"])
    return DEFAULT_PACKAGE


def execute(store: PersonalTaskStore, task_id: str, run_id: str, serial: str) -> str:
    """Walk the on-screen task list. The saved prompt is a catalog, not an order."""
    task = store.get_task(task_id)
    if not serial:
        _run_catalog_without_device(store, task, run_id)
        saved = store.get_run(run_id)
        return str(saved.get("status") or "failed")
    package = _task_package(task)
    script = load_app_script(package, store)
    print(f"App 脚本 {package} 第 {script.SCRIPT_VERSION} 版", flush=True)
    evidence = Evidence(run_id)
    report: dict[str, Any] = {
        "entered": False,
        "completed": 0,
        "failed": 0,
        "script_failures": [],
        "stopped_with_pending": [],
    }
    phone = Phone(serial)
    phone.keep_awake()
    print(f"打开 {package}", flush=True)
    phone.launch(package)
    if not script.enter_list(phone):
        evidence.save(phone, "not_on_list")
        print("还没进入任务奖励列表，按当前页面继续做", flush=True)
    else:
        report["entered"] = True
        script.roll_dice(phone, leave_sheet=True)
    catalog = list(task["subtasks"])
    done: set[str] = set()
    outcomes: list[dict[str, Any]] = []
    scrolls = 0
    stop_checks = 0
    page_model_calls = 0
    for _step in range(40):
        script.claim_visible(phone)
        script.roll_dice(phone, leave_sheet=False)
        _xml, elements = phone.dump()
        on_list = bool(script.on_task_list(elements))
        if on_list:
            report["entered"] = True
            page_model_calls = 0
        lines = _screen_lines(elements)
        visible = list(script.row_titles(elements)) if on_list else []
        if not on_list:
            hit = _pending_on_page(_pending_titles(catalog, done), lines)
            if hit:
                visible = [hit]
        for name in visible:
            if name not in done and script.is_skip_row(name) and _catalog_hit(name, catalog) is None:
                done.add(name)
                print(f"[小任务] {name} 按脚本规则跳过", flush=True)
        picked = _pick_row(visible, catalog, done)
        if picked is None:
            pending = _pending_titles(catalog, done)
            action = _when_nothing_picked(
                on_list=on_list,
                pending=pending,
                lines=lines,
                scrolls=scrolls,
                stop_checks=stop_checks,
            )
            if action == "done":
                print("收录里没标跳过的条目都处理过了", flush=True)
                break
            if action == "work-page":
                evidence.save(phone, "off_list", "、".join(pending[:8]))
                target = _open_page_target(elements, "", "")
                if target is not None:
                    x_pos, y_pos = center(target)
                    phone.tap(x_pos, y_pos)
                    print(f"页面上点了「{target.get('text')}」", flush=True)
                    continue
                if script.return_to_list(phone):
                    scrolls = 0
                    continue
                _xml, elements = phone.dump()
                if script.on_task_list(elements):
                    continue
                hit = _pending_on_page(pending, _screen_lines(elements))
                if hit:
                    picked = (hit, _catalog_hit(hit, catalog))
                elif page_model_calls < 4:
                    page_model_calls += 1
                    _finish_open_page(pending, _screen_lines(elements), serial)
                    continue
                else:
                    print("还不是列表，再按页面进一次", flush=True)
                    script.enter_list(phone)
                    continue
            elif action == "scroll":
                phone.swipe_list(toward_top=False)
                scrolls += 1
                continue
            elif action == "rewind":
                scrolls = 0
                phone.swipe_list(toward_top=True)
                continue
            elif action == "stop":
                print(f"列表已打开，询问两次后仍没有这些收录，停止：{'、'.join(pending[:8])}", flush=True)
                report["stopped_with_pending"] = pending
                break
            else:
                evidence.save(phone, "stop_check", "、".join(pending[:8]))
                stop_checks += 1
                verdict = _judge_stop(lines, pending)
                print(f"未完成且未跳过：{'、'.join(pending[:8])}。模型判断：{verdict}", flush=True)
                if verdict == "继续":
                    scrolls = 0
                    for _ in range(4):
                        phone.swipe_list(toward_top=True)
                    continue
                report["stopped_with_pending"] = pending
                break
        if picked is None:
            continue
        scrolls = 0
        title, match = picked
        done.add(title)
        if match is not None:
            done.add(str(match.get("title") or ""))
        if match is None:
            match = _remember_title(store, task_id, title)
            catalog.append(match)
        started = time.time()
        print(f"[小任务] {title} 开始 runner={match.get('runner')}", flush=True)
        if _is_skipped(match):
            status, reason, log_text = "skipped", "已标记直接跳过", "跳过"
        elif match.get("runner") == "script" and match.get("script"):
            status, reason, log_text = run_script_subtask(phone, match, script)
            if status == "failed":
                evidence.save(phone, "script_failed", title)
                report["script_failures"].append({"title": title, "reason": reason})
        else:
            status, reason, log_text = run_model_subtask(match, serial)
        claimed = script.claim_visible(phone)
        rolled = script.roll_dice(phone, leave_sheet=True)
        if claimed or rolled:
            reason = f"{reason}。领取 {claimed} 次，掷骰子 {rolled} 次"
            log_text = f"{log_text}\n领取奖励 {claimed} 次，掷骰子 {rolled} 次"
        evidence.collect_notes(phone)
        store.add_subtask_log(
            run_id=run_id,
            subtask_id=str(match["id"]),
            position=int(match.get("position") or 0),
            title=str(match.get("title") or title),
            runner=str(match.get("runner") or "ai"),
            status=status,
            reason=reason,
            log_text=log_text,
            started_at=started,
            finished_at=time.time(),
        )
        outcomes.append(
            {
                "subtask_id": str(match["id"]),
                "title": str(match.get("title") or title),
                "status": status,
                "reason": reason,
                "log_text": log_text,
            }
        )
        print(f"[小任务] {title} → {status} {reason}", flush=True)
    evidence.collect_notes(phone)
    pending_left = _pending_titles(catalog, done)
    if pending_left and not report.get("stopped_with_pending"):
        report["stopped_with_pending"] = pending_left
    if not report["entered"] and not outcomes:
        store.finish_run(run_id, "failed", "没有进入任务奖励列表")
        print("没有进入任务奖励列表", flush=True)
        _maintain(store, package, script, run_id, report, evidence)
        return "failed"
    failed = sum(1 for item in outcomes if item["status"] == "failed")
    completed = sum(1 for item in outcomes if item["status"] == "completed")
    report["completed"] = completed
    report["failed"] = failed
    summary = f"{len(outcomes) - failed} 条完成，{failed} 条未完成"
    outcome = "completed" if failed == 0 else "partial"
    store.finish_run(run_id, outcome, summary)
    print(summary, flush=True)
    _evolve_catalog(store, task_id, outcomes)
    _maintain(store, package, script, run_id, report, evidence)
    return outcome


def _maintain(
    store: PersonalTaskStore,
    package: str,
    script: Any,
    run_id: str,
    report: dict[str, Any],
    evidence: Evidence,
) -> None:
    try:
        run = store.get_run(run_id)
        message = maintain(
            store,
            package=package,
            version_id=str(script.SCRIPT_VERSION_ID),
            report=report,
            evidence=evidence.items,
            stdout_tail=evidence.stdout_tail(),
            subtask_logs=run.get("logs") or [],
        )
    except Exception as exc:
        message = f"脚本维护出错：{exc}"
    if message:
        print(message, flush=True)


def _pick_row(
    visible: list[str], catalog: list[dict[str, Any]], done: set[str]
) -> tuple[str, dict[str, Any] | None] | None:
    """Prefer a saved row that is on this screen. Unknown rows come after those."""
    unknown: tuple[str, dict[str, Any] | None] | None = None
    for title in visible:
        if title in done:
            continue
        match = _catalog_hit(title, catalog)
        if match is not None and str(match.get("title") or "") in done:
            done.add(title)
            continue
        if match is not None and _is_skipped(match):
            done.add(title)
            done.add(str(match.get("title") or ""))
            continue
        if match is not None:
            return title, match
        if unknown is None:
            unknown = (title, None)
    return unknown


def _catalog_hit(title: str, catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in catalog:
        name = str(item.get("title") or "")
        if title_matches(name, [title]) or title_matches(title, [name]):
            return item
    return None


def _is_skipped(item: dict[str, Any]) -> bool:
    blob = f"{item.get('prompt') or ''}\n{item.get('detail') or ''}"
    script = item.get("script") or {}
    return "直接跳过" in blob or script.get("kind") == "skip" or int(item.get("fail_count") or 0) >= 3


def _page_shows_title(elements: list[dict[str, Any]], title: str) -> bool:
    """The open page names this task. Short fragments are not enough."""
    name = title.split("。", 1)[0].strip()
    if len(name) < 4:
        return False
    return any(name in str(element.get("text") or "") for element in elements)


def _open_page_target(elements: list[dict[str, Any]], hint: str, done_button: str):
    """A control on the current page that moves this task forward."""
    labels = [hint] if hint else []
    labels.extend(("任务完成", "跳过", "关闭广告", "残忍离开"))
    if done_button:
        labels.append(done_button)
    return find_exact(elements, *labels)


def _pending_on_page(pending: list[str], lines: list[str]) -> str | None:
    """A catalog title written on this page. A shorter line does not count."""
    blob = "\n".join(lines)
    for title in pending:
        name = title.split("。", 1)[0].strip()
        if len(name) >= 4 and name in blob:
            return title
    return None


def _when_nothing_picked(
    *,
    on_list: bool,
    pending: list[str],
    lines: list[str],
    scrolls: int,
    stop_checks: int,
) -> str:
    """What to do when this screen has no row to start.

    Off the list the run keeps going: read the page and finish it.
    Stopping is only for an open list whose remaining names are absent.
    """
    if not pending:
        return "done"
    if not on_list:
        return "work-page"
    if scrolls < 4:
        return "scroll"
    if any(title_matches(name, lines) for name in pending):
        return "rewind"
    if stop_checks >= 2:
        return "stop"
    return "ask"


def _finish_open_page(pending: list[str], lines: list[str], serial: str) -> None:
    """Ask the model to finish whatever this non-list page is, then get back."""
    prompt = (
        "当前屏幕不是闲鱼币任务奖励列表。根据页面上的文字和按钮把眼前这件事做完，"
        "做完回到任务奖励列表。不要因为不是列表就结束，不要重新打开应用。\n"
        f"屏幕文字：{'、'.join(lines[:30]) or '（空）'}\n"
        f"还没做完：{'、'.join(pending[:8])}"
    )
    status, reason, _log_text = run_model_subtask({"title": "", "prompt": prompt}, serial)
    print(f"[页面] {status} {reason}", flush=True)


def _pending_titles(catalog: list[dict[str, Any]], done: set[str]) -> list[str]:
    """Catalog rows this run has not finished and has not marked 直接跳过."""
    pending: list[str] = []
    for item in catalog:
        title = str(item.get("title") or "")
        if not title or title in done or _is_skipped(item):
            continue
        if "进入" in title and "列表" in title:
            continue
        pending.append(title)
    return pending


def _screen_lines(elements: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for element in elements:
        text = str(element.get("text") or "").strip().replace("\n", " ")
        if text and text not in lines:
            lines.append(text)
    return lines[:40]


def _judge_stop(visible: list[str], pending: list[str]) -> str:
    """Ask the model whether the remaining catalog rows are still on screen.

    A missing key or a bad answer means 继续, so a model failure does not end
    a run that still has unfinished rows.
    """
    prompt = (
        "你在看闲鱼币任务奖励列表。只回答一个词：停止 或 继续。\n"
        "停止：任务奖励列表已经打开，并且这些名字在名单里确实没有。\n"
        "继续：列表没打开、还能点赚骰子回去，或者屏幕上还有这些名字。\n"
        f"屏幕文字：{'、'.join(visible) or '（空）'}\n"
        f"未完成且未标跳过：{'、'.join(pending)}"
    )
    try:
        answer = chat(prompt, max_tokens=8, timeout=20)
    except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError) as exc:
        print(f"停止判断失败，继续做未完成的收录：{exc}", flush=True)
        return "继续"
    return "停止" if "停止" in answer and "继续" not in answer else "继续"


def _remember_title(store: PersonalTaskStore, task_id: str, title: str) -> dict[str, Any]:
    task = store.get_task(task_id)
    prompt = append_catalog_row(
        str(task.get("prompt") or ""),
        title,
        "约20秒。按钮是「去完成」就进入后返回，变成「领取奖励」再点一次。",
    )
    updated = store.update_prompt(task_id, prompt, keep_title=True)
    match = _catalog_hit(title, updated["subtasks"]) or updated["subtasks"][-1]
    if match.get("runner") == "script":
        store.force_ai(str(match["id"]))
        match = dict(match)
        match["runner"] = "ai"
        match["script"] = None
    return match


def _evolve_catalog(
    store: PersonalTaskStore, task_id: str, outcomes: list[dict[str, Any]]
) -> None:
    """Turn a stable success into a script, and skip a row after three AI failures."""
    if not outcomes:
        return
    task = store.get_task(task_id)
    by_id = {str(item["id"]): item for item in task["subtasks"]}
    prompt = str(task.get("prompt") or "")
    rewrite = False
    for outcome in outcomes:
        subtask = by_id.get(str(outcome["subtask_id"]))
        if subtask is None:
            continue
        status = str(outcome["status"])
        if status == "completed":
            store.promote_script_if_possible(str(subtask["id"]))
            continue
        if status != "failed":
            continue
        immediate = should_drop_row(status, str(outcome["reason"]), str(outcome["log_text"]))
        count = 3 if immediate else int(subtask.get("fail_count") or 0) + 1
        store.set_fail_count(str(subtask["id"]), count)
        subtask["fail_count"] = count
        if count >= 3:
            prompt = mark_row_skip(prompt, str(subtask["title"]))
            rewrite = True
    if rewrite:
        store.update_prompt(task_id, prompt, keep_title=True)
        print("收录提示词已按本轮日志标成直接跳过", flush=True)


def _dwell(phone: Phone, seconds: int, hint: str | None, deadline: float, lines: list[str]) -> None:
    """Stay on the open page and act on what it shows, until the stay time is up."""
    dwell_until = min(deadline, time.monotonic() + max(seconds, 0))
    while time.monotonic() < dwell_until:
        _xml, elements = phone.dump()
        target = _open_page_target(elements, str(hint or ""), "")
        if target is not None:
            label = str(target.get("text") or "")
            x_pos, y_pos = center(target)
            phone.tap(x_pos, y_pos)
            lines.append(f"页面上点了「{label}」")
            if hint and hint in label:
                return
            continue
        time.sleep(2.0)


def _back_until_row(
    phone: Phone,
    script: Any,
    title: str,
    labels: tuple[str, ...],
    deadline: float,
    lines: list[str],
) -> bool:
    """Close ads on the activity page, then let the App script find the list again."""
    for _ in range(2):
        if time.monotonic() >= deadline:
            return False
        _xml, elements = phone.dump()
        if row_action(elements, title, labels) is not None or script.on_task_list(elements):
            return True
        close = find_exact(elements, "关闭广告", "残忍离开", "关闭")
        if close is None:
            break
        x_pos, y_pos = center(close)
        phone.tap(x_pos, y_pos)
        lines.append("点了关闭")
    if time.monotonic() >= deadline:
        return False
    back_to_list = bool(script.return_to_list(phone))
    lines.append("回到列表" if back_to_list else "没有回到列表")
    return back_to_list


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a collected personal task.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--device-serial", default="")
    args = parser.parse_args(argv)
    serial = (args.device_serial or "").strip()
    store = PersonalTaskStore()
    lock = None
    outcome = "failed"
    if serial:
        lock = DeviceExecutionLock(serial, description="personal task", session_id=args.run_id)
        lock.acquire(timeout=60)
    try:
        outcome = execute(store, args.task_id, args.run_id, serial)
    except PhoneLocked as exc:
        print(str(exc), flush=True)
        store.finish_run(args.run_id, "failed", str(exc))
        outcome = "failed"
    except Exception as exc:
        print(f"个人任务中断：{exc}", flush=True)
        try:
            store.finish_run(args.run_id, "failed", str(exc))
        except Exception:
            pass
        outcome = "failed"
    finally:
        if lock is not None:
            lock.release()
    return 0 if outcome != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
