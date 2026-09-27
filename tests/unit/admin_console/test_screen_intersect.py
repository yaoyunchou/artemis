from apps.admin_console.services.screen_intersect import (
    matching_goals,
    parse_visible,
    title_from_goal,
    title_matches,
)


def test_parse_visible_line():
    text = "核对完毕。\nVISIBLE: 浏览推荐的国补商品 | 去蚂蚁庄园逛一逛 | 看15秒视频领奖励\n"
    assert parse_visible(text) == [
        "浏览推荐的国补商品",
        "去蚂蚁庄园逛一逛",
        "看15秒视频领奖励",
    ]


def test_title_from_goal_uses_the_line_after_the_stop_sentence():
    goal = "共享规则\n只执行下面这一条，做完就结束。\n去蚂蚁庄园逛一逛。约20秒"
    assert title_from_goal(goal) == "去蚂蚁庄园逛一逛"


def test_matching_goals_keeps_only_names_on_screen():
    goals = [
        "只执行下面这一条，做完就结束。\n618去淘金币赢20亿。约20秒",
        "只执行下面这一条，做完就结束。\n去蚂蚁庄园逛一逛。约20秒",
        "只执行下面这一条，做完就结束。\n看视频领奖励。等到跳过",
        "只执行下面这一条，做完就结束。\n进入闲鱼币任务奖励列表",
    ]
    visible = ["去蚂蚁庄园逛一逛", "看15秒视频领奖励", "浏览推荐的国补商品"]
    kept = matching_goals(goals, visible)
    assert [title_from_goal(goal) for goal in kept] == ["去蚂蚁庄园逛一逛", "看视频领奖励"]
    assert title_matches("618去淘金币赢20亿", visible) is False
