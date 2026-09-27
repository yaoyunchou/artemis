"""Run one collected task. Subtasks are timed; the parent task is not."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Any

from artemis.runtime.device_lock import DeviceExecutionLock

from apps.admin_console.services.list_script_worker import (
    Phone,
    PhoneLocked,
    claim_visible,
    dice_badge_visible,
    enter_list,
    open_reward_sheet,
    return_to_list,
    roll_dice,
    rows_to_start,
)
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
from apps.admin_console.services.task_script import (
    center,
    dice_left,
    find_exact,
    is_game_row,
    on_task_list,
    row_action,
)


def run_script_subtask(phone: Phone, subtask: dict[str, Any]) -> tuple[str, str, str]:
    """Tap the buttons named by this subtask's script. Stop at 60 seconds."""
    recipe = subtask.get("script") or {}
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
    while time.monotonic() < deadline:
        _xml, elements = phone.dump()
        if not on_task_list(elements):
            if not return_to_list(phone):
                stage = "没有回到列表"
                lines.append(stage)
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
            stage = "活动页"
            _dwell(phone, seconds, hint, deadline, lines)
            if not _back_until_row(phone, title, labels, deadline, lines):
                stage = "没有回到这一行"
                break
            stage = "回到列表"
            continue
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
            f"只做闲鱼币任务列表中的「{title}」。{detail}\n"
            "做完回到任务奖励列表。不要掷骰子，不要打开签到规则或玩法说明。"
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


def execute(store: PersonalTaskStore, task_id: str, run_id: str, serial: str) -> str:
    """Walk the on-screen task list. The saved prompt is a catalog, not an order."""
    task = store.get_task(task_id)
    if not serial:
        _run_catalog_without_device(store, task, run_id)
        saved = store.get_run(run_id)
        return str(saved.get("status") or "failed")
    package = ""
    for subtask in task["subtasks"]:
        script = subtask.get("script") or {}
        if script.get("package"):
            package = str(script["package"])
            break
    phone = Phone(serial)
    phone.keep_awake()
    if package:
        print(f"打开 {package}", flush=True)
        phone.launch(package)
    if not enter_list(phone, package or None):
        store.finish_run(run_id, "failed", "没有进入任务奖励列表")
        print("没有进入任务奖励列表", flush=True)
        return "failed"
    rolled = roll_dice(phone, limit=20, leave_sheet=True)
    if rolled:
        print(f"按剩余次数掷骰子 {rolled} 次", flush=True)
    catalog = list(task["subtasks"])
    done: set[str] = set()
    outcomes: list[dict[str, Any]] = []
    scrolls = 0
    stop_checks = 0
    for _step in range(40):
        claim_visible(phone)
        _xml, elements = phone.dump()
        if not on_task_list(elements) and dice_left(elements):
            roll_dice(phone, leave_sheet=False)
            _xml, elements = phone.dump()
        visible = rows_to_start(elements)
        for name in visible:
            if name not in done and is_game_row(name) and _catalog_hit(name, catalog) is None:
                done.add(name)
                print(f"[小任务] {name} 要玩一局小游戏，按提示词跳过", flush=True)
        picked = _pick_row(visible, catalog, done)
        if picked is None:
            if not on_task_list(elements) and (return_to_list(phone) or enter_list(phone, package or None)):
                scrolls = 0
                continue
            pending = _pending_titles(catalog, done)
            if not pending:
                print("收录里没标跳过的条目都处理过了", flush=True)
                break
            if scrolls < 4 and on_task_list(elements):
                phone.swipe_list(toward_top=False)
                scrolls += 1
                continue
            still_visible = [
                name for name in pending if title_matches(name, _screen_lines(elements))
            ]
            if still_visible and on_task_list(elements):
                scrolls = 0
                phone.swipe_list(toward_top=True)
                continue
            if stop_checks >= 2:
                print(f"还有未完成收录，已询问两次，停止：{'、'.join(pending[:8])}", flush=True)
                break
            stop_checks += 1
            verdict = _judge_stop(_screen_lines(elements), pending)
            print(f"未完成且未跳过：{'、'.join(pending[:8])}。模型判断：{verdict}", flush=True)
            if verdict == "继续":
                scrolls = 0
                if not on_task_list(elements):
                    enter_list(phone, package or None)
                else:
                    for _ in range(4):
                        phone.swipe_list(toward_top=True)
                continue
            break
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
            status, reason, log_text = run_script_subtask(phone, match)
        else:
            status, reason, log_text = run_model_subtask(match, serial)
        claimed = claim_visible(phone)
        rolled = roll_dice(phone, leave_sheet=True)
        if claimed or rolled:
            reason = f"{reason}。领取 {claimed} 次，掷骰子 {rolled} 次"
            log_text = f"{log_text}\n领取奖励 {claimed} 次，掷骰子 {rolled} 次"
        finished = time.time()
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
            finished_at=finished,
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
    failed = sum(1 for item in outcomes if item["status"] == "failed")
    summary = f"{len(outcomes) - failed} 条完成，{failed} 条未完成"
    outcome = "completed" if failed == 0 else "partial"
    store.finish_run(run_id, outcome, summary)
    print(summary, flush=True)
    _evolve_catalog(store, task_id, outcomes)
    return outcome


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
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = (os.environ.get("ARTEMIS_MODEL") or "qwen3.8-flash").strip()
    if not key:
        print("没有模型密钥，未完成的收录继续做", flush=True)
        return "继续"
    prompt = (
        "你在看闲鱼币任务奖励列表。只回答一个词：停止 或 继续。\n"
        "停止：任务奖励列表已经打开，并且这些名字在名单里确实没有。\n"
        "继续：列表没打开、还能点赚骰子回去，或者屏幕上还有这些名字。\n"
        f"屏幕文字：{'、'.join(visible) or '（空）'}\n"
        f"未完成且未标跳过：{'、'.join(pending)}"
    )
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        answer = str(payload["choices"][0]["message"]["content"])
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
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
    dwell_until = min(deadline, time.monotonic() + max(seconds, 0))
    while time.monotonic() < dwell_until:
        _xml, elements = phone.dump()
        if hint and any(hint in str(element.get("text") or "") for element in elements):
            lines.append(f"出现「{hint}」")
            return
        time.sleep(2.0)


def _back_until_row(
    phone: Phone,
    title: str,
    labels: tuple[str, ...],
    deadline: float,
    lines: list[str],
) -> bool:
    for _ in range(5):
        if time.monotonic() >= deadline:
            return False
        _xml, elements = phone.dump()
        if row_action(elements, title, labels) is not None:
            return True
        if on_task_list(elements):
            return True
        if dice_badge_visible(elements):
            lines.append("回到闲鱼币页，点赚骰子打开列表")
            open_reward_sheet(phone)
            continue
        close = find_exact(elements, "关闭广告", "残忍离开", "关闭")
        if close is not None:
            x_pos, y_pos = center(close)
            phone.tap(x_pos, y_pos)
            lines.append("点了关闭")
            continue
        phone.back()
        lines.append("返回")
    _xml, elements = phone.dump()
    return on_task_list(elements)


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
