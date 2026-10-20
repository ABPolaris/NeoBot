"""从 DSH compaction 借鉴的通用上下文压缩。

两处统一使用：
- 私聊上下文超限：把较早消息压成一条 user 检查点，保留最近消息；
- 档案（文档）压缩：用同一份结构化 compact 指令生成压缩档。

检查点标记与 DSH 一致（<compacted-summary>），保留最近的 tool 配对。
"""

from __future__ import annotations

import json
from typing import Any

COMPACT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of the "
    "conversation to free up context. Treat the captured context as established "
    "background and build on it without restating it. Continue the task directly from "
    "the messages that follow, without acknowledging this checkpoint."
)

COMPACT_MARKER_OPEN = "<compacted-summary>"
COMPACT_MARKER_CLOSE = "</compacted-summary>"

#: 与 DSH compaction-basic 相同的结构化 checkpoint 章节；空章节写 (none)。
COMPACT_INSTRUCTIONS = """You are now acting as a compaction engine for this AI chat assistant. Condense the content ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.

## Primary Request and Intent
- [the user's original and evolving goals; quote verbatim where exact wording matters]

## Key Technical Concepts
- [topics, facts, preferences, names, dates, numbers, decisions]

## Files and Code
- [exact identifiers: keys, paths, commands, urls, values that matter]

## Errors and Fixes
- [error: how it was resolved, plus any related user feedback]

## Pending Jobs
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]

Rules:
- Preserve exact identifiers, numbers, file paths, commands, error strings and syntax fragments.
- Never invent facts not present in the source.
- Keep the summary much shorter than the source.
"""


def checkpoint_message(summary: str) -> dict[str, str]:
    """把摘要包成 DSH 风格的 user 检查点。"""
    text = str(summary or "").strip()
    return {
        "role": "user",
        "content": f"{COMPACT_PREAMBLE}\n\n{COMPACT_MARKER_OPEN}\n{text}\n{COMPACT_MARKER_CLOSE}",
    }


def _render_transcript(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif content is None:
            text = ""
        else:
            text = json.dumps(content, ensure_ascii=False)
        if message.get("tool_calls"):
            calls = []
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                calls.append(
                    f"{fn.get('name')}({str(fn.get('arguments') or '')[:2000]})"
                )
            text += "\n[tool_calls] " + "; ".join(calls)
        if len(text) > 4000:
            text = text[:4000] + "…"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _tail_start(messages: list[dict[str, Any]], retain: int) -> int:
    """保留最近 retain 条，且不切断 assistant/tool 配对。"""
    start = max(0, len(messages) - max(1, retain))
    while start > 0 and messages[start].get("role") == "tool":
        start -= 1
    if start > 0 and messages[start].get("role") == "assistant" and messages[start].get("tool_calls"):
        return start
    return start


async def compact_messages(
    provider: Any,
    messages: list[dict[str, Any]],
    *,
    retain: int = 8,
    timeout: float = 90.0,
) -> dict[str, Any] | None:
    """压缩较早消息；失败/不可压缩返回 None，不抛异常。"""
    if provider is None or not callable(getattr(provider, "chat", None)):
        return None
    system_messages = [m for m in messages if m.get("role") == "system"]
    conversation = [m for m in messages if m.get("role") != "system"]
    start = _tail_start(conversation, retain)
    older = conversation[:start]
    if len(older) < 2:
        return None
    tail = conversation[start:]
    transcript = _render_transcript(older)
    if len(transcript.strip()) < 20:
        return None
    request = [
        {"role": "system", "content": "You compact conversation history for NeoBot."},
        {
            "role": "user",
            "content": f"{COMPACT_INSTRUCTIONS}\n\n---\nConversation to compact:\n{transcript}",
        },
    ]
    try:
        import asyncio

        response = await asyncio.wait_for(provider.chat(request), timeout=timeout)
    except Exception:
        return None
    if not isinstance(response, dict):
        return None
    summary = response.get("content")
    if not isinstance(summary, str):
        return None
    summary = summary.strip()
    if len(summary) < 20:
        return None
    if len(summary) >= len(transcript):
        # 没有真正缩短就放弃，避免下一次仍然超限。
        return None
    compacted = system_messages + [checkpoint_message(summary)] + tail
    return {
        "messages": compacted,
        "removed": len(older),
        "summary": summary,
        "checkpoint": checkpoint_message(summary)["content"],
    }


def archive_compact_guide() -> str:
    """档案压缩复用同一份 compact 指令的结构与标记约定。"""
    return (
        "Compact the archive record below instead of rewriting it as free-form prose.\n"
        "The compressed record must follow the same structured checkpoint sections as "
        "the compaction engine:\n\n"
        f"{COMPACT_INSTRUCTIONS}\n"
        "For this archive, map the sections to durable archive facts; keep all section "
        "headings and write (none) when a section is empty.\n"
    )


async def compact_archive_record(
    provider: Any,
    text: str,
    *,
    target_chars: int,
    table_name: str = "",
    key: str = "",
    timeout: float = 90.0,
) -> str | None:
    """用 compact 模板直接压缩一条档案；返回候选文本，失败返回 None。

    这是档案压缩的兜底/直写路径：不依赖工具调用，候选必须缩短且 <= target。
    """
    if provider is None or not callable(getattr(provider, "chat", None)):
        return None
    source = str(text or "").strip()
    target = int(target_chars or 0)
    if not source or target <= 0 or len(source) <= target:
        return None
    prompt = (
        f"Archive table: {table_name}\nArchive key: {key}\n"
        f"Source chars: {len(source)}\nTarget chars: {target}\n"
        "Output ONLY the complete compressed archive record. Do not call tools, do not use "
        "code fences, and do not explain. Keep every durable fact (names, dates, numbers, "
        "preferences, decisions, unresolved items), merge duplicates and drop repetition; "
        "never invent facts. The output MUST be <= target chars and shorter than the source.\n"
        f"\nRecord:\n{source}"
    )
    messages = [
        {"role": "system", "content": "You are the archive compaction engine for NeoBot."},
        {"role": "user", "content": f"{COMPACT_INSTRUCTIONS}\n\n---\n{prompt}"},
    ]
    try:
        import asyncio

        response = await asyncio.wait_for(provider.chat(messages), timeout=timeout)
    except Exception:
        return None
    content = response.get("content", "") if isinstance(response, dict) else ""
    candidate = str(content or "").strip()
    fence = chr(96) * 3
    if candidate.startswith(fence):
        lines = candidate.splitlines()
        if lines and lines[0].startswith(fence):
            lines = lines[1:]
        if lines and lines[-1].strip() == fence:
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    min_chars = min(100, max(20, target // 10))
    if (
        not candidate
        or len(candidate) < min_chars
        or len(candidate) > target
        or len(candidate) >= len(source)
    ):
        return None
    return candidate
