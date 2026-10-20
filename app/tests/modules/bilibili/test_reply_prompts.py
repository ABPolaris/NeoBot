"""B站提示词拼装测试：B站差异规则、评论树标记、私信历史、任务/日记模板。"""

from __future__ import annotations

from datetime import datetime

from neobot_app.bilibili.prompts import (
    CommentContext,
    CommentNode,
    PrivateMessageContext,
    build_comment_prompt,
    build_diary_prompt,
    build_private_prompt,
    build_task_prompt,
)

_NOW = datetime(2026, 10, 19, 20, 30)


def _comment_context() -> CommentContext:
    return CommentContext(
        bot_name="Neo",
        bot_uid=1234,
        bot_data="你是一个可爱的机器人",
        target_oid=99,
        target_title="测试视频",
        target_up_name="某UP",
        target_desc="视频简介",
        target_url="https://www.bilibili.com/video/BV1xx",
        comment_tree=[
            CommentNode(
                rpid=1,
                uid=11,
                uname="张三",
                content="原评论",
                ctime=1_700_000_000,
                children=[
                    CommentNode(
                        rpid=2,
                        uid=22,
                        uname="李四",
                        content="楼中楼回复",
                        ctime=1_700_000_100,
                    )
                ],
            )
        ],
        reply_target_rpid=2,
        reply_root_rpid=1,
        current_time=_NOW,
    )


def test_comment_system_prompt_has_bilibili_rules() -> None:
    system, _ = build_comment_prompt(_comment_context())

    assert "<你是谁>" in system
    assert "B站" in system
    assert "不是QQ" in system
    assert "不懂装懂" in system
    assert "听着xx" in system
    assert "实际上xx" in system
    assert "调调" in system
    assert "[SKIP]" in system
    assert "楼中楼" in system


def test_comment_user_prompt_renders_target_and_tree() -> None:
    _, user = build_comment_prompt(_comment_context())

    assert "测试视频" in user
    assert "某UP" in user
    assert "★" in user
    assert "李四" in user
    assert "<待回复评论>" in user
    assert "楼中楼回复" in user


def test_comment_tree_truncates() -> None:
    ctx = _comment_context()
    ctx.comment_tree[0].children = [
        CommentNode(rpid=100 + index, uid=index, uname=f"路人{index}", content="x" * 10)
        for index in range(20)
    ]
    _, user = build_comment_prompt(ctx, max_comments=5)

    assert "已截断" in user
    assert "待回复评论" in user


def test_commenter_profiles_section() -> None:
    ctx = _comment_context()
    ctx.commenter_profiles = [
        {
            "uname": "张三",
            "uid": 11,
            "remark": "老熟人",
            "bilibili_memory": "喜欢鬼畜",
            "qq_memory": "QQ上聊过",
        }
    ]
    _, user = build_comment_prompt(ctx)

    assert "<评论者档案>" in user
    assert "老熟人" in user
    assert "喜欢鬼畜" in user
    assert "QQ上聊过" in user


def test_private_prompt_renders_history_and_memory() -> None:
    ctx = PrivateMessageContext(
        bot_name="Neo",
        bot_uid=1234,
        sender_name="王五",
        sender_uid=55,
        sender_remark="群友",
        sender_profile="爱问问题",
        memory_list="他上次问过天气",
        history=[
            {"role": "user", "content": "在吗", "sender_uid": 55, "time": 1_700_000_000},
            {"role": "bot", "content": "在", "time": 1_700_000_010},
        ],
        current_message="今天干嘛",
        current_time=_NOW,
    )
    system, user = build_private_prompt(ctx)

    assert "私信" in system
    assert "王五" in user
    assert "你的备注:群友" in user
    assert "他上次问过天气" in user
    assert "今天干嘛" in user
    assert "在吗" in user


def test_private_history_truncation() -> None:
    ctx = PrivateMessageContext(bot_name="Neo")
    ctx.history = [
        {"role": "user", "content": f"消息{index}", "time": 1_700_000_000 + index}
        for index in range(10)
    ]
    _, user = build_private_prompt(ctx, max_history=3)

    assert "仅显示最近 3 条" in user
    assert "消息9" in user
    assert "消息0" not in user


def test_task_prompt_contains_task_and_history() -> None:
    system, user = build_task_prompt(
        "告诉对方明天集合", history="对方:你好", bot_name="Neo", bot_uid=1234
    )

    assert "跨聊天任务" in system
    assert "[SKIP]" in system
    assert "告诉对方明天集合" in user
    assert "对方:你好" in user


def test_diary_prompt_contains_material_and_rules() -> None:
    system, user = build_diary_prompt(
        "今天聊了很多游戏", date_text="2026-10-19", bot_name="Neo", max_chars=300
    )

    assert "动态" in system
    assert "隐私" in system
    assert "2026-10-19" in user
    assert "今天聊了很多游戏" in user
    assert "300" in system


def test_diary_prompt_truncates_material() -> None:
    _, user = build_diary_prompt(
        "x" * 1000, date_text="2026-10-19", max_material_chars=100
    )

    assert "素材过长已截断" in user


def test_comment_prompt_renders_video_context() -> None:
    ctx = _comment_context()
    ctx.video_context = "标题：视频标题\n来源：site\n摘要：字幕摘要"

    _, user = build_comment_prompt(ctx)

    assert "<相关视频>" in user
    assert "标题：视频标题" in user
    assert "来源：site" in user
    assert "字幕摘要" in user


def test_private_prompt_renders_video_context() -> None:
    ctx = PrivateMessageContext(
        bot_name="Neo",
        sender_name="王五",
        sender_uid=55,
        current_message="看这个",
        video_context="标题：私信里的视频\n来源：asr\n摘要：语音转写",
    )

    _, user = build_private_prompt(ctx)

    assert "<相关视频>" in user
    assert "私信里的视频" in user
    assert "语音转写" in user
