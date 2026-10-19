"""URL 构造与请求头基线（对照 amagi api.ts:205-218 / config.ts:16-35）。"""

from __future__ import annotations

import pytest

from bilibili.headers import DEFAULT_UA, live_room_headers, sec_ch_ua
from bilibili.urls import (
    LIVE_API_BASE,
    live_room_info_url,
    live_room_init_url,
    live_room_page_url,
    normalize_host_mid,
    normalize_room_id,
    user_live_status_url,
)


class TestUrls:
    def test_user_live_status_url_matches_amagi(self):
        assert (
            user_live_status_url(401742377)
            == LIVE_API_BASE + "/room/v1/Room/getRoomInfoOld?mid=401742377"
        )

    def test_live_room_info_url_matches_amagi(self):
        assert live_room_info_url("1") == LIVE_API_BASE + "/room/v1/Room/get_info?room_id=1"

    def test_live_room_init_url_matches_amagi(self):
        assert live_room_init_url("1") == LIVE_API_BASE + "/room/v1/Room/room_init?id=1"

    def test_room_id_accepts_int_and_str_equally(self):
        """改进项：amagi 只接受字符串 room_id，本实现把数字也归一化。"""
        assert live_room_init_url(5440) == live_room_init_url("5440")
        assert live_room_init_url("  5440  ") == live_room_init_url(5440)

    def test_host_mid_accepts_numeric_str(self):
        assert user_live_status_url("401742377") == user_live_status_url(401742377)

    def test_room_page_url(self):
        assert live_room_page_url(1) == "https://live.bilibili.com/1"

    @pytest.mark.parametrize("bad", [0, -1, "", "abc", "12a", None, 1.5, True])
    def test_normalize_room_id_rejects_bad_input(self, bad):
        with pytest.raises((TypeError, ValueError)):
            normalize_room_id(bad)

    @pytest.mark.parametrize("bad", [0, -3, "", "abc", None, False])
    def test_normalize_host_mid_rejects_bad_input(self, bad):
        with pytest.raises((TypeError, ValueError)):
            normalize_host_mid(bad)


class TestHeaders:
    def test_referer_points_at_live_room(self):
        headers = live_room_headers(21452505)
        assert headers["referer"] == "https://live.bilibili.com/21452505"
        assert headers["origin"] == "https://live.bilibili.com"

    def test_uid_query_falls_back_to_site_root(self):
        assert live_room_headers()["referer"] == "https://live.bilibili.com/"

    def test_cookie_only_present_when_set(self):
        assert "cookie" not in live_room_headers(1)
        headers = live_room_headers(1, cookie=" SESSDATA=abc ")
        assert headers["cookie"] == "SESSDATA=abc"

    def test_default_ua_and_derived_sec_ch_ua(self):
        headers = live_room_headers(1)
        assert headers["user-agent"] == DEFAULT_UA
        assert "Chromium" in headers["sec-ch-ua"]
        assert 'v="142"' in headers["sec-ch-ua"]

    def test_sec_ch_ua_falls_back_to_125(self):
        assert 'v="125"' in sec_ch_ua("curl/8.0")

    def test_custom_user_agent_is_used(self):
        headers = live_room_headers(1, user_agent="my-ua Chrome/100.0.0.0")
        assert headers["user-agent"] == "my-ua Chrome/100.0.0.0"
        assert 'v="100"' in headers["sec-ch-ua"]

