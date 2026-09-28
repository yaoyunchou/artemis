# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Device session for an external agent that decides every action.

This layer does not call a model. It remembers the last successful screen
observation and turns an element index into the normalized center the actuator
already knows how to tap.
"""

from typing import Any

from artemis.mcp.action_types import ActionCode, ActionResult, ObserveResult
from artemis.mcp.actuators.adb import AdbActuator
from artemis.mcp.observation import observe

__all__ = ["HANDS_TOOL_NAMES", "HandsSession", "TargetError", "resolve_target"]

#: Tools the hands MCP exposes. Kept here so installers and tests share one list
#: without importing the transport.
HANDS_TOOL_NAMES: tuple[str, ...] = (
    "list_devices",
    "use_device",
    "look",
    "click",
    "long_press",
    "swipe",
    "input_text",
    "press_key",
    "manage_app",
    "open_link",
    "erase_one_char",
    "focus_and_clear_text",
    "wait_for_delay",
    "wait_for_text",
    "click_sequence",
)


class TargetError(ValueError):
    """The caller named a target the last observation cannot resolve."""


def resolve_target(
    target: Any,
    elements: list[dict[str, Any]],
    width: int,
    height: int,
    *,
    observed: bool,
) -> tuple[int, int]:
    """Maps an element index or a normalized pair onto ``(x, y)`` in 0-1000 space.

    An index uses the center stored by the last successful ``look``. A two-item
    list is already a normalized coordinate and does not consult the element list.
    """
    if isinstance(target, bool):
        raise TargetError(
            "Invalid target. Use an element index from look (e.g. 3) or normalized [x, y]."
        )
    if isinstance(target, str) and target.strip().isdigit():
        target = int(target.strip())
    if isinstance(target, int):
        if not observed:
            raise TargetError("No screen observation yet. Call look first.")
        if not elements:
            raise TargetError(
                "The element list is empty on this screen. Pass normalized [x, y] instead."
            )
        if not 1 <= target <= len(elements):
            raise TargetError(
                f"Invalid target index {target}. Active index range is 1 to {len(elements)}."
            )
        center = elements[target - 1].get("center")
        if not (isinstance(center, (list, tuple)) and len(center) == 2):
            raise TargetError(f"Element [{target}] has no usable center.")
        nx = int(max(0, min(1000, round(float(center[0]) * 1000 / max(1, width)))))
        ny = int(max(0, min(1000, round(float(center[1]) * 1000 / max(1, height)))))
        return nx, ny
    if (
        isinstance(target, (list, tuple))
        and len(target) == 2
        and not any(isinstance(part, bool) for part in target)
    ):
        return int(target[0]), int(target[1])
    raise TargetError(
        f"Invalid target {target!r}. Use an element index from look (e.g. 3) or normalized [x, y]."
    )


class HandsSession:
    """One bound device: observe, remember the indexed elements, then act."""

    def __init__(self, actuator: AdbActuator):
        self.actuator = actuator
        self.elements: list[dict[str, Any]] = []
        self._observed = False
        width, height = actuator._dims()
        self.width = width
        self.height = height

    async def look(
        self, *, include_image: bool = True, settle_ms: int = 400
    ) -> tuple[ObserveResult, bytes | None]:
        """Captures the screen. A failed hierarchy parse keeps the previous index."""
        obs, image = await observe(
            self.actuator.ctx,
            self.actuator.controller,
            settle_ms=settle_ms,
        )
        if obs.ok and obs.hierarchy_ok:
            self.elements = list(obs.elements or [])
            self._observed = True
            if obs.width:
                self.width = obs.width
            if obs.height:
                self.height = obs.height
        return obs, image if include_image else None

    def _point(self, target: Any, action: str) -> tuple[int, int] | ActionResult:
        try:
            return resolve_target(
                target,
                self.elements,
                self.width,
                self.height,
                observed=self._observed,
            )
        except TargetError as exc:
            return ActionResult.failure(action, str(exc), code=ActionCode.INVALID_ARGS)

    async def click(self, target: Any, times: int = 1, delay_ms: int = 100) -> ActionResult:
        point = self._point(target, "click")
        if isinstance(point, ActionResult):
            return point
        return await self.actuator.click(point[0], point[1], times=times, delay_ms=delay_ms)

    async def long_press(self, target: Any, duration_ms: int = 1000) -> ActionResult:
        point = self._point(target, "long_press")
        if isinstance(point, ActionResult):
            return point
        return await self.actuator.long_press(point[0], point[1], duration_ms=duration_ms)

    async def input_text(
        self,
        text: str,
        target: Any = None,
        clear_exist: bool = True,
    ) -> ActionResult:
        resolved: tuple[int, int] | None = None
        if target is not None:
            point = self._point(target, "input_text")
            if isinstance(point, ActionResult):
                return point
            resolved = point
        return await self.actuator.input_text(text, target=resolved, clear_exist=clear_exist)

    async def focus_and_clear_text(self, target: Any) -> ActionResult:
        point = self._point(target, "focus_and_clear_text")
        if isinstance(point, ActionResult):
            return point
        return await self.actuator.focus_and_clear_text(point[0], point[1])

    async def swipe(
        self,
        start: list[int],
        end: list[int],
        duration_ms: int = 800,
    ) -> ActionResult:
        if len(start) != 2 or len(end) != 2:
            return ActionResult.failure(
                "swipe",
                "swipe start and end must each be normalized [x, y].",
                code=ActionCode.INVALID_ARGS,
            )
        return await self.actuator.swipe(
            (int(start[0]), int(start[1])),
            (int(end[0]), int(end[1])),
            duration_ms=duration_ms,
        )

    async def press_key(self, key: str) -> ActionResult:
        return await self.actuator.press_key(key)

    async def manage_app(self, action: str, app_name: str) -> ActionResult:
        return await self.actuator.manage_app(action, app_name)

    async def open_link(self, url: str) -> ActionResult:
        return await self.actuator.open_link(url)

    async def erase_one_char(self) -> ActionResult:
        return await self.actuator.erase_one_char()

    async def wait_for_delay(self, time_in_ms: int) -> ActionResult:
        return await self.actuator.wait_for_delay(time_in_ms)

    async def wait_for_text(
        self,
        text: str,
        wait_state: str = "appear",
        timeout_ms: int = 5000,
    ) -> ActionResult:
        return await self.actuator.wait_for_text(text, wait_state=wait_state, timeout_ms=timeout_ms)

    async def click_sequence(self, sequence: list[list[int]], delay_ms: int = 50) -> ActionResult:
        points: list[tuple[int, int]] = []
        for index, item in enumerate(sequence):
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                return ActionResult.failure(
                    "click_sequence",
                    "click_sequence entries must be normalized [x, y] pairs, not element indexes.",
                    code=ActionCode.INVALID_ARGS,
                )
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                return ActionResult.failure(
                    "click_sequence",
                    f"Sequence entry {index + 1} must be normalized [x, y].",
                    code=ActionCode.INVALID_ARGS,
                )
            points.append((int(item[0]), int(item[1])))
        return await self.actuator.click_sequence(points, delay_ms=delay_ms)
