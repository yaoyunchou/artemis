"""Append hands actions to the day's jsonl and keep the open row's own slice."""

from __future__ import annotations

import json
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

    def __init__(self, root: Path, device: str = "") -> None:
        self.root = root
        self.device = device.strip()
        self.title: str | None = None
        self.events: list[dict[str, Any]] = []

    def path_for(self, when: datetime | None = None) -> Path:
        day = (when or datetime.now()).strftime("%Y%m%d")
        device = self.device or "unknown"
        return self.root / "logs" / "hands" / device / f"{day}.jsonl"

    def append(self, event: dict[str, Any], *, when: datetime | None = None) -> None:
        moment = when or datetime.now()
        path = self.path_for(moment)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": moment.strftime("%Y%m%d"),
            "device": self.device,
            "ts": moment.isoformat(timespec="seconds"),
            **event,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def note(self, event: dict[str, Any]) -> None:
        """Write the action, and keep it on the open row when one is being learned."""
        self.append(event)
        if self.title is not None:
            self.events.append(event)

    def begin(self, title: str) -> None:
        if self.title:
            raise RowBusy(self.title)
        self.append({"op": "begin_row", "title": title})
        self.title = title
        self.events = []

    def finish(self, *, learned: bool) -> list[dict[str, Any]]:
        """Close the open row. A miss does not close it, so the next row cannot start."""
        if not self.title:
            raise RuntimeError("没有进行中的行")
        events = list(self.events)
        if learned:
            self.append({"op": "end_row", "title": self.title, "learned": True})
            self.title = None
            self.events = []
        return events

    def abandon(self) -> None:
        if not self.title:
            raise RuntimeError("没有进行中的行")
        self.append({"op": "end_row", "title": self.title, "learned": False, "abandon": True})
        self.title = None
        self.events = []
