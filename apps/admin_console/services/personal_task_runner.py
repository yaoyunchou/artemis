"""Run one collected task. Subtasks are timed; the parent task is not."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from typing import Any

from artemis.runtime.device_lock import DeviceExecutionLock

from apps.admin_console.services.list_script_worker import Phone
from apps.admin_console.services.personal_task_store import PersonalTaskStore
from apps.admin_console.services.prompt_recipe import AI_LIMIT_SECONDS, SCRIPT_LIMIT_SECONDS, budget_result
from apps.admin_console.services.task_script import find_exact, row_action


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
    while time.monotonic() < deadline:
        _xml, elements = phone.dump()
        action = row_action(elements, title, labels)
        if action is None:
            stage = "未找到这一行"
            lines.append(stage)
            phone.swipe_list(toward_top=False)
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
    prompt = str(subtask.get("prompt") or subtask.get("title") or "")
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


def execute(store: PersonalTaskStore, task_id: str, run_id: str, serial: str) -> None:
    task = store.get_task(task_id)
    phone: Phone | None = None
    package = ""
    for subtask in task["subtasks"]:
        script = subtask.get("script") or {}
        if script.get("package"):
            package = str(script["package"])
            break
    if serial:
        phone = Phone(serial)
        if package:
            print(f"打开 {package}", flush=True)
            phone.launch(package)
    failed = 0
    for subtask in task["subtasks"]:
        started = time.time()
        title = str(subtask["title"])
        print(f"[小任务] {title} 开始 runner={subtask['runner']}", flush=True)
        if subtask.get("runner") == "script" and subtask.get("script") and phone is not None:
            status, reason, log_text = run_script_subtask(phone, subtask)
        elif subtask.get("runner") == "script":
            status, reason, log_text = "failed", "没有设备，脚本无法点按", "没有设备"
        else:
            status, reason, log_text = run_model_subtask(subtask, serial)
            if status == "completed":
                store.promote_script_if_possible(str(subtask["id"]))
        if status != "completed":
            failed += 1
        finished = time.time()
        store.add_subtask_log(
            run_id=run_id,
            subtask_id=str(subtask["id"]),
            position=int(subtask["position"]),
            title=title,
            runner=str(subtask["runner"]),
            status=status,
            reason=reason,
            log_text=log_text,
            started_at=started,
            finished_at=finished,
        )
        print(f"[小任务] {title} → {status} {reason}", flush=True)
    summary = f"{len(task['subtasks']) - failed} 条完成，{failed} 条未完成"
    store.finish_run(run_id, "completed" if failed == 0 else "partial", summary)
    print(summary, flush=True)


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
    for _ in range(4):
        if time.monotonic() >= deadline:
            return False
        _xml, elements = phone.dump()
        if row_action(elements, title, labels) is not None:
            return True
        close = find_exact(elements, "关闭广告", "残忍离开", "关闭")
        if close is not None:
            from apps.admin_console.services.task_script import center

            x_pos, y_pos = center(close)
            phone.tap(x_pos, y_pos)
            lines.append("点了关闭")
            continue
        phone.back()
        lines.append("返回")
    _xml, elements = phone.dump()
    return row_action(elements, title, labels) is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a collected personal task.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--device-serial", default="")
    args = parser.parse_args(argv)
    serial = (args.device_serial or "").strip()
    store = PersonalTaskStore()
    lock = None
    if serial:
        lock = DeviceExecutionLock(serial, description="personal task", session_id=args.run_id)
        lock.acquire(timeout=60)
    try:
        execute(store, args.task_id, args.run_id, serial)
    except Exception as exc:
        print(f"个人任务中断：{exc}", flush=True)
        try:
            store.finish_run(args.run_id, "failed", str(exc))
        except Exception:
            pass
    finally:
        if lock is not None:
            lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
