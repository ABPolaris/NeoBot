"""Native-vision policy, budgets and automatic parser switching."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from neobot_app.image.parser import ImageParseService
from neobot_app.reply.orchestrator import ReplyOrchestrator
from neobot_app.reply.tools import ReplyToolExecutor
from neobot_app.reply.vision_context import append_image_context
from neobot_app.skills.base import SkillManager
from neobot_app.skills.image_context_skill import ImageContextSkill
from neobot_app.skills.image_parse_skill import ImageParseSkill


@pytest.mark.parametrize("native", [False, True])
async def test_tool_policy_blocks_hidden_tools_even_for_direct_calls(native):
    provider = SimpleNamespace(native_vision=native)
    manager = SkillManager()
    manager.register(ImageContextSkill())
    manager.register(ImageParseSkill(vision_provider=object()))
    executor = ReplyToolExecutor(skill_manager=manager, native_vision_provider=provider)
    try:
        names = {t["function"]["name"] for t in executor.definitions()}
        assert ("image_context__add_image" in names) is native
        assert ("image_parse__parse_image" in names) is not native
        forbidden = "image_parse__parse_image" if native else "image_context__add_image"
        assert "Error" in await executor.execute(forbidden, {})
        for name in ("drawing__inspect_image", "user_profile__analyze_user_avatar"):
            assert executor.is_tool_authorized(name) is not native
        assert executor.is_tool_authorized("vision_detect__detect")
        provider.native_vision = not native
        assert executor.is_tool_authorized(forbidden)
    finally:
        await executor.close()


async def test_native_vision_does_not_bypass_skill_allowlist():
    executor = ReplyToolExecutor(
        native_vision_provider=SimpleNamespace(native_vision=True),
        allowed_tools={"wait"},
    )
    try:
        assert not executor.is_tool_authorized("image_context__add_image")
    finally:
        await executor.close()


async def test_auto_parser_preserves_images_until_native_vision_falls_back():
    provider = SimpleNamespace(native_vision=True)
    service = ImageParseService(native_vision_provider=provider)
    message = SimpleNamespace(message=[{"type": "image", "data": {"file": "a.png"}}])
    service._parse_and_replace = AsyncMock()
    await service.parse_message_images(message, "1")
    assert not service._pending
    service._parse_and_replace.assert_not_awaited()
    assert message.message[0]["type"] == "image"
    provider.native_vision = False
    await service.parse_message_images(message, "1")
    await service.wait_for_queue("1")
    service._parse_and_replace.assert_awaited_once()


def test_image_token_estimate_does_not_count_base64():
    def part(value):
        return {"type": "image_url", "image_url": {"url": value}}

    small = ReplyOrchestrator._estimate_tokens([{"role": "user", "content": [part("a")]}])
    large = ReplyOrchestrator._estimate_tokens([{"role": "user", "content": [part("a" * 1_000_000)]}])
    assert small == large


@pytest.mark.parametrize("count", [0, 2, 4, 7])
async def test_automatic_count_and_end_appendix_never_limit_manual_images(count):
    from neobot_app.reply.vision_context import ReplyVisionContext
    from neobot_app.skills.image_context_skill import ImageContextResult

    entries = [SimpleNamespace(
        message=SimpleNamespace(message_id=i, message=[{"type": "image"}]),
        replied_messages=[],
    ) for i in range(1, 7)]
    queue = SimpleNamespace(entries=lambda key: entries)
    numbering = SimpleNamespace(mapping={i: i for i in range(1, 7)})

    async def execute(name, args):
        return ImageContextResult('{"ok": true}', [{"type": "image_url", "image_url": {"url": f"image-{args['msg_number']}"}}])

    loader = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    context = ReplyVisionContext()
    # Explicitly loading >16 images must never trigger count-based eviction.
    context.manual_parts = [{"type": "image_url", "image_url": {"url": f"manual-{i}"}} for i in range(20)]
    history = [{"role": "user", "content": "正文：[图片]"}, {"role": "tool", "tool_call_id": "1", "content": "工具正文"}]
    kwargs = dict(queue=queue, queue_key="1", numbering=numbering, loader=loader, pipeline_key="private:1", max_images=count)
    await context.refresh_defaults(**kwargs)
    request = context.request_messages(history)
    assert history == request[:-1]
    assert all(isinstance(m["content"], str) for m in history)
    appendix = request[-1]
    assert appendix["role"] == "user"
    parts = appendix["content"]
    assert sum(part["type"] == "image_url" for part in parts) == min(count, 6) + 20
    selected = list(range(1, 7))[-count:] if count else []
    assert [call.args[1]["msg_number"] for call in loader.execute.call_args_list] == selected
    for number in selected:
        assert any(f"正文消息编号 {number}" in p.get("text", "") for p in parts)
    await context.refresh_defaults(**kwargs)
    assert loader.execute.await_count == min(count, 6), "unchanged automatic images should be cached"
    history.append({"role": "user", "content": "新正文"})
    assert context.request_messages(history)[-2]["content"] == "新正文"
    await context.refresh_defaults(**{**kwargs, "max_images": 0})
    assert sum(p["type"] == "image_url" for p in context.request_messages(history)[-1]["content"]) == 20


async def test_default_loading_timeout_keeps_progress_not_stale_images():
    import asyncio
    from neobot_app.reply.vision_context import ReplyVisionContext
    from neobot_app.skills.image_context_skill import ImageContextResult

    def entry(number):
        return SimpleNamespace(message=SimpleNamespace(message_id=number, message=[{"type": "image"}]))

    entries = [entry(1)]
    queue = SimpleNamespace(entries=lambda key: entries)
    numbering = SimpleNamespace(mapping={1: 1, 2: 2, 3: 3})
    hanging = True

    async def execute(name, args):
        if hanging and args["msg_number"] == 3:
            await asyncio.Event().wait()
        return ImageContextResult('{"ok":true}', [{"type": "image_url", "image_url": {"url": str(args["msg_number"])}}])

    loader = SimpleNamespace(execute=execute)
    context = ReplyVisionContext()
    kwargs = dict(queue=queue, queue_key="1", numbering=numbering, loader=loader, pipeline_key="private:1", max_images=2)
    await context.refresh_defaults(**kwargs)
    entries[:] = [entry(2), entry(3)]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(context.refresh_defaults(**kwargs), timeout=0.02)
    parts = context.request_messages([])[-1]["content"]
    assert [p["image_url"]["url"] for p in parts if p["type"] == "image_url"] == ["2"]
    assert any("正文消息编号 3" in p.get("text", "") and "尚未加载" in p["text"] for p in parts)
    hanging = False
    await context.refresh_defaults(**kwargs)
    assert sum(p["type"] == "image_url" for p in context.request_messages([])[-1]["content"]) == 2


def test_default_auto_image_count_is_four_and_zero_is_supported():
    orch = ReplyOrchestrator.__new__(ReplyOrchestrator)
    orch._config = None
    assert orch._get_native_vision_default_image_count() == 4
    orch._config = SimpleNamespace(chat=SimpleNamespace(native_vision_default_image_count=0))
    assert orch._get_native_vision_default_image_count() == 0


def test_image_body_uses_default_description_not_payload():
    from neobot_app.message.queue import MessageQueue

    formatters = MessageQueue._segment_formatters()
    assert formatters["image"]({"file": "base64://secret-payload"}) == "[图片]"
    assert formatters["cardimage"]({"url": "https://private/image.png"}) == "[卡片图片]"
    assert formatters["image"]({"summary": "[动画表情]", "file": "image.png"}) == "[动画表情]"


def test_character_cache_estimator_cannot_extend_image_pipeline():
    orch = ReplyOrchestrator.__new__(ReplyOrchestrator)
    orch._cost_pipeline_enabled = lambda: True
    # Must return before using a calculator which only understands text prefixes.
    assert not orch._cache_continue_cheaper_than_restart([
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "image"}}]},
    ])


def test_vision_context_is_run_local():
    first, second = [], []
    append_image_context(first, [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}])
    append_image_context(second, [])
    assert first and not second


# ── fix(16):默认图与 add_image 统一发送者标注/坐标 ──────────────────

def _sender_queue(*, nicknames):
    """真实 MessageQueue：每 nickname 一条只带图片段的消息。"""
    from neobot_adapter.model.basic import PostMessageMessagesender
    from neobot_adapter.model.message import GroupMessage, MessageSegment
    from neobot_app.message.queue import MessageQueue

    queue = MessageQueue(timestamp_interval_seconds=10**9)
    for index, (nickname, user_id) in enumerate(nicknames):
        queue.push(
            "1",
            GroupMessage(
                message_id=101 + index,
                user_id=user_id,
                group_id=1,
                message=[MessageSegment(type="image", data={"file": "a.png"})],
                sender=PostMessageMessagesender(user_id=user_id, nickname=nickname),
            ),
            occurred_at=1_700_000_000 + index,
        )
    return queue


async def test_default_image_labels_carry_sender_with_duplicate_disambiguation():
    from neobot_app.reply.vision_context import ReplyVisionContext

    queue = _sender_queue(nicknames=[("小明", 10001), ("小明", 10002)])
    numbering = SimpleNamespace(mapping={1: 101, 2: 102})

    async def execute(name, args):
        from neobot_app.skills.image_context_skill import ImageContextResult

        return ImageContextResult(
            '{"ok": true}',
            [{"type": "image_url", "image_url": {"url": f"img-{args['msg_number']}"}}],
        )

    context = ReplyVisionContext()
    await context.refresh_defaults(
        queue=queue, queue_key="1", numbering=numbering,
        loader=SimpleNamespace(execute=execute), pipeline_key="group:1", max_images=2,
    )
    parts = context.request_messages([])[-1]["content"]
    texts = [part["text"] for part in parts if part["type"] == "text"]

    assert any(
        "发送者：小明(10001)" in text and "正文消息编号 1" in text for text in texts
    ), texts
    assert any(
        "发送者：小明(10002)" in text and "正文消息编号 2" in text for text in texts
    ), texts


async def test_default_image_sender_is_escaped_for_multiline_nickname():
    from neobot_app.reply.vision_context import ReplyVisionContext

    queue = _sender_queue(nicknames=[("小\n明<x>", 10001)])
    numbering = SimpleNamespace(mapping={1: 101})

    async def execute(name, args):
        from neobot_app.skills.image_context_skill import ImageContextResult

        return ImageContextResult('{"ok": true}', [{"type": "image_url", "image_url": {"url": "x"}}])

    context = ReplyVisionContext()
    await context.refresh_defaults(
        queue=queue, queue_key="1", numbering=numbering,
        loader=SimpleNamespace(execute=execute), pipeline_key="group:1", max_images=1,
    )
    texts = [
        part["text"]
        for part in context.request_messages([])[-1]["content"]
        if part["type"] == "text"
    ]

    assert any("发送者：小 明＜x＞" in text for text in texts), texts
    assert all("\n" not in text for text in texts)


async def test_failed_default_image_placeholder_keeps_sender():
    from neobot_app.reply.vision_context import ReplyVisionContext
    from neobot_app.skills.image_context_skill import ImageContextResult

    queue = _sender_queue(nicknames=[("小明", 10001)])
    failure = ImageContextResult('{"ok": false, "error": "加载失败"}', [])
    context = ReplyVisionContext()
    await context.refresh_defaults(
        queue=queue, queue_key="1", numbering=SimpleNamespace(mapping={1: 101}),
        loader=SimpleNamespace(execute=AsyncMock(return_value=failure)),
        pipeline_key="group:1", max_images=1,
    )
    texts = [
        part["text"]
        for part in context.request_messages([])[-1]["content"]
        if part["type"] == "text"
    ]

    assert any("发送者：小明" in text and "加载失败" in text for text in texts), texts


def test_tool_images_use_the_same_sender_and_coordinate_system():
    import json as _json

    from neobot_app.reply.vision_context import labelled_tool_images
    from neobot_app.skills.image_context_skill import ImageContextResult

    result = ImageContextResult(
        _json.dumps({
            "ok": True,
            "images": [{
                "sender": "小明", "msg_number": 3, "message_id": 42,
                "image_index": 1, "label": "旧标签",
            }],
        }),
        [{"type": "image_url", "image_url": {"url": "x"}}],
    )

    text = labelled_tool_images(result, tool_call_id="call-1")[0]["text"]

    assert "发送者：小明" in text
    assert "正文消息编号 3" in text
    assert "消息ID 42" in text
    assert "图片序号 2" in text
    assert "第 1 张" not in text


def test_tool_images_without_coordinates_fall_back_to_ordinal():
    from neobot_app.reply.vision_context import labelled_tool_images
    from neobot_app.skills.image_context_skill import ImageContextResult

    result = ImageContextResult(
        '{"ok": true, "images": [{"index": 0}]}',
        [{"type": "image_url", "image_url": {"url": "x"}}],
    )

    text = labelled_tool_images(result)[0]["text"]

    assert "第 1 张" in text
