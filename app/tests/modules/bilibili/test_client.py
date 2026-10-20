"""B 站 HTTP 客户端：认证、评论、私信、重试与 WBI 签名。"""

from __future__ import annotations

import time

import pytest
import requests

from neobot_app.bilibili.client import BilibiliClient, BilibiliMessage, BilibiliSession
from neobot_app.bilibili.errors import BilibiliApiError, BilibiliAuthError

from ._fakes import FakeResponse, FakeSession

_NAV_OK = {
    "code": 0,
    "data": {"isLogin": True, "mid": 7, "uname": "tester", "face": "face.png"},
}
_NAV_WBI = {
    "code": 0,
    "data": {
        "isLogin": True,
        "mid": 7,
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
        },
    },
}


def _client(session: FakeSession, uid: int = 7, **kwargs: object) -> BilibiliClient:
    return BilibiliClient("sessdata", "bili-jct", uid, session=session, **kwargs)


def test_verify_auth_true_when_nav_logged_in() -> None:
    session = FakeSession([FakeResponse(_NAV_OK)])
    client = _client(session)
    assert client.verify_auth() is True
    assert session.calls[0]["url"].endswith("/x/web-interface/nav")
    assert client.my_uid == 7


def test_verify_auth_false_when_nav_not_logged_in() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {"isLogin": False}})])
    assert _client(session, uid=0).verify_auth() is False


def test_verify_auth_false_on_auth_error() -> None:
    session = FakeSession([FakeResponse({"code": -101, "message": "账号未登录"})])
    assert _client(session).verify_auth() is False


def test_get_reply_feed_returns_items_and_respects_limit() -> None:
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    session = FakeSession([FakeResponse({"code": 0, "data": {"items": items}})])
    feed = _client(session).get_reply_feed(limit=2)
    assert feed == items[:2]
    assert session.calls[0]["params"]["platform"] == "web"


def test_get_reply_feed_returns_empty_on_api_error() -> None:
    session = FakeSession([FakeResponse({"code": -400, "message": "请求错误"})])
    assert _client(session).get_reply_feed() == []


def test_get_at_feed_returns_items_and_uses_message_center_context() -> None:
    items = [{"id": 1}, {"id": 2}]
    session = FakeSession([FakeResponse({"code": 0, "data": {"items": items}})])
    feed = _client(session).get_at_feed(limit=1)
    assert feed == items[:1]
    call = session.calls[0]
    assert call["url"].endswith("/x/msgfeed/at")
    assert call["params"] == {"platform": "web", "build": 0, "mobi_app": "web"}
    assert call["headers"]["Referer"] == "https://message.bilibili.com/"


def test_get_at_feed_returns_empty_on_api_error() -> None:
    session = FakeSession([FakeResponse({"code": -101, "message": "账号未登录"})])
    assert _client(session).get_at_feed() == []


def test_get_video_comments_signs_wbi_params() -> None:
    session = FakeSession(
        [
            FakeResponse(_NAV_WBI),
            FakeResponse({"code": 0, "data": {"replies": [{"rpid": 1}, {"rpid": 2}]}}),
        ]
    )
    replies = _client(session).get_video_comments(oid=170001, page=2, order="time")
    assert [item["rpid"] for item in replies] == [1, 2]
    params = session.calls[-1]["params"]
    # WBI 签名会把参数值统一转成字符串再计算 w_rid。
    assert params["type"] == "1"
    assert params["oid"] == "170001"
    assert params["mode"] == "2"
    assert params["next"] == "1"
    assert params["wts"]
    assert len(params["w_rid"]) == 32


def test_get_video_comments_hot_uses_mode_3() -> None:
    session = FakeSession(
        [FakeResponse(_NAV_WBI), FakeResponse({"code": 0, "data": {"replies": []}})]
    )
    assert _client(session).get_video_comments(oid=1, order="hot") == []
    assert session.calls[-1]["params"]["mode"] == "3"


def test_get_comment_replies_hits_reply_reply_and_signs() -> None:
    replies = [{"rpid": 11}, {"rpid": 12}]
    session = FakeSession(
        [
            FakeResponse(_NAV_WBI),
            FakeResponse({"code": 0, "data": {"replies": replies}}),
        ]
    )
    result = _client(session).get_comment_replies(
        oid=170001, root=9, page=2, page_size=10
    )
    assert result == replies
    call = session.calls[-1]
    assert call["url"].endswith("/x/v2/reply/reply")
    params = call["params"]
    # WBI 签名会把参数值统一转成字符串再计算 w_rid。
    assert params["type"] == "1"
    assert params["oid"] == "170001"
    assert params["root"] == "9"
    assert params["pn"] == "2"
    assert params["ps"] == "10"
    assert len(params["w_rid"]) == 32


def test_get_comment_replies_returns_empty_on_error_or_bad_args() -> None:
    session = FakeSession(
        [FakeResponse(_NAV_WBI), FakeResponse({"code": -400, "message": "请求错误"})]
    )
    client = _client(session)
    assert client.get_comment_replies(oid=170001, root=9) == []
    assert client.get_comment_replies(oid=0, root=9) == []
    assert client.get_comment_replies(oid=170001, root=0) == []
    assert len(session.calls) == 2


