"""二维码登录：状态机、cookie 提取、PNG 渲染与异步轮询。"""

from __future__ import annotations

import importlib.util

import pytest

from neobot_app.bilibili.errors import BilibiliApiError, BilibiliAuthError
from neobot_app.bilibili.qr_login import (
    QR_STATE_EXPIRED,
    QR_STATE_PENDING,
    QR_STATE_SCANNED,
    QR_STATE_SUCCESS,
    BilibiliQrLogin,
    QrCodeLogin,
    QrPollResult,
    make_qr_png,
)

from ._fakes import FakeResponse, FakeSession

_GENERATE_PAYLOAD = {
    "code": 0,
    "data": {
        "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=key-1",
        "qrcode_key": "key-1",
    },
}


def _login(session: FakeSession) -> BilibiliQrLogin:
    return BilibiliQrLogin(session=session)


def test_generate_parses_qrcode() -> None:
    session = FakeSession([FakeResponse(_GENERATE_PAYLOAD)])
    info = _login(session).generate()
    assert isinstance(info, QrCodeLogin)
    assert info.qrcode_key == "key-1"
    assert info.url.startswith("https://account.bilibili.com/")
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"].endswith("/x/passport-login/web/qrcode/generate")
    assert session.headers["User-Agent"]


def test_generate_without_required_fields_raises() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {}})])
    with pytest.raises(BilibiliApiError):
        _login(session).generate()


def test_make_qr_png_matches_dependency_state() -> None:
    png = make_qr_png("https://example.com")
    if importlib.util.find_spec("qrcode") is None:
        assert png is None
    else:
        assert png is not None
        assert png.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    ("code", "state"),
    [
        (86101, QR_STATE_PENDING),
        (86090, QR_STATE_SCANNED),
        (86038, QR_STATE_EXPIRED),
    ],
)
def test_poll_maps_bilibili_states(code: int, state: str) -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {"code": code, "message": state}})])
    result = _login(session).poll("key-1")
    assert result.state == state
    assert result.raw_code == code
    assert result.success is False
    assert session.calls[0]["params"] == {"qrcode_key": "key-1"}


def test_poll_success_extracts_cookies_from_redirect_url() -> None:
    cross_domain = (
        "https://passport.biligame.com/x/passport-login/web/crossDomain"
        "?DedeUserID=42&SESSDATA=sd%2Cvalue&bili_jct=jct-token"
    )
    response = FakeResponse(
        {"code": 0, "data": {"code": 0, "url": cross_domain, "refresh_token": "refresh-1"}}
    )
    spi = FakeResponse({"code": 0, "data": {"b_3": "buvid-from-spi"}})
    session = FakeSession([response, spi])
    result = _login(session).poll("key-1")
    assert result.success is True
    assert result.cookies == {
        "SESSDATA": "sd,value",
        "bili_jct": "jct-token",
        "DedeUserID": "42",
        "buvid3": "buvid-from-spi",
    }
    assert result.refresh_token == "refresh-1"
    assert session.calls[-1]["url"].endswith("/x/frontend/finger/spi")


def test_poll_success_collects_cookies_from_response_headers() -> None:
    response = FakeResponse(
        {"code": 0, "data": {"code": 0, "url": "", "refresh_token": "rt"}},
        cookies={
            "SESSDATA": "sd",
            "bili_jct": "jct",
            "DedeUserID": "7",
            "buvid3": "buvid-from-cookie",
        },
    )
    session = FakeSession([response])
    result = _login(session).poll("key-1")
    assert result.cookies["DedeUserID"] == "7"
    assert result.cookies["buvid3"] == "buvid-from-cookie"
    # 已有 buvid3，不应再请求 spi 接口。
    assert len(session.calls) == 1


def test_poll_success_without_required_cookies_raises() -> None:
    session = FakeSession(
        [
            FakeResponse({"code": 0, "data": {"code": 0, "url": "", "refresh_token": "rt"}}),
            FakeResponse({"code": 0, "data": {}}),
        ]
    )
    with pytest.raises(BilibiliApiError):
        _login(session).poll("key-1")


def test_poll_root_auth_error_raises_auth_error() -> None:
    session = FakeSession([FakeResponse({"code": -101, "message": "账号未登录"})])
    with pytest.raises(BilibiliAuthError):
        _login(session).poll("key-1")


def test_poll_unknown_state_raises_api_error() -> None:
    session = FakeSession([FakeResponse({"code": 0, "data": {"code": 12345, "message": "?"}})])
    with pytest.raises(BilibiliApiError):
        _login(session).poll("key-1")


async def test_async_wait_polls_until_success() -> None:
    login = _login(FakeSession())
    results = [
        QrPollResult(state=QR_STATE_PENDING, raw_code=86101),
        QrPollResult(state=QR_STATE_SCANNED, raw_code=86090),
        QrPollResult(
            state=QR_STATE_SUCCESS,
            raw_code=0,
            message="ok",
            cookies={"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "1"},
            refresh_token="rt",
        ),
    ]
    login.poll = lambda _key: results.pop(0)  # type: ignore[method-assign]
    seen: list[str] = []

    async def on_status(result: QrPollResult) -> None:
        seen.append(result.state)

    final = await login.async_wait("key-1", on_status=on_status, interval=0, timeout=5)
    assert final.success is True
    assert seen == [QR_STATE_PENDING, QR_STATE_SCANNED, QR_STATE_SUCCESS]
    assert results == []


async def test_async_wait_timeout_returns_expired() -> None:
    login = _login(FakeSession())
    login.poll = lambda _key: QrPollResult(state=QR_STATE_PENDING, raw_code=86101)  # type: ignore[method-assign]
    result = await login.async_wait("key-1", interval=0, timeout=0.02)
    assert result.state == QR_STATE_EXPIRED
    assert "超时" in result.message


async def test_login_emits_qr_and_status_callbacks() -> None:
    session = FakeSession(
        [
            FakeResponse(_GENERATE_PAYLOAD),
            FakeResponse(
                {"code": 0, "data": {"code": 0, "url": "", "refresh_token": "rt"}},
                cookies={
                    "SESSDATA": "s",
                    "bili_jct": "j",
                    "DedeUserID": "7",
                    "buvid3": "b3",
                },
            ),
        ]
    )
    login = _login(session)
    qr_codes: list[QrCodeLogin] = []
    statuses: list[str] = []

    def on_qr(info: QrCodeLogin) -> None:
        qr_codes.append(info)

    async def on_status(result: QrPollResult) -> None:
        statuses.append(result.state)

    result = await login.login(on_qr=on_qr, on_status=on_status, interval=0, timeout=5)
    assert result.success is True
    assert [info.qrcode_key for info in qr_codes] == ["key-1"]
    assert statuses == [QR_STATE_SUCCESS]

