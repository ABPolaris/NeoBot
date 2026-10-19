"""B站运行时装配：服务、回复引擎、轮询器、日记与命令注册。

由 bootstrap 创建并随应用生命周期启动/停止；所有失败都降级为日志，不影响主进程。
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
from datetime import datetime, timedelta
from typing import Any

from neobot_app.bilibili.channel import BilibiliCrossChatChannel
from neobot_app.bilibili.comment_monitor import BilibiliCommentMonitor
from neobot_app.bilibili.diary import BilibiliDiaryService, BilibiliDiarySkill
from neobot_app.bilibili.private_monitor import BilibiliPrivateMonitor
from neobot_app.bilibili.reply_engine import BilibiliReplyEngine
from neobot_app.bilibili.service import BilibiliService
from neobot_app.skills.bilibili_link import BilibiliLinkSkill
from neobot_app.skills.bilibili_memory import BilibiliMemorySkill
from neobot_contracts.ports.logging import Logger, NullLogger


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


class BilibiliRuntime:
    """B站功能的一站式运行时。"""

    def __init__(
        self,
        *,
        config: Any,
        provider: Any,
        archive_service: Any,
        uow_factory: Any,
        command_service: Any,
        logger: Logger | None = None,
        bot_name: str = "",
        bot_data: str = "",
    ) -> None:
        self._config = config
        self._logger = logger or NullLogger()
        self._command_service = command_service
        self._started = False
        cfg = getattr(config, "bilibili", None)
        self.service = BilibiliService(cfg, logger=self._logger)
        self.engine = BilibiliReplyEngine(
            provider,
            archive_service=archive_service,
            uow_factory=uow_factory,
            bot_name=bot_name,
            bot_uid=0,
            bot_data=bot_data,
        )
        self.comment_monitor = BilibiliCommentMonitor.from_config(
            self.service, self.engine, cfg
        )
        self.private_monitor = BilibiliPrivateMonitor.from_config(
            self.service, self.engine, cfg
        )
        self.diary = BilibiliDiaryService.from_config(
            provider,
            self.service,
            cfg,
            archive_service=archive_service,
            bot_name=bot_name,
            bot_data=bot_data,
        )
        self.diary_skill = BilibiliDiarySkill(self.diary)
        self.channel = BilibiliCrossChatChannel(self.service, self.engine)
        self._diary_task: asyncio.Task[None] | None = None

    # ── 生命周期 ──

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            await self.service.start()
        except Exception:
            self._logger.exception("B站服务启动失败（已降级为休眠）")
        try:
            await self.comment_monitor.start()
            await self.private_monitor.start()
        except Exception:
            self._logger.exception("B站轮询器启动失败（已跳过）")

    async def stop(self) -> None:
        self._started = False
        task = self._diary_task
        self._diary_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for monitor in (self.comment_monitor, self.private_monitor):
            try:
                await _maybe_await(monitor.stop())
            except Exception:
                self._logger.exception("B站轮询器停止失败")
        try:
            await self.service.stop()
        except Exception:
            self._logger.exception("B站服务停止失败")

    async def run(self) -> None:
        """后台常驻协程：随应用启动，被取消时停止全部子服务。"""
        await self.start()
        if getattr(getattr(self._config, "bilibili", None), "dynamic_diary_enabled", False):
            self._diary_task = asyncio.create_task(self._diary_loop())
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    # ── 每日动态日记 ──

    def _seconds_until_diary(self, now: datetime | None = None) -> float | None:
        """距下一次 dynamic_diary_time（HH:MM）的秒数；未启用/配置非法返回 None。"""
        cfg = getattr(self._config, "bilibili", None)
        if not getattr(cfg, "dynamic_diary_enabled", False):
            return None
        raw = str(getattr(cfg, "dynamic_diary_time", "") or "").strip()
        try:
            hour_text, minute_text = raw.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (TypeError, ValueError):
            hour = minute = -1
        if not (0 <= hour < 24 and 0 <= minute < 60):
            self._logger.warning(f"B站动态日记时间配置非法: {raw!r}（应为 HH:MM）")
            return None
        current = now or datetime.now().astimezone()
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)  # 今天的时间点已过，排到明天
        return (target - current).total_seconds()

    async def _diary_loop(self) -> None:
        while True:
            delay = self._seconds_until_diary()
            if delay is None:
                return
            await asyncio.sleep(delay)
            try:
                result = await self.diary.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 单次失败只记日志，主循环与后续调度继续。
                self._logger.exception("B站动态日记执行失败")
                continue
            if not result.get("ok"):
                self._logger.warning(
                    f"B站动态日记未发布: {result.get('error') or '未知原因'}"
                )

    # ── 对外门面 ──

    def status_text(self) -> str:
        try:
            st = self.service.status()
        except Exception as exc:
            return f"B站状态读取失败：{exc}"
        if not st.get("available"):
            return "B站未登录（功能等同关闭）。发送 /login bilibili 扫码登录。"
        state = "有效" if st.get("valid") else "登录态待校验"
        return (
            f"B站已登录：{st.get('uname') or '(未命名)'} (UID {st.get('uid') or 0})\n"
            f"登录态：{state}；登录时间：{st.get('login_at') or '未知'}\n"
            f"评论回复：{'开' if getattr(getattr(self._config, 'bilibili', None), 'comment_reply_enabled', False) else '关'}；"
            f"私信回复：{'开' if getattr(getattr(self._config, 'bilibili', None), 'private_message_enabled', False) else '关'}"
        )

    async def login_with_qr(self, *, on_qr: Any = None, on_status: Any = None) -> bool:
        return await self.service.login_with_qr(on_qr=on_qr, on_status=on_status)

    async def publish_dynamic(self, text: str, images: Any = None) -> int:
        return await self.service.publish_dynamic(text, images)

    # ── 命令注册 ──

    def register_commands(self, command_service: Any) -> None:
        from neobot_app.commands.model import PERM_SUB_ADMIN, Command

        registry = command_service.registry
        self._register_login_command(registry, command_service)
        if not _has_command(registry, "bili"):
            registry.register(
                Command(
                    name="bili",
                    description="B站状态/发动态（登录用 /login bilibili）",
                    usage="[status|dynamic <文本>]",
                    permission=PERM_SUB_ADMIN,
                    handler=self._handle_bili,
                    params=(
                        ("status", "查看B站登录与轮询状态"),
                        ("dynamic <文本>", "发布一条B站纯文本动态"),
                    ),
                )
            )

    def _register_login_command(self, registry: Any, command_service: Any) -> None:
        from neobot_app.commands.model import PERM_SUB_ADMIN, Command

        async def _handler(ctx: Any) -> None:
            args = [str(a).lower() for a in (ctx.args or [])]
            if args and args[0] not in ("bilibili", "b站", "bili"):
                await ctx.reply("用法：/login bilibili（扫码登录B站）")
                return
            if self.service.available:
                await ctx.reply("B站已登录。如需重新登录，请先在B站 App 退出登录后重试，或删除 cookies 文件。")
                return

            async def _on_qr(login: Any) -> None:
                png = getattr(login, "png_bytes", None)
                if png:
                    await command_service.send_image_bytes(
                        ctx.kind,
                        ctx.conv_id,
                        png,
                        at_user_id=ctx.user_id,
                        filename="bilibili-login.png",
                    )
                else:
                    await ctx.reply("请用B站App扫码登录：" + str(getattr(login, "url", "")))

            async def _run() -> None:
                ok = False
                try:
                    ok = await self.login_with_qr(on_qr=_on_qr)
                except Exception as exc:
                    self._logger.exception("B站扫码登录失败")
                    await command_service.reply_to(
                        ctx.kind, ctx.conv_id, f"B站登录失败：{exc}", at_user_id=None
                    )
                    return
                if ok:
                    await command_service.reply_to(
                        ctx.kind, ctx.conv_id, "B站登录成功。", at_user_id=None
                    )
                else:
                    await command_service.reply_to(
                        ctx.kind,
                        ctx.conv_id,
                        "B站登录超时或未确认，请重新发送 /login bilibili。",
                        at_user_id=None,
                    )

            asyncio.create_task(_run())
            return None

        if not _has_command(registry, "login"):
            registry.register(
                Command(
                    name="login",
                    description="扫码登录外部平台（当前支持 bilibili）",
                    usage="bilibili",
                    permission=PERM_SUB_ADMIN,
                    handler=_handler,
                    params=(("bilibili", "B站扫码登录"),),
                )
            )

    async def _handle_bili(self, ctx: Any) -> str | None:
        args = list(ctx.args or [])
        sub = str(args[0]).lower() if args else "status"
        if sub in ("status", "状态", ""):
            return self.status_text()
        if sub in ("dynamic", "动态"):
            text = " ".join(str(a) for a in args[1:]).strip()
            if not text:
                return "用法：/bili dynamic <文本>"
            if not self.service.available:
                return "B站未登录，先发送 /login bilibili。"
            try:
                dyn_id = await self.publish_dynamic(text)
            except Exception as exc:
                return f"发布B站动态失败：{exc}"
            return f"已发布B站动态，dynamic_id={dyn_id}"
        return "用法：/bili [status|dynamic <文本>]"


def _has_command(registry: Any, name: str) -> bool:
    try:
        return registry.get(name) is not None
    except Exception:
        return False


def build_bilibili_skills(
    *,
    archive_service: Any,
    uow_factory: Any,
    runtime: BilibiliRuntime | None,
) -> list[Any]:
    """构造需要注册进 SkillManager 的B站技能（runtime 为空时仍注册账号/记忆技能）。"""
    skills: list[Any] = []
    try:
        skills.append(BilibiliLinkSkill(uow_factory=uow_factory, archive_service=archive_service))
        skills.append(BilibiliMemorySkill(archive_service=archive_service))
    except Exception:
        pass
    if runtime is not None:
        skills.append(runtime.diary_skill)
    return skills
