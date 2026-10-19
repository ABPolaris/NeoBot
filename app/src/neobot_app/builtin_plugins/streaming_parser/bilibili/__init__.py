"""B 站协议原型（纯 Python，仅依赖标准库）。

这是 `research/` 里的技术验证代码，用于把 amagi 的 TypeScript 协议逐字翻译成
Python 并用测试固定下来；成熟后再搬进插件正式代码。
"""

from __future__ import annotations

from .client import BilibiliLiveClient, HttpxTransport, RawResponse, Transport
from .errors import BilibiliAPIError, BilibiliError, BilibiliTransportError
from .headers import DEFAULT_UA, live_room_headers, sec_ch_ua
from .judge import (
    ANTIBOT_PAGE,
    COOKIE_EXPIRED,
    NOT_FOUND,
    PLATFORM_ERROR,
    RATE_LIMITED,
    RISK_CONTROL,
    Verdict,
    judge,
)
from .models import (
    LiveRoomInfo,
    LiveRoomInit,
    UserLiveStatus,
    normalize_live_time,
)
from .urls import (
    LIVE_API_BASE,
    LIVE_WEB_BASE,
    live_room_info_url,
    live_room_init_url,
    live_room_page_url,
    normalize_room_id,
    user_live_status_url,
)
from .wbi import MIXIN_KEY_ENC_TAB, WbiSigner, encode_wbi, extract_key, get_mixin_key

__all__ = [
    "ANTIBOT_PAGE",
    "COOKIE_EXPIRED",
    "DEFAULT_UA",
    "LIVE_API_BASE",
    "LIVE_WEB_BASE",
    "MIXIN_KEY_ENC_TAB",
    "NOT_FOUND",
    "PLATFORM_ERROR",
    "RATE_LIMITED",
    "RISK_CONTROL",
    "BilibiliAPIError",
    "BilibiliError",
    "BilibiliLiveClient",
    "BilibiliTransportError",
    "HttpxTransport",
    "LiveRoomInfo",
    "LiveRoomInit",
    "RawResponse",
    "Transport",
    "UserLiveStatus",
    "Verdict",
    "WbiSigner",
    "encode_wbi",
    "extract_key",
    "get_mixin_key",
    "judge",
    "live_room_headers",
    "live_room_info_url",
    "live_room_init_url",
    "live_room_page_url",
    "normalize_live_time",
    "normalize_room_id",
    "sec_ch_ua",
    "user_live_status_url",
]
