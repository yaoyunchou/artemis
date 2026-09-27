"""SQLite store for collected personal tasks, their subtasks, and run logs."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

from apps.admin_console.services.prompt_recipe import compile_recipe, split_saved_prompt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS personal_subtasks (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    detail TEXT NOT NULL,
    runner TEXT NOT NULL,
    script_json TEXT,
    edited INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS personal_task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS personal_subtask_logs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    subtask_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    runner TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    log_text TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL
);
"""


class PersonalTaskStore:
    """Collected tasks. A parent row has no timeout column."""

    def __init__(self, db_path=None):
        self.db_path = db_path
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with db_session(self.db_path) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def create_task(self, prompt: str, title: str | None = None) -> dict[str, Any]:
        now = time.time()
        task_id = uuid.uuid4().hex
        subtasks = split_saved_prompt(prompt)
        shown = (title or "").strip() or (subtasks[0]["title"] if subtasks else "未命名任务")
        with db_session(self.db_path) as conn:
            conn.execute(
                "INSERT INTO personal_tasks (id, title, prompt, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, shown, prompt, now, now),
            )
            self._insert_subtasks(conn, task_id, subtasks)
            conn.commit()
        return self.get_task(task_id)

    def list_tasks(self) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM personal_tasks ORDER BY updated_at DESC"
            ).fetchall()
        return [self.get_task(str(row["id"])) for row in rows]

    def get_task(self, task_id: str) -> dict[str, Any]:
        with db_session(self.db_path) as conn:
            row = conn.execute("SELECT * FROM personal_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            sub_rows = conn.execute(
                "SELECT * FROM personal_subtasks WHERE task_id = ? ORDER BY position ASC",
                (task_id,),
            ).fetchall()
        task = _task_dict(row)
        task["subtasks"] = [_subtask_dict(item) for item in sub_rows]
        return task

    def update_prompt(self, task_id: str, prompt: str) -> dict[str, Any]:
        current = self.get_task(task_id)
        fresh = split_saved_prompt(prompt)
        kept = {
            str(item["title"]): item
            for item in current["subtasks"]
            if item.get("edited")
        }
        merged: list[dict[str, Any]] = []
        for item in fresh:
            previous = kept.get(item["title"])
            if previous is None:
                merged.append(item)
                continue
            merged.append(
                {
                    "title": previous["title"],
                    "prompt": previous["prompt"],
                    "detail": previous["detail"],
                    "runner": previous["runner"],
                    "script": previous["script"],
                    "edited": 1,
                }
            )
        now = time.time()
        with db_session(self.db_path) as conn:
            conn.execute(
                "UPDATE personal_tasks SET prompt = ?, title = ?, updated_at = ? WHERE id = ?",
                (prompt, fresh[0]["title"] if fresh else current["title"], now, task_id),
            )
            conn.execute("DELETE FROM personal_subtasks WHERE task_id = ?", (task_id,))
            self._insert_subtasks(conn, task_id, merged)
            conn.commit()
        return self.get_task(task_id)

    def update_subtask_prompt(self, task_id: str, subtask_id: str, prompt: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        match = next((item for item in task["subtasks"] if item["id"] == subtask_id), None)
        if match is None:
            raise KeyError(subtask_id)
        title = match["title"]
        recipe = compile_recipe(title, prompt, source=prompt)
        now = time.time()
        with db_session(self.db_path) as conn:
            conn.execute(
                """
                UPDATE personal_subtasks
                SET prompt = ?, detail = ?, runner = ?, script_json = ?, edited = 1
                WHERE id = ? AND task_id = ?
                """,
                (
                    prompt,
                    prompt,
                    "script" if recipe else "ai",
                    json.dumps(recipe, ensure_ascii=False) if recipe else None,
                    subtask_id,
                    task_id,
                ),
            )
            conn.execute("UPDATE personal_tasks SET updated_at = ? WHERE id = ?", (now, task_id))
            conn.commit()
        return self.get_task(task_id)

    def promote_script_if_possible(self, subtask_id: str) -> None:
        """After a finished model run, keep a script only when the prompt compiles."""
        with db_session(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM personal_subtasks WHERE id = ?", (subtask_id,)
            ).fetchone()
            if row is None or row["runner"] == "script":
                return
            recipe = compile_recipe(str(row["title"]), str(row["detail"]), source=str(row["prompt"]))
            if recipe is None:
                return
            conn.execute(
                "UPDATE personal_subtasks SET runner = 'script', script_json = ? WHERE id = ?",
                (json.dumps(recipe, ensure_ascii=False), subtask_id),
            )
            conn.commit()

    def start_run(self, task_id: str) -> dict[str, Any]:
        self.get_task(task_id)
        run_id = uuid.uuid4().hex
        now = time.time()
        with db_session(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO personal_task_runs (id, task_id, status, started_at, finished_at, summary)
                VALUES (?, ?, 'running', ?, NULL, '')
                """,
                (run_id, task_id, now),
            )
            conn.commit()
        return self.get_run(run_id)

    def finish_run(self, run_id: str, status: str, summary: str) -> None:
        with db_session(self.db_path) as conn:
            conn.execute(
                """
                UPDATE personal_task_runs
                SET status = ?, summary = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, summary, time.time(), run_id),
            )
            conn.commit()

    def add_subtask_log(
        self,
        *,
        run_id: str,
        subtask_id: str,
        position: int,
        title: str,
        runner: str,
        status: str,
        reason: str,
        log_text: str,
        started_at: float,
        finished_at: float,
    ) -> None:
        with db_session(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO personal_subtask_logs (
                    id, run_id, subtask_id, position, title, runner, status, reason,
                    log_text, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    run_id,
                    subtask_id,
                    position,
                    title,
                    runner,
                    status,
                    reason,
                    log_text,
                    started_at,
                    finished_at,
                ),
            )
            conn.commit()

    def list_runs(self, task_id: str) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM personal_task_runs WHERE task_id = ? ORDER BY started_at DESC",
                (task_id,),
            ).fetchall()
        return [self.get_run(str(row["id"])) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with db_session(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM personal_task_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            logs = conn.execute(
                "SELECT * FROM personal_subtask_logs WHERE run_id = ? ORDER BY position ASC",
                (run_id,),
            ).fetchall()
        payload = dict(row)
        payload["logs"] = [dict(item) for item in logs]
        return payload

    def _insert_subtasks(self, conn, task_id: str, subtasks: list[dict[str, Any]]) -> None:
        for position, item in enumerate(subtasks):
            script = item.get("script")
            conn.execute(
                """
                INSERT INTO personal_subtasks (
                    id, task_id, position, title, prompt, detail, runner, script_json, edited
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    task_id,
                    position,
                    item["title"],
                    item["prompt"],
                    item.get("detail") or item["prompt"],
                    item["runner"],
                    json.dumps(script, ensure_ascii=False) if script else None,
                    int(item.get("edited") or 0),
                ),
            )


def _task_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "prompt": row["prompt"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _subtask_dict(row) -> dict[str, Any]:
    script = None
    if row["script_json"]:
        script = json.loads(row["script_json"])
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "position": row["position"],
        "title": row["title"],
        "prompt": row["prompt"],
        "detail": row["detail"],
        "runner": row["runner"],
        "script": script,
        "edited": bool(row["edited"]),
    }
