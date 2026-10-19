"""异常类型。"""

from __future__ import annotations

from typing import Any


class BilibiliError(Exception):
    """本模块所有异常的基类。"""


class BilibiliTransportError(BilibiliError):
    """网络层失败（连接、超时、TLS 等），未拿到任何平台响应。"""


class BilibiliAPIError(BilibiliError):
    """拿到了平台响应但判定为失败。

    字段与 amagi 的 AmagiError 信封一一对应：
    kind / code / retryable / platform.code / platform.message / http.status
    """

    def __init__(
        self,
        *,
        kind: str,
        code: str,
        message: str = "",
        retryable: bool = False,
        platform_code: Any = None,
        platform_message: str = "",
        http_status: int | None = None,
        raw: Any = None,
    ) -> None:
        super().__init__(message or code)
        self.kind = kind
        self.code = code
        self.message = message
        self.retryable = retryable
        self.platform_code = platform_code
        self.platform_message = platform_message
        self.http_status = http_status
        self.raw = raw

    def __str__(self) -> str:
        parts = [f"{self.kind}/{self.code}"]
        if self.platform_code is not None:
            parts.append(f"platform.code={self.platform_code}")
        if self.http_status is not None:
            parts.append(f"http={self.http_status}")
        if self.message:
            parts.append(self.message)
        return " ".join(parts)
