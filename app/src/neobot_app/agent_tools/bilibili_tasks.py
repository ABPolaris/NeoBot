"""Child-safe B站后台解析与聊天流报告工具。"""

from __future__ import annotations

from typing import Any

from .contracts import AgentToolError, ToolContext, tool_definition


class BilibiliTaskTools:
    """把耗时的 B站视频/评论解析交给后台，并允许子 agent 主动报告。"""

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    def definitions(self) -> list[dict]:
        return [
            tool_definition(
                "bilibili_watch_video",
                "后台解析B站视频（字幕/ASR转写，可附带评论）；立即返回 task_id，完成后通过通知管线回到当前聊天流。",
                {
                    "properties": {
                        "identifier": {"type": "string", "description": "BV号、av号或视频链接"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000, "default": 3000},
                        "include_comments": {"type": "integer", "minimum": 0, "maximum": 20, "default": 0},
                    },
                    "required": ["identifier"],
                },
                ["identifier"],
            )
        ]

    async def execute(self, name: str, args: dict, context: ToolContext) -> Any:
        if name != "bilibili_watch_video":
            raise AgentToolError("UNKNOWN_TOOL", name)
        if self.manager is None:
            raise AgentToolError("TOOL_UNAVAILABLE", "B站视频后台解析未配置")
        return await self.manager.submit(
            flow_key=context.chat_flow_id,
            identifier=str(args.get("identifier") or ""),
            max_chars=int(args.get("max_chars") or 3000),
            include_comments=int(args.get("include_comments") or 0),
        )


class ChatReportTools:
    """可选地向当前聊天流报告一次进度/结果；不调用就不会通知。"""

    def __init__(self, hub: Any = None) -> None:
        self.hub = hub

    def definitions(self) -> list[dict]:
        return [
            tool_definition(
                "report_to_chat",
                "可选：把一条任务进度/结果报告给当前聊天流（通知管线），不强制使用。",
                {"properties": {"content": {"type": "string", "minLength": 1, "maxLength": 8000}},
                 "required": ["content"]},
                ["content"],
            )
        ]

    async def execute(self, name: str, args: dict, context: ToolContext) -> Any:
        if name != "report_to_chat":
            raise AgentToolError("UNKNOWN_TOOL", name)
        if self.hub is None:
            raise AgentToolError("TOOL_UNAVAILABLE", "通知管线未配置")
        text = str(args.get("content") or "").strip()
        if not text:
            raise AgentToolError("INVALID_ARGS", "content 不能为空")
        kind, _, conversation_id = context.chat_flow_id.partition(":")
        started = await self.hub.publish(
            source="child_agent_report",
            kind=kind or "private",
            conversation_id=conversation_id or context.chat_flow_id,
            content="子 agent 报告：\n" + text[:8000],
            manager_name="agent_tools",
            metadata={"agent_id": context.agent_id, "owner": context.owner},
        )
        return {"ok": True, "queued": bool(started), "message": "报告已交给通知管线"}
