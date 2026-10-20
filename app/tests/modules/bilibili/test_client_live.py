"""B站直播与视频互动写操作：端点参数、csrf、BV 解析与失败降级。"""

from __future__ import annotations

from neobot_app.bilibili.client import BilibiliClient

from ._fakes import FakeResponse, FakeSession

_ROOM_INFO = {
    "code": 0,
    "data": {
        "room_info": {"room_id": 21452505, "title": "测试直播间"},
        "anchor_info": {"base_info": {"uname": "主播"}},
    },
}
_DANMU_INFO = {
    "code": 0,
    "data": {
        "token": "live-token",
        "host_list": [{"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}],
    },
}
_NAV_WBI = {
    "code": 0,
    "data": {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
        }
    },
}

_VIEW = {"code": 0, "data": {"aid": 170001, "bvid": "BV1xx411c7mD"}}


def _client(session: FakeSession) -> BilibiliClient:
    return BilibiliClient("sessdata", "bili-jct", 7, session=session)


def test_get_live_room_info_returns_data() -> None:
    session = FakeSession([FakeResponse(_ROOM_INFO)])
    data = _client(session).get_live_room_info(5440)
    assert data["room_info"]["room_id"] == 21452505
    call = session.calls[0]
    assert call["url"].endswith("/xlive/web-room/v1/index/getInfoByRoom")
    assert call["params"] == {"room_id": 5440}


def test_get_live_danmu_info_uses_real_room_id() -> None:
    session = FakeSession([FakeResponse(_ROOM_INFO), FakeResponse(_NAV_WBI), FakeResponse(_DANMU_INFO)])
    data = _client(session).get_live_danmu_info(5440)
    assert data["token"] == "live-token"
    call = session.calls[-1]
    assert call["url"].endswith("/xlive/web-room/v1/index/getDanmuInfo")
    assert call["params"]["id"] == "21452505"
    assert call["params"]["type"] == "0"
    assert call["params"]["wts"]
    assert call["params"]["w_rid"]
    assert call["headers"]["Referer"] == "https://live.bilibili.com/21452505"


def test_get_live_room_info_and_danmu_info_degrade() -> None:
    assert _client(FakeSession()).get_live_room_info(0) == {}
    session = FakeSession([FakeResponse(_ROOM_INFO), FakeResponse(_NAV_WBI), FakeResponse({"code": -400, "message": "请求错误"})])
    assert _client(session).get_live_danmu_info(5440) == {}


def test_send_danmaku_posts_expected_form() -> None:
    session = FakeSession([FakeResponse(_ROOM_INFO), FakeResponse({"code": 0, "data": {}})])
    assert _client(session).send_danmaku(5440, "  hi  ") is True
    call = session.calls[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/msg/send")
    form = call["data"]
    assert form["roomid"] == 21452505
    assert form["msg"] == "hi"
    assert form["color"] == 16777215
    assert form["mode"] == 1
    assert form["fontsize"] == 25
    assert form["csrf"] == "bili-jct"
    assert form["csrf_token"] == "bili-jct"
    assert isinstance(form["rnd"], int)
    assert call["headers"]["Origin"] == "https://live.bilibili.com"


def test_send_danmaku_returns_false_on_api_error_and_missing_cookies() -> None:
    session = FakeSession([FakeResponse(_ROOM_INFO), FakeResponse({"code": 10030, "message": "超出限制"})])
    assert _client(session).send_danmaku(5440, "hi") is False

    client = _client(FakeSession())
    client._bili_jct = ""  # type: ignore[attr-defined]
    assert client.send_danmaku(5440, "hi") is False


def test_post_video_comment_resolves_bv_to_aid() -> None:
    session = FakeSession([FakeResponse(_VIEW), FakeResponse({"code": 0, "data": {"rpid": 1}})])
    client = _client(session)
    assert client.post_video_comment("https://www.bilibili.com/video/BV1xx411c7mD/", " hi ") is True
    assert session.calls[0]["params"] == {"bvid": "BV1xx411c7mD"}
    call = session.calls[-1]
    assert call["url"].endswith("/x/v2/reply/add")
    assert call["data"] == {
        "oid": 170001,
        "type": 1,
        "message": "hi",
        "plat": 1,
        "csrf": "bili-jct",
    }


def test_post_video_comment_av_and_root_parent() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {"rpid": 2}})])
    assert _client(session).post_video_comment("av170001", "回复", root=10, parent=11) is True
    form = session.calls[0]["data"]
    assert form["oid"] == 170001
    assert form["root"] == 10
    assert form["parent"] == 11


def test_post_video_comment_degrades_on_bad_identifier_and_error() -> None:
    assert _client(FakeSession()).post_video_comment("没有视频号", "hi") is False
    session = FakeSession([FakeResponse(_VIEW), FakeResponse({"code": 12002, "message": "评论区已关闭"})])
    assert _client(session).post_video_comment("BV1xx411c7mD", "hi") is False


def test_like_video_resolves_bv_and_posts_form() -> None:
    session = FakeSession([FakeResponse(_VIEW), FakeResponse({"code": 0, "data": {}})])
    assert _client(session).like_video("BV1xx411c7mD") is True
    call = session.calls[-1]
    assert call["url"].endswith("/x/web-interface/archive/like")
    assert call["data"] == {"aid": 170001, "like": 1, "csrf": "bili-jct"}


