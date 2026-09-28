"""Load the App script for a package, check it, and keep its versions in step.

The database holds every version. ``tasks/scripts/<name>.py`` mirrors the
current one so it can be opened and read. A hand edit to that file becomes a
new version on the next load, if it passes the same checks as an AI edit.
"""

from __future__ import annotations

import ast
import json
import sys
import types
from pathlib import Path
from typing import Any

from artemis.config import WORKSPACE_ROOT
from artemis.drivers.android.hierarchy import parse_ui_hierarchy

from apps.admin_console.services import app_script_helpers
from apps.admin_console.services.personal_task_store import PersonalTaskStore

SCRIPTS_DIR = WORKSPACE_ROOT / "tasks" / "scripts"
REQUIRED = (
    "on_task_list",
    "row_titles",
    "is_skip_row",
    "enter_list",
    "return_to_list",
    "roll_dice",
    "claim_visible",
)
ALLOWED_IMPORTS = {"__future__", "time", "re", "typing", app_script_helpers.MODULE_NAME}
BANNED_NAMES = {
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
    "input",
    "breakpoint",
    "exit",
    "quit",
}


class ScriptRejected(ValueError):
    """A script that fails the checks. ``problems`` says which ones."""

    def __init__(self, problems: list[str]):
        super().__init__("；".join(problems))
        self.problems = problems


def script_name(package: str) -> str:
    return package.rsplit(".", 1)[-1] or package


def script_path(package: str) -> Path:
    return SCRIPTS_DIR / f"{script_name(package)}.py"


def fixtures_dir(package: str) -> Path:
    return SCRIPTS_DIR / "fixtures" / script_name(package)


def check_source(code: str) -> list[str]:
    """Static checks: parses, only allowed imports, no escape hatches, full interface."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"语法错误：第 {exc.lineno} 行 {exc.msg}"]
    problems: list[str] = []
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    problems.append(f"不允许导入 {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                problems.append(f"不允许导入 {node.module}")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"不允许使用 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"不允许访问 {node.attr}")
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            defined.add(node.name)
    missing = [name for name in REQUIRED if name not in defined]
    if missing:
        problems.append(f"缺少函数：{'、'.join(missing)}")
    return problems


def load_module(code: str, package: str) -> types.ModuleType:
    """Execute a checked script as its own module."""
    problems = check_source(code)
    if problems:
        raise ScriptRejected(problems)
    sys.modules.setdefault(app_script_helpers.MODULE_NAME, app_script_helpers)
    module = types.ModuleType(f"app_script_{script_name(package)}")
    module.__file__ = str(script_path(package))
    try:
        exec(compile(code, module.__file__, "exec"), module.__dict__)  # noqa: S102
    except Exception as exc:
        raise ScriptRejected([f"加载时出错：{exc}"]) from exc
    return module


def load_fixtures(package: str) -> list[dict[str, Any]]:
    """Labeled screens: ``<name>.xml`` plus ``<name>.json`` with the expected answers."""
    folder = fixtures_dir(package)
    cases: list[dict[str, Any]] = []
    if not folder.is_dir():
        return cases
    for xml_path in sorted(folder.glob("*.xml")):
        expect_path = xml_path.with_suffix(".json")
        expect = json.loads(expect_path.read_text(encoding="utf-8")) if expect_path.exists() else {}
        cases.append(
            {
                "name": xml_path.stem,
                "elements": parse_ui_hierarchy(xml_path.read_text(encoding="utf-8")),
                "expect": expect,
            }
        )
    return cases


def replay(module: types.ModuleType, cases: list[dict[str, Any]]) -> list[str]:
    """Run the read-only functions on saved screens and compare with the labels.

    A case without labels only has to not raise, so fresh evidence from a run
    still guards against a script that crashes on the screen that broke it.
    """
    problems: list[str] = []
    for case in cases:
        name = case["name"]
        elements = case["elements"]
        expect = case.get("expect") or {}
        try:
            on_list = bool(module.on_task_list(elements))
            rows = list(module.row_titles(elements))
            for title in rows:
                module.is_skip_row(title)
        except Exception as exc:
            problems.append(f"{name}：读屏出错 {exc}")
            continue
        if "on_task_list" in expect and on_list != bool(expect["on_task_list"]):
            problems.append(f"{name}：on_task_list 应为 {expect['on_task_list']}，实际 {on_list}")
        for title in expect.get("rows_include") or []:
            if title not in rows:
                problems.append(f"{name}：row_titles 缺少「{title}」")
        for title in expect.get("rows_exclude") or []:
            if title in rows:
                problems.append(f"{name}：row_titles 不该有「{title}」")
        for title in expect.get("skip") or []:
            if not module.is_skip_row(title):
                problems.append(f"{name}：「{title}」应跳过")
        for title in expect.get("keep") or []:
            if module.is_skip_row(title):
                problems.append(f"{name}：「{title}」不该跳过")
    return problems


def validate(code: str, package: str, extra_cases: list[dict[str, Any]] | None = None) -> types.ModuleType:
    """Static checks, load, then replay on the labeled screens and any extra evidence."""
    module = load_module(code, package)
    problems = replay(module, load_fixtures(package) + list(extra_cases or []))
    if problems:
        raise ScriptRejected(problems)
    return module


def write_current_file(package: str, code: str) -> None:
    path = script_path(package)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")


def _sync_from_file(store: PersonalTaskStore, package: str) -> dict[str, Any] | None:
    """Seed version 1 from the file, or take a hand edit of the file as a new version."""
    current = store.current_script(package)
    path = script_path(package)
    if not path.exists():
        return current
    code = path.read_text(encoding="utf-8")
    if current is not None and current["code"] == code:
        return current
    try:
        validate(code, package)
    except ScriptRejected as exc:
        if current is None:
            raise
        print(f"脚本文件改动没通过检查，继续用第 {current['version']} 版：{exc}", flush=True)
        write_current_file(package, current["code"])
        return current
    reason = "初始版本" if current is None else "手动修改了脚本文件"
    return store.add_script_version(
        package,
        code,
        reason,
        status="current",
        parent_id=None if current is None else current["id"],
    )


def load_app_script(package: str, store: PersonalTaskStore | None = None) -> types.ModuleType:
    """The current version as a module. A broken current falls back to the last one that loads."""
    store = store or PersonalTaskStore()
    current = _sync_from_file(store, package)
    if current is None:
        raise FileNotFoundError(f"没有 {package} 的 App 脚本：{script_path(package)}")
    candidates = [current] + [
        item
        for item in store.list_script_versions(package)
        if item["id"] != current["id"] and item["status"] == "retired"
    ]
    last_error: Exception | None = None
    for item in candidates:
        try:
            module = load_module(str(item["code"]), package)
        except ScriptRejected as exc:
            last_error = exc
            store.mark_script(str(item["id"]), "rejected", f"加载失败：{exc}")
            continue
        if item["id"] != current["id"]:
            store.set_current_script(str(item["id"]), previous_status="rejected")
            write_current_file(package, str(item["code"]))
            print(f"当前脚本加载失败，退回第 {item['version']} 版", flush=True)
        module.SCRIPT_VERSION_ID = str(item["id"])
        module.SCRIPT_VERSION = int(item["version"])
        return module
    raise RuntimeError(f"{package} 没有能加载的脚本版本：{last_error}")
