from apps.admin_console.services import personal_task_runner
import time
from pathlib import Path

from apps.admin_console.services.app_script_host import load_module
from apps.admin_console.services.task_script import is_locked

_SCRIPT = Path(__file__).resolve().parents[3] / "tasks" / "scripts" / "idlefish.py"


def _idlefish():
    return load_module(_SCRIPT.read_text(encoding="utf-8"), "com.taobao.idlefish")
from apps.admin_console.services.personal_task_store import PersonalTaskStore
from apps.admin_console.services.prompt_recipe import (
    AI_LIMIT_SECONDS,
    SCRIPT_LIMIT_SECONDS,
    budget_result,
    compile_recipe,
    drop_numbered_rows,
    mark_row_skip,
    should_drop_row,
    split_saved_prompt,
)


def test_recipe_uses_buttons_written_in_the_prompt():
    recipe = compile_recipe(
        "浏览推荐的国补商品",
        "浏览 15 秒。按钮是「去完成」就进入。回到列表后变成「领取奖励」再点一次。不要点商品。",
    )
    assert recipe is not None
    assert recipe["kind"] == "browse"
    assert recipe["seconds"] == 15
    assert recipe["start_button"] == "去完成"
    assert recipe["done_button"] == "领取奖励"
    assert recipe["limit_seconds"] == SCRIPT_LIMIT_SECONDS
    assert AI_LIMIT_SECONDS == 120


def test_recipe_keeps_a_different_button_pair():
    recipe = compile_recipe(
        "去看看新货",
        "按钮是「去看看」就进入后停留 15 秒。回到列表后若变成「收下」再点一次。",
    )
    assert recipe is not None
    assert recipe["start_button"] == "去看看"
    assert recipe["done_button"] == "收下"
    assert recipe["start_button"] != "去完成"
    assert recipe["done_button"] != "领取奖励"


def test_task_list_row_defaults_to_the_popup_buttons():
    recipe = compile_recipe("去百度逛一逛", "约20秒。不要点商品。")
    assert recipe is not None
    assert recipe["kind"] == "browse"
    assert recipe["start_button"] == "去完成"
    assert recipe["done_button"] == "领取奖励"


def test_prompt_without_a_button_pair_stays_on_the_model():
    assert compile_recipe("看看电量", "打开设置，停留 10 秒。") is None
    rows = split_saved_prompt("打开设置看看电量")
    assert len(rows) == 1
    assert rows[0]["runner"] == "ai"
    assert rows[0]["script"] is None


def test_numbered_prompt_splits_and_compiles_each_row_from_its_own_text():
    raw = """
打开闲鱼。去完成和领取奖励写在总说明里也不该套到没写按钮的行。
1. 浏览推荐的国补商品
浏览 15 秒。按钮是「去完成」就进入。变成「领取奖励」再点一次。
2. 去某个没见过的活动
按页面上的特殊按钮操作。
"""
    rows = split_saved_prompt(raw)
    assert [row["runner"] for row in rows] == ["script", "ai"]
    assert rows[0]["script"]["seconds"] == 15
    assert "特殊按钮" in rows[1]["prompt"]


def test_three_failures_mark_the_row_skip_and_screen_order_is_not_the_catalog_order():
    prompt = "打开闲鱼。\n1. 去百度逛一逛。约20秒。\n"
    marked = mark_row_skip(prompt, "去百度逛一逛")
    assert "直接跳过" in marked
    assert personal_task_runner._is_skipped({"prompt": marked, "detail": "", "fail_count": 0, "script": None})
    hit = personal_task_runner._catalog_hit(
        "去百度逛一逛 +80",
        [{"title": "去百度逛一逛", "prompt": "约20秒"}],
    )
    assert hit is not None
    assert personal_task_runner._catalog_hit("去河马剧场看优质短剧", [{"title": "去百度逛一逛"}]) is None


def test_failed_missing_row_is_removed_from_the_saved_prompt():
    prompt = "打开闲鱼。\n1. 去蚂蚁庄园逛一逛。约20秒。\n2. 618去淘金币赢20亿。约20秒。\n3. 去百度逛一逛。约20秒。\n"
    assert should_drop_row("failed", "列表中不存在「618去淘金币赢20亿」", "")
    assert should_drop_row("failed", "是否确认开始下载一淘", "")
    assert not should_drop_row("completed", "列表中不存在", "")
    updated = drop_numbered_rows(prompt, ["618去淘金币赢20亿"])
    assert "618" not in updated
    assert "去蚂蚁庄园逛一逛" in updated
    assert "去百度逛一逛" in updated


def test_timeout_skips_only_that_subtask():
    status, reason = budget_result(
        elapsed=60,
        limit=SCRIPT_LIMIT_SECONDS,
        done=False,
        stage="活动页",
        done_button="领取奖励",
    )
    assert status == "failed"
    assert "60" in reason
    assert "领取奖励" in reason
    assert "活动页" in reason

    ai_status, ai_reason = budget_result(
        elapsed=120,
        limit=AI_LIMIT_SECONDS,
        done=False,
        stage="模型仍在执行",
        done_button="收下",
    )
    assert ai_status == "failed"
    assert "120" in ai_reason
    assert "收下" in ai_reason