def test_like_video_av_and_failures() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {}})])
    assert _client(session).like_video("av170001") is True
    assert len(session.calls) == 1
    denied = FakeSession([FakeResponse({"code": -101, "message": "未登录"})])
    assert _client(denied).like_video("av170001") is False
    assert _client(FakeSession()).like_video("不是视频") is False


def test_like_comment_posts_form_and_degrades() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {}})])
    assert _client(session).like_comment(oid=170001, rpid=9, type_=1) is True
    call = session.calls[0]
    assert call["url"].endswith("/x/v2/reply/action")
    assert call["data"] == {
        "oid": 170001,
        "rpid": 9,
        "action": 1,
        "type": 1,
        "csrf": "bili-jct",
    }
    assert _client(FakeSession()).like_comment(oid=0, rpid=9) is False
    denied = FakeSession([FakeResponse({"code": -412, "message": "风控"})])
    assert _client(denied).like_comment(oid=1, rpid=2) is False


# ── 投币 / 分享 / 收藏 ──

_FAV_FOLDERS = {
    "code": 0,
    "data": {
        "list": [
            {"id": 101, "fid": 101, "title": "默认收藏夹", "media_count": 3, "attr": 1},
            "bad",
        ],
        "count": 1,
    },
}


def test_coin_video_resolves_bv_and_defaults_to_one_coin() -> None:
    session = FakeSession([FakeResponse(_VIEW), FakeResponse({"code": 0, "data": {}})])
    assert _client(session).coin_video("https://www.bilibili.com/video/BV1xx411c7mD/") is True
    assert session.calls[0]["params"] == {"bvid": "BV1xx411c7mD"}
    call = session.calls[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/x/web-interface/coin/add")
    assert call["data"] == {"aid": 170001, "multiply": 1, "csrf": "bili-jct"}


def test_coin_video_two_coins_av_and_failures() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {}})])
    assert _client(session).coin_video("av170001", 2) is True
    assert len(session.calls) == 1
    assert session.calls[0]["data"] == {"aid": 170001, "multiply": 2, "csrf": "bili-jct"}

    limited = _client(FakeSession([FakeResponse({"code": -412, "message": "请求被拦截"})]))
    assert limited.coin_video("av170001") is False
    assert limited._write_interval > 1.0  # type: ignore[attr-defined]
    assert _client(FakeSession()).coin_video("不是视频") is False


def test_share_video_posts_bvid_and_resolves_av() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {}})])
    assert _client(session).share_video("BV1xx411c7mD") is True
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"].endswith("/x/web-interface/share/add")
    assert call["data"] == {"bvid": "BV1xx411c7mD", "csrf": "bili-jct"}

    av_session = FakeSession([FakeResponse(_VIEW), FakeResponse({"code": 0, "data": {}})])
    assert _client(av_session).share_video("av170001") is True
    assert av_session.calls[0]["params"] == {"aid": 170001}
    assert av_session.calls[-1]["data"]["bvid"] == "BV1xx411c7mD"


def test_share_video_degrades() -> None:
    denied = FakeSession([FakeResponse({"code": -101, "message": "未登录"})])
    assert _client(denied).share_video("av170001") is False
    assert _client(FakeSession()).share_video("不是视频") is False


def test_interaction_writes_require_login() -> None:
    client = _client(FakeSession())
    client._bili_jct = ""  # type: ignore[attr-defined]
    assert client.coin_video("av170001") is False
    assert client.share_video("av170001") is False
    assert client.favorite_video("av170001", 101) is False
    assert client.get_favorite_folders() == []


def test_get_favorite_folders_lists_created_folders() -> None:
    session = FakeSession([FakeResponse(_FAV_FOLDERS)])
    folders = _client(session).get_favorite_folders()
    assert folders == [
        {"id": 101, "fid": 101, "title": "默认收藏夹", "media_count": 3, "attr": 1}
    ]
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/x/v3/fav/folder/created/list-all")
    assert call["params"] == {"up_mid": 7}


def test_get_favorite_folders_degrades() -> None:
    denied = FakeSession([FakeResponse({"code": -101, "message": "未登录"})])
    assert _client(denied).get_favorite_folders() == []
    empty = FakeSession([FakeResponse({"code": 0, "data": {"list": None}})])
    assert _client(empty).get_favorite_folders() == []


def test_favorite_video_posts_deal_form_and_degrades() -> None:
    session = FakeSession([FakeResponse(_VIEW), FakeResponse({"code": 0, "data": {}})])
    assert _client(session).favorite_video("BV1xx411c7mD", 101) is True
    assert session.calls[0]["params"] == {"bvid": "BV1xx411c7mD"}
    call = session.calls[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/x/v3/fav/resource/deal")
    assert call["data"] == {
        "rid": 170001,
        "type": 2,
        "add_media_ids": 101,
        "del_media_ids": "",
        "csrf": "bili-jct",
    }

    assert _client(FakeSession()).favorite_video("av170001", 0) is False
    assert _client(FakeSession()).favorite_video("不是视频", 101) is False
    denied = FakeSession([FakeResponse({"code": -111, "message": "csrf校验失败"})])
    assert _client(denied).favorite_video("av170001", 101) is False
