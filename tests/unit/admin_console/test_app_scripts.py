import shutil
from pathlib import Path

import pytest

from apps.admin_console.services import app_script_host as host
from apps.admin_console.services import app_script_maintainer as maintainer
from apps.admin_console.services.personal_task_store import PersonalTaskStore

PACKAGE = "com.taobao.idlefish"
REPO_SCRIPTS = Path(__file__).resolve().parents[3] / "tasks" / "scripts"
ORIGINAL = (REPO_SCRIPTS / "idlefish.py").read_text(encoding="utf-8")


@pytest.fixture
def scripts(tmp_path, monkeypatch):
    """A private copy of tasks/scripts so tests never touch the real file or DB."""
    folder = tmp_path / "scripts"
    shutil.copytree(REPO_SCRIPTS, folder)
    monkeypatch.setattr(host, "SCRIPTS_DIR", folder)
    store = PersonalTaskStore(tmp_path / "scripts.db")
    return folder, store


def _module():
    return host.load_module(ORIGINAL, PACKAGE)


# ------------------------------------------------------------------ the 闲鱼 script


def test_browse_reward_page_is_closed_instead_of_treated_as_the_list():
    """看视频打开的「点击浏览商品 N 秒拿奖励」不是任务列表，先点右上角关闭。"""
    script = _module()
    case = next(item for item in host.load_fixtures(PACKAGE) if item["name"] == "browse_reward")
    elements = case["elements"]
    assert script.on_task_list(elements) is False
    assert script._is_ad(elements) is True
    close = script._ad_close_point(elements)
    assert close is not None
    assert 986 <= close[0] <= 1054
    assert 158 <= close[1] <= 226

    sleeps: list[float] = []
    script.time.sleep = lambda seconds: sleeps.append(seconds)

    class _Phone:
        def __init__(self) -> None:
            self.screens = [elements, [{"text": "任务奖励", "bounds": [40, 400, 200, 460]}]]
            self.taps: list[tuple[int, int]] = []

        def dump(self):
            return "", self.screens[0 if not self.taps else 1]

        def tap(self, x_pos: int, y_pos: int) -> None:
            self.taps.append((x_pos, y_pos))

        def back(self) -> None:
            raise AssertionError("关闭按钮还在时不该先按返回")

        def launch(self, _package: str) -> None:
            raise AssertionError("关闭按钮还在时不该先重开闲鱼")

    phone = _Phone()
    assert script._leave_ad(phone) is True
    assert phone.taps == [close]
    assert 3.0 not in sleeps


def test_idlefish_passes_its_labeled_screens():
    assert host.replay(_module(), host.load_fixtures(PACKAGE)) == []
    assert {case["name"] for case in host.load_fixtures(PACKAGE)} >= {"home", "board", "task_list"}


def test_dice_and_earn_dice_come_from_the_badge_when_there_is_no_text():
    script = _module()
    board = [
        {"text": "", "bounds": [0, 0, 1080, 2400]},
        {"text": "×6", "bounds": [601, 942, 695, 997]},
    ]
    assert script._dice_left(board) == 6
    assert script._dice_point(board) == (648, 1097)
    assert script._earn_dice_point(board) == (847, 1079)
    board.append({"text": "领", "bounds": [813, 1055, 882, 1105]})
    assert script._earn_dice_point(board) == (847, 1080)


def test_home_badge_sits_above_the_search_button():
    script = _module()
    point = script._home_badge_point(
        [{"text": "搜索，按钮", "resource_id": f"{PACKAGE}:id/search_btn", "bounds": [921, 214, 1037, 288]}]
    )
    assert 960 <= point[0] <= 1040
    assert 100 <= point[1] <= 180


def test_board_label_is_not_counted_as_the_row_it_is_part_of():
    rows = _module().row_titles(
        [
            {"text": "任务奖励", "bounds": [840, 317, 1050, 391]},
            {"text": "消了还想消", "bounds": [49, 1682, 233, 1732]},
            {"text": "去消了还想消玩1关", "bounds": [196, 1690, 540, 1748]},
            {"text": "+", "bounds": [254, 1756, 280, 1806]},
            {"text": "去完成", "bounds": [863, 1724, 971, 1774]},
            {"text": "向往的生活", "bounds": [651, 1700, 837, 1750]},
        ]
    )
    assert rows == ["去消了还想消玩1关"]


# ------------------------------------------------------------------ host checks


def test_check_source_blocks_imports_and_escape_hatches():
    problems = host.check_source("import os\ndef on_task_list(e):\n    return open('x').__class__\n")
    text = "；".join(problems)
    assert "不允许导入 os" in text
    assert "不允许使用 open" in text
    assert "不允许访问 __class__" in text
    assert "缺少函数" in text
    assert host.check_source(ORIGINAL) == []


def test_validate_rejects_a_script_that_breaks_a_labeled_screen():
    broken = ORIGINAL.replace(
        "return has_action and has_sheet", "return has_sheet"
    )
    with pytest.raises(host.ScriptRejected) as caught:
        host.validate(broken, PACKAGE)
    assert any("on_task_list" in problem for problem in caught.value.problems)


