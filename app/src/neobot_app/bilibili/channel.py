"""B站作为跨聊天通道 — 供 CrossChatSkill 把任务投到 B站、或查询 B站对话。

B站侧没有常驻主 agent，所以「投递任务」= 用一次 LLM 调用把任务改写成符合人设的
B站发言，再由 client 发出去；有生成结果时回传给发起方聊天。

conversation_id 约定（CrossChatSkill 的 target_id）：
  private:<uid>                       B站私信会话（对方的 UID）
  comment:<oid>:<root>:<parent>       视频/稿件评论区楼中楼（type=1）
  dynamic:<oid>:<root>:<parent>       动态评论区（type=17）
  opus:<oid>:<root>:<parent>          图文动态（type=11）
  article:<oid>:<root>:<parent>       专栏（type=12）
"""

from __future__ import annotations

from typing import Any

from neobot_app.bilibili._compat import call_client, get_field, resolve_client
from neobot_app.utils.logger import get_module_logger

logger = get_module_logger("bilibili.channel")

#: 评论区前缀 -> reply type_（B站 CommentResourceType）。
_COMMENT_KINDS = {
    "comment": 1,
    "video": 1,
    "dynamic": 17,
    "opus": 11,
    "article": 12,
}
_MAX_HISTORY_LINES = 30


class BilibiliCrossChatChannel:
    """CrossChatSkill 的 B站后端：send_task / get_history。"""

    def __init__(self, client: Any = None, reply_engine: Any = None) -> None:
        # client 可以是 BilibiliClient 或 BilibiliService（调用时重新解析登录态）。
        self._client = client
        self._engine = reply_engine

    @property
    def available(self) -> bool:
        return bool(getattr(self._engine, "available", False))

    async def send_task(
        self, conversation_id: str, task: str
    ) -> tuple[bool, str, str]:
        """把任务变成一句 B站发言并发送。返回 (是否成功, 生成的回复, 说明)。"""
        target = parse_conversation_id(conversation_id)
        if target is None:
            return False, "", (
                "B站会话ID格式错误，应为 private:<uid> 或 "
                "comment:<oid>:<root>:<parent>（可把点赞/回复目标写在 task 里）"
            )
        client = resolve_client(self._client)
        if client is None:
            return False, "", "B站未登录或客户端不可用"
        if self._engine is None or not getattr(self._engine, "available", False):
            return False, "", "B站回复引擎不可用"
        history = ""
        if target["kind"] == "private":
            history = await self.get_history(conversation_id, count=20)
        reply = await self._engine.generate_task_reply(str(task or ""), history=history)
        if reply is None:
            return False, "", "B站侧决定不回应这个任务"
        if target["kind"] == "private":
            sent = await call_client(
                client.send_message, target["uid"], reply
            )
        else:
            sent = await call_client(
                client.send_comment_reply,
                oid=target["oid"],
                root=target["root"],
                parent=target["parent"],
                text=reply,
                type_=target["type_"],
            )
        if not sent:
            logger.warning("B站跨聊天投递失败", conversation_id=conversation_id)
            return False, "", "B站发送失败（可能被风控或评论区已关闭）"
        logger.info("B站跨聊天任务已发送", conversation_id=conversation_id, chars=len(reply))
        return True, reply, "已发送到B站"

    async def get_history(self, conversation_id: str, count: int = 20) -> str:
        """读取 B站对话内容，格式化成给 QQ agent 看的文本。"""
        target = parse_conversation_id(conversation_id)
        if target is None:
            return ""
        client = resolve_client(self._client)
        if client is None:
            return ""
        limit = max(1, min(int(count or 20), _MAX_HISTORY_LINES * 2))
        try:
            if target["kind"] == "private":
                messages = await call_client(client.get_messages, target["uid"], size=limit)
                return _render_private_history(messages, client)
            comments = await call_client(client.get_video_comments, target["oid"], page=1)
            return _render_comment_history(comments, limit)
        except Exception as exc:
            logger.warning(
                "B站跨聊天历史读取失败",
                conversation_id=conversation_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return ""


def parse_conversation_id(conversation_id: Any) -> dict | None:
    """解析 conversation_id；非法格式返回 None。"""
    text = str(conversation_id or "").strip()
    parts = text.split(":")
    if len(parts) == 2 and parts[0] in ("private", "pmsg"):
        try:
            uid = int(parts[1])
        except ValueError:
            return None
        return {"kind": "private", "uid": uid} if uid > 0 else None
    if len(parts) == 4 and parts[0] in _COMMENT_KINDS:
        try:
            oid, root, parent = (int(parts[1]), int(parts[2]), int(parts[3]))
        except ValueError:
            return None
        if oid <= 0 or root <= 0 or parent <= 0:
            return None
        return {
            "kind": "comment",
            "type_": _COMMENT_KINDS[parts[0]],
            "oid": oid,
            "root": root,
            "parent": parent,
        }
    return None


def _render_private_history(messages: Any, client: Any) -> str:
    my_uid = int(getattr(client, "my_uid", 0) or 0)
    lines: list[str] = []
    ordered = sorted(
        list(messages or []), key=lambda msg: int(get_field(msg, "timestamp", 0) or 0)
    )
    for msg in ordered[-_MAX_HISTORY_LINES:]:
        text = _message_text(msg)
        if not text:
            continue
        sender = int(get_field(msg, "sender_uid", 0) or 0)
        who = "你" if (my_uid and sender == my_uid) or get_field(msg, "is_self", False) else f"UID:{sender}"
        lines.append(f"{who}:{text}")
    return "\n".join(lines)


def _render_comment_history(comments: Any, limit: int) -> str:
    lines: list[str] = []
    for comment in list(comments or [])[:limit]:
        member = get_field(comment, "member", None) or {}
        content = get_field(comment, "content", None) or {}
        uname = str(get_field(member, "uname", "") or "?")
        message = str(get_field(content, "message", "") or "").replace("\n", " ")
        if message:
            lines.append(f"@{uname}:{message}")
    return "\n".join(lines)


def _message_text(msg: Any) -> str:
    text = get_field(msg, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = get_field(msg, "content", "")
    if isinstance(content, dict):
        return str(content.get("content", "") or "").strip()
    raw = str(content or "").strip()
    return raw if int(get_field(msg, "msg_type", 1) or 1) == 1 else ""
