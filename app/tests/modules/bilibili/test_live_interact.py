"""B站直播交互器：持久化、弹幕协议、过滤与自动回复。"""

from __future__ import annotations

import json
import struct
import time
import zlib
from pathlib import Path

from neobot_app.bilibili import live_interact
from neobot_app.bilibili.live_interact import (
    BilibiliLiveInteractor,
    decode_packets,
    encode_packet,
    parse_danmu_message,
)

_ROOM_INFO = {
    "room_info": {"room_id": 21452505, "title": "测试直播间"},
    "anchor_info": {"base_info": {"uname": "主播"}},
}


class _FakeClient:
    def __init__(
        self,
        *,
        room_info: dict | None = None,
        danmu_info: dict | None = None,
        my_uid: int = 7,
        send_ok: bool = True,
    ) -> None:
        self.room_info = _ROOM_INFO if room_info is None else room_info
        self.danmu_info = danmu_info or {}
        self.my_uid = my_uid
        self.send_ok = send_ok
        self.room_calls: list[int] = []
        self.sent: list[tuple[int, str]] = []

    def get_live_room_info(self, room_id: int) -> dict:
        self.room_calls.append(room_id)
        return self.room_info

    def get_live_danmu_info(self, room_id: int) -> dict:
        return self.danmu_info

    def send_danmaku(self, room_id: int, text: str) -> bool:
        if not self.send_ok:
            return False
        self.sent.append((room_id, text))
        return True


class _FakeProvider:
    def __init__(self, content: str = "在呢", *, raises: bool = False) -> None:
        self.content = content
        self.raises = raises
        self.messages: list[list[dict]] = []

    async def chat(self, messages: list[dict]) -> dict:
        self.messages.append(messages)
        if self.raises:
            raise RuntimeError("模型炸了")
        return {"content": self.content}


async def _enabled(
    tmp_path: Path,
    *,
    provider: object = None,
    bot_name: str = "小助手",
    cooldown: float = 0.0,
    at_only: bool = True,
    auto_reply: bool = True,
) -> tuple[BilibiliLiveInteractor, _FakeClient, _FakeProvider | None]:
    client = _FakeClient()
    interactor = BilibiliLiveInteractor(
        client,
        provider=provider,
        bot_name=bot_name,
        data_dir=tmp_path,
        cooldown_seconds=cooldown,
        max_reply_chars=40,
        at_only=at_only,
    )
    await interactor.enable(5440, auto_reply=auto_reply)
    return interactor, client, provider  # type: ignore[return-value]


async def test_enable_persists_state_and_restores_on_start(tmp_path: Path) -> None:
    client = _FakeClient()
    interactor = BilibiliLiveInteractor(client, data_dir=tmp_path)
    result = await interactor.enable(5440)
    assert result == {
        "ok": True,
        "room_id": 5440,
        "real_room_id": 21452505,
        "name": "主播",
        "auto_reply": True,
    }
    assert client.room_calls == [5440]
    state_path = tmp_path / "bilibili" / "live_interact.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state == {
        "rooms": {
            "5440": {
                "real_room_id": 21452505,
                "name": "主播",
                "auto_reply": True,
            }
        }
    }

    restarted = BilibiliLiveInteractor(None, data_dir=tmp_path)
    assert restarted.list_rooms() == [
        {
            "room_id": 5440,
            "real_room_id": 21452505,
            "name": "主播",
            "auto_reply": True,
        }
    ]
    await restarted.start()
    assert restarted.is_running is True
    assert len(restarted._tasks) == 1
    await restarted.stop()
    assert restarted.is_running is False


