"""DSH compact 检查点：私聊/档案共用。"""

from __future__ import annotations

import pytest

from neobot_app.runtime.compact import (
    COMPACT_MARKER_CLOSE,
    COMPACT_MARKER_OPEN,
    COMPACT_PREAMBLE,
    archive_compact_guide,
    compact_messages,
)


class _Provider:
    def __init__(self, content: str = "## Primary Request and Intent\n- 用户要压缩") -> None:
        self.content = content
        self.calls: list[list[dict]] = []

    async def chat(self, messages):
        self.calls.append(messages)
        return {"role": "assistant", "content": self.content}


def _messages(count: int = 12) -> list[dict]:
    out = [{"role": "system", "content": "system prompt"}]
    for index in range(count):
        out.append({"role": "user" if index % 2 == 0 else "assistant", "content": f"message {index}"})
    return out


@pytest.mark.asyncio
async def test_compact_replaces_older_and_keeps_recent() -> None:
    provider = _Provider()
    messages = _messages()
    result = await compact_messages(provider, messages, retain=4)
    assert result is not None
    compacted = result["messages"]
    assert compacted[0] == {"role": "system", "content": "system prompt"}
    assert compacted[1]["role"] == "user"
    assert COMPACT_PREAMBLE in compacted[1]["content"]
    assert COMPACT_MARKER_OPEN in compacted[1]["content"]
    assert COMPACT_MARKER_CLOSE in compacted[1]["content"]
    assert compacted[-1]["content"] == "message 11"
    assert result["removed"] == 8
    assert provider.calls


@pytest.mark.asyncio
async def test_compact_skips_when_nothing_to_do() -> None:
    provider = _Provider()
    assert await compact_messages(provider, _messages(3), retain=8) is None
    assert provider.calls == []


@pytest.mark.asyncio
async def test_compact_does_not_accept_non_shrinking_summary() -> None:
    provider = _Provider("x" * 10000)
    assert await compact_messages(provider, _messages(4), retain=2) is None


@pytest.mark.asyncio
async def test_compact_degrades_on_provider_error() -> None:
    class Broken:
        async def chat(self, messages):
            raise RuntimeError("boom")

    assert await compact_messages(Broken(), _messages(), retain=4) is None


def test_archive_guide_uses_same_sections() -> None:
    guide = archive_compact_guide()
    assert "## Primary Request and Intent" in guide
    assert "## Critical Context" in guide
    assert "compaction engine" in guide



@pytest.mark.asyncio
async def test_compact_prompt_puts_source_before_instructions() -> None:
    provider = _Provider()
    await compact_messages(provider, _messages(), retain=4)
    content = provider.calls[0][1]["content"]
    assert content.index("message 0") < content.index("You are now acting as a compaction engine")
