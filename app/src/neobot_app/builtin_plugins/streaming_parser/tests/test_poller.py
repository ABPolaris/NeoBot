"""轮询测试：跳变判定（纯逻辑）与快照转换。"""

from __future__ import annotations

from datetime import datetime

import pytest
from streaming_parser.bilibili.models import CST, LiveRoomInfo
from streaming_parser.poller import (
    EVENT_LIVE,
    EVENT_LIVE_END,
    RoomSnapshot,
    detect_transition,
)


def snapshot(status: int) -> RoomSnapshot:
    return RoomSnapshot(room_id=1, live_status=status)


class TestDetectTransition:
    def test_first_sighting_never_notifies(self):
        # 首次见到某个房间只记录基线，避免插件重启后误报『刚开播』
        assert detect_transition(None, snapshot(1)) is None
        assert detect_transition(None, snapshot(0)) is None

    def test_offline_to_living_is_live(self):
        assert detect_transition(0, snapshot(1)) == EVENT_LIVE

    def test_rebroadcast_to_living_is_live(self):
        # 2 是轮播，转到 1 视为开播
        assert detect_transition(2, snapshot(1)) == EVENT_LIVE

    def test_living_to_offline_is_live_end(self):
        assert detect_transition(1, snapshot(0)) == EVENT_LIVE_END

    def test_living_to_rebroadcast_is_live_end(self):
        assert detect_transition(1, snapshot(2)) == EVENT_LIVE_END

    def test_no_change_returns_none(self):
        assert detect_transition(1, snapshot(1)) is None
        assert detect_transition(0, snapshot(0)) is None
        assert detect_transition(2, snapshot(2)) is None


class TestRoomSnapshot:
    def test_from_room_info_keeps_card_fields(self):
        info = LiveRoomInfo(
            room_id=21452505,
            short_id=5440,
            uid=401742377,
            title='测试标题',
            live_status=1,
            live_time=None,
            online=12345,
            area_name='虚拟主播',
            user_cover='https://example.invalid/cover.jpg',
        )
        snap = RoomSnapshot.from_room_info(info)
        assert snap.room_id == 21452505
        assert snap.title == '测试标题'
        assert snap.cover_url == 'https://example.invalid/cover.jpg'
        assert snap.area_name == '虚拟主播'
        assert snap.online == 12345
        assert snap.is_living is True

    @pytest.mark.parametrize('status,expected', [(0, False), (1, True), (2, False)])
    def test_only_status_one_counts_as_living(self, status, expected):
        assert RoomSnapshot(room_id=1, live_status=status).is_living is expected

    def test_cover_prefers_user_cover(self):
        info = LiveRoomInfo(
            room_id=1,
            short_id=0,
            uid=1,
            title='t',
            live_status=1,
            live_time=None,
            user_cover='uc.jpg',
            keyframe='kf.jpg',
        )
        assert RoomSnapshot.from_room_info(info).cover_url == 'uc.jpg'

    def test_cover_falls_back_to_keyframe(self):
        info = LiveRoomInfo(
            room_id=1,
            short_id=0,
            uid=1,
            title='t',
            live_status=1,
            live_time=None,
            user_cover='',
            keyframe='kf.jpg',
        )
        assert RoomSnapshot.from_room_info(info).cover_url == 'kf.jpg'

    def test_live_time_timezone_survives(self):
        moment = datetime(2026, 9, 26, 20, 30, tzinfo=CST)
        info = LiveRoomInfo(
            room_id=1, short_id=0, uid=1, title='t', live_status=1, live_time=moment
        )
        assert RoomSnapshot.from_room_info(info).live_time == moment

