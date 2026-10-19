"""B 站网页扫码登录：二维码生成、轮询状态机与 PNG 渲染。

用法::

    qr = BilibiliQrLogin()
    info = qr.generate()                 # QrCodeLogin(url, qrcode_key, png_bytes)
    result = qr.poll(info.qrcode_key)    # 单次轮询 QrPollResult
    result = await qr.login(on_qr=..., on_status=...)   # 完整异步流程

B 站 poll 接口 data.code 状态：
    0     = 登录成功（data.url 查询串里带 SESSDATA / bili_jct / DedeUserID）
    86101 = 未扫码
    86090 = 已扫码待确认
    86038 = 二维码已失效
"""

from __future__ import annotations

import asyncio
import inspect
import io
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from neobot_contracts.ports.logging import Logger, NullLogger

from .client import DEFAULT_USER_AGENT
from .errors import BilibiliApiError, BilibiliAuthError

_QRCODE_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_QRCODE_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_BUVID3_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

#: 登录成功后需要从响应里收集的 cookie。
_COOKIE_NAMES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3")

QR_STATE_PENDING = "pending"
QR_STATE_SCANNED = "scanned"
QR_STATE_SUCCESS = "success"
QR_STATE_EXPIRED = "expired"

_QR_POLL_STATES = {86101: QR_STATE_PENDING, 86090: QR_STATE_SCANNED, 86038: QR_STATE_EXPIRED}


@dataclass
class QrCodeLogin:
    """一次二维码登录的二维码信息。"""

    url: str
    qrcode_key: str
    #: 二维码 PNG 字节；未安装 qrcode 依赖时为 None（仍可用 url 展示给用户）。
    png_bytes: bytes | None = None


@dataclass
class QrPollResult:
    """一次扫码状态轮询的结果。"""

    #: pending / scanned / success / expired
    state: str
    raw_code: int = -1
    message: str = ""
    #: 成功时带 SESSDATA / bili_jct / DedeUserID / buvid3 等。
    cookies: dict[str, str] = field(default_factory=dict)
    refresh_token: str = ""

    @property
    def success(self) -> bool:
        return self.state == QR_STATE_SUCCESS


def make_qr_png(url: str, *, box_size: int = 10, border: int = 2) -> bytes | None:
    """把登录 URL 渲染成 PNG 二维码；未安装 qrcode 依赖时返回 None。"""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(box_size=box_size, border=border)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _notify(callback: Callable[[Any], Any] | None, value: Any) -> None:
    """调用状态回调，同时兼容同步函数与协程函数。"""
    if callback is None:
        return
    outcome = callback(value)
    if inspect.isawaitable(outcome):
        await outcome


