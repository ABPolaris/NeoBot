"""B站直播弹幕交互器 — 独立于 streaming_parser 插件的直播监听。

一个实例管理多个直播间：用 BilibiliClient 的直播 HTTP 接口拿 token/host_list，
再连 B站弹幕 WebSocket（`wss://host:wss_port/sub`）收 DANMU_MSG，按需调用
provider 生成一句极短回复，再由 BilibiliClient.send_danmaku 发出。

启用房间持久化在 `<DATA_DIR>/bilibili/live_interact.json`；start() 恢复连接。
不 import streaming_parser，也不依赖它的订阅/轮询。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import struct
import time
import zlib
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import aiohttp

from neobot_app.bilibili._compat import call_client, get_field, resolve_client
from neobot_app.bilibili.client import DEFAULT_USER_AGENT
from neobot_app.bilibili.reply_engine import clean_reply
from neobot_app.core import DATA_DIR
from neobot_app.utils.atomic import atomic_write_text
from neobot_app.utils.logger import get_module_logger
from neobot_contracts.ports.logging import Logger

_LOGGER = get_module_logger("bilibili.live_interact")

#: 弹幕协议头：总长度 / 头长度 / 协议版本 / 操作码 / 序号。
_WS_HEADER = struct.Struct(">IHHII")
_WS_HEADER_SIZE = _WS_HEADER.size
_WS_OP_HEARTBEAT = 2
_WS_OP_MESSAGE = 5
_WS_OP_AUTH = 7
_WS_HEARTBEAT_SECONDS = 30.0
_WS_RECONNECT_MAX_SECONDS = 60.0
_WS_HEALTHY_SECONDS = 60.0
#: 单房间每分钟最多自动回复条数（冷却之外的硬上限）。
_MAX_REPLIES_PER_MINUTE = 3
_REPLY_WINDOW_SECONDS = 60.0


def encode_packet(op: int, body: bytes = b"", protover: int = 1) -> bytes:
    """组一个 B站弹幕包（16 字节头 + body）。"""
    payload = bytes(body or b"")
    return _WS_HEADER.pack(_WS_HEADER_SIZE + len(payload), _WS_HEADER_SIZE, int(protover), int(op), 1) + payload


def decode_packets(data: bytes) -> list[tuple[int, bytes]]:
    """拆弹幕包；protover=2 的 body 会用 zlib 解开并继续拆内部包。"""
    return _decode_packets(bytes(data or b""), 0)


def _decode_packets(data: bytes, depth: int) -> list[tuple[int, bytes]]:
    if depth > 3:
        return []
    packets: list[tuple[int, bytes]] = []
    offset = 0
    while offset + _WS_HEADER_SIZE <= len(data):
        total, header_len, protover, op, _seq = _WS_HEADER.unpack_from(data, offset)
        if header_len < _WS_HEADER_SIZE or total < header_len or offset + total > len(data):
            break
        body = data[offset + header_len : offset + total]
        if protover == 2:
            try:
                packets.extend(_decode_packets(zlib.decompress(body), depth + 1))
            except zlib.error:
                pass
        else:
            packets.append((op, body))
        offset += total
    return packets


def parse_danmu_message(payload: Any) -> dict[str, Any] | None:
    """从 op=5 的 JSON 里取 DANMU_MSG；非弹幕或结构异常返回 None。"""
    if not isinstance(payload, dict):
        return None
    cmd = payload.get("cmd")
    if not isinstance(cmd, str) or cmd.split(":", 1)[0] != "DANMU_MSG":
        return None
    info = payload.get("info")
    try:
        text = str(info[1]).strip()  # type: ignore[index]
        uid = int(info[2][0])  # type: ignore[index]
        name = str(info[2][1])  # type: ignore[index]
    except (IndexError, TypeError, ValueError):
        return None
    if not text:
        return None
    return {"uid": uid, "name": name, "text": text}


class BilibiliLiveInteractor:
    """多房间直播弹幕监听 + 可选自动回复。"""

    def __init__(
        self,
        service: Any = None,
        *,
        provider: Any = None,
        bot_name: str = "",
        data_dir: Path | None = None,
        logger: Logger | None = None,
        cooldown_seconds: float = 20.0,
        max_reply_chars: int = 40,
        at_only: bool = True,
    ) -> None:
        # service 可以是 BilibiliService 或 BilibiliClient；每轮用 resolve_client 取当前 client。
        self._service = service
        self._provider = provider
        self._bot_name = str(bot_name or "").strip().lstrip("@")
        base_dir = Path(data_dir) if data_dir is not None else DATA_DIR
        self._path = base_dir / "bilibili" / "live_interact.json"
        self._logger = logger or _LOGGER
        self._cooldown = max(0.0, float(cooldown_seconds or 0.0))
        self._max_reply_chars = max(0, int(max_reply_chars or 0))
        self._at_only = bool(at_only)
        self._rooms: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._reply_times: dict[str, deque[float]] = defaultdict(deque)
        self._last_reply_at: dict[str, float] = {}
        self._loaded = False
        self._running = False

    # ── 生命周期 ──

    async def start(self) -> None:
        """读取持久化房间并为每个房间启动监听任务；失败只记日志。"""
        if self._running:
            return
        self._load()
        self._running = True
        for room_id in list(self._rooms):
            self._start_task(room_id)
        self._logger.info("B站直播交互器已启动", rooms=len(self._rooms))

    async def stop(self) -> None:
        """停止所有监听任务；持久化状态保留，下次 start() 恢复。"""
        self._running = False
        tasks = list(self._tasks.items())
        self._tasks.clear()
        for _room_id, task in tasks:
            task.cancel()
        for _room_id, task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._logger.info("B站直播交互器已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    # ── 房间管理 ──

    async def enable(self, room_id: int, *, auto_reply: bool = True) -> dict[str, Any]:
        """启用房间并写入状态；拿到房间信息时补真实房间号/主播名。"""
        rid = _to_int(room_id)
        if rid <= 0:
            return {"ok": False, "error": "房间号无效"}
        self._load()
        data: dict[str, Any] = {}
        client = self._get_client()
        if client is not None:
            try:
                data = await call_client(client.get_live_room_info, rid) or {}
            except Exception as exc:
                self._logger.warning(
                    "B站直播间信息读取失败，先按输入房间号启用",
                    room_id=rid,
                    error=f"{type(exc).__name__}: {exc}",
                )
        room_info = data.get("room_info") if isinstance(data, dict) else None
        real_room_id = (_to_int(room_info.get("room_id")) if isinstance(room_info, dict) else 0) or rid
        anchor = data.get("anchor_info") if isinstance(data, dict) else None
        base_info = anchor.get("base_info") if isinstance(anchor, dict) else None
        name = str(base_info.get("uname") or "") if isinstance(base_info, dict) else ""
        room = {
            "real_room_id": real_room_id,
            "name": name,
            "auto_reply": bool(auto_reply),
        }
        self._rooms[str(rid)] = room
        self._save()
        self._start_task(str(rid))
        self._logger.info("B站直播房间已启用", room_id=rid, auto_reply=room["auto_reply"])
        return {"ok": True, "room_id": rid, **room}

    async def disable(self, room_id: int) -> dict[str, Any]:
        """停用房间并删除持久化状态。"""
        rid = _to_int(room_id)
        self._load()
        removed = self._rooms.pop(str(rid), None) is not None
        await self._cancel_task(str(rid))
        if removed:
            self._save()
            self._logger.info("B站直播房间已停用", room_id=rid)
        return {"ok": removed, "room_id": rid}

    def list_rooms(self) -> list[dict[str, Any]]:
        """已启用的房间列表（按房间号排序）。"""
        self._load()
        rooms = []
        for room_id in sorted(self._rooms, key=_to_int):
            room = self._rooms[room_id]
            rooms.append(
                {
                    "room_id": _to_int(room_id),
                    "real_room_id": _to_int(room.get("real_room_id")) or _to_int(room_id),
                    "name": str(room.get("name") or ""),
                    "auto_reply": bool(room.get("auto_reply", True)),
                }
            )
        return rooms

    async def set_auto_reply(self, room_id: int, on: bool) -> dict[str, Any]:
        """开关已启用房间的自动回复（不影响 WebSocket 监听）。"""
        rid = _to_int(room_id)
        self._load()
        room = self._rooms.get(str(rid))
        if room is None:
            return {"ok": False, "error": "房间未启用"}
        room["auto_reply"] = bool(on)
        self._save()
        return {"ok": True, "room_id": rid, "auto_reply": room["auto_reply"]}

    async def send_danmaku(self, room_id: int, text: str) -> dict[str, Any]:
        """手动发一条直播弹幕（不受自动回复开关影响）。"""
        rid = _to_int(room_id)
        message = str(text or "").strip()
        if rid <= 0 or not message:
            return {"ok": False, "error": "房间号或弹幕内容不能为空"}
        client = self._get_client()
        if client is None:
            return {"ok": False, "error": "B站未登录或客户端不可用"}
        try:
            sent = await call_client(client.send_danmaku, rid, message)
        except Exception as exc:
            self._logger.warning(
                "B站直播弹幕发送失败",
                room_id=rid,
                error=f"{type(exc).__name__}: {exc}",
            )
            return {"ok": False, "error": "B站直播弹幕发送失败"}
        if not sent:
            return {"ok": False, "error": "B站直播弹幕发送失败"}
        return {"ok": True, "error": ""}

    # ── WebSocket ──

    async def _room_loop(self, room_id: str) -> None:
        backoff = 1.0
        while self._running:
            started = time.monotonic()
            try:
                await self._connect_once(room_id)
                if time.monotonic() - started >= _WS_HEALTHY_SECONDS:
                    backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning(
                    "B站直播弹幕连接失败，准备重连",
                    room_id=room_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if not self._running:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _WS_RECONNECT_MAX_SECONDS)

    async def _connect_once(self, room_id: str) -> None:
        client = self._get_client()
        if client is None:
            raise RuntimeError("B站未登录或客户端不可用")
        rid = _to_int(room_id)
        room = self._rooms.get(room_id) or {}
        real_room_id = _to_int(room.get("real_room_id")) or rid
        info = await call_client(client.get_live_danmu_info, rid)
        if not isinstance(info, dict):
            raise RuntimeError("弹幕服务器信息为空")
        token = str(info.get("token") or "")
        hosts = [item for item in info.get("host_list") or [] if isinstance(item, dict) and item.get("host")]
        if not token or not hosts:
            raise RuntimeError("弹幕服务器信息缺少 token/host_list")
        host = str(hosts[0]["host"])
        port = _to_int(hosts[0].get("wss_port")) or 443
        url = f"wss://{host}:{port}/sub"
        uid = _to_int(get_field(client, "my_uid", 0))
        auth = {
            "uid": uid,
            "roomid": real_room_id,
            "protover": 2,
            "platform": "web",
            "type": 2,
            "key": token,
        }
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Origin": "https://live.bilibili.com",
            "Referer": f"https://live.bilibili.com/{real_room_id}",
        }
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, headers=headers) as ws:
                await ws.send_bytes(encode_packet(_WS_OP_AUTH, json.dumps(auth).encode("utf-8")))
                heartbeat = asyncio.create_task(self._heartbeat(ws))
                try:
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.BINARY:
                            for op, body in decode_packets(message.data):
                                await self._handle_packet(room_id, op, body)
                        elif message.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        ):
                            break
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat

    @staticmethod
    async def _heartbeat(ws: Any) -> None:
        while True:
            await asyncio.sleep(_WS_HEARTBEAT_SECONDS)
            try:
                await ws.send_bytes(encode_packet(_WS_OP_HEARTBEAT))
            except Exception:
                return

    async def _handle_packet(self, room_id: str, op: int, body: bytes) -> None:
        if op != _WS_OP_MESSAGE or not body:
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        danmu = parse_danmu_message(payload)
        if danmu is not None:
            await self._handle_danmu(room_id, danmu)

    # ── 自动回复 ──

    async def _handle_danmu(self, room_id: str, danmu: dict[str, Any]) -> None:
        room_id = str(room_id)
        room = self._rooms.get(room_id)
        if room is None or not room.get("auto_reply", True):
            return
        if self._provider is None:
            return
        client = self._get_client()
        if client is None:
            return
        my_uid = _to_int(get_field(client, "my_uid", 0))
        if my_uid and _to_int(danmu.get("uid")) == my_uid:
            return
        text = str(danmu.get("text") or "")
        if self._at_only and not self._mentions_bot(text):
            return
        if not self._reply_allowed(room_id):
            return
        reply = await self._generate_reply(text)
        if not reply:
            return
        try:
            sent = await call_client(client.send_danmaku, _to_int(room_id), reply)
        except Exception as exc:
            self._logger.warning(
                "B站直播自动回复发送失败",
                room_id=room_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        if not sent:
            self._logger.warning("B站直播自动回复发送失败", room_id=room_id)
            return
        self._record_reply(room_id)
        self._logger.info("B站直播已自动回复", room_id=room_id, chars=len(reply))

    def _mentions_bot(self, text: str) -> bool:
        if not self._bot_name:
            return False
        return self._bot_name.lower() in text.lower()

    def _reply_allowed(self, room_id: str) -> bool:
        now = time.monotonic()
        times = self._reply_times[room_id]
        while times and now - times[0] >= _REPLY_WINDOW_SECONDS:
            times.popleft()
        if len(times) >= _MAX_REPLIES_PER_MINUTE:
            return False
        last = self._last_reply_at.get(room_id)
        return last is None or now - last >= self._cooldown

    def _record_reply(self, room_id: str) -> None:
        now = time.monotonic()
        self._reply_times[room_id].append(now)
        self._last_reply_at[room_id] = now

    async def _generate_reply(self, text: str) -> str:
        system = (
            f"你在B站直播间以「{self._bot_name or '主播助手'}」的身份回一条直播弹幕。"
            "要求：B站直播弹幕，极短（不超过一句话）、口语化、像真人随口接话，不刷屏；"
            "不要复述观众原话，不要发链接，不要使用 markdown。"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ]
        try:
            response = self._provider.chat(messages)
            if inspect.isawaitable(response):
                response = await response
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.warning("B站直播回复生成失败", error=f"{type(exc).__name__}: {exc}")
            return ""
        content = response.get("content", "") if isinstance(response, dict) else get_field(response, "content", "")
        return clean_reply(content, self._max_reply_chars)

    # ── 内部：任务与持久化 ──

    def _get_client(self) -> Any | None:
        """取当前可用 client；BilibiliService 休眠时 resolve_client 会回退成 service 本身。"""
        client = resolve_client(self._service)
        return client if callable(getattr(client, "send_danmaku", None)) else None

    def _start_task(self, room_id: str) -> None:
        if not self._running:
            return
        task = self._tasks.get(room_id)
        if task is not None and not task.done():
            return
        self._tasks[room_id] = asyncio.create_task(self._room_loop(room_id), name=f"bilibili-live-{room_id}")

    async def _cancel_task(self, room_id: str) -> None:
        task = self._tasks.pop(room_id, None)
        if task is None:
            return
        task.cancel()
        if task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        rooms = raw.get("rooms") if isinstance(raw, dict) else None
        if not isinstance(rooms, dict):
            return
        for key, value in rooms.items():
            room_id = _to_int(key)
            if room_id <= 0 or not isinstance(value, dict):
                continue
            self._rooms[str(room_id)] = {
                "real_room_id": _to_int(value.get("real_room_id")) or room_id,
                "name": str(value.get("name") or ""),
                "auto_reply": bool(value.get("auto_reply", True)),
            }

    def _save(self) -> None:
        payload = {
            "rooms": {
                room_id: {
                    "real_room_id": _to_int(room.get("real_room_id")) or _to_int(room_id),
                    "name": str(room.get("name") or ""),
                    "auto_reply": bool(room.get("auto_reply", True)),
                }
                for room_id, room in sorted(self._rooms.items(), key=lambda item: _to_int(item[0]))
            }
        }
        try:
            atomic_write_text(self._path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        except OSError as exc:
            self._logger.warning("B站直播交互状态保存失败", error=str(exc))


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
