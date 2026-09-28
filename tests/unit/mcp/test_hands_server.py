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

"""Hands MCP: the caller decides each action; the server only observes and taps."""

import ast
from pathlib import Path

import pytest

from artemis.drivers.base import ScreenData
from artemis.mcp.actuators import MockActuator
from artemis.mcp.hands_session import HANDS_TOOL_NAMES, HandsSession

_REPO = Path(__file__).resolve().parents[3]


def _screen(actuator: MockActuator) -> ScreenData:
    return ScreenData(
        screenshot_bytes=actuator.driver._mock_bytes,
        screenshot_base64=actuator.driver._mock_b64,
        ui_hierarchy_xml="",
        ui_elements=[{"text": "Settings", "bounds": "[0,0][1080,200]"}],
        width=1080,
        height=2400,
        platform="mock",
    )


async def _session_looking_at_settings() -> tuple[HandsSession, MockActuator]:
    actuator = MockActuator(width=1080, height=2400)

    async def get_screen_data(skip_settling: bool = False) -> ScreenData:
        del skip_settling
        return _screen(actuator)

    actuator.driver.get_screen_data = get_screen_data
    session = HandsSession(actuator)
    obs, image = await session.look(include_image=True, settle_ms=0)
    assert obs.ok
    assert obs.hierarchy_ok
    assert image
    assert session.elements
    assert "Settings" in (obs.elements_text or "")
    return session, actuator


@pytest.mark.asyncio
async def test_look_remembers_element_and_click_hits_center():
    session, actuator = await _session_looking_at_settings()
    result = await session.click(1)
    assert result.ok
    # Center of [0,0][1080,200] is (540, 100) -> normalized (500, 42).
    assert result.normalized_coordinates == [500, 42]
    tap = actuator.action_history[-1]
    assert tap["action"] == "tap"
    assert tap["x"] == 540
    assert tap["y"] == 100


@pytest.mark.asyncio
async def test_click_index_out_of_range_fails():
    session, actuator = await _session_looking_at_settings()
    before = len(actuator.action_history)
    result = await session.click(9)
    assert not result.ok
    assert result.code.value == "INVALID_ARGS"
    assert "Invalid target index 9" in result.message
    assert len(actuator.action_history) == before


@pytest.mark.asyncio
async def test_click_index_before_look_fails():
    actuator = MockActuator(width=1080, height=2400)
    session = HandsSession(actuator)
    result = await session.click(1)
    assert not result.ok
    assert "Call look first" in result.message
    assert actuator.action_history == []


def test_hands_modules_do_not_reference_a_model():
    for name in ("hands_session.py", "hands_server.py"):
        source = (_REPO / "artemis" / "mcp" / name).read_text(encoding="utf-8")
        assert "artemis.services.llm" not in source
        assert "mobile_run_task" not in source
        imported: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        assert not any("llm" in item.split(".") for item in imported)


def test_hands_server_registers_device_tools_only():
    from artemis.mcp.hands_server import _registered_tool_names

    names = _registered_tool_names()
    assert names == set(HANDS_TOOL_NAMES)
    assert "mobile_run_task" not in names


def test_cursor_config_adds_hands_without_replacing_agent():
    from artemis.interfaces.cli.commands.mcp import _get_config_snippet

    snippet = _get_config_snippet("cursor", "python", "/project")
    assert snippet["mcpServers"]["artemis"]["args"] == ["-m", "mcp_server"]
    hands_args = snippet["mcpServers"]["artemis-hands"]["args"]
    assert hands_args == ["/project/mcp_server/hands_entry.py"]
