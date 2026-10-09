"""Append hands actions to the day's jsonl and keep the open row's own slice.

The agent reloads MCP after a code change, so the open row is mirrored to
``.session.json``. A row left open by a dead process is closed as a learning
debt on restore -- never left to block the next ``begin_row``.

Learning is a debt, not a lock: ``finish`` always closes the row so a single
unlearnable row cannot stall the rest of the run.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class RowBusy(RuntimeError):
    """begin_row was called while another row is still open."""

    def __init__(self, title: str) -> None:
        self.title = title
        super().__init__(f"上一行「{title}」还没 end_row")


class HandsTrace:
    """One process: a daily log plus at most one row being learned."""

    def __init__(self, root: Path, device: str = "", run_id: str = "") -> None:
        self.root = root
        self.device = device.strip()
        self.run_id = run_id or uuid.uuid4().hex[:8]
        #: Counts ``begin_run`` calls within this process. Starts at 0 and only
        #: ever grows; every later record stamps the current value so actions can
        #: be sliced by run even when the process itself never restarts.
        self.run_seq: int = 0
        self.started = datetime.now().isoformat(timespec="seconds")
        self.title: str | None = None
        self.events: list[dict[str, Any]] = []
        self.before: dict[str, Any] | None = None
        self.completed: list[str] = []
        self.pending_learn: list[str] = []
        self.recovered: list[str] = []
        # The MCP runtime builds the trace before a device is chosen, so a
        # device-less instance has no log to recover from yet. ``bind`` does it.
        if self.device:
            self.restore()

    def bind(self, device: str) -> None:
        """Point the trace at a device and adopt any row the last process left open.

        The runtime locks a device only after this object exists, so the
        recovery scan has to happen here -- doing it in ``__init__`` looks under
        ``logs/hands/unknown/`` and silently finds nothing.
        """
        chosen = device.strip()
        if chosen == self.device:
            return
        if self.title:
            self.finish(learned=False, reason="切换设备，上一行未收尾")
        self.device = chosen
        self.completed = []
        self.pending_learn = []
        self.recovered = []
        self.before = None
        self.restore()

    # ---------------------------------------------------------------- paths

    def path_for(self, when: datetime | None = None) -> Path:
        day = (when or datetime.now()).strftime("%Y%m%d")
        device = self.device or "unknown"
        return self.root / "logs" / "hands" / device / f"{day}.jsonl"

    def session_path(self) -> Path:
        device = self.device or "unknown"
        return self.root / "logs" / "hands" / device / ".session.json"

    # ---------------------------------------------------------------- state

    def snapshot(self) -> dict[str, Any]:
        return {
            "date": datetime.now().strftime("%Y%m%d"),
            "device": self.device,
            "run_id": self.run_id,
            "run_seq": self.run_seq,
            "started": self.started,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "open_row": (
                {"title": self.title, "before": self.before} if self.title else None
            ),
            "completed": self.completed,
            "pending_learn": self.pending_learn,
            "recovered": self.recovered,
        }

    def save(self) -> None:
        """Mirror the session so a reload can pick the run back up."""
        path = self.session_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            # A session mirror that cannot be written must never break the run.
            pass

    def _open_rows_from_log(self) -> list[str]:
        """Every begin_row in today's log that never got an end_row.

        The log is the source of truth: a process killed before it could write a
        session file still leaves an unmatched begin_row behind. There can be
        more than one -- a reload loses the in-memory row, so the next
        begin_row starts a new one while the old begin_row is still unclosed.
        Tracking only the last one would hide the older orphan behind the newer
        pair.
        """
        path = self.path_for()
        if not path.exists():
            return []
        open_titles: list[str] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                title = str(event.get("title") or "")
                if not title:
                    continue
                op = event.get("op")
                if op == "begin_row":
                    open_titles.append(title)
                elif op == "end_row" and title in open_titles:
                    open_titles.remove(title)
        except OSError:
            return []
        return open_titles

    def restore(self) -> dict[str, Any]:
        """Adopt the previous session and close every row it left open.

        The process that opened those rows is gone, so their work becomes an
        offline debt instead of a lock on every later row. The session file is
        checked first, then today's log, so a process that died before writing
        anything is still recovered.
        """
        data: dict[str, Any] = {}
        path = self.session_path()
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                data = loaded

        self.completed = [str(item) for item in data.get("completed") or []]
        self.pending_learn = [str(item) for item in data.get("pending_learn") or []]
        self.recovered = [str(item) for item in data.get("recovered") or []]

        to_close: list[str] = []
        debts: list[str] = []

        open_row = data.get("open_row")
        if isinstance(open_row, dict):
            title = str(open_row.get("title") or "")
            before = open_row.get("before")
            if isinstance(before, dict):
                self.before = before
            if title:
                # A session from a previous day would drop a confusing end_row
                # into today's log; carry the debt without writing that event.
                if str(data.get("date") or "") == datetime.now().strftime("%Y%m%d"):
                    to_close.append(title)
                else:
                    debts.append(title)

        for title in self._open_rows_from_log():
            if title not in to_close and title not in debts:
                to_close.append(title)

        if not to_close and not debts:
            return data

        for title in to_close:
            self.append(
                {
                    "op": "end_row",
                    "title": title,
                    "learned": False,
                    "reason": "进程重启，上一行未收尾",
                    "recovered": True,
                }
            )
        for title in to_close + debts:
            if title not in self.pending_learn:
                self.pending_learn.append(title)
            if title not in self.recovered:
                self.recovered.append(title)
        self.title = None
        self.before = None
        self.events = []
        self.save()
        return data

    # ---------------------------------------------------------------- append

    def append(self, event: dict[str, Any], *, when: datetime | None = None) -> None:
        moment = when or datetime.now()
        path = self.path_for(moment)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": moment.strftime("%Y%m%d"),
            "device": self.device,
            "ts": moment.isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "run_seq": self.run_seq,
            **event,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def begin_run(self, label: str = "") -> dict[str, Any]:
        """Rotate the run identity so every task run is separable in the log.

        ``run_id`` is otherwise minted once per process, so a long-lived MCP
        process reuses one id across days (measured: ten days, one id). This
        mints a fresh id and bumps ``run_seq``, then writes a ``run_start`` line.

        It deliberately does **not** touch the open row: ``title`` / ``events`` /
        ``before`` are left exactly as they were, so a run boundary taken in the
        middle of a row cannot corrupt what is being learned. The ``run_start``
        record carries no ``title``, so ``_open_rows_from_log`` ignores it.

        Returns the new ``{"run_id", "run_seq"}`` for the caller to echo back.
        """
        self.run_id = uuid.uuid4().hex[:8]
        self.run_seq += 1
        self.append({"op": "run_start", "label": label})
        return {"run_id": self.run_id, "run_seq": self.run_seq}

    def note(self, event: dict[str, Any]) -> None:
        """Write the action, and keep it on the open row when one is being learned."""
        self.append(event)
        if self.title is not None:
            self.events.append(event)

    # ---------------------------------------------------------------- row

    def begin(self, title: str, extra: dict[str, Any] | None = None) -> None:
        if self.title:
            raise RowBusy(self.title)
        payload: dict[str, Any] = {"op": "begin_row", "title": title}
        if extra:
            payload.update(extra)
        self.append(payload)
        self.title = title
        self.events = []
        self.before = None
        self.save()

    def set_before(self, snapshot: dict[str, Any]) -> None:
        """Keep the first snapshot of the open row; later looks do not overwrite it."""
        if self.title and self.before is None:
            self.before = snapshot
            self.save()

    def finish(
        self,
        *,
        learned: bool,
        reason: str = "",
        extra: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Close the open row. Always closes, so one miss cannot stall the run.

        An unlearned row becomes a debt in ``pending_learn`` for the offline
        pass, instead of blocking every later row.

        ``extra`` adds diagnostic fields (button, package, element count, stored
        steps) to the ``end_row`` log line. It is a log detail only: the outcome
        is decided by ``learned`` and ``reason`` exactly as before.
        """
        if not self.title:
            raise RuntimeError("没有进行中的行")
        events = list(self.events)
        title = self.title
        payload: dict[str, Any] = {
            "op": "end_row",
            "title": title,
            "learned": learned,
            "reason": reason,
        }
        if extra:
            payload.update(extra)
        self.append(payload)
        if learned:
            if title not in self.completed:
                self.completed.append(title)
        elif title not in self.pending_learn:
            self.pending_learn.append(title)
        self.title = None
        self.before = None
        self.events = []
        self.save()
        return events

    def abandon(self, *, extra: dict[str, Any] | None = None) -> None:
        """Close the row as deliberately not worth learning (catalog marks it skip)."""
        if not self.title:
            raise RuntimeError("没有进行中的行")
        payload: dict[str, Any] = {
            "op": "end_row",
            "title": self.title,
            "learned": False,
            "abandon": True,
        }
        if extra:
            payload.update(extra)
        self.append(payload)
        self.title = None
        self.before = None
        self.events = []
        self.save()
