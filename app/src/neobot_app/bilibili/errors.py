"""B 站模块异常类型。"""

from __future__ import annotations


class BilibiliError(Exception):
    """B 站模块错误基类。"""


class BilibiliApiError(BilibiliError):
    """B 站接口返回了非 0 的业务错误码。"""

    def __init__(self, code: int, message: str = "") -> None:
        self.code = int(code)
        self.message = message or ""
        detail = f"B站 API 错误 {self.code}"
        if self.message:
            detail += f": {self.message}"
        super().__init__(detail)


class BilibiliAuthError(BilibiliError):
    """未登录、登录态失效或功能休眠。"""
