"""Append hands actions to the day's jsonl and keep the open row's own slice."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

_SKIP_AUTO_TITLE_OPS = frozenset(
    {
        "begin_row",
        "end_row",
        "run_start",
        "replay_start",
        "replay_end",
        "replay_step",
    }
)


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

    def _enrich_open_row(self, event: dict[str, Any]) -> dict[str, Any]:
        if not self.title:
            return event
        op = str(event.get("op") or "")
        if op in _SKIP_AUTO_TITLE_OPS:
            return event
        enriched = dict(event)
        enriched.setdefault("title", self.title)
        enriched.setdefault("in_row", True)
        return enriched

    def append(self, event: dict[str, Any], *, when: datetime | None = None) -> None:
        moment = when or datetime.now()
        path = self.path_for(moment)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": moment.strftime("%Y%m%d"),
            "device": self.device,
            "ts": moment.isoformat(timespec="seconds"),
            **self._enrich_open_row(event),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def note(self, event: dict[str, Any]) -> None:
        """Write the action, and keep it on the open row when one is being learned."""
        enriched = self._enrich_open_row(event)
        self.append(enriched)
        if self.title is not None:
            self.events.append(enriched)

    def begin(self, title: str, extra: dict[str, Any] | None = None) -> None:
        if self.title:
            raise RowBusy(self.title)
        event: dict[str, Any] = {"op": "begin_row", "title": title}
        if extra:
            for key, value in extra.items():
                if value not in (None, ""):
                    event[key] = value
        self.append(event)
        self.title = title
        self.events = []

    def close_span(
        self,
        *,
        learned: bool,
        outcome: str,
        reason: str = "",
        abandon: bool = False,
    ) -> None:
        if not self.title:
            raise RuntimeError("没有进行中的行")
        payload: dict[str, Any] = {
            "op": "end_row",
            "title": self.title,
            "learned": learned,
            "outcome": outcome,
        }
        if reason:
            payload["reason"] = reason
        if abandon:
            payload["abandon"] = True
        self.append(payload)
        self.title = None
        self.events = []

    def finish(
        self,
        *,
        learned: bool,
        outcome: str = "",
        reason: str = "",
    ) -> list[dict[str, Any]]:
        """Close the open row when ``learned`` is true. A miss does not close it."""
        if not self.title:
            raise RuntimeError("没有进行中的行")
        events = list(self.events)
        if learned:
            self.close_span(
                learned=True,
                outcome=outcome or "done",
                reason=reason,
            )
        return events

    def abandon(self, *, outcome: str = "skip", reason: str = "") -> None:
        if not self.title:
            raise RuntimeError("没有进行中的行")
        self.close_span(learned=False, outcome=outcome, reason=reason, abandon=True)
