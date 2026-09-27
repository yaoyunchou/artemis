from apps.admin_console.services import personal_task_runner
from apps.admin_console.services.personal_task_store import PersonalTaskStore
from apps.admin_console.services.prompt_recipe import (
    AI_LIMIT_SECONDS,
    SCRIPT_LIMIT_SECONDS,
    budget_result,
    compile_recipe,
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


class _FakePhone:
    def __init__(self, elements):
        self.elements = elements
        self.taps: list[tuple[int, int]] = []

    def dump(self):
        return "", self.elements

    def swipe_list(self, *, toward_top: bool) -> None:
        return None

    def tap(self, x_pos: int, y_pos: int) -> None:
        self.taps.append((x_pos, y_pos))

    def back(self) -> None:
        return None


def test_script_completes_only_after_the_prompt_done_button_is_tapped():
    phone = _FakePhone(
        [
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
    )
    assert status == "completed"
    assert "领取奖励" in reason
    assert phone.taps == [(900, 540)]
    assert "点了「领取奖励」" in log_text


def test_script_timeout_records_the_reason_and_the_parent_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(personal_task_runner, "SCRIPT_LIMIT_SECONDS", 0.05)
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