def test_parent_task_has_no_timeout_field(tmp_path):
    store = PersonalTaskStore(tmp_path / "personal.db")
    task = store.create_task(
        "1. 浏览推荐的国补商品\n浏览 15 秒。按钮是「去完成」就进入。变成「领取奖励」再点一次。\n"
        "2. 去蚂蚁庄园逛一逛\n浏览 15 秒。按钮是「去完成」就进入。变成「领取奖励」再点一次。\n"
    )
    assert "timeout" not in task
    assert "deadline" not in task
    assert "limit_seconds" not in task
    assert task["subtasks"][0]["runner"] == "script"

    store.update_subtask_prompt(task["id"], task["subtasks"][1]["id"], "这一条改成手动说明，没有按钮。")
    updated = store.update_prompt(
        task["id"],
        "1. 浏览推荐的国补商品\n浏览 20 秒。按钮是「去完成」就进入。变成「领取奖励」再点一次。\n"
        "2. 去蚂蚁庄园逛一逛\n浏览 15 秒。按钮是「去完成」就进入。变成「领取奖励」再点一次。\n",
    )
    edited = next(item for item in updated["subtasks"] if item["title"] == "去蚂蚁庄园逛一逛")
    assert edited["edited"] is True
    assert edited["runner"] == "ai"
    untouched = next(item for item in updated["subtasks"] if item["title"] == "浏览推荐的国补商品")
    assert untouched["script"]["seconds"] == 20
    assert updated["subtasks"][0]["last_status"] is None

    run = store.start_run(task["id"])
    store.add_subtask_log(
        run_id=run["id"],
        subtask_id=untouched["id"],
        position=0,
        title=untouched["title"],
        runner="script",
        status="completed",
        reason="已点领取奖励",
        log_text="ok",
        started_at=1,
        finished_at=2,
    )
    recorded = store.get_task(task["id"])
    done = next(item for item in recorded["subtasks"] if item["id"] == untouched["id"])
    assert done["last_status"] == "completed"
    assert done["last_reason"] == "已点领取奖励"

    remaining = store.delete_subtask(task["id"], edited["id"])
    assert [item["title"] for item in remaining["subtasks"]] == ["浏览推荐的国补商品"]


class _FakePhone:
    def __init__(self, elements):
        self.elements = elements
        self.taps: list[tuple[int, int]] = []

        self.notes: list[tuple[str, str, str]] = []
        self.last_xml = ""

    def dump(self):
        return "", self.elements

    def swipe_list(self, *, toward_top: bool) -> None:
        return None

    def tap(self, x_pos: int, y_pos: int) -> None:
        self.taps.append((x_pos, y_pos))

    def back(self) -> None:
        return None

    def launch(self, package: str) -> None:
        return None

    def note(self, tag: str, text: str = "") -> None:
        self.notes.append((tag, text, ""))


class _SteppedPhone(_FakePhone):
    """Each tap advances to the next screen."""

    def __init__(self, screens: list[list[dict]]):
        super().__init__(screens[0])
        self.screens = screens
        self.index = 0

    def dump(self):
        self.elements = self.screens[min(self.index, len(self.screens) - 1)]
        return "", self.elements

    def tap(self, x_pos: int, y_pos: int) -> None:
        super().tap(x_pos, y_pos)
        self.index += 1


def test_script_completes_only_after_the_prompt_done_button_is_tapped():
    phone = _FakePhone(
        [
            {"text": "任务奖励", "bounds": [840, 200, 1050, 280]},
            {"text": "浏览推荐的国补商品", "bounds": [40, 500, 700, 580]},
            {"text": "领取奖励", "bounds": [800, 510, 1000, 570]},
        ]
    )
    status, reason, log_text = personal_task_runner.run_script_subtask(
        phone,
        {
            "title": "浏览推荐的国补商品",
            "script": {
                "kind": "browse",
                "seconds": 15,
                "start_button": "去完成",
                "done_button": "领取奖励",
                "done_hint": None,
            },
        },
        _idlefish(),
    )
    assert status == "completed"
    assert "领取奖励" in reason
    assert phone.taps == [(900, 540)]
    assert "点了「领取奖励」" in log_text


