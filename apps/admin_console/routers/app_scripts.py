"""App scripts that the runner loads per package, and their AI-written versions."""

from __future__ import annotations

import difflib

from fastapi import APIRouter, HTTPException

try:
    from admin_console.services.app_script_host import (
        ScriptRejected,
        load_app_script,
        load_module,
        write_current_file,
    )
    from admin_console.services.personal_task_store import PersonalTaskStore
except ImportError:
    from apps.admin_console.services.app_script_host import (
        ScriptRejected,
        load_app_script,
        load_module,
        write_current_file,
    )
    from apps.admin_console.services.personal_task_store import PersonalTaskStore

router = APIRouter(tags=["app-scripts"])
_store = PersonalTaskStore()


def _summary(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "code"}


@router.get("/api/app-scripts/{package}")
async def get_app_script(package: str):
    try:
        load_app_script(package, _store)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    versions = _store.list_script_versions(package)
    current = next((item for item in versions if item["status"] == "current"), None)
    return {
        "package": package,
        "current": _summary(current) if current else None,
        "versions": [_summary(item) for item in versions],
    }


@router.get("/api/app-scripts/{package}/versions/{version_id}")
async def get_app_script_version(package: str, version_id: str):
    try:
        item = _store.get_script_version(version_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Not found: {exc}") from exc
    if item["package"] != package:
        raise HTTPException(status_code=404, detail="Version belongs to another package.")
    parent_code = ""
    if item.get("parent_id"):
        try:
            parent_code = str(_store.get_script_version(str(item["parent_id"]))["code"])
        except KeyError:
            parent_code = ""
    diff = "\n".join(
        difflib.unified_diff(
            parent_code.splitlines(),
            str(item["code"]).splitlines(),
            fromfile="上一版",
            tofile=f"第 {item['version']} 版",
            lineterm="",
        )
    )
    return {**item, "diff": diff}


@router.post("/api/app-scripts/{package}/rollback/{version_id}")
async def rollback_app_script(package: str, version_id: str):
    try:
        item = _store.get_script_version(version_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Not found: {exc}") from exc
    if item["package"] != package:
        raise HTTPException(status_code=404, detail="Version belongs to another package.")
    try:
        load_module(str(item["code"]), package)
    except ScriptRejected as exc:
        raise HTTPException(status_code=400, detail=f"这一版加载不了：{exc}") from exc
    _store.set_current_script(version_id)
    write_current_file(package, str(item["code"]))
    return await get_app_script(package)
