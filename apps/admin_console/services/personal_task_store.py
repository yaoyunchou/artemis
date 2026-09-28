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
    edited INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS app_script_versions (
    id TEXT PRIMARY KEY,
    package TEXT NOT NULL,
    version INTEGER NOT NULL,
    code TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    parent_id TEXT,
    created_at REAL NOT NULL,
    run_count INTEGER NOT NULL DEFAULT 0,
    last_completed INTEGER,
    last_failed INTEGER,
    last_entered INTEGER,
    last_run_at REAL
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
            columns = {row[1] for row in conn.execute("PRAGMA table_info(personal_subtasks)")}
            if "fail_count" not in columns:
                conn.execute(
                    "ALTER TABLE personal_subtasks ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0"
                )
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
            latest = _latest_logs(conn, [str(item["id"]) for item in sub_rows])
        task = _task_dict(row)
        task["subtasks"] = [_subtask_dict(item, latest.get(str(item["id"]))) for item in sub_rows]
        return task

    def update_prompt(self, task_id: str, prompt: str, *, keep_title: bool = False) -> dict[str, Any]:
        current = self.get_task(task_id)
        fresh = split_saved_prompt(prompt)
        kept = {
            str(item["title"]): item
            for item in current["subtasks"]
            if item.get("edited")
        }
        counts = {
            str(item["title"]): int(item.get("fail_count") or 0) for item in current["subtasks"]
        }
        merged: list[dict[str, Any]] = []
        for item in fresh:
            item["fail_count"] = counts.get(item["title"], int(item.get("fail_count") or 0))
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
                    "fail_count": int(previous.get("fail_count") or 0),
                }
            )
        now = time.time()
        with db_session(self.db_path) as conn:
            conn.execute(
                "UPDATE personal_tasks SET prompt = ?, title = ?, updated_at = ? WHERE id = ?",
                (
                    prompt,
                    current["title"] if keep_title else (fresh[0]["title"] if fresh else current["title"]),
                    now,
                    task_id,
                ),
            )
            conn.execute("DELETE FROM personal_subtasks WHERE task_id = ?", (task_id,))
            self._insert_subtasks(conn, task_id, merged)
            conn.commit()
        return self.get_task(task_id)

    def delete_subtask(self, task_id: str, subtask_id: str) -> dict[str, Any]:
        self.get_task(task_id)
        now = time.time()
        with db_session(self.db_path) as conn:
            removed = conn.execute(
                "DELETE FROM personal_subtasks WHERE id = ? AND task_id = ?",
                (subtask_id, task_id),
            ).rowcount
            if not removed:
                raise KeyError(subtask_id)
            rows = conn.execute(
                "SELECT id FROM personal_subtasks WHERE task_id = ? ORDER BY position ASC",
                (task_id,),
            ).fetchall()
            for position, item in enumerate(rows):
                conn.execute(
                    "UPDATE personal_subtasks SET position = ? WHERE id = ?",
                    (position, item["id"]),
                )
            conn.execute("UPDATE personal_tasks SET updated_at = ? WHERE id = ?", (now, task_id))
            conn.commit()
        return self.get_task(task_id)

    def force_ai(self, subtask_id: str) -> None:
        """A newly seen row is tried by the model once before it can become a script."""
        with db_session(self.db_path) as conn:
            conn.execute(
                "UPDATE personal_subtasks SET runner = 'ai', script_json = NULL WHERE id = ?",
                (subtask_id,),
            )
            conn.commit()

    def set_fail_count(self, subtask_id: str, fail_count: int) -> None:
        with db_session(self.db_path) as conn:
            conn.execute(
                "UPDATE personal_subtasks SET fail_count = ? WHERE id = ?",
                (fail_count, subtask_id),
            )
            conn.commit()

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

    # ------------------------------------------------------------ App scripts
    # status: current (the one that runs), retired, rejected (failed checks),
    # rolled_back (ran worse than its parent).

    def current_script(self, package: str) -> dict[str, Any] | None:
        with db_session(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM app_script_versions WHERE package = ? AND status = 'current' "
                "ORDER BY version DESC LIMIT 1",
                (package,),
            ).fetchone()
        return dict(row) if row else None

    def list_script_versions(self, package: str) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM app_script_versions WHERE package = ? ORDER BY version DESC",
                (package,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_script_version(self, version_id: str) -> dict[str, Any]:
        with db_session(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM app_script_versions WHERE id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise KeyError(version_id)
        return dict(row)

    def add_script_version(
        self,
        package: str,
        code: str,
        reason: str,
        *,
        status: str,
        parent_id: str | None,
    ) -> dict[str, Any]:
        version_id = uuid.uuid4().hex
        with db_session(self.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS top FROM app_script_versions WHERE package = ?",
                (package,),
            ).fetchone()
            version = int(row["top"]) + 1
            if status == "current":
                conn.execute(
                    "UPDATE app_script_versions SET status = 'retired' "
                    "WHERE package = ? AND status = 'current'",
                    (package,),
                )
            conn.execute(
                """
                INSERT INTO app_script_versions (
                    id, package, version, code, reason, status, parent_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (version_id, package, version, code, reason, status, parent_id, time.time()),
            )
            conn.commit()
        return self.get_script_version(version_id)

    def set_current_script(self, version_id: str, *, previous_status: str = "retired") -> dict[str, Any]:
        target = self.get_script_version(version_id)
        with db_session(self.db_path) as conn:
            conn.execute(
                "UPDATE app_script_versions SET status = ? WHERE package = ? AND status = 'current'",
                (previous_status, target["package"]),
            )
            conn.execute(
                "UPDATE app_script_versions SET status = 'current' WHERE id = ?", (version_id,)
            )
            conn.commit()
        return self.get_script_version(version_id)

    def mark_script(self, version_id: str, status: str, reason: str | None = None) -> None:
        with db_session(self.db_path) as conn:
            if reason is None:
                conn.execute(
                    "UPDATE app_script_versions SET status = ? WHERE id = ?", (status, version_id)
                )
            else:
                conn.execute(
                    "UPDATE app_script_versions SET status = ?, reason = ? WHERE id = ?",
                    (status, reason, version_id),
                )
            conn.commit()

    def record_script_result(
        self, version_id: str, *, completed: int, failed: int, entered: bool
    ) -> dict[str, Any]:
        with db_session(self.db_path) as conn:
            conn.execute(
                """
                UPDATE app_script_versions
                SET run_count = run_count + 1, last_completed = ?, last_failed = ?,
                    last_entered = ?, last_run_at = ?
                WHERE id = ?
                """,
                (completed, failed, 1 if entered else 0, time.time(), version_id),
            )
            conn.commit()
        return self.get_script_version(version_id)

    def _insert_subtasks(self, conn, task_id: str, subtasks: list[dict[str, Any]]) -> None:
        for position, item in enumerate(subtasks):
            script = item.get("script")
            conn.execute(
                """
                INSERT INTO personal_subtasks (
                    id, task_id, position, title, prompt, detail, runner, script_json, edited, fail_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    int(item.get("fail_count") or 0),
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


def _latest_logs(conn, subtask_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not subtask_ids:
        return {}
    marks = ",".join("?" for _ in subtask_ids)
    rows = conn.execute(
        f"""
        SELECT subtask_id, status, reason, finished_at
        FROM personal_subtask_logs
        WHERE subtask_id IN ({marks})
        ORDER BY finished_at DESC
        """,
        subtask_ids,
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        subtask_id = str(row["subtask_id"])
        if subtask_id not in latest:
            latest[subtask_id] = dict(row)
    return latest


def _subtask_dict(row, last: dict[str, Any] | None = None) -> dict[str, Any]:
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
        "fail_count": int(row["fail_count"]) if "fail_count" in row.keys() else 0,
        "last_status": None if last is None else last.get("status"),
        "last_reason": None if last is None else last.get("reason"),
        "last_finished_at": None if last is None else last.get("finished_at"),
    }
