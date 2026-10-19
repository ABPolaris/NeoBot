"""B站动态日记 — 把今天聊天/档案里的内容写成一条 B站动态并发布。

默认关闭（config.bilibili.dynamic_diary_enabled=false）。生成走一次 LLM 调用，
素材来源可注入（material_provider，推荐接聊天记录/档案服务）；未注入时退回
读取档案表里的 group_summary / user_summary 当日条目。
发布只调用 BilibiliClient/BilibiliService.publish_dynamic，不碰 QQ 空间、不回复消息。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from neobot_app.bilibili._compat import bilibili_config, call_client, get_field
from neobot_app.bilibili.prompts import build_diary_prompt
from neobot_app.bilibili.reply_engine import clean_reply
from neobot_app.skills.base import SkillModule
from neobot_app.time_context import get_current_time_values, today_local, to_local
from neobot_app.utils.logger import get_module_logger

logger = get_module_logger("bilibili.diary")

DEFAULT_MATERIAL_TABLES = ("group_summary", "user_summary")
#: 每张档案表最多取多少条当日条目做素材。
_MAX_ARCHIVE_ITEMS = 20
#: 单条档案素材最多截取多少字符。
_ARCHIVE_ITEM_CHARS = 500


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class BilibiliDiaryService:
    """每日摘要 -> B站动态。"""

    def __init__(
        self,
        provider: Any = None,
        client: Any = None,
        *,
        archive_service: Any = None,
        material_provider: Any = None,
        bot_name: str = "",
        bot_data: str = "",
        enabled: bool = False,
        max_chars: int = 600,
        max_material_chars: int = 6000,
        timeout_seconds: float = 60.0,
    ) -> None:
        # client 可以是 BilibiliClient（同步 publish_dynamic）或 BilibiliService（异步门面）。
        self._provider = provider
        self._client = client
        self._archive = archive_service
        self._material_provider = material_provider
        self._bot_name = bot_name
        self._bot_data = bot_data
        self._enabled = bool(enabled)
        self._max_chars = max(1, int(max_chars or 600))
        self._max_material_chars = max(0, int(max_material_chars or 0))
        self._timeout_seconds = float(timeout_seconds)

    @classmethod
    def from_config(
        cls,
        provider: Any,
        client: Any,
        config: Any,
        *,
        archive_service: Any = None,
        material_provider: Any = None,
        bot_name: str = "",
        bot_data: str = "",
        **overrides: Any,
    ) -> "BilibiliDiaryService":
        cfg = bilibili_config(config)
        values: dict[str, Any] = {
            "archive_service": archive_service,
            "material_provider": material_provider,
            "bot_name": bot_name,
            "bot_data": bot_data,
            "enabled": bool(getattr(cfg, "dynamic_diary_enabled", False)),
        }
        values.update(overrides)
        return cls(provider, client, **values)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def available(self) -> bool:
        return self._provider is not None and self._enabled

    # ── 素材 ──

    async def collect_material(self) -> str:
        """收集今天的聊天/档案素材（material_provider 优先）。"""
        if self._material_provider is not None:
            try:
                material = await call_client(self._material_provider)
            except Exception as exc:
                logger.warning("B站日记素材生成失败", error=str(exc))
                material = ""
            if material:
                return str(material).strip()
        return await self._archive_material()

    async def _archive_material(self) -> str:
        if self._archive is None:
            return ""
        today = today_local()
        lines: list[str] = []
        for table in DEFAULT_MATERIAL_TABLES:
            try:
                items = await call_client(
                    self._archive.list, table, limit=_MAX_ARCHIVE_ITEMS
                )
            except Exception as exc:
                logger.debug("读取日记档案失败", table=table, error=str(exc))
                continue
            for item in items or []:
                updated = get_field(item, "updated_at", None)
                if isinstance(updated, datetime):
                    try:
                        if to_local(updated).date() != today:
                            continue
                    except Exception:
                        pass
                key = str(get_field(item, "key", "") or "")
                value = str(get_field(item, "value", "") or "").strip()
                if not value:
                    continue
                lines.append(f"[{table}:{key}] {value[:_ARCHIVE_ITEM_CHARS]}")
        return "\n".join(lines)

    # ── 生成与发布 ──

    async def generate(self) -> str:
        """生成动态正文；返回空字符串表示生成失败。"""
        if self._provider is None:
            return ""
        material = await self.collect_material()
        date_text = get_current_time_values()["current_date"]
        system, user = build_diary_prompt(
            material,
            date_text=date_text,
            bot_name=self._bot_name,
            bot_data=self._bot_data,
            max_chars=self._max_chars,
            max_material_chars=self._max_material_chars,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            chat = self._provider.chat(messages)
            if self._timeout_seconds > 0:
                response = await asyncio.wait_for(chat, timeout=self._timeout_seconds)
            else:
                response = await chat
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("B站日记生成失败", error=f"{type(exc).__name__}: {exc}")
            return ""
        content = (
            response.get("content")
            if isinstance(response, dict)
            else get_field(response, "content", "")
        )
        return clean_reply(content, self._max_chars)

    async def publish(self, text: str) -> dict:
        """发布一条文本动态，返回 {ok, dynamic_id, error}。"""
        target = self._client
        publisher = getattr(target, "publish_dynamic", None) if target is not None else None
        if not callable(publisher):
            return {"ok": False, "error": "B站客户端未配置或不支持发布动态"}
        try:
            dynamic_id = await call_client(publisher, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("B站动态发布失败", error=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        logger.info("B站动态已发布", dynamic_id=dynamic_id, chars=len(text))
        return {"ok": True, "dynamic_id": dynamic_id}

    async def run(self, *, preview: bool = False) -> dict:
        """生成并（非 preview 时）发布；返回结构化结果。"""
        if not self._enabled:
            return {"ok": False, "error": "B站动态日记未启用"}
        text = await self.generate()
        if not text:
            return {"ok": False, "error": "生成失败或模型没有产出"}
        if preview:
            return {"ok": True, "preview": True, "text": text}
        result = await self.publish(text)
        result["text"] = text
        return result


class BilibiliDiarySkill(SkillModule):
    """Agent 工具：生成/发布 B站动态日记。"""

    def __init__(self, service: BilibiliDiaryService | None = None) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "bilibili_diary"

    @property
    def description(self) -> str:
        return "B站动态日记：把今天的聊天/档案写成一条B站动态并发布（默认关闭，可先 preview）"

    @property
    def instructions(self) -> str:
        return (
            "B站动态日记 Skill 提供以下能力：\n\n"
            "  publish_bilibili_diary — 生成今天的B站动态并发布；preview=true 时只生成不发布。\n\n"
            "使用前提：B站已登录且已开启动态日记（默认关闭）。"
            "动态内容只来自聊天/档案摘要，不会读私聊原文，也不会修改QQ空间。"
        )

    def reset(self) -> None:
        pass

    def get_tools(self) -> list[dict]:
        if self._service is None:
            return []
        return [
            self._tool_def(
                "publish_bilibili_diary",
                "生成今天的B站动态并发布。preview=true 时只返回草稿、不发布。",
                {
                    "properties": {
                        "preview": {
                            "type": "boolean",
                            "description": "只生成不发布，默认 false",
                            "default": False,
                        }
                    },
                    "required": [],
                },
            )
        ]

    async def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        if tool_name != "publish_bilibili_diary" or self._service is None:
            return _json({"ok": False, "error": f"unknown tool: {tool_name}"})
        try:
            result = await self._service.run(preview=bool(args.get("preview", False)))
        except Exception as exc:
            return _json({"ok": False, "error": str(exc)})
        return _json(result)