def test_send_comment_reply_posts_expected_form() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {"rpid": 5566}})])
    assert _client(session).send_comment_reply(oid=1, root=2, parent=3, text="  hi  ")
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/x/v2/reply/add")
    assert call["data"] == {
        "oid": 1,
        "type": 1,
        "message": "hi",
        "plat": 1,
        "csrf": "bili-jct",
        "root": 2,
        "parent": 3,
    }


def test_send_comment_reply_returns_false_on_api_error() -> None:
    session = FakeSession([FakeResponse({"code": 12002, "message": "评论区已关闭"})])
    assert _client(session).send_comment_reply(oid=1, root=2, parent=2, text="hi") is False


def test_send_message_posts_expected_form() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {}})])
    assert _client(session).send_message(8, "hello") is True
    form = session.calls[0]["data"]
    assert form["msg[sender_uid]"] == 7
    assert form["msg[receiver_id]"] == 8
    assert form["csrf"] == "bili-jct"
    assert "hello" in form["msg[content]"]


def test_send_message_without_uid_returns_false() -> None:
    session = FakeSession([FakeResponse({"code": -101, "message": "未登录"})])
    client = _client(session, uid=0)
    assert client.send_message(8, "hello") is False


def test_get_sessions_parses_and_sorts_by_last_message() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "session_list": [
                            {
                                "talker_id": 1,
                                "last_msg": {
                                    "sender_uid": 1,
                                    "msg_type": 1,
                                    "content": '{"content": "old"}',
                                    "timestamp": 100,
                                },
                            },
                            {
                                "talker_id": 2,
                                "last_msg": {
                                    "sender_uid": 7,
                                    "msg_type": 1,
                                    "content": '{"content": "new"}',
                                    "timestamp": 200,
                                },
                            },
                        ]
                    },
                }
            )
        ]
    )
    sessions = _client(session).get_sessions(count=10)
    assert [item.talker_id for item in sessions] == [2, 1]
    assert isinstance(sessions[0], BilibiliSession)
    assert sessions[0].last_msg is not None
    assert sessions[0].last_msg.text == "new"


def test_get_messages_parses_messages() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "messages": [
                            {
                                "receiver_id": 7,
                                "sender_uid": 7,
                                "msg_type": 1,
                                "content": '{"content": "mine"}',
                                "timestamp": 300,
                            },
                            {
                                "receiver_id": 7,
                                "sender_uid": 9,
                                "msg_type": 2,
                                "content": '{"url": "https://x/y.png"}',
                                "timestamp": 301,
                            },
                        ]
                    },
                }
            )
        ]
    )
    messages = _client(session).get_messages(7, size=5)
    assert [item.text for item in messages] == ["mine", ""]
    assert isinstance(messages[0], BilibiliMessage)
    assert messages[0].is_self is True
    assert messages[1].is_self is False


def test_get_new_messages_deduplicates_and_skips_self() -> None:
    payload = {
        "code": 0,
        "data": {
            "messages": [
                {"sender_uid": 9, "msg_type": 1, "content": '{"content": "a"}', "timestamp": 10},
                {"sender_uid": 7, "msg_type": 1, "content": '{"content": "self"}', "timestamp": 11},
            ]
        },
    }
    session = FakeSession([FakeResponse(payload)])
    client = _client(session)
    session_obj = BilibiliSession(talker_id=7)
    first = client.get_new_messages(session_obj)
    assert [item.text for item in first] == ["a"]
    session.queue(FakeResponse(payload))
    assert client.get_new_messages(session_obj) == []


def test_retries_on_http_5xx(monkeypatch) -> None:
    monkeypatch.setattr("neobot_app.bilibili.client.time.sleep", lambda _seconds: None)
    session = FakeSession([FakeResponse(None, status_code=503), FakeResponse(_NAV_OK)])
    assert _client(session).verify_auth() is True
    assert len(session.calls) == 2


def test_network_error_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr("neobot_app.bilibili.client.time.sleep", lambda _seconds: None)

    class _RaisingSession(FakeSession):
        def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            raise requests.ConnectionError("boom")

    assert _client(_RaisingSession()).get_reply_feed() == []


def test_rate_limiter_sleeps_between_writes(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("neobot_app.bilibili.client.time.sleep", sleeps.append)
    session = FakeSession([FakeResponse({"code": 0, "data": {}})])
    client = _client(session)
    client._last_write_at = time.monotonic()  # type: ignore[attr-defined]
    assert client.send_message(8, "hello") is True
    assert sleeps and sleeps[0] > 0


def test_write_limiter_error_codes_raise_interval() -> None:
    client = _client(FakeSession())
    initial = client._write_interval  # type: ignore[attr-defined]
    client._on_write_error("评论回复", BilibiliApiError(-412, "风控"))  # type: ignore[attr-defined]
    assert client._write_interval > initial  # type: ignore[attr-defined]


def test_auth_error_from_write_is_not_raised() -> None:
    session = FakeSession([FakeResponse({"code": -101, "message": "未登录"})])
    assert _client(session).send_comment_reply(oid=1, root=2, parent=2, text="hi") is False
    with pytest.raises(BilibiliAuthError):
        _client(FakeSession([FakeResponse({"code": -101})])).publish_dynamic("hi")
