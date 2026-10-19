"""WBI 签名（P1 能力，直播接口不需要，但它是 B 站协议里最容易写错的一环）。

对照参考：amagi packages/core/src/platforms/bilibili/sign/wbi.ts:35-159。
精确步骤：
  1. 请求 /x/web-interface/nav，取 data.wbi_img.img_url / sub_url 的文件名主干；
  2. mixin_key = (img_key + sub_key) 按 mixinKeyEncTab 取 64 个字符后截前 32；
  3. 参数加 wts（秒级时间戳），按 key 排序；
  4. 每个 value 过滤 !'()* 四个字符后 percent-encode，用 & 拼接；
  5. w_rid = md5(query + mixin_key)；
  6. 以字符串形式把 "&wts=..&w_rid=.." 追加到 URL 末尾。

验证向量见 tests/test_wbi.py（该向量已用独立实现交叉验证过 md5）。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlsplit

#: sign/wbi.ts:35-38 的 64 项表（原样照抄）
MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
)

#: wbi.ts:19：密钥缓存 30 分钟
WBI_TTL_SECONDS = 30 * 60

#: wbi.ts:70：只过滤 value 里的这五个字符
_CHR_FILTER = re.compile("[!'()*]")

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


def wts_now() -> int:
    """秒级 Unix 时间戳（amagi 用 Math.round(Date.now() / 1000)）。"""
    return int(time.time())


def extract_key(url: str) -> str:
    """从 wbi_img 的 URL 里取文件名主干。"""
    name = url.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """按表重排 img_key + sub_key 并截取前 32 位。"""
    origin = img_key + sub_key
    return "".join(origin[index] for index in MIXIN_KEY_ENC_TAB)[:32]


def _encode_value(value: Any) -> str:
    """过滤 + percent-encode。

    过滤 !'()* 之后，JS 的 encodeURIComponent 与 Python 的 quote(safe="") 转义集一致
    （两者都不转义 A-Za-z0-9_.-~）。
    """
    return quote(_CHR_FILTER.sub("", str(value)), safe="")


def build_query(params: dict[str, Any], *, wts: int | None = None) -> str:
    """构造参与签名的 query 串（含 wts，不含 w_rid）。"""
    merged: dict[str, Any] = dict(params)
    merged["wts"] = wts_now() if wts is None else wts
    return "&".join(
        f"{_encode_value(key)}={_encode_value(merged[key])}" for key in sorted(merged, key=str)
    )


def encode_wbi(params: dict[str, Any], img_key: str, sub_key: str, *, wts: int | None = None) -> str:
    """返回可直接追加到 URL 末尾的 "&wts=..&w_rid=.."。"""
    stamp = wts_now() if wts is None else wts
    mixin_key = get_mixin_key(img_key, sub_key)
    query = build_query(params, wts=stamp)
    w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return f"&wts={stamp}&w_rid={w_rid}"


def query_params_of(url: str) -> dict[str, str]:
    """取出 URL 已有的 query（签名要覆盖它们，wbi.ts:149-159）。"""
    from urllib.parse import parse_qsl

    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


NavFetcher = Callable[[], Awaitable[dict[str, Any]]]


class WbiSigner:
    """带 TTL 缓存的 WBI 签名器（对照 wbi.ts:113-128 的实例级缓存）。"""

    def __init__(self, *, ttl_seconds: float = WBI_TTL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._expires_at: float | None = None
        self._keys: tuple[str, str] | None = None

    @property
    def cached(self) -> bool:
        return self._keys is not None

    def invalidate(self) -> None:
        self._keys = None
        self._expires_at = None

    async def keys(self, fetch_nav: NavFetcher) -> tuple[str, str]:
        now = self._clock()
        if self._keys is not None and self._expires_at is not None and now < self._expires_at:
            return self._keys
        body = await fetch_nav()
        data = (body or {}).get("data") or {}
        wbi_img = data.get("wbi_img") or {}
        img_url = wbi_img.get("img_url")
        sub_url = wbi_img.get("sub_url")
        if not img_url or not sub_url:
            raise ValueError("wbi 密钥获取失败：/nav 响应缺少 wbi_img")
        self._keys = (extract_key(str(img_url)), extract_key(str(sub_url)))
        self._expires_at = now + self._ttl
        return self._keys

    async def sign(self, url: str, fetch_nav: NavFetcher, *, wts: int | None = None) -> str:
        img_key, sub_key = await self.keys(fetch_nav)
        return url + encode_wbi(query_params_of(url), img_key, sub_key, wts=wts)