async def test_disable_and_auto_reply_persist(tmp_path: Path) -> None:
    interactor, _client, _provider = await _enabled(tmp_path)
    assert (await interactor.set_auto_reply(5440, False))["ok"] is True
    assert interactor.list_rooms()[0]["auto_reply"] is False
    state = json.loads((tmp_path / "bilibili" / "live_interact.json").read_text())
    assert state["rooms"]["5440"]["auto_reply"] is False

    assert (await interactor.set_auto_reply(9999, True))["ok"] is False
    assert (await interactor.disable(5440)) == {"ok": True, "room_id": 5440}
    assert interactor.list_rooms() == []
    assert (await interactor.disable(5440))["ok"] is False
    state = json.loads((tmp_path / "bilibili" / "live_interact.json").read_text())
    assert state == {"rooms": {}}


def test_encode_and_decode_packets_including_zlib() -> None:
    heartbeat = encode_packet(2)
    total, header_len, protover, op, seq = struct.unpack(">IHHII", heartbeat)
    assert (total, header_len, protover, op, seq) == (16, 16, 1, 2, 1)

    plain = encode_packet(5, b'{"cmd":"DANMU_MSG"}')
    assert decode_packets(plain) == [(5, b'{"cmd":"DANMU_MSG"}')]

    inner = encode_packet(5, b"one") + encode_packet(5, b"two")
    compressed = encode_packet(5, zlib.compress(inner), protover=2)
    assert decode_packets(compressed) == [(5, b"one"), (5, b"two")]
    assert decode_packets(plain[:8]) == []


def test_parse_danmu_message() -> None:
    payload = {
        "cmd": "DANMU_MSG:4:0:2:2:2:0",
        "info": [[], "你好", [12345, "观众甲"]],
    }
    assert parse_danmu_message(payload) == {
        "uid": 12345,
        "name": "观众甲",
        "text": "你好",
    }
    assert parse_danmu_message({"cmd": "SEND_GIFT", "info": []}) is None
    assert parse_danmu_message({"cmd": "DANMU_MSG", "info": []}) is None
    assert parse_danmu_message("not-json") is None


async def test_at_filter_generates_and_sends(tmp_path: Path) -> None:
    provider = _FakeProvider("在呢")
    interactor, client, _ = await _enabled(tmp_path, provider=provider)
    await interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "@小助手 唱首歌"})
    assert client.sent == [(5440, "在呢")]
    system, user = provider.messages[0]
    assert system["role"] == "system"
    assert "极短" in system["content"]
    assert user == {"role": "user", "content": "@小助手 唱首歌"}

    await interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "随便说"})
    assert client.sent == [(5440, "在呢")]


async def test_at_only_false_replies_to_any_danmu(tmp_path: Path) -> None:
    provider = _FakeProvider()
    interactor, client, _ = await _enabled(tmp_path, provider=provider, at_only=False)
    await interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "随便说"})
    assert client.sent == [(5440, "在呢")]


async def test_self_message_and_disabled_room_are_ignored(tmp_path: Path) -> None:
    provider = _FakeProvider()
    interactor, client, _ = await _enabled(tmp_path, provider=provider)
    await interactor._handle_danmu("5440", {"uid": 7, "name": "自己", "text": "@小助手 我发的"})
    assert client.sent == []
    assert provider.messages == []

    off, off_client, off_provider = await _enabled(tmp_path, provider=_FakeProvider(), auto_reply=False)
    await off._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "@小助手 hi"})
    assert off_client.sent == []
    assert off_provider.messages == []


async def test_provider_none_only_listens(tmp_path: Path) -> None:
    interactor, client, _ = await _enabled(tmp_path)
    await interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "@小助手 hi"})
    assert client.sent == []


async def test_cooldown_blocks_until_elapsed(tmp_path: Path) -> None:
    provider = _FakeProvider()
    interactor, client, _ = await _enabled(tmp_path, provider=provider, cooldown=20.0)
    await interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "@小助手 1"})
    await interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "@小助手 2"})
    assert len(client.sent) == 1
    assert len(provider.messages) == 1

    interactor._last_reply_at["5440"] = time.monotonic() - 21.0
    await interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "@小助手 3"})
    assert len(client.sent) == 2


