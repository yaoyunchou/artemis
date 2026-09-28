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

"""Model Context Protocol (MCP) server command (artemis mcp)."""

import json
import os
from pathlib import Path
import re
import sys
import threading
import tomllib
from typing import Annotated

from mcp_server.base import mcp as agent_mcp
import mcp_server.tools  # noqa: F401
from mcp_server.utils import env_utils
from artemis.mcp.adb_server import mcp as adb_mcp
from artemis.mcp.hands_session import HANDS_TOOL_NAMES
from artemis.runtime import shutdown_awake_service, start_awake_service
from artemis.utils.logger import get_logger
from rich.console import Console
from rich.syntax import Syntax
import typer

logger = get_logger(__name__)
console = Console()

ARTEMIS_MCP_TOOLS = (
    "mobile_run_task",
    "mobile_manage_task",
    "mobile_get_device_state",
    "mobile_inspect_trace",
    "mobile_diagnose",
)


def _eager_tools_config() -> dict[str, dict[str, bool]]:
    """Return the eager-loading extension used by clients that support it."""
    return {tool_name: {"eager": True} for tool_name in ARTEMIS_MCP_TOOLS}


def _get_vscode_user_dir() -> Path:
    """Returns the platform-specific VS Code user configuration directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Code" / "User"
    else:
        return Path.home() / ".config" / "Code" / "User"


def _hands_launch(python_exe: str, project_root: str) -> dict:
    """Launch config for the hands server. A script path avoids importing the agent MCP."""
    return {
        "command": python_exe,
        "args": [str(Path(project_root) / "mcp_server" / "hands_entry.py")],
        "env": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": project_root,
        },
    }


def _add_hands_server(client: str, snippet: dict, python_exe: str, project_root: str) -> dict:
    """Adds the ``artemis-hands`` server beside the existing agent server."""
    launch = _hands_launch(python_exe, project_root)
    with_cwd = {**launch, "cwd": project_root}
    if client == "jetski":
        snippet["mcpServers"]["artemis-hands"] = {
            **with_cwd,
            "disabledTools": [],
            "tools": {name: {"eager": True} for name in HANDS_TOOL_NAMES},
        }
    elif client == "antigravity":
        snippet["mcpServers"]["artemis-hands"] = {**with_cwd, "disabledTools": []}
    elif client == "openclaw":
        snippet["mcp"]["servers"]["artemis-hands"] = {
            **with_cwd,
            "enabled": True,
            "connectionTimeoutMs": 120000,
        }
    elif client == "codex":
        snippet["mcp_servers"]["artemis-hands"] = {
            **with_cwd,
            "enabled": True,
            "required": False,
            "startup_timeout_sec": 120,
            "enabled_tools": list(HANDS_TOOL_NAMES),
        }
    elif client == "vscode":
        snippet["servers"]["artemis-hands"] = {**with_cwd, "type": "stdio"}
    elif client in ("claude", "claude_code", "claude_desktop"):
        snippet["mcpServers"]["artemis-hands"] = {**launch, "type": "stdio"}
    elif client == "cline":
        snippet["mcpServers"]["artemis-hands"] = {
            **launch,
            "disabled": False,
            "autoApprove": [],
            "timeout": 120,
        }
    elif client in ("roo", "roo_code"):
        snippet["mcpServers"]["artemis-hands"] = {
            **launch,
            "disabled": False,
            "alwaysAllow": [],
            "timeout": 120,
        }
    else:
        snippet["mcpServers"]["artemis-hands"] = launch
    return snippet


def _get_config_snippet(client: str, python_exe: str, project_root: str) -> dict:
    """Generates MCP configuration dictionary for the specified client."""
    config_body = {
        "command": python_exe,
        "args": ["-m", "mcp_server"],
        "env": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": project_root,
            "ARTEMIS_DESKTOP_NOTIFY": "true",
        },
    }
    config_with_cwd = {**config_body, "cwd": project_root}

    if client == "antigravity":
        snippet = {
            "mcpServers": {
                "artemis": {
                    **config_with_cwd,
                    # This is the documented way to expose every tool. Avoid
                    # proprietary eager fields in the current config so a
                    # strict parser can always load the server.
                    "disabledTools": [],
                }
            }
        }
    elif client == "jetski":
        snippet = {
            "mcpServers": {
                "artemis": {
                    **config_with_cwd,
                    "disabledTools": [],
                    # Retain the legacy Jetski eager extension only in its
                    # legacy config file. Current Antigravity gets the strict,
                    # documented configuration above as a safe fallback.
                    "tools": _eager_tools_config(),
                }
            }
        }
    elif client == "openclaw":
        snippet = {
            "mcp": {
                "servers": {
                    "artemis": {
                        **config_with_cwd,
                        "enabled": True,
                        "connectionTimeoutMs": 120000,
                    }
                }
            }
        }
    elif client == "codex":
        snippet = {
            "mcp_servers": {
                "artemis": {
                    **config_with_cwd,
                    # Codex has no eager/deferred MCP setting. Requiring the
                    # server makes it initialize during host startup, while an
                    # explicit allow-list guarantees all Artemis tools are
                    # registered. The host may still present schemas through
                    # tool search to control context size.
                    "enabled": True,
                    "required": True,
                    "startup_timeout_sec": 120,
                    "enabled_tools": list(ARTEMIS_MCP_TOOLS),
                }
            }
        }
    elif client == "vscode":
        snippet = {"servers": {"artemis": {**config_with_cwd, "type": "stdio"}}}
    elif client in ("claude", "claude_code", "claude_desktop"):
        snippet = {"mcpServers": {"artemis": {**config_body, "type": "stdio"}}}
    elif client == "cline":
        snippet = {
            "mcpServers": {
                "artemis": {
                    **config_body,
                    "disabled": False,
                    "autoApprove": [],
                    "timeout": 120,
                }
            }
        }
    elif client in ("roo", "roo_code"):
        snippet = {
            "mcpServers": {
                "artemis": {
                    **config_body,
                    "disabled": False,
                    "alwaysAllow": [],
                    "timeout": 120,
                }
            }
        }
    else:  # cursor, windsurf, generic
        snippet = {"mcpServers": {"artemis": config_body}}
    return _add_hands_server(client, snippet, python_exe, project_root)


def _parse_json_lenient(text: str) -> dict | None:
    """Parses JSON or JSONC (JSON with comments / trailing commas) safely.

    Returns ``None`` when the text cannot be parsed as a JSON object, so callers
    can tell a genuinely empty ``{}`` file apart from a corrupt one.
    """
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        # Not strict JSON; fall through to the lenient JSONC cleanup below.
        pass
    # Remove // single-line comments
    cleaned = re.sub(r"//.*", "", text)
    # Remove /* ... */ multi-line comments
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    # Remove trailing commas before } or ]
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _merge_json_file(
    file_path: Path,
    server_name: str,
    server_config: dict,
    key_name: str | tuple[str, ...] = "mcpServers",
    remove_paths: tuple[tuple[str, ...], ...] = (),
    strict_parse: bool = False,
) -> bool:
    """Merges server configuration into a target JSON file without overwriting existing servers.

    With ``strict_parse=True``, an existing non-empty file that cannot be parsed
    aborts the merge instead of being replaced. Use this for files that hold
    state beyond MCP config (e.g. ~/.claude.json), where a degraded rewrite
    would destroy unrelated data.
    """
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if file_path.exists() and file_path.stat().st_size > 0:
            raw_text = file_path.read_text(encoding="utf-8")
            parsed = _parse_json_lenient(raw_text)
            data = parsed or {}
            if parsed is None and raw_text.strip():
                if strict_parse:
                    logger.warning(
                        f"Existing JSON in {file_path} could not be parsed; refusing to rewrite it. "
                        "Fix or remove the file, then re-run the install."
                    )
                    return False
                # Backup unparseable existing file to prevent accidental loss
                backup_path = file_path.with_suffix(file_path.suffix + ".bak")
                try:
                    backup_path.write_text(raw_text, encoding="utf-8")
                    logger.warning(
                        f"Existing JSON in {file_path} could not be parsed; backup created at {backup_path}"
                    )
                except OSError as exc:
                    logger.warning(
                        f"Existing JSON in {file_path} could not be parsed AND the backup to"
                        f" {backup_path} failed ({exc}); refusing to rewrite it."
                        " Fix or remove the file, then re-run the install."
                    )
                    return False

        for remove_path in remove_paths:
            parent = data
            for part in remove_path[:-1]:
                child = parent.get(part)
                if not isinstance(child, dict):
                    parent = {}
                    break
                parent = child
            if parent:
                parent.pop(remove_path[-1], None)

        key_path = (key_name,) if isinstance(key_name, str) else key_name
        target = data
        for part in key_path:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]

        target[server_name] = server_config
        file_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not update MCP config file {file_path}: {e}")
        return False


def _toml_table(server_name: str, suffix: str = "") -> str:
    """Renders a TOML table path, quoting server names that are not bare keys."""
    key = server_name if re.fullmatch(r"[A-Za-z0-9_]+", server_name) else json.dumps(server_name)
    table = f"mcp_servers.{key}"
    if suffix:
        table = f"{table}.{suffix}"
    return table


def _codex_server_lines(server_name: str, server_config: dict) -> list[str]:
    """Renders one Codex MCP server table, without the managed-block markers."""
    command = json.dumps(str(server_config["command"]), ensure_ascii=False)
    args = json.dumps(server_config.get("args", []), ensure_ascii=False)
    cwd = json.dumps(str(server_config["cwd"]), ensure_ascii=False)
    env = server_config.get("env", {})
    lines = [
        f"[{_toml_table(server_name)}]",
        f"command = {command}",
        f"args = {args}",
        f"cwd = {cwd}",
    ]
    for key in ("enabled", "required"):
        if key in server_config:
            lines.append(f"{key} = {'true' if server_config[key] else 'false'}")
    if "startup_timeout_sec" in server_config:
        lines.append(f"startup_timeout_sec = {int(server_config['startup_timeout_sec'])}")
    if "enabled_tools" in server_config:
        lines.append(
            f"enabled_tools = {json.dumps(server_config['enabled_tools'], ensure_ascii=False)}"
        )
    if env:
        lines.extend(["", f"[{_toml_table(server_name, 'env')}]"])
        for key, value in env.items():
            lines.append(f"{key} = {json.dumps(str(value), ensure_ascii=False)}")
    return lines


def _codex_toml_block(server_config: dict, hands_config: dict | None = None) -> str:
    """Renders a managed Codex MCP server block using TOML-compatible JSON strings."""
    lines = ["# BEGIN ARTEMIS MCP CONFIG", *_codex_server_lines("artemis", server_config)]
    if hands_config:
        lines.extend(["", *_codex_server_lines("artemis-hands", hands_config)])
    lines.append("# END ARTEMIS MCP CONFIG")
    return "\n".join(lines) + "\n"


def _merge_codex_toml(
    file_path: Path, server_config: dict, hands_config: dict | None = None
) -> bool:
    """Adds or updates Artemis in Codex config.toml while preserving unrelated settings."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        existing = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
        managed_block = _codex_toml_block(server_config, hands_config)
        begin_marker = "# BEGIN ARTEMIS MCP CONFIG"
        end_marker = "# END ARTEMIS MCP CONFIG"

        if begin_marker in existing and end_marker in existing:
            managed_pattern = re.compile(
                rf"(?ms)^{re.escape(begin_marker)}\n.*?^{re.escape(end_marker)}\n?"
            )
            updated = managed_pattern.sub(managed_block, existing, count=1)
        else:
            # Remove an older, unmanaged Artemis table before adding the managed block.
            kept_lines: list[str] = []
            skipping_artemis = False
            header_pattern = re.compile(r"^\s*\[{1,2}\s*(.+?)\s*\]{1,2}\s*(?:#.*)?$")
            for line in existing.splitlines(keepends=True):
                header_match = header_pattern.match(line.rstrip("\r\n"))
                if header_match:
                    normalized = re.sub(r"[\s\"']", "", header_match.group(1)).lower()
                    skipping_artemis = (
                        normalized == "mcp_servers.artemis"
                        or normalized.startswith("mcp_servers.artemis.")
                        or normalized == "mcp_servers.artemis-hands"
                        or normalized.startswith("mcp_servers.artemis-hands.")
                    )
                if not skipping_artemis:
                    kept_lines.append(line)
            preserved = "".join(kept_lines).rstrip()
            updated = f"{preserved}\n\n{managed_block}" if preserved else managed_block

        # Never replace a user's config with invalid TOML.
        tomllib.loads(updated)
        if updated != existing:
            file_path.write_text(updated, encoding="utf-8")
        return True
    except Exception as e:
        if file_path.exists():
            try:
                backup_path = file_path.with_suffix(file_path.suffix + ".bak")
                backup_path.write_text(file_path.read_text(encoding="utf-8"), encoding="utf-8")
                logger.warning(
                    f"Could not update {file_path}; backup created at {backup_path}: {e}"
                )
            except (OSError, UnicodeDecodeError) as backup_exc:
                logger.warning(
                    f"Could not update {file_path} ({e}); backup also failed: {backup_exc}"
                )
        else:
            logger.warning(f"Could not create Codex MCP config file {file_path}: {e}")
        return False


