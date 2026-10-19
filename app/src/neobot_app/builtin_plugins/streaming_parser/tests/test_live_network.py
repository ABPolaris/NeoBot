"""真实网络冒烟测试（默认跳过，用 -m network 显式运行）。

这些测试断言的是「协议现在还能用」，因此断网、风控、房间下播都可能让它失败。
风控类失败会转成 skip 并打印原因，避免把平台抖动误报成代码回归。
"""

from __future__ import annotations

import pytest

from bilibili.client import BilibiliLiveClient, HttpxTransport
from bilibili.errors import BilibiliAPIError

pytestmark = pytest.mark.network

#: B 站官方直播间（room 1）；短号 5440 对应 room 21452505（随平台变化，仅用于解析）
OFFICIAL_ROOM_ID = 1


def make_client() -> BilibiliLiveClient:
    return BilibiliLiveClient(HttpxTransport())


async def test_live_room_init_online():
    client = make_client()
    try:
        init = await client.fetch_live_room_init(OFFICIAL_ROOM_ID)
    except BilibiliAPIError as exc:
        pytest.skip(f"平台侧未能完成请求: {exc}")
    assert init.room_id > 0
    assert init.live_status in (0, 1, 2)
    # 真实流量里 live_time 可能是 0 / 负哨兵 / 越界值，模型必须归一化成 None
    assert init.live_time is None or init.live_time.year >= 1970


async def test_live_room_info_online():
    client = make_client()
    try:
        info = await client.fetch_live_room_info(OFFICIAL_ROOM_ID)
    except BilibiliAPIError as exc:
        pytest.skip(f"平台侧未能完成请求: {exc}")
    assert info.room_id > 0
    assert isinstance(info.title, str)


async def test_user_live_status_online():
    client = make_client()
    try:
        status = await client.fetch_user_live_status(2)
    except BilibiliAPIError as exc:
        pytest.skip(f"平台侧未能完成请求: {exc}")
    assert status.room_status in (0, 1)


async def test_room_init_matches_get_info_room_id():
    """两个接口对同一个房间号应给出一致的真实房间号（交叉验证）。"""
    client = make_client()
    try:
        init = await client.fetch_live_room_init(OFFICIAL_ROOM_ID)
        info = await client.fetch_live_room_info(OFFICIAL_ROOM_ID)
    except BilibiliAPIError as exc:
        pytest.skip(f"平台侧未能完成请求: {exc}")
    assert init.room_id == info.room_id