def test_first_load_seeds_version_one_and_a_hand_edit_becomes_version_two(scripts):
    folder, store = scripts
    first = host.load_app_script(PACKAGE, store)
    assert first.SCRIPT_VERSION == 1
    edited = ORIGINAL.replace("掷骰子 {rolled} 次", "掷了 {rolled} 次骰子")
    (folder / "idlefish.py").write_text(edited, encoding="utf-8")
    second = host.load_app_script(PACKAGE, store)
    assert second.SCRIPT_VERSION == 2
    assert store.current_script(PACKAGE)["reason"] == "手动修改了脚本文件"


def test_a_broken_hand_edit_keeps_the_current_version(scripts):
    folder, store = scripts
    host.load_app_script(PACKAGE, store)
    (folder / "idlefish.py").write_text("import os\n", encoding="utf-8")
    module = host.load_app_script(PACKAGE, store)
    assert module.SCRIPT_VERSION == 1
    assert (folder / "idlefish.py").read_text(encoding="utf-8") == ORIGINAL


# ------------------------------------------------------------------ maintainer


def _report(**overrides):
    report = {"entered": True, "completed": 3, "failed": 0, "script_failures": [], "stopped_with_pending": []}
    report.update(overrides)
    return report


def test_a_clean_run_records_the_result_and_asks_nothing(scripts):
    _folder, store = scripts
    module = host.load_app_script(PACKAGE, store)

    def never(_prompt):
        raise AssertionError("model should not be called")

    message = maintainer.maintain(
        store,
        package=PACKAGE,
        version_id=module.SCRIPT_VERSION_ID,
        report=_report(),
        evidence=[],
        stdout_tail="",
        subtask_logs=[],
        ask=never,
    )
    assert message == ""
    assert store.current_script(PACKAGE)["last_completed"] == 3


def test_a_stuck_run_gets_a_new_version_that_passes_checks(scripts):
    folder, store = scripts
    module = host.load_app_script(PACKAGE, store)
    fixed = ORIGINAL.replace("dice_taps < 3", "dice_taps < 4")
    prompts: list[str] = []

    def ask(prompt):
        prompts.append(prompt)
        return f"REASON: 多点一次赚骰子\n```python\n{fixed}```"

    message = maintainer.maintain(
        store,
        package=PACKAGE,
        version_id=module.SCRIPT_VERSION_ID,
        report=_report(entered=False, completed=0),
        evidence=[],
        stdout_tail="还没进列表（第 10 次）",
        subtask_logs=[],
        ask=ask,
    )
    assert "第 2 版" in message
    assert "没有进入任务奖励列表" in prompts[0]
    assert store.current_script(PACKAGE)["reason"] == "多点一次赚骰子"
    assert "dice_taps < 4" in (folder / "idlefish.py").read_text(encoding="utf-8")


def test_a_rewrite_that_fails_checks_is_stored_as_rejected(scripts):
    folder, store = scripts
    module = host.load_app_script(PACKAGE, store)
    message = maintainer.maintain(
        store,
        package=PACKAGE,
        version_id=module.SCRIPT_VERSION_ID,
        report=_report(script_failures=[{"title": "去蚂蚁庄园逛一逛", "reason": "没有回到列表"}]),
        evidence=[],
        stdout_tail="",
        subtask_logs=[],
        ask=lambda _prompt: "REASON: 坏的\n```python\nimport os\n```",
    )
    assert "没通过检查" in message
    statuses = [item["status"] for item in store.list_script_versions(PACKAGE)]
    assert statuses == ["rejected", "current"]
    assert (folder / "idlefish.py").read_text(encoding="utf-8") == ORIGINAL


def test_a_new_version_that_runs_worse_goes_back_to_its_parent(scripts):
    folder, store = scripts
    first = host.load_app_script(PACKAGE, store)
    store.record_script_result(first.SCRIPT_VERSION_ID, completed=5, failed=0, entered=True)
    newer = ORIGINAL.replace("dice_taps < 3", "dice_taps < 4")
    second = store.add_script_version(PACKAGE, newer, "试一下", status="current", parent_id=first.SCRIPT_VERSION_ID)
    host.write_current_file(PACKAGE, newer)
    message = maintainer.maintain(
        store,
        package=PACKAGE,
        version_id=second["id"],
        report=_report(completed=2),
        evidence=[],
        stdout_tail="",
        subtask_logs=[],
        ask=lambda _prompt: "",
    )
    assert "退回第 1 版" in message
    assert store.current_script(PACKAGE)["id"] == first.SCRIPT_VERSION_ID
    assert store.get_script_version(second["id"])["status"] == "rolled_back"
    assert (folder / "idlefish.py").read_text(encoding="utf-8") == ORIGINAL


def test_parse_answer_takes_the_code_block_and_reason():
    reason, code = maintainer.parse_answer("REASON: 改了入口\n说明\n```python\nx = 1\n```\n")
    assert reason == "改了入口"
    assert code == "x = 1\n"