async def test_per_minute_cap(tmp_path: Path) -> None:
    provider = _FakeProvider()
    interactor, client, _ = await _enabled(tmp_path, provider=provider, cooldown=0.0)
    for index in range(4):
        await interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": f"@小助手 {index}"})
    assert len(client.sent) == 3


class _FakeWS:
    def __init__(self, messages: list[object]) -> None:
        self.messages = list(messages)
        self.sent: list[bytes] = []

    async def __aenter__(self) -> "_FakeWS":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def __aiter__(self) -> "_FakeWS":
        return self

    async def __anext__(self) -> object:
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


class _FakeSession:
    def __init__(self, ws: _FakeWS) -> None:
        self.ws = ws
        self.url = ""
        self.headers: dict = {}

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def ws_connect(self, url: str, *, headers: dict) -> _FakeWS:
        self.url = url
        self.headers = headers
        return self.ws


async def test_websocket_auth_packet_and_danmu_flow(tmp_path: Path, monkeypatch) -> None:
    provider = _FakeProvider("来了")
    client = _FakeClient(
        danmu_info={
            "token": "live-token",
            "host_list": [{"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}],
        }
    )
    interactor = BilibiliLiveInteractor(client, provider=provider, bot_name="小助手", data_dir=tmp_path)
    await interactor.enable(5440)
    danmu = {
        "cmd": "DANMU_MSG:4:0:2:2:2:0",
        "info": [[], "@小助手 在吗", [8, "观众"]],
    }
    packet = encode_packet(5, json.dumps(danmu).encode("utf-8"))
    message = type("Msg", (), {"type": live_interact.aiohttp.WSMsgType.BINARY, "data": packet})()
    ws = _FakeWS([message])
    session = _FakeSession(ws)
    monkeypatch.setattr(live_interact.aiohttp, "ClientSession", lambda *a, **k: session)

    await interactor._connect_once("5440")

    assert session.url == "wss://broadcastlv.chat.bilibili.com:443/sub"
    assert session.headers["Origin"] == "https://live.bilibili.com"
    op, body = decode_packets(ws.sent[0])[0]
    assert op == 7
    assert json.loads(body) == {
        "uid": 7,
        "roomid": 21452505,
        "protover": 2,
        "platform": "web",
        "type": 2,
        "key": "live-token",
    }
    assert client.sent == [(5440, "来了")]


async def test_reply_is_cleaned_and_truncated(tmp_path: Path) -> None:
    provider = _FakeProvider("这是一句很长的话" * 20)
    interactor, client, _ = await _enabled(tmp_path, provider=provider)
    interactor._max_reply_chars = 5
    await interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "@小助手 hi"})
    assert len(client.sent) == 1
    assert len(client.sent[0][1]) <= 5


async def test_provider_error_and_send_failure_do_not_raise(tmp_path: Path) -> None:
    broken = _FakeProvider(raises=True)
    interactor, client, _ = await _enabled(tmp_path, provider=broken)
    await interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "@小助手 hi"})
    assert client.sent == []

    failing = _FakeClient(send_ok=False)
    provider = _FakeProvider()
    failing_interactor = BilibiliLiveInteractor(failing, provider=provider, bot_name="小助手", data_dir=tmp_path)
    await failing_interactor.enable(5440)
    await failing_interactor._handle_danmu("5440", {"uid": 8, "name": "观众", "text": "@小助手 hi"})
    assert failing.sent == []


async def test_public_send_danmaku(tmp_path: Path) -> None:
    client = _FakeClient()
    interactor = BilibiliLiveInteractor(client, data_dir=tmp_path)
    assert (await interactor.send_danmaku(5440, "  hi  "))["ok"] is True
    assert client.sent == [(5440, "hi")]
    assert (await interactor.send_danmaku(0, "hi"))["ok"] is False
    assert (await interactor.send_danmaku(5440, "  "))["ok"] is False
    assert (await BilibiliLiveInteractor(None, data_dir=tmp_path).send_danmaku(5440, "hi"))["ok"] is False
