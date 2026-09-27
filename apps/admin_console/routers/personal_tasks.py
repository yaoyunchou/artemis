"""Collected personal tasks: save a prompt, edit its pieces, and run them."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

try:
    from admin_console.services.personal_task_store import PersonalTaskStore
    from admin_console.services.task_queue_service import task_queue_service
except ImportError:
    from apps.admin_console.services.personal_task_store import PersonalTaskStore
    from apps.admin_console.services.task_queue_service import task_queue_service

router = APIRouter(tags=["personal-tasks"])
_store = PersonalTaskStore()


class PromptBody(BaseModel):
    prompt: str
    title: str | None = None


class RunBody(BaseModel):
    profile: str | None = "flash"
    device_serial: str | None = None


def _missing(exc: KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Not found: {exc}")


@router.get("/api/personal-tasks")
async def list_personal_tasks():
    return {"tasks": _store.list_tasks()}


@router.post("/api/personal-tasks")
async def create_personal_task(body: PromptBody):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required.")
    return _store.create_task(prompt, body.title)


@router.get("/api/personal-tasks/{task_id}")
async def get_personal_task(task_id: str):
    try:
        return _store.get_task(task_id)
    except KeyError as exc:
        raise _missing(exc) from exc


@router.patch("/api/personal-tasks/{task_id}")
async def update_personal_task(task_id: str, body: PromptBody):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required.")
    try:
        return _store.update_prompt(task_id, prompt)
    except KeyError as exc:
        raise _missing(exc) from exc


@router.delete("/api/personal-tasks/{task_id}/subtasks/{subtask_id}")
async def delete_personal_subtask(task_id: str, subtask_id: str):
    try:
        return _store.delete_subtask(task_id, subtask_id)
    except KeyError as exc:
        raise _missing(exc) from exc


@router.patch("/api/personal-tasks/{task_id}/subtasks/{subtask_id}")
async def update_personal_subtask(task_id: str, subtask_id: str, body: PromptBody):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required.")
    try:
        return _store.update_subtask_prompt(task_id, subtask_id, prompt)
    except KeyError as exc:
        raise _missing(exc) from exc


@router.post("/api/personal-tasks/{task_id}/run")
async def run_personal_task(task_id: str, body: RunBody | None = None):
    payload = body or RunBody()
    try:
        task = _store.get_task(task_id)
        run = _store.start_run(task_id)
    except KeyError as exc:
        raise _missing(exc) from exc
    queued = await task_queue_service.enqueue_tasks(
        [f"个人收录：{task['title']}"],
        profile=payload.profile or "flash",
        device_serial=payload.device_serial,
        personal_task_id=task_id,
        personal_run_id=run["id"],
    )
    return {"task": task, "run": run, "queue": queued}


@router.get("/api/personal-tasks/{task_id}/runs")
async def list_personal_runs(task_id: str):
    try:
        _store.get_task(task_id)
    except KeyError as exc:
        raise _missing(exc) from exc
    return {"runs": _store.list_runs(task_id)}