class BilibiliQrLogin:
    """B 站网页扫码登录（同步底层 + 异步轮询包装）。"""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        logger: Logger | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._session = session if session is not None else requests.Session()
        self._session.headers.update(
            {"User-Agent": user_agent, "Referer": "https://www.bilibili.com/"}
        )
        self._timeout = timeout
        self._logger = logger or NullLogger()

    # ── 同步原子操作 ──

    def generate(self) -> QrCodeLogin:
        """申请一张登录二维码。"""
        payload = self._get_json(_QRCODE_GENERATE_URL)
        info = payload.get("data") or {}
        url = str(info.get("url") or "")
        qrcode_key = str(info.get("qrcode_key") or "")
        if not url or not qrcode_key:
            raise BilibiliApiError(-1, "二维码生成响应缺少 url/qrcode_key")
        png_bytes = make_qr_png(url)
        if png_bytes is None:
            self._logger.warning("qrcode 依赖未安装，无法生成二维码 PNG（可改用 url 展示）")
        self._logger.info("B站登录二维码已生成，等待扫码")
        return QrCodeLogin(url=url, qrcode_key=qrcode_key, png_bytes=png_bytes)

    def poll(self, qrcode_key: str) -> QrPollResult:
        """轮询一次扫码状态。"""
        response = self._request(_QRCODE_POLL_URL, params={"qrcode_key": qrcode_key})
        payload = self._decode(response)
        data = payload.get("data") or {}
        code = int(data.get("code", -1))
        message = str(data.get("message") or "")
        if code == 0:
            cookies = self._extract_cookies(data, response)
            self._logger.info("B站扫码登录成功")
            return QrPollResult(
                state=QR_STATE_SUCCESS,
                raw_code=code,
                message=message or "登录成功",
                cookies=cookies,
                refresh_token=str(data.get("refresh_token") or ""),
            )
        state = _QR_POLL_STATES.get(code)
        if state is None:
            raise BilibiliApiError(code, message or "未知的扫码状态")
        self._logger.debug(f"B站扫码状态: {state}（code={code}）")
        return QrPollResult(state=state, raw_code=code, message=message)

    # ── 异步流程 ──

    async def async_wait(
        self,
        qrcode_key: str,
        *,
        on_status: Callable[[QrPollResult], Any] | None = None,
        interval: float = 2.0,
        timeout: float = 180.0,
    ) -> QrPollResult:
        """轮询到终态（success/expired）；超时按 expired 处理。"""
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            result = await asyncio.to_thread(self.poll, qrcode_key)
            await _notify(on_status, result)
            if result.state in (QR_STATE_SUCCESS, QR_STATE_EXPIRED):
                return result
            if time.monotonic() >= deadline:
                timeout_result = QrPollResult(
                    state=QR_STATE_EXPIRED, raw_code=-1, message="二维码登录超时"
                )
                await _notify(on_status, timeout_result)
                return timeout_result
            await asyncio.sleep(max(float(interval), 0.0))

    async def login(
        self,
        *,
        on_qr: Callable[[QrCodeLogin], Any] | None = None,
        on_status: Callable[[QrPollResult], Any] | None = None,
        interval: float = 2.0,
        timeout: float = 180.0,
    ) -> QrPollResult:
        """生成二维码并轮询到终态；on_qr 先拿到二维码，on_status 拿到每次轮询。"""
        info = await asyncio.to_thread(self.generate)
        await _notify(on_qr, info)
        return await self.async_wait(
            info.qrcode_key, on_status=on_status, interval=interval, timeout=timeout
        )

    # ── 内部 HTTP / 解析 ──

    def _request(self, url: str, *, params: dict[str, Any] | None = None) -> requests.Response:
        try:
            response = self._session.get(url, params=params, timeout=self._timeout)
        except requests.RequestException as exc:
            raise BilibiliApiError(-1, f"B站请求失败: {exc}") from exc
        if response.status_code >= 500:
            raise BilibiliApiError(-1, f"B站服务异常 HTTP {response.status_code}")
        return response

    @staticmethod
    def _decode(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliApiError(-1, "B站响应不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise BilibiliApiError(-1, "B站响应不是 JSON 对象")
        code = int(payload.get("code") or 0)
        if code != 0:
            message = str(payload.get("message") or payload.get("msg") or "")
            if code == -101:
                raise BilibiliAuthError(f"B站登录态失效: {message}")
            raise BilibiliApiError(code, message)
        return payload

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._decode(self._request(url, params=params))

    def _extract_cookies(
        self,
        data: dict[str, Any],
        response: requests.Response | None = None,
    ) -> dict[str, str]:
        """从 data.url 查询串 / Set-Cookie 中收集登录 cookie，缺 buvid3 时补一次。"""
        cookies: dict[str, str] = {}
        query = parse_qs(urlparse(str(data.get("url") or "")).query)
        for name in _COOKIE_NAMES:
            values = query.get(name)
            if values and values[0]:
                cookies[name] = values[0]
        for name in _COOKIE_NAMES:
            if cookies.get(name):
                continue
            value = ""
            if response is not None:
                value = str(response.cookies.get(name) or "")
            if not value:
                value = str(self._session.cookies.get(name) or "")
            if value:
                cookies[name] = value
        if not cookies.get("buvid3"):
            buvid3 = self._fetch_buvid3()
            if buvid3:
                cookies["buvid3"] = buvid3
        missing = [name for name in ("SESSDATA", "bili_jct", "DedeUserID") if not cookies.get(name)]
        if missing:
            raise BilibiliApiError(-1, "登录成功但未取到 cookie: " + ", ".join(missing))
        return cookies

    def _fetch_buvid3(self) -> str:
        """游客设备标识接口；失败返回空串（不阻断登录）。"""
        try:
            response = self._session.get(_BUVID3_SPI_URL, timeout=self._timeout)
            payload = response.json()
        except (requests.RequestException, ValueError):
            return ""
        if not isinstance(payload, dict) or int(payload.get("code") or 0) != 0:
            return ""
        return str((payload.get("data") or {}).get("b_3") or "")
