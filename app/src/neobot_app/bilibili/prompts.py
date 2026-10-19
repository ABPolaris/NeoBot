"""B站交互提示词 — 评论回复 / 私信回复 / 跨聊天任务 / 动态日记。

与 QQ 提示词完全独立：B站是公开评论区（短评、楼中楼）+ 一对一的私信，
没有 QQ 群聊语境，因此不套用 QQ 模板。本模块只负责拼装文本，
调用 LLM、记忆档案读取与发送都在 reply_engine / monitors 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from neobot_app.time_context import get_current_time_values

#: B站场景的通用风格规则（评论与私信共用）。
#: 「活人感增强」条目与 prompts.toml 的 [human_feel] 保持同一口径，
#: 但这里独立维护：B站链路的提示词不能依赖 QQ 提示词是否启用。
BILI_STYLE_RULES = """<回复要求>
这是B站,不是QQ:不要提QQ、QQ群、QQ号、QQ空间,也不要使用QQ聊天的功能或工具。
评论区是公开场合,私信是一对一消息:回复要短,像真人随手敲出来的,不要长篇大论,不要说教,不要刻意突出自己的专业背景,不要重复自己说过的话。
不要使用markdown、代码块、列表、括号动作描写;不要输出前后缀、"回复:"、引号或颜文字。
楼中楼回复直接说话:只有对方明确问你说的是哪句话,或者有明显歧义时才引用原评论,平时不要复制对方的话,也不要随意艾特人。
如果有人要求你做什么事,你不一定要答应;做不到就直说做不到。
</回复要求>
<活人感增强>
- 禁止使用以下词汇或句式:「调调」「够狠的」「xx归xx,但xxx」「好歹xx」。
- 对你不明确知道的内容(例如没有具体资料的动漫、游戏、人物、事件等话题),禁止插话;即使插话也不应该装作知道,严禁任何形式的不懂装懂。
</活人感增强>
<跳过>
如果这条内容不值得回复(刷屏/无意义/广告/你完全不懂),只输出:[SKIP]
</跳过>"""

_DIARY_RULES = """<动态要求>
你要写的是自己今天的一条B站动态:第一人称,像真人随手发的日常记录,不是工作周报。
不要出现QQ、QQ群号、QQ号、真实姓名、手机号、地址等隐私信息;不要把私聊原文大段贴出来。
不要编造今天没发生过的事;不清楚的细节就别写。不要使用markdown、话题标签(#)或艾特任何人。
只输出动态正文,不要解释,不要加引号。
</动态要求>"""


# ── 数据模型 ──


@dataclass
class CommentNode:
    """评论树节点。"""

    rpid: int
    uid: int
    uname: str
    content: str
    ctime: int = 0
    children: list["CommentNode"] = field(default_factory=list)
    up_replied: bool = False


@dataclass
class CommentContext:
    """评论回复上下文。"""

    bot_name: str = ""
    bot_uid: int = 0
    bot_data: str = ""
    other_name: str = ""
    target_oid: int = 0
    target_type: str = "视频"
    business_id: int = 1
    target_title: str = ""
    target_url: str = ""
    target_up_name: str = ""
    target_desc: str = ""
    comment_tree: list[CommentNode] = field(default_factory=list)
    reply_target_rpid: int = 0
    reply_root_rpid: int = 0
    #: 评论者档案（由 reply_engine 从记忆档案/账号关联里补），每人一条 dict。
    commenter_profiles: list[dict] = field(default_factory=list)
    #: 注入到提示词的额外记忆文本。
    memory: str = ""
    current_time: datetime | None = None


@dataclass
class PrivateMessageContext:
    """私信回复上下文。"""

    bot_name: str = ""
    bot_uid: int = 0
    bot_data: str = ""
    other_name: str = ""
    sender_name: str = ""
    sender_uid: int = 0
    sender_remark: str = ""
    sender_profile: str = ""
    memory_list: str = ""
    history: list[dict] = field(default_factory=list)
    current_message: str = ""
    current_time: datetime | None = None


# ── 对外入口 ──


def build_comment_prompt(ctx: CommentContext, max_comments: int = 100) -> tuple[str, str]:
    """返回评论回复的 (system, user) 两段提示词。"""
    return _comment_system_prompt(ctx), _comment_user_prompt(ctx, max_comments)


def build_private_prompt(
    ctx: PrivateMessageContext, max_history: int = 30
) -> tuple[str, str]:
    """返回私信回复的 (system, user) 两段提示词。"""
    return _private_system_prompt(ctx), _private_user_prompt(ctx, max_history)


def build_task_prompt(
    task: str,
    *,
    history: str = "",
    bot_name: str = "",
    bot_uid: int = 0,
    bot_data: str = "",
    other_name: str = "",
) -> tuple[str, str]:
    """跨聊天任务：让 B站侧用一次 LLM 生成要发出去的那句话。"""
    system = "\n".join(
        [
            _identity(bot_name, bot_uid, bot_data, other_name),
            BILI_STYLE_RULES,
            "<跨聊天任务>有人通过跨聊天把一项任务交给了你,你要在B站当前这个对话里发一句话回应。"
            "只输出你要发出去的那句话;如果这个任务不适合在B站回应,只输出:[SKIP]</跨聊天任务>",
        ]
    )
    parts = []
    if history:
        parts.append(f"<对话背景>\n{history}\n</对话背景>")
    parts.append(f"<任务>\n{task or '(空任务)'}\n</任务>")
    return system, "\n".join(parts)


def build_diary_prompt(
    material: str,
    *,
    date_text: str,
    bot_name: str = "",
    bot_data: str = "",
    max_chars: int = 600,
    max_material_chars: int = 6000,
) -> tuple[str, str]:
    """B站动态日记：把今天的聊天/档案素材写成一条动态。"""
    system = "\n".join(
        [
            _identity(bot_name, 0, bot_data, ""),
            _DIARY_RULES,
            f"<长度>整条动态控制在{max_chars}字以内。</长度>",
        ]
    )
    body = str(material or "").strip()
    if max_material_chars > 0 and len(body) > max_material_chars:
        body = body[:max_material_chars] + "\n...[素材过长已截断]"
    user = (
        f"<日期>{date_text}</日期>\n"
        f"<今日聊天与档案>\n{body or '(今天没有可用素材)'}\n</今日聊天与档案>\n"
        "请根据以上素材,写出今天的B站动态正文。"
    )
    return system, user


# ── system 提示词 ──


def _identity(
    bot_name: str, bot_uid: int, bot_data: str, other_name: str
) -> str:
    who = f"你的名字是{bot_name or 'Neo'}"
    if bot_uid:
        who += f",你的B站UID是{bot_uid}"
    if other_name:
        who += f",也有人叫你{other_name}"
    who += "."
    if bot_data:
        who += f"\n{bot_data}"
    return f"<你是谁>\n{who}\n</你是谁>"


def _time_text(moment: datetime | None) -> str:
    return get_current_time_values(moment)["current_time"]


def _comment_system_prompt(ctx: CommentContext) -> str:
    return "\n".join(
        [
            _identity(ctx.bot_name, ctx.bot_uid, ctx.bot_data, ctx.other_name),
            BILI_STYLE_RULES,
            "<场景>你正在B站评论区回复别人,回复会作为楼中楼发出去。</场景>",
        ]
    )


def _private_system_prompt(ctx: PrivateMessageContext) -> str:
    return "\n".join(
        [
            _identity(ctx.bot_name, ctx.bot_uid, ctx.bot_data, ctx.other_name),
            BILI_STYLE_RULES,
            "<场景>你正在B站私信里和对方一对一聊天,对方看不到你的思考过程。</场景>",
        ]
    )


# ── user 提示词 ──


def _comment_user_prompt(ctx: CommentContext, max_comments: int) -> str:
    parts: list[str] = [f"<当前时间>{_time_text(ctx.current_time)}</当前时间>"]
    parts.append(f"<评论区>{_target_info(ctx)}</评论区>")
    if ctx.memory:
        parts.append(f"<你的记忆>\n{ctx.memory}\n</你的记忆>")
    profiles = _commenter_profiles_section(ctx.commenter_profiles)
    if profiles:
        parts.append(profiles)
    parts.append(f"<待回复评论>\n{_target_comment(ctx)}\n</待回复评论>")
    if ctx.comment_tree:
        parts.append(f"<评论树>\n{_render_comment_tree(ctx, max_comments)}\n</评论树>")
    parts.append(
        "<回复指引>★ 标记的评论是你需要回复的目标。直接输出回复文本即可;"
        "不值得回复时只输出 [SKIP]。</回复指引>"
    )
    return "\n".join(parts)


def _private_user_prompt(ctx: PrivateMessageContext, max_history: int) -> str:
    parts: list[str] = [f"<当前时间>{_time_text(ctx.current_time)}</当前时间>"]
    target = f"{ctx.sender_name or '对方'}(UID:{ctx.sender_uid})"
    if ctx.sender_remark:
        target = f"{ctx.sender_name or '对方'}(你的备注:{ctx.sender_remark},UID:{ctx.sender_uid})"
    parts.append(f"<聊天对象>{target}</聊天对象>")
    if ctx.sender_profile:
        parts.append(f"<你对ta的印象>{ctx.sender_profile}</你对ta的印象>")
    if ctx.memory_list:
        parts.append(f"<你的记忆>你想起来{ctx.memory_list}</你的记忆>")
    parts.append(f"<对话历史>\n{_history_text(ctx, max_history)}\n</对话历史>")
    parts.append(f"<待回复消息>{ctx.current_message or '(空消息)'}</待回复消息>")
    return "\n".join(parts)


# ── 渲染细节 ──


def _target_info(ctx: CommentContext) -> str:
    prefix = f"{ctx.target_up_name}的" if ctx.target_up_name else ""
    text = f"{prefix}{ctx.target_type}「{ctx.target_title or '未知'}」[oid:{ctx.target_oid}]"
    if ctx.target_url:
        text += f"\n{ctx.target_url}"
    if ctx.target_desc:
        text += f"\n简介:{ctx.target_desc[:500]}"
    return text


def _target_comment(ctx: CommentContext) -> str:
    if not ctx.reply_target_rpid:
        return "(未指定待回复评论)"
    node = _find_node(ctx.comment_tree, ctx.reply_target_rpid)
    if node is None:
        return f"(未找到 rpid={ctx.reply_target_rpid} 的评论)"
    lines = [
        f"作者:@{node.uname}(UID:{node.uid})",
        f"时间:{_format_ts(node.ctime)}",
        f"内容:{node.content}",
    ]
    if node.up_replied:
        lines.append("注意:你已回复过这条评论")
    return "\n".join(lines)


def _render_comment_tree(ctx: CommentContext, max_comments: int) -> str:
    nodes = _flatten(ctx.comment_tree)
    if max_comments > 0 and len(nodes) > max_comments:
        keep = nodes[:max_comments]
        head = f"(评论区共 {len(nodes)} 条,已截断至 {len(keep)} 条)"
        lines = [head]
        _render_nodes(keep, ctx.reply_target_rpid, lines)
        return "\n".join(lines)
    lines: list[str] = []
    _render_nodes(ctx.comment_tree, ctx.reply_target_rpid, lines)
    return "\n".join(lines)


def _render_nodes(
    nodes: list[CommentNode], target_rpid: int, parts: list[str], depth: int = 0
) -> None:
    indent = "  " * depth
    for node in nodes:
        marker = " ★ ← 需要回复" if node.rpid == target_rpid else ""
        replied = " [已回复]" if node.up_replied else ""
        parts.append(
            f"{indent}├─ @{node.uname}(UID:{node.uid}){marker}{replied} "
            f"[{_format_ts(node.ctime)}]"
        )
        content = node.content.replace("\n", " ")[:200]
        if len(node.content) > 200:
            content += " ...(截断)"
        parts.append(f"{indent}│  {content}")
        if node.children:
            _render_nodes(node.children, target_rpid, parts, depth + 1)


def _commenter_profiles_section(profiles: list[dict]) -> str:
    if not profiles:
        return ""
    lines = ["<评论者档案>"]
    for index, profile in enumerate(profiles, start=1):
        parts = [f"B站昵称:{profile.get('uname', '?')}", f"UID:{profile.get('uid', '?')}"]
        for key, label in (
            ("remark", "你对Ta的备注"),
            ("profile", "你对Ta的印象"),
            ("known_gender", "Ta告诉你的性别"),
            ("avatar_analysis", "头像记忆"),
            ("bilibili_memory", "你记得关于Ta的B站的事"),
            ("qq_memory", "你在QQ上记得关于Ta的事"),
        ):
            value = str(profile.get(key) or "").strip()
            if value:
                parts.append(f"{label}:{value}")
        lines.append(f"<评论者_{index}>{','.join(parts)}</评论者_{index}>")
    lines.append("</评论者档案>")
    return "\n".join(lines)


def _history_text(ctx: PrivateMessageContext, max_history: int) -> str:
    if not ctx.history:
        return "(无历史消息)"
    history = ctx.history
    lines: list[str] = []
    if max_history > 0 and len(history) > max_history:
        lines.append(f"(共 {len(history)} 条消息,仅显示最近 {max_history} 条)")
        history = history[-max_history:]
    for index, msg in enumerate(history, start=1):
        content = str(msg.get("content", ""))
        role = msg.get("role", "")
        when = _format_ts(int(msg.get("time", 0) or 0))
        if role in ("bot", "self"):
            lines.append(f"[{index}] {when} 你({ctx.bot_name}):{content}")
        else:
            who = msg.get("sender_name") or f"UID:{msg.get('sender_uid', '?')}"
            lines.append(f"[{index}] {when} {who}:{content}")
    return "\n".join(lines)


def _flatten(nodes: list[CommentNode]) -> list[CommentNode]:
    result: list[CommentNode] = []
    for node in nodes:
        result.append(node)
        if node.children:
            result.extend(_flatten(node.children))
    return result


def _find_node(nodes: list[CommentNode], rpid: int) -> CommentNode | None:
    for node in nodes:
        if node.rpid == rpid:
            return node
        if node.children:
            found = _find_node(node.children, rpid)
            if found is not None:
                return found
    return None


def _format_ts(timestamp: int) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(int(timestamp)).strftime("%m-%d %H:%M")