def _remove_rules_block(file_path: Path) -> bool:
    """Removes a previously injected managed ARTEMIS block from a rules file.

    Used to migrate clients that load both their main rules file and a
    standalone rule file (e.g. Claude Code loads ~/.claude/CLAUDE.md AND
    ~/.claude/rules/*.md), where keeping the injected block would duplicate
    the rules in the model's context. If the file contains nothing but the
    managed block, the file is deleted entirely.
    """
    try:
        if not file_path.exists():
            return True
        begin_marker = "<!-- BEGIN ARTEMIS MOBILE TESTING RULES -->"
        end_marker = "<!-- END ARTEMIS MOBILE TESTING RULES -->"
        existing = file_path.read_text(encoding="utf-8")
        if begin_marker not in existing or end_marker not in existing:
            return True
        prefix = existing.split(begin_marker)[0]
        suffix = existing.split(end_marker)[1]
        remainder = f"{prefix.rstrip()}\n\n{suffix.lstrip()}".strip()
        if remainder:
            file_path.write_text(remainder + "\n", encoding="utf-8")
        else:
            file_path.unlink()
        return True
    except Exception as e:
        logger.warning(f"Could not remove rules block from {file_path}: {e}")
        return False


def _inject_rules_block(
    file_path: Path,
    content: str,
    begin_marker: str = "<!-- BEGIN ARTEMIS MOBILE TESTING RULES -->",
    end_marker: str = "<!-- END ARTEMIS MOBILE TESTING RULES -->",
) -> bool:
    """Injects or updates a managed ARTEMIS block in a global rules file without overwriting user content."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        new_block = f"{begin_marker}\n{content.strip()}\n{end_marker}\n"

        existing = ""
        if file_path.exists():
            try:
                existing = file_path.read_text(encoding="utf-8")
            except Exception:
                existing = ""

        if begin_marker in existing and end_marker in existing:
            prefix = existing.split(begin_marker)[0]
            suffix = existing.split(end_marker)[1]
            updated = f"{prefix.rstrip()}\n\n{new_block}\n{suffix.lstrip()}".strip() + "\n"
        else:
            updated = f"{existing.rstrip()}\n\n{new_block}".strip() + "\n"

        if existing == updated:
            return True

        file_path.write_text(updated, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not inject rules block into {file_path}: {e}")
        return False


def _write_rule_file(file_path: Path, content: str) -> bool:
    """Writes or overwrites a standalone rule file."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        new_text = content.strip() + "\n"
        if file_path.exists():
            try:
                if file_path.read_text(encoding="utf-8") == new_text:
                    return True
            except (OSError, UnicodeDecodeError):
                # Unreadable existing file: skip the no-op check and rewrite it.
                pass
        file_path.write_text(new_text, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not write rule file {file_path}: {e}")
        return False


def _write_cursor_mdc(
    file_path: Path,
    content: str,
    description: str = "Artemis Mobile Testing Mindset & Rules",
) -> bool:
    """Writes a Cursor .mdc rule file with required YAML frontmatter."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        quoted = description.replace('"', '\\"')
        mdc_header = f'---\ndescription: "{quoted}"\nglobs: **/*\nalwaysApply: true\n---\n\n'
        new_text = mdc_header + content.strip() + "\n"
        if file_path.exists():
            try:
                if file_path.read_text(encoding="utf-8") == new_text:
                    return True
            except (OSError, UnicodeDecodeError):
                # Unreadable existing file: skip the no-op check and rewrite it.
                pass
        file_path.write_text(new_text, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not write Cursor rule file {file_path}: {e}")
        return False


def install_rules(client: str, project_root: str) -> list[str]:
    """Auto-installs ARTEMIS testing rules globally into user IDE rule directories across any OS."""
    installed_paths: list[str] = []
    rules_src = Path(project_root) / "mcp_server" / "rules.md"
    if not rules_src.exists():
        alt_src = Path(__file__).resolve().parents[4] / "mcp_server" / "rules.md"
        if alt_src.exists():
            rules_src = alt_src
        else:
            logger.warning(f"Could not locate rules.md at {rules_src}")
            return []

    try:
        raw_rules = rules_src.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"Could not read rules file {rules_src}: {e}")
        return []

    targets = (
        [
            "antigravity",
            "cursor",
            "claude",
            "windsurf",
            "vscode",
            "cline",
            "roo",
            "openclaw",
            "codex",
        ]
        if client == "all"
        else [client]
    )

    for target in targets:
        if target in ("antigravity", "jetski"):
            gemini_md = Path.home() / ".gemini" / "GEMINI.md"
            global_rule = Path.home() / ".gemini" / "rules" / "artemis.md"
            if _inject_rules_block(gemini_md, raw_rules):
                installed_paths.append(str(gemini_md))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "cursor":
            cursor_rules_file = Path.home() / ".cursorrules"
            global_mdc = Path.home() / ".cursor" / "rules" / "artemis.mdc"
            if _inject_rules_block(cursor_rules_file, raw_rules):
                installed_paths.append(str(cursor_rules_file))
            if _write_cursor_mdc(global_mdc, raw_rules):
                installed_paths.append(str(global_mdc))
        elif target in ("claude", "claude_code"):
            # Claude Code loads BOTH ~/.claude/CLAUDE.md and ~/.claude/rules/*.md
            # into context every session, so install the rules in exactly one
            # place (the standalone rule file) and migrate away any block a
            # previous version injected into CLAUDE.md.
            claude_md = Path.home() / ".claude" / "CLAUDE.md"
            global_rule = Path.home() / ".claude" / "rules" / "artemis.md"
            _remove_rules_block(claude_md)
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "claude_desktop":
            # Claude Desktop only reads ~/.claude/CLAUDE.md (not rules/*.md), so
            # the rules block stays injected there for this target.
            claude_md = Path.home() / ".claude" / "CLAUDE.md"
            if _inject_rules_block(claude_md, raw_rules):
                installed_paths.append(str(claude_md))
        elif target == "windsurf":
            global_rule = Path.home() / ".codeium" / "windsurf" / "rules" / "artemis.md"
            global_mem = Path.home() / ".codeium" / "windsurf" / "memories" / "global_rules.md"
            if _inject_rules_block(global_mem, raw_rules):
                installed_paths.append(str(global_mem))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "vscode":
            global_rule = Path.home() / ".vscode" / "rules" / "artemis.md"
            user_rule = _get_vscode_user_dir() / "rules" / "artemis.md"
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
            if _write_rule_file(user_rule, raw_rules):
                installed_paths.append(str(user_rule))
        elif target == "cline":
            clinerules = Path.home() / ".clinerules"
            global_rule = Path.home() / ".cline" / "rules" / "artemis.md"
            if _inject_rules_block(clinerules, raw_rules):
                installed_paths.append(str(clinerules))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target in ("roo", "roo_code"):
            roorules = Path.home() / ".roorules"
            global_rule = Path.home() / ".roo" / "rules" / "artemis.md"
            if _inject_rules_block(roorules, raw_rules):
                installed_paths.append(str(roorules))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "openclaw":
            openclaw_md = Path.home() / ".openclaw" / "OPENCLAW.md"
            global_rule = Path.home() / ".openclaw" / "rules" / "artemis.md"
            if _inject_rules_block(openclaw_md, raw_rules):
                installed_paths.append(str(openclaw_md))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "codex":
            codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
            override_file = codex_home / "AGENTS.override.md"
            agents_file = (
                override_file
                if override_file.exists() and override_file.read_text(encoding="utf-8").strip()
                else codex_home / "AGENTS.md"
            )
            if _inject_rules_block(agents_file, raw_rules):
                installed_paths.append(str(agents_file))

    return installed_paths


_HANDS_RULES_BEGIN = "<!-- BEGIN ARTEMIS HANDS RULES -->"
_HANDS_RULES_END = "<!-- END ARTEMIS HANDS RULES -->"


def install_hands_rules(client: str, project_root: str) -> list[str]:
    """Installs the short hands-server rules next to the agent testing rules."""
    installed_paths: list[str] = []
    rules_src = Path(project_root) / "mcp_server" / "hands_rules.md"
    if not rules_src.exists():
        alt_src = Path(__file__).resolve().parents[4] / "mcp_server" / "hands_rules.md"
        if alt_src.exists():
            rules_src = alt_src
        else:
            logger.warning(f"Could not locate hands_rules.md at {rules_src}")
            return []
    try:
        raw_rules = rules_src.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"Could not read rules file {rules_src}: {e}")
        return []

    targets = (
        [
            "antigravity",
            "cursor",
            "claude",
            "windsurf",
            "vscode",
            "cline",
            "roo",
            "openclaw",
            "codex",
        ]
        if client == "all"
        else [client]
    )
    cursor_description = "Artemis Hands: the connected agent decides every device action"

    for target in targets:
        if target in ("antigravity", "jetski"):
            path = Path.home() / ".gemini" / "rules" / "artemis-hands.md"
            if _write_rule_file(path, raw_rules):
                installed_paths.append(str(path))
        elif target == "cursor":
            path = Path.home() / ".cursor" / "rules" / "artemis-hands.mdc"
            if _write_cursor_mdc(path, raw_rules, description=cursor_description):
                installed_paths.append(str(path))
        elif target in ("claude", "claude_code", "claude_desktop"):
            path = Path.home() / ".claude" / "rules" / "artemis-hands.md"
            if _write_rule_file(path, raw_rules):
                installed_paths.append(str(path))
        elif target == "windsurf":
            path = Path.home() / ".codeium" / "windsurf" / "rules" / "artemis-hands.md"
            if _write_rule_file(path, raw_rules):
                installed_paths.append(str(path))
        elif target == "vscode":
            for path in (
                Path.home() / ".vscode" / "rules" / "artemis-hands.md",
                _get_vscode_user_dir() / "rules" / "artemis-hands.md",
            ):
                if _write_rule_file(path, raw_rules):
                    installed_paths.append(str(path))
        elif target == "cline":
            path = Path.home() / ".cline" / "rules" / "artemis-hands.md"
            if _write_rule_file(path, raw_rules):
                installed_paths.append(str(path))
        elif target in ("roo", "roo_code"):
            path = Path.home() / ".roo" / "rules" / "artemis-hands.md"
            if _write_rule_file(path, raw_rules):
                installed_paths.append(str(path))
        elif target == "openclaw":
            path = Path.home() / ".openclaw" / "rules" / "artemis-hands.md"
            if _write_rule_file(path, raw_rules):
                installed_paths.append(str(path))
        elif target == "codex":
            codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
            override_file = codex_home / "AGENTS.override.md"
            agents_file = (
                override_file
                if override_file.exists() and override_file.read_text(encoding="utf-8").strip()
                else codex_home / "AGENTS.md"
            )
            if _inject_rules_block(agents_file, raw_rules, _HANDS_RULES_BEGIN, _HANDS_RULES_END):
                installed_paths.append(str(agents_file))

    return installed_paths


def _merge_servers(
    file_path: Path,
    servers: dict,
    key_name: str | tuple[str, ...] = "mcpServers",
    remove_paths: tuple[tuple[str, ...], ...] = (),
    strict_parse: bool = False,
) -> bool:
    """Writes the agent server and the hands server into one MCP config file."""
    wrote = False
    for name in ("artemis", "artemis-hands"):
        cfg = servers.get(name)
        if not isinstance(cfg, dict):
            continue
        if _merge_json_file(
            file_path,
            name,
            cfg,
            key_name=key_name,
            remove_paths=remove_paths if name == "artemis" else (),
            strict_parse=strict_parse,
        ):
            wrote = True
    return wrote


def install_mcp_config(client: str, python_exe: str, project_root: str) -> list[str]:
    """Auto-installs/merges ARTEMIS MCP configuration and testing rules into IDE config files across any OS."""
    installed_paths: list[str] = []
    targets = (
        [
            "antigravity",
            "cursor",
            "claude",
            "windsurf",
            "vscode",
            "cline",
            "roo",
            "openclaw",
            "codex",
        ]
        if client == "all"
        else [client]
    )

    for target in targets:
        snippet = _get_config_snippet(target, python_exe, project_root)
        if target in ("antigravity", "jetski"):
            current_servers = _get_config_snippet("antigravity", python_exe, project_root)[
                "mcpServers"
            ]
            legacy_servers = _get_config_snippet("jetski", python_exe, project_root)["mcpServers"]
            jetski_path = Path.home() / ".gemini" / "jetski" / "mcp_config.json"
            antigravity_legacy_path = Path.home() / ".gemini" / "antigravity" / "mcp_config.json"
            config_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
            if _merge_servers(jetski_path, legacy_servers):
                installed_paths.append(str(jetski_path))
            if _merge_servers(antigravity_legacy_path, current_servers):
                installed_paths.append(str(antigravity_legacy_path))
            if _merge_servers(config_path, current_servers):
                installed_paths.append(str(config_path))
        elif target in ("claude", "claude_code", "claude_desktop"):
            server_cfg = snippet["mcpServers"]
            if sys.platform == "darwin":
                claude_path = (
                    Path.home()
                    / "Library"
                    / "Application Support"
                    / "Claude"
                    / "claude_desktop_config.json"
                )
            elif sys.platform == "win32":
                appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
                claude_path = Path(appdata) / "Claude" / "claude_desktop_config.json"
            else:
                claude_path = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
            if _merge_servers(claude_path, server_cfg):
                installed_paths.append(str(claude_path))

            # Also install to Claude Code CLI global config (~/.claude.json).
            # This file holds Claude Code state well beyond MCP config, so a
            # parse failure must abort the merge rather than rewrite the file.
            claude_code_path = Path.home() / ".claude.json"
            if _merge_servers(claude_code_path, server_cfg, strict_parse=True):
                installed_paths.append(str(claude_code_path))
        elif target == "cursor":
            server_cfg = snippet["mcpServers"]
            cursor_path = Path.home() / ".cursor" / "mcp.json"
            if _merge_servers(cursor_path, server_cfg):
                installed_paths.append(str(cursor_path))
        elif target == "windsurf":
            server_cfg = snippet["mcpServers"]
            windsurf_path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
            if _merge_servers(windsurf_path, server_cfg):
                installed_paths.append(str(windsurf_path))
        elif target == "vscode":
            server_cfg = snippet["servers"]
            vscode_path = _get_vscode_user_dir() / "mcp.json"
            copilot_path = Path.home() / ".copilot" / "mcp-config.json"
            if _merge_servers(vscode_path, server_cfg, key_name="servers"):
                installed_paths.append(str(vscode_path))
            if _merge_servers(copilot_path, server_cfg, key_name="servers"):
                installed_paths.append(str(copilot_path))
        elif target == "cline":
            server_cfg = snippet["mcpServers"]
            cline_paths = (
                Path.home() / ".cline" / "data" / "settings" / "cline_mcp_settings.json",
                Path.home() / ".cline" / "mcp.json",
                _get_vscode_user_dir()
                / "globalStorage"
                / "saoudrizwan.claude-dev"
                / "settings"
                / "cline_mcp_settings.json",
            )
            for cline_path in cline_paths:
                if _merge_servers(cline_path, server_cfg):
                    installed_paths.append(str(cline_path))
        elif target in ("roo", "roo_code"):
            server_cfg = snippet["mcpServers"]
            roo_settings_dir = (
                _get_vscode_user_dir() / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings"
            )
            roo_paths = (
                roo_settings_dir / "mcp_settings.json",
                roo_settings_dir / "cline_mcp_settings.json",
                Path(project_root) / ".roo" / "mcp.json",
            )
            for roo_path in roo_paths:
                if _merge_servers(roo_path, server_cfg):
                    installed_paths.append(str(roo_path))
        elif target == "openclaw":
            server_cfg = snippet["mcp"]["servers"]
            openclaw_path = Path.home() / ".openclaw" / "openclaw.json"
            if _merge_servers(
                openclaw_path,
                server_cfg,
                key_name=("mcp", "servers"),
                remove_paths=(("plugins", "artemis_mcp"),),
            ):
                installed_paths.append(str(openclaw_path))
        elif target == "codex":
            server_cfg = snippet["mcp_servers"]["artemis"]
            hands_cfg = snippet["mcp_servers"]["artemis-hands"]
            codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
            codex_path = codex_home / "config.toml"
            if _merge_codex_toml(codex_path, server_cfg, hands_cfg):
                installed_paths.append(str(codex_path))

    rules_paths = install_rules(client, project_root)
    installed_paths.extend(install_hands_rules(client, project_root))
    installed_paths.extend(rules_paths)

    unique_paths: list[str] = []
    for p in installed_paths:
        if p not in unique_paths:
            unique_paths.append(p)
    return unique_paths


def mcp_command(
    server_type: Annotated[
        str,
        typer.Option(
            "--type",
            "-t",
            help="Type of MCP server to start: 'agent' (default, universal IDE mobile agent), 'hands' (device actions only; the client decides), 'adb' (raw adb), 'xml' (xml fuzzy search).",
        ),
    ] = "agent",
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            help="MCP transport protocol ('stdio' or 'sse').",
        ),
    ] = "stdio",
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Host address when running with SSE transport.",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Port number when running with SSE transport.",
        ),
    ] = 8001,
    install_config: Annotated[
        str | None,
        typer.Option(
            "--install",
            "-i",
            help="Auto-install and merge ARTEMIS MCP configuration and testing rules into 'antigravity', 'claude', 'cursor', 'windsurf', 'vscode', 'cline', 'roo', 'openclaw', 'codex', or 'all'.",
        ),
    ] = None,
    generate_config: Annotated[
        str | None,
        typer.Option(
            "--generate-config",
            "-g",
            help="Output ready-to-use MCP configuration for 'antigravity', 'cursor', 'claude', 'windsurf', 'vscode', 'cline', 'roo', 'openclaw', 'codex', or 'all'.",
        ),
    ] = None,
) -> None:
    """Launch or configure the ARTEMIS Model Context Protocol (MCP) server."""
    project_root = env_utils.get_project_root()
    python_exe = env_utils.resolve_python_executable(project_root)

    if install_config:
        client = install_config.lower()
        if client not in (
            "antigravity",
            "jetski",
            "claude",
            "claude_code",
            "claude_desktop",
            "cursor",
            "windsurf",
            "vscode",
            "cline",
            "roo",
            "roo_code",
            "openclaw",
            "codex",
            "all",
        ):
            console.print(
                f"[bold red]Unsupported install target: '{install_config}'. Use 'antigravity', 'claude', 'cursor', 'windsurf', 'vscode', 'cline', 'roo', 'openclaw', 'codex', or 'all'.[/bold red]"
            )
            raise typer.Exit(1)
        installed_paths = install_mcp_config(client, python_exe, project_root)
        console.print(
            "[bold green]✔ Successfully installed ARTEMIS MCP server configuration & rules to:[/bold green]"
        )
        for path in installed_paths:
            console.print(f"  • [cyan]{path}[/cyan]")
        console.print(
            "\n[dim]Please restart or reload your IDE window to activate the Artemis MCP tools.[/dim]"
        )
        raise typer.Exit(0)

    if generate_config:
        client = generate_config.lower()
        if client == "all":
            all_configs = {
                "antigravity (~/.gemini/jetski/mcp_config.json)": _get_config_snippet(
                    "antigravity", python_exe, project_root
                ),
                "cursor (.cursor/mcp.json)": _get_config_snippet(
                    "cursor", python_exe, project_root
                ),
                "claude (claude_desktop_config.json & ~/.claude.json)": _get_config_snippet(
                    "claude", python_exe, project_root
                ),
                "windsurf (~/.codeium/windsurf/mcp_config.json)": _get_config_snippet(
                    "windsurf", python_exe, project_root
                ),
                "vscode (mcp.json)": _get_config_snippet("vscode", python_exe, project_root),
                "cline (cline_mcp_settings.json)": _get_config_snippet(
                    "cline", python_exe, project_root
                ),
                "roo (cline_mcp_settings.json)": _get_config_snippet(
                    "roo", python_exe, project_root
                ),
                "openclaw (openclaw.json)": _get_config_snippet(
                    "openclaw", python_exe, project_root
                ),
                "codex (~/.codex/config.toml)": _get_config_snippet(
                    "codex", python_exe, project_root
                ),
            }
            json_str = json.dumps(all_configs, indent=2)
            syntax_language = "json"
        elif client == "codex":
            codex_servers = _get_config_snippet("codex", python_exe, project_root)["mcp_servers"]
            json_str = _codex_toml_block(codex_servers["artemis"], codex_servers["artemis-hands"])
            syntax_language = "toml"
        else:
            snippet = _get_config_snippet(client, python_exe, project_root)
            json_str = json.dumps(snippet, indent=2)
            syntax_language = "json"

        syntax = Syntax(json_str, syntax_language, theme="monokai", line_numbers=False)
        console.print(f"[bold cyan]MCP Configuration for {client.upper()}:[/bold cyan]")
        console.print(syntax)
        raise typer.Exit(0)

    threading.Thread(target=start_awake_service, daemon=True, name="artemis-awake-init").start()
    try:
        st = server_type.lower()
        if st in ("agent", "mobile", "artemis", "default"):
            logger.info(f"Starting Artemis Mobile Agent MCP Server over {transport}...")
            if transport.lower() == "sse":
                agent_mcp.run(transport="sse", host=host, port=port)
            else:
                agent_mcp.run(transport="stdio")
        elif st == "hands":
            from artemis.mcp.hands_server import main as hands_main

            logger.info(f"Starting Artemis Hands MCP Server over {transport}...")
            hands_main(transport=transport, host=host, port=port, manage_awake=False)
        elif st == "adb":
            if transport.lower() == "sse":
                logger.info(f"Starting Artemis ADB MCP Server over {transport}...")
                adb_mcp.run(transport="sse", host=host, port=port)
            else:
                # stdio carries the MCP JSON-RPC stream: redirect logging and detach
                # from the parent DataEngine before any output can corrupt it.
                from artemis.mcp.adb_server import configure_stdio_mode

                configure_stdio_mode()
                logger.info("Starting Artemis ADB MCP Server over stdio...")
                adb_mcp.run(transport="stdio")
        elif st == "xml":
            from artemis.mcp.xml_search_server import mcp as xml_mcp

            logger.info(f"Starting Artemis XML Fuzzy Search MCP Server over {transport}...")
            if transport.lower() == "sse":
                xml_mcp.run(transport="sse", host=host, port=port)
            else:
                xml_mcp.run(transport="stdio")
        else:
            logger.error(
                f"Unsupported MCP server type: {server_type}. Use 'agent', 'hands', 'adb', or 'xml'."
            )
            raise typer.Exit(1)
    finally:
        shutdown_awake_service()
