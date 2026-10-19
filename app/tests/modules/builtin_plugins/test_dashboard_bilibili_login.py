"""面板 B站扫码登录：独立页、会话/CSRF 鉴权与二维码状态流转。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from test_dashboard_api import _login, _start_panel


class _FakeBiliService:
    """替代 BilibiliService：把 on_qr/on_status 调到终态，便于断言状态机。"""

    def __init__(self) -> None:
        self.available = False
        self.login_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def status(self) -> dict:
        return {
            "available": self.available,
            "uid": 7 if self.available else 0,
            "uname": "测试UP" if self.available else "",
        }

    async def login_with_qr(self, *, on_qr=None, on_status=None) -> bool:
        self.login_calls += 1
        if on_qr is not None:
            on_qr(
                SimpleNamespace(
                    url="https://example.test/qr",
                    qrcode_key="key",
                    png_bytes=b"png-bytes",
                )
            )
        if on_status is not None:
            on_status(SimpleNamespace(state="scanned", message="已扫码，请确认"))
        self.started.set()
        await self.release.wait()
        if on_status is not None:
            on_status(SimpleNamespace(state="success", message="登录成功"))
        self.available = True
        return True


class _Services:
    def __init__(self, **items: object) -> None:
        self._items = items

    def get(self, name: str, default=None):
        return self._items.get(name, default)

    def describe(self) -> list:
        return []


@pytest.fixture()
async def bili_panel(tmp_path: Path):
    service = _FakeBiliService()
    server, _, base, _ = await _start_panel(tmp_path, services=_Services(bilibili_service=service))
    try:
        yield server, base, service
    finally:
        await server.stop()


async def test_login_page_is_public_and_lightweight(bili_panel) -> None:
    _, base, _ = bili_panel
    async with httpx.AsyncClient() as client:
        page = await client.get(base + "/bilibili/login")
        script = await client.get(base + "/bilibili/login.js")
        unauthenticated = await client.get(base + "/api/bilibili/login/status")

    assert page.status_code == 200
    assert "B站扫码登录" in page.text
    assert 'id="qr"' in page.text
    assert 'id="refresh"' in page.text
    assert script.status_code == 200
    assert "/api/bilibili/login/" in script.text
    # 二维码接口仍然要求面板会话，页面本身不含账号数据。
    assert unauthenticated.status_code == 401


async def test_qr_flow_requires_csrf_and_reaches_success(bili_panel) -> None:
    server, base, service = bili_panel
    token, csrf = await _login(base)
    auth = {"X-Token": token}
    async with httpx.AsyncClient() as client:
        idle = await client.get(base + "/api/bilibili/login/status", headers=auth)
        no_csrf = await client.post(base + "/api/bilibili/login/start", headers=auth)
        started = await client.post(
            base + "/api/bilibili/login/start",
            headers={**auth, "X-CSRF-Token": csrf},
        )

        assert idle.status_code == 200
        assert idle.json()["state"] == "idle"
        assert no_csrf.status_code == 403
        assert started.status_code == 200
        assert started.json()["running"] is True

        await asyncio.wait_for(service.started.wait(), timeout=5)
        scanned = await client.get(base + "/api/bilibili/login/status", headers=auth)
        assert scanned.json()["state"] == "scanned"
        assert scanned.json()["qr_png"].startswith("data:image/png;base64,")

        service.release.set()
        assert server.bilibili_login._task is not None
        await asyncio.wait_for(server.bilibili_login._task, timeout=5)
        done = await client.get(base + "/api/bilibili/login/status", headers=auth)

    assert done.json()["logged_in"] is True
    assert done.json()["state"] == "success"
    assert done.json()["uname"] == "测试UP"
    assert service.login_calls == 1


async def test_remote_access_can_be_disabled(tmp_path: Path) -> None:
    server, _, base, _ = await _start_panel(tmp_path, trust_proxy=True)
    server._allow_remote_manage = False
    token, _ = await _login(base)
    headers = {"X-Token": token, "X-Forwarded-For": "203.0.113.9"}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(base + "/api/bilibili/login/status", headers=headers)
        assert response.status_code == 403
        assert "本机" in response.json()["error"]
    finally:
        await server.stop()
