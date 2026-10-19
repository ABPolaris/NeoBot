"""响应模型与 live_time 归一化。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bilibili.models import (
    CST,
    LiveRoomInfo,
    LiveRoomInit,
    UserLiveStatus,
    normalize_live_time,
)

#: 1700000000 秒 → 2023-11-15 06:13:20 +08:00
EXPECTED = datetime(2023, 11, 15, 6, 13, 20, tzinfo=CST)


class TestNormalizeLiveTime:
    def test_int_timestamp_from_room_init(self):
        assert normalize_live_time(1700000000) == EXPECTED

    def test_str_datetime_from_get_info(self):
        assert normalize_live_time("2023-11-15 06:13:20") == EXPECTED

    def test_offset_is_east_eight(self):
        assert normalize_live_time(1700000000).utcoffset() == timedelta(hours=8)
        assert CST == timezone(timedelta(hours=8))

    @pytest.mark.parametrize(
        "value",
        [None, 0, "", "   ", "0000-00-00 00:00:00", True, False],
    )
    def test_placeholder_values_mean_no_live_time(self, value):
        assert normalize_live_time(value) is None

    def test_real_world_sentinel_from_room_init(self):
        """真实流量：room_init 用 -62170012800 表示「没有开播时间」。

        这是 .NET DateTime.MinValue 的秒数；直接 fromtimestamp 会抛 OSError，
        参考项目没有记录这个值，是本项目实测补充的。
        """
        assert normalize_live_time(-62170012800) is None

    @pytest.mark.parametrize("value", [-1, 4102444801, 10**18])
    def test_out_of_range_timestamps_are_none(self, value):
        assert normalize_live_time(value) is None

    def test_numeric_string_is_treated_as_timestamp(self):
        assert normalize_live_time("1700000000") == EXPECTED

    def test_unparseable_string_raises(self):
        with pytest.raises(ValueError):
            normalize_live_time("not-a-time")

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            normalize_live_time(["2023"])


class TestLiveRoomInit:
    def test_from_fixture(self, load_fixture):
        init = LiveRoomInit.from_payload(load_fixture("live_room_init.json"))
        assert init.room_id == 21452505
        assert init.short_id == 5440
        assert init.uid == 401742377
        assert init.live_status == 1
        assert init.live_time == EXPECTED
        assert init.is_living is True
        assert init.has_short_id is True

    def test_real_room_one_payload(self, load_fixture):
        """真实抓取的 room_init(id=1)：room_id 5440 / short_id 1 / 轮播中。"""
        init = LiveRoomInit.from_payload(load_fixture("live_room_init_sentinel.json"))
        assert init.room_id == 5440
        assert init.short_id == 1
        assert init.uid == 9617619
        assert init.live_status == 2
        assert init.live_time is None
        assert init.is_living is False

    def test_short_id_zero_means_no_short_id(self):
        init = LiveRoomInit.from_payload(
            {"code": 0, "data": {"room_id": 1, "short_id": 0, "live_status": 0}}
        )
        assert init.has_short_id is False
        assert init.is_living is False

    def test_round_play_is_not_living(self):
        init = LiveRoomInit.from_payload({"code": 0, "data": {"live_status": 2}})
        assert init.is_living is False


class TestLiveRoomInfo:
    def test_from_fixture(self, load_fixture):
        info = LiveRoomInfo.from_payload(load_fixture("live_room_info.json"))
        assert info.room_id == 21452505
        assert info.title == "测试直播间标题"
        assert info.online == 12345
        assert info.area_name == "虚拟主播"
        assert info.live_time == EXPECTED
        assert info.is_living is True

    def test_cover_falls_back_to_keyframe(self):
        info = LiveRoomInfo.from_payload(
            {"code": 0, "data": {"user_cover": "", "keyframe": "kf.jpg"}}
        )
        assert info.cover == "kf.jpg"

    def test_cover_prefers_user_cover(self):
        info = LiveRoomInfo.from_payload(
            {"code": 0, "data": {"user_cover": "uc.jpg", "keyframe": "kf.jpg"}}
        )
        assert info.cover == "uc.jpg"


class TestUserLiveStatus:
    def test_living_fixture(self, load_fixture):
        status = UserLiveStatus.from_payload(load_fixture("user_live_status.json"))
        assert status.live_status == 1
        assert status.room_status == 1
        assert status.room_id == 21452505
        assert status.is_living is True
        assert status.has_room is True

    def test_offline_and_no_room_fixture(self, load_fixture):
        status = UserLiveStatus.from_payload(
            load_fixture("user_live_status_offline.json")
        )
        assert status.is_living is False
        assert status.has_room is False


class TestPayloadGuards:
    @pytest.mark.parametrize("payload", [None, "text", 42, []])
    def test_non_dict_payload_raises(self, payload):
        with pytest.raises(TypeError):
            UserLiveStatus.from_payload(payload)

    def test_missing_data_raises(self):
        with pytest.raises(ValueError):
            LiveRoomInit.from_payload({"code": 0})

