from apps.admin_console.services.task_script import (
    SCRIPT_GOAL,
    classify,
    compile_plan,
    parse_script_log,
    prepare_submission,
    row_action,
    split_row,
)


def _goal(title: str, detail: str) -> str:
    preamble = (
        "打开闲鱼。出现「跳过」就点。关闭广告。禁止点开始游戏。重新打开会掉进淘宝。\n"
        "只执行下面这一条，做完就结束。不要做清单里的其他事项。\n"
    )
    return f"{preamble}{title}\n{detail}"


def test_split_row_ignores_the_shared_preamble():
    title, detail = split_row(_goal("去蚂蚁庄园逛一逛", "浏览类，停留 15 秒。不要喂小鸡。"))
    assert title == "去蚂蚁庄园逛一逛"
    assert "关闭广告" not in detail
    assert "停留 15 秒" in detail


def test_preamble_does_not_change_the_row_kind():
    title, detail = split_row(_goal("去蚂蚁庄园逛一逛", "浏览类，停留 15 秒。记为跳过。"))
    assert classify(title, detail) == "browse"


def test_compile_plan_covers_the_fixed_shapes():
    goals = [
        _goal("去蚂蚁庄园逛一逛", "浏览类，停留 15 秒。按钮是「去完成」就进入。变成「领取奖励」再点一次。不要喂小鸡。"),
        _goal("去玩闲鱼小游戏15s", "是去完成则直接跳过。已经是领取奖励就点一次。禁止点开始游戏。"),
        _goal("发布一件新宝贝", "只有按钮已经是领取奖励才点。是去完成就直接结束。不要打开发布页。"),
        _goal("看视频领奖励", "按钮是「去完成」就进入。等到出现「跳过」或关闭。变成「领取奖励」再点。弹出放弃时点关闭广告。"),
        _goal("去一淘签到领现金", "按钮是「去完成」就进入。只点签到，然后返回。变成「领取奖励」再点一次。"),
        _goal("去某个没见过的活动", "按页面上的特殊按钮操作，规则临时再说。"),
    ]
    plan = compile_plan(goals)
    assert plan is not None
    assert plan["package"] == "com.taobao.idlefish"
    kinds = [row["kind"] for row in plan["rows"]]
    assert kinds == ["browse", "skip", "claim_only", "video", "sign", "model"]
    assert plan["rows"][0]["seconds"] == 15
    assert "不要做其他行" in plan["rows"][5]["fallback"]


def test_one_scriptable_row_stays_on_the_model_path():
    goals = [
        _goal("去蚂蚁庄园逛一逛", "浏览类，停留 15 秒。"),
        _goal("去某个没见过的活动", "按页面上的特殊按钮操作。"),
    ]
    assert compile_plan(goals) is None


def test_prepare_submission_uses_one_script_instead_of_a_scout():
    goals = [
        _goal("去蚂蚁庄园逛一逛", "浏览类，停留 15 秒。按钮是「去完成」就进入。变成「领取奖励」再点一次。"),
        _goal("去百度逛一逛", "浏览类，停留 15 秒。按钮是「去完成」就进入。变成「领取奖励」再点一次。"),
    ]
    scripted = prepare_submission(goals, screen_intersect=True, allow_list_script=True)
    assert scripted.goals == [SCRIPT_GOAL]
    assert scripted.plan is not None
    assert scripted.followups is None

    scout = prepare_submission(goals, screen_intersect=True, allow_list_script=False)
    assert scout.plan is None
    assert scout.followups == goals
    assert "VISIBLE:" in scout.goals[0]


def test_quoted_title_from_a_single_sentence_goal():
    goal = (
        "当前应在闲鱼币任务奖励列表。只处理「去百度逛一逛」。"
        "是去完成就互动约20秒，出现任务完成就返回并领取。"
    )
    title, _detail = split_row(goal)
    assert title == "去百度逛一逛"
    assert classify(title, _detail) == "browse"


def test_row_action_picks_the_button_on_the_same_band():
    elements = [
        {"text": "去蚂蚁庄园逛一逛", "bounds": [40, 500, 700, 580]},
        {"text": "领取奖励", "bounds": [800, 510, 1000, 570]},
        {"text": "去淘宝闪购逛一逛", "bounds": [40, 800, 700, 880]},
        {"text": "去完成", "bounds": [800, 810, 1000, 870]},
    ]
    assert row_action(elements, "去蚂蚁庄园逛一逛") == ("领取奖励", (900, 540))
    assert row_action(elements, "去淘宝闪购逛一逛") == ("去完成", (900, 840))


def test_parse_script_log_keeps_only_failed_indexes():
    text = "[脚本] 甲 → 已领取\nSCRIPT_RAN\nSCRIPT_FAIL: 1\nSCRIPT_FAIL: 4\n"
    assert parse_script_log(text) == ("ran", [1, 4])
    assert parse_script_log("脚本未能进入\nSCRIPT_BLOCKED\n") == ("blocked", [])
    assert parse_script_log("SCRIPT_RAN\nSCRIPT_BLOCKED\nSCRIPT_FAIL: 2\n") == ("ran", [2])
