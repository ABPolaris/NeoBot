"""直播意愿判断：@ 保证与独立于 QQ 群的会话 id。"""

from __future__ import annotations

from neobot_app.config.schemas.bot import BotConfig
from neobot_app.willing.service import WillingService


def test_evaluate_live_mentions_bot_is_guaranteed(monkeypatch) -> None:
    config = BotConfig()
    config.bot.nick_name = "小助手"
    config.bot.account = 123
    config.chat.at_mention_guaranteed_reply = True
    service = WillingService(config)

    decision = service.evaluate_live(
        room_id=5440,
        uid=9,
        name="观众",
        text="小助手 在吗",
        observed_messages=(),
    )

    assert decision.should_reply is True
    assert "at_guaranteed_reply" in " ".join(decision.reasons)


def test_evaluate_live_uses_live_conversation_id(monkeypatch) -> None:
    config = BotConfig()
    service = WillingService(config)
    captured = {}
    original = service._manager.evaluate

    def _capture(context):
        captured["conversation_id"] = context.conversation_id
        captured["is_allowed"] = context.is_allowed
        return original(context)

    monkeypatch.setattr(service._manager, "evaluate", _capture)
    service.evaluate_live(room_id=5440, uid=9, name="观众", text="在吗")

    assert captured["conversation_id"] == "live:5440"
    assert captured["is_allowed"] is True
