"""动态发布：请求体、图片上传、csrf 与错误处理。"""

from __future__ import annotations

import pytest

from neobot_app.bilibili.client import BilibiliClient
from neobot_app.bilibili.errors import BilibiliApiError, BilibiliAuthError

from ._fakes import FakeResponse, FakeSession

_CREATE_OK = {"code": 0, "data": {"dyn_id": 588320531406678918, "dyn_id_str": "588320531406678918"}}


def _client(session: FakeSession) -> BilibiliClient:
    return BilibiliClient("sessdata", "bili-jct", 7, session=session)


def test_publish_text_dynamic_payload() -> None:
    session = FakeSession([FakeResponse(_CREATE_OK)])
    dynamic_id = _client(session).publish_dynamic("hello bilibili")
    assert dynamic_id == 588320531406678918

    call = session.calls[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/x/dynamic/feed/create/dyn")
    assert call["params"] == {"csrf": "bili-jct"}
    dyn_req = call["json"]["dyn_req"]
    assert dyn_req["scene"] == 1
    assert dyn_req["content"]["contents"] == [
        {"biz_id": "", "type": 1, "raw_text": "hello bilibili"}
    ]
    assert dyn_req["meta"] == {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}}
    assert dyn_req["attach_card"] is None
    assert "pics" not in dyn_req


def test_publish_dynamic_with_image_uploads_first(tmp_path) -> None:
    image = tmp_path / "pixel.png"
    image.write_bytes(b"\x89PNG-fake-image")
    upload_payload = {
        "code": 0,
        "data": {
            "image_url": "https://i0.hdslb.com/bfs/new_dyn/pixel.png",
            "image_width": 640,
            "image_height": 480,
            "img_size": 12.5,
        },
    }
    session = FakeSession([FakeResponse(upload_payload), FakeResponse(_CREATE_OK)])
    dynamic_id = _client(session).publish_dynamic("with image", images=[image])
    assert dynamic_id == 588320531406678918

    upload_call = session.calls[0]
    assert upload_call["url"].endswith("/x/dynamic/feed/draw/upload_bfs")
    assert upload_call["data"] == {"category": "daily", "biz": "new_dyn", "csrf": "bili-jct"}
    filename, content, mime_type = upload_call["files"]["file_up"]
    assert filename == "pixel.png"
    assert content == b"\x89PNG-fake-image"
    assert mime_type == "image/png"

    dyn_req = session.calls[-1]["json"]["dyn_req"]
    assert dyn_req["scene"] == 2
    assert dyn_req["pics"] == [
        {
            "img_src": "https://i0.hdslb.com/bfs/new_dyn/pixel.png",
            "img_width": 640,
            "img_height": 480,
            "img_size": 12.5,
        }
    ]


def test_publish_dynamic_image_only_has_empty_contents(tmp_path) -> None:
    image = tmp_path / "pixel.png"
    image.write_bytes(b"fake")
    upload_payload = {"code": 0, "data": {"image_url": "https://x/y.png"}}
    session = FakeSession([FakeResponse(upload_payload), FakeResponse(_CREATE_OK)])
    _client(session).publish_dynamic("", images=[image])
    dyn_req = session.calls[-1]["json"]["dyn_req"]
    assert dyn_req["content"]["contents"] == []
    assert dyn_req["scene"] == 2


def test_publish_dynamic_accepts_dyn_id_str() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {"dyn_id_str": "987654321"}})])
    assert _client(session).publish_dynamic("hi") == 987654321


def test_publish_dynamic_without_id_raises() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {}})])
    with pytest.raises(BilibiliApiError):
        _client(session).publish_dynamic("hi")


def test_publish_dynamic_auth_error_raises() -> None:
    session = FakeSession([FakeResponse({"code": -101, "message": "账号未登录"})])
    with pytest.raises(BilibiliAuthError):
        _client(session).publish_dynamic("hi")


def test_publish_dynamic_business_error_raises_api_error() -> None:
    session = FakeSession([FakeResponse({"code": 4126021, "message": "没有绑定手机"})])
    with pytest.raises(BilibiliApiError) as exc_info:
        _client(session).publish_dynamic("hi")
    assert exc_info.value.code == 4126021


def test_publish_dynamic_requires_content() -> None:
    client = _client(FakeSession())
    with pytest.raises(ValueError):
        client.publish_dynamic("   ")


def test_publish_dynamic_missing_image_file_raises(tmp_path) -> None:
    client = _client(FakeSession())
    with pytest.raises(BilibiliApiError):
        client.publish_dynamic("hi", images=[tmp_path / "missing.png"])


def test_publish_dynamic_retries_upload_5xx(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("neobot_app.bilibili.client.time.sleep", sleeps.append)
    payload = {"code": 0, "data": {"image_url": "https://x/y.png"}}
    session = FakeSession(
        [
            FakeResponse(None, status_code=500),
            FakeResponse(payload),
            FakeResponse(_CREATE_OK),
        ]
    )
    client = _client(session)
    from pathlib import Path

    image = Path(__file__).with_name("_fakes.py")  # 任意存在的文件即可
    assert client.publish_dynamic("hi", images=[image]) == 588320531406678918
    assert len(session.calls) == 3
