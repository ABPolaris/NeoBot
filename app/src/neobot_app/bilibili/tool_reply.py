"""B站回复的发送工具：和 QQ 一样通过 send_reply 指定要发出的文本。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from neobot_chat import Agent
from neobot_chat.schema.types import ToolAccessRule
from neobot_chat.tools.toolset import ToolSpec, Toolset

_SEND_REPLY = {
    "type": "function",
    "function": {
        "name": "send_reply",
        "description": "发送这条B站回复。必须调用它；直接输出的正文不会被发送。",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 20000}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}
_CANCEL = {
    "type": "function",
    "function": {
        "name": "cancel",
        "description": "本轮不回复。只有确定不值得回复时才调用。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


@dataclass
class ToolReply:
    text: str | None = None
    usage: dict[str, Any] | None = None


class _ReplyExecutor:
    def __init__(self, *, max_chars: int = 0) -> None:
        self.max_chars = max(0, int(max_chars or 0))
        self.text: str | None = None
        self.cancelled = False

    def definitions(self) -> list[dict]:
        return [_SEND_REPLY, _CANCEL]

    async def execute(self, name: str, args: dict) -> str:
        if name == "cancel":
            self.cancelled = True
            self.text = None
            return '{"ok": true}'
        if name == "send_reply":
            text = str(args.get("text") or "").strip()
            if not text:
                return '{"ok": false, "error": "text 不能为空"}'
            if text.upper() == "[SKIP]":
                self.cancelled = True
                self.text = None
                return '{"ok": true, "cancelled": true}'
            if self.max_chars:
                text = text[: self.max_chars]
            self.text = text
            return '{"ok": true}'
        return '{"ok": false, "error": "未知工具"}'

    async def close(self) -> None:
        return None


async def run_tool_reply(
    provider: Any,
    system: str,
    user: str,
    *,
    max_chars: int = 0,
    timeout: float = 90.0,
) -> ToolReply:
    """跑一轮只含 send_reply/cancel 的 agent 循环，返回工具指定的回复。"""
    if provider is None:
        return ToolReply()
    executor = _ReplyExecutor(max_chars=max_chars)
    toolset = Toolset(
        executor=executor,
        specs=[
            ToolSpec(_SEND_REPLY, lambda *_: ToolAccessRule(action="allow")),
            ToolSpec(_CANCEL, lambda *_: ToolAccessRule(action="allow")),
        ],
    )
    agent = Agent(
        provider,
        toolset=toolset,
        include_builtin_tools=False,
        max_iterations=1,
        system_prompt=system,
    )
    state = await asyncio.wait_for(
        agent.invoke({"messages": [{"role": "user", "content": user}]}),
        timeout=timeout,
    )
    usage = None
    for message in reversed(state.get("messages", [])):
        if message.get("role") != "assistant":
            continue
        value = (message.get("extensions") or {}).get("usage")
        if isinstance(value, dict):
            usage = value
        break
    return ToolReply(text=None if executor.cancelled else executor.text, usage=usage)
