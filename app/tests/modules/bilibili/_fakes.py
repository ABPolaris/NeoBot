"""B 站模块测试替身：最小 requests.Session 与 Client。"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

from requests.cookies import RequestsCookieJar


class FakeResponse:
    """requests.Response 的最小替身。"""

    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        cookies: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.cookies = RequestsCookieJar()
        for name, value in (cookies or {}).items():
            self.cookies.set(name, value, domain=".bilibili.com")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeSession:
    """requests.Session 的最小替身：request() 按队列返回响应并记录调用。"""

    def __init__(self, responses: Iterable[FakeResponse] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.cookies = RequestsCookieJar()
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self._responses: deque[FakeResponse] = deque(responses or ())

    def queue(self, *responses: FakeResponse) -> None:
        self._responses.extend(responses)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"未排队的请求: {method} {url}")
        return self._responses.popleft()

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self.closed = True


class FakeClient:
    """BilibiliClient 的最小替身（只覆盖 BilibiliService 用到的接口）。"""

    def __init__(
        self,
        *args: Any,
        info: Mapping[str, Any] | None = None,
        dynamic_id: int = 99,
        **kwargs: Any,
    ) -> None:
        self.info = dict(info or {})
        self.dynamic_id = dynamic_id
        self.closed = False
        self.published: list[tuple[str, Any]] = []

    def get_my_info(self) -> dict[str, Any]:
        return dict(self.info)

    def verify_auth(self) -> bool:
        return bool(self.info.get("isLogin") and self.info.get("mid"))

    def publish_dynamic(self, text: str, images: Any = None) -> int:
        self.published.append((text, images))
        return self.dynamic_id

    def close(self) -> None:
        self.closed = True