def test_script_timeout_records_the_reason_and_the_parent_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(personal_task_runner, "SCRIPT_LIMIT_SECONDS", 0.05)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    phone = _FakePhone([])
    status, reason, log_text = personal_task_runner.run_script_subtask(
        phone,
        {
            "title": "浏览推荐的国补商品",
            "script": {
                "kind": "browse",
                "seconds": 15,
                "start_button": "去完成",
                "done_button": "领取奖励",
                "done_hint": None,
            },
        },
        _idlefish(),
    )
    assert status == "failed"
    assert "超过 0 秒" in reason or "超过 0 秒" in log_text or "未找到这一行" in log_text
    assert "领取奖励" in reason

    monkeypatch.setattr(
        personal_task_runner,
        "run_model_subtask",
        lambda *_args, **_kwargs: ("failed", "超过 120 秒仍未完成", "模型日志"),
    )
    store = PersonalTaskStore(tmp_path / "runs.db")
    task = store.create_task(
        "1. 浏览推荐的国补商品\n浏览 15 秒。按钮是「去完成」就进入。变成「领取奖励」再点一次。\n"
        "2. 去某个没见过的活动\n按页面上的特殊按钮操作。\n"
    )
    run = store.start_run(task["id"])
    personal_task_runner.execute(store, task["id"], run["id"], "")
    saved = store.get_run(run["id"])
    assert saved["status"] == "partial"
    assert "timeout" not in saved
    assert "deadline" not in saved
    assert [item["status"] for item in saved["logs"]] == ["failed", "failed"]
    assert saved["logs"][1]["reason"] == "超过 120 秒仍未完成"
    assert saved["logs"][1]["log_text"] == "模型日志"


def test_lock_screen_is_recognised():
    xml = '<hierarchy><node package="com.android.systemui" /></hierarchy>'
    assert is_locked(xml, [{"text": "请用数字密码或指纹解锁", "bounds": [254, 529, 826, 599]}])
    assert not is_locked('<node package="com.taobao.idlefish" />', [{"text": "解锁", "bounds": [0, 0, 1, 1]}])


def test_a_model_transcript_does_not_mark_a_real_row_skipped():
    reason = "超过 120 秒仍未完成。最后输出：页面上这个按钮不存在，开始循环"
    assert should_drop_row("failed", reason, reason) is False
    assert should_drop_row("failed", "列表里没有这一行", "") is True


def test_visible_catalog_row_runs_before_an_unknown_row():
    catalog = [
        {"title": "去蚂蚁庄园逛一逛", "prompt": "约20秒", "script": {"kind": "browse"}, "fail_count": 0},
    ]
    picked = personal_task_runner._pick_row(
        ["点闪购商品领叠加红包", "去蚂蚁庄园逛一逛"],
        catalog,
        set(),
    )
    assert picked == ("去蚂蚁庄园逛一逛", catalog[0])


def test_an_open_page_is_finished_from_its_own_content():
    phone = _SteppedPhone(
        [
            [
                {"text": "浏览推荐的国补商品", "bounds": [40, 400, 700, 480]},
                {"text": "任务完成", "bounds": [400, 1800, 680, 1880]},
            ],
            [
                {"text": "任务奖励", "bounds": [840, 200, 1050, 280]},
                {"text": "浏览推荐的国补商品", "bounds": [40, 500, 700, 580]},
                {"text": "领取奖励", "bounds": [800, 510, 1000, 570]},
            ],
        ]
    )
    status, reason, log_text = personal_task_runner.run_script_subtask(
        phone,
        {
            "title": "浏览推荐的国补商品",
            "script": {
                "kind": "browse",
                "seconds": 15,
                "start_button": "去完成",
                "done_button": "领取奖励",
                "done_hint": "任务完成",
            },
        },
        _idlefish(),
    )
    assert status == "completed"
    assert "领取奖励" in reason
    assert "页面上点了「任务完成」" in log_text
    assert phone.taps[0] == (540, 1840)
    assert phone.taps[1] == (900, 540)


def test_a_page_that_is_not_the_list_does_not_stop_the_run():
    pending = ["去蚂蚁庄园逛一逛"]
    assert (
        personal_task_runner._when_nothing_picked(
            on_list=False,
            pending=pending,
            lines=["商品详情", "加入购物车"],
            scrolls=4,
            stop_checks=2,
        )
        == "work-page"
    )
    assert personal_task_runner._pending_on_page(pending, ["商品"]) is None
    assert (
        personal_task_runner._pending_on_page(
            ["浏览推荐的国补商品"],
            ["商品详情", "浏览推荐的国补商品", "任务完成"],
        )
        == "浏览推荐的国补商品"
    )
    assert (
        personal_task_runner._when_nothing_picked(
            on_list=True,
            pending=pending,
            lines=["别的任务", "去完成"],
            scrolls=4,
            stop_checks=2,
        )
        == "stop"
    )


def test_stop_only_after_pending_rows_are_finished_or_skipped():
    catalog = [
        {"title": "进入闲鱼币任务奖励列表", "prompt": "进入", "script": None, "fail_count": 0},
        {"title": "去蚂蚁庄园逛一逛", "prompt": "约20秒", "script": {"kind": "browse"}, "fail_count": 0},
        {"title": "去玩闲鱼小游戏15s", "prompt": "直接跳过", "script": {"kind": "skip"}, "fail_count": 0},
    ]
    assert personal_task_runner._pending_titles(catalog, set()) == ["去蚂蚁庄园逛一逛"]
    assert personal_task_runner._pending_titles(catalog, {"去蚂蚁庄园逛一逛"}) == []
