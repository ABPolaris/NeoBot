"""请求头基线。

对照参考：amagi packages/core/src/platforms/bilibili/config.ts:16-35。
差异（有意为之的改进，不是复刻）：
- amagi 对直播接口沿用 Referer: https://www.bilibili.com/；这里改成真实来源页面
  https://live.bilibili.com/{room_id} 并补上 Origin，更贴近浏览器真实请求。
- 保留 amagi 把 sec-ch-ua 由 UA 现算的做法（contracts/ua.ts:34-38）。
"""

from __future__ import annotations

import re

from .urls import live_room_page_url

#: contracts/ua.ts:11 的默认 UA
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

_CHROME_VERSION = re.compile("Chrome/([0-9]+)")


def sec_ch_ua(user_agent: str | None = None) -> str:
    """由 UA 现算 sec-ch-ua；取不到版本号时回落到 125（与 amagi 一致）。"""
    match = _CHROME_VERSION.search(user_agent or DEFAULT_UA)
    version = match.group(1) if match else "125"
    return f'"Not_A Brand";v="99", "Chromium";v="{version}", "Google Chrome";v="{version}"'


def live_room_headers(
    room_id: int | str | None = None,
    *,
    cookie: str = "",
    user_agent: str | None = None,
) -> dict[str, str]:
    """直播接口的请求头。

    room_id 为 None 时（例如按 uid 查询）以站点根路径作为 Referer。
    """
    ua = (user_agent or DEFAULT_UA).strip() or DEFAULT_UA
    if room_id is None:
        referer = "https://live.bilibili.com/"
    else:
        referer = live_room_page_url(room_id)

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": referer,
        "origin": "https://live.bilibili.com",
        "sec-ch-ua": sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": ua,
    }
    if cookie.strip():
        headers["cookie"] = cookie.strip()
    return headers

