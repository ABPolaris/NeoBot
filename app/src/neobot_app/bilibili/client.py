"""B 站 HTTP 客户端：登录态校验、评论/私信读写、动态发布、视频点赞与直播弹幕。

所有方法都是同步实现（requests）。需要 async 的调用方用 asyncio.to_thread
包一层即可（BilibiliService 已经这样做）。

写操作（发评论/私信/动态）统一走 _rate_limit_wait() 限速；网络错误与 HTTP 5xx
会退避重试 2 次，B 站业务错误码不重试。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import threading
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from neobot_contracts.ports.logging import Logger, NullLogger

from .errors import BilibiliApiError, BilibiliAuthError, BilibiliError

API_BASE = "https://api.bilibili.com"
VC_BASE = "https://api.vc.bilibili.com"
_NAV_URL = f"{API_BASE}/x/web-interface/nav"
_REPLY_FEED_URL = f"{API_BASE}/x/msgfeed/reply"
_AT_FEED_URL = f"{API_BASE}/x/msgfeed/at"
_VIDEO_COMMENTS_URL = f"{API_BASE}/x/v2/reply/main"
_COMMENT_ADD_URL = f"{API_BASE}/x/v2/reply/add"
_DYNAMIC_CREATE_URL = f"{API_BASE}/x/dynamic/feed/create/dyn"
_DYNAMIC_IMAGE_UPLOAD_URL = f"{API_BASE}/x/dynamic/feed/draw/upload_bfs"
_SEARCH_URL = f"{API_BASE}/x/web-interface/wbi/search/type"
_VIDEO_VIEW_URL = f"{API_BASE}/x/web-interface/view"
_SPACE_INFO_URL = f"{API_BASE}/x/space/wbi/acc/info"
_SPACE_VIDEOS_URL = f"{API_BASE}/x/space/arc/search"
_PLAYER_INFO_URL = f"{API_BASE}/x/player/wbi/v2"
_PLAYURL_URL = f"{API_BASE}/x/player/wbi/playurl"
_VIDEO_LIKE_URL = f"{API_BASE}/x/web-interface/archive/like"
_COMMENT_ACTION_URL = f"{API_BASE}/x/v2/reply/action"

LIVE_API_BASE = "https://api.live.bilibili.com"
_LIVE_ROOM_INFO_URL = f"{LIVE_API_BASE}/xlive/web-room/v1/index/getInfoByRoom"
_LIVE_DANMU_INFO_URL = f"{LIVE_API_BASE}/xlive/web-room/v1/index/getDanmuInfo"
_LIVE_SEND_DANMAKU_URL = f"{LIVE_API_BASE}/msg/send"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_DEVICE_ID = "B1994F2C-C5C9-4C0E-8F4C-F8E5F7E8F9E0"

#: WBI 签名用的固定置换表（B 站 web 端一致）。
_WBI_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 52, 44, 34,
)
_WBI_FILTERED_CHARS = frozenset("!'()*")

#: 命中这些业务错误码时加大写操作冷却时间（风控/评论限流）。
_WRITE_RETRY_CODES = frozenset({-412, -799})

_BV_PATTERN = re.compile(r"(?i)bv1[0-9a-z]{9}")
_AV_PATTERN = re.compile(r"(?i)(?<![0-9a-z])av(\d+)")


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_video_id(identifier: str) -> tuple[str, int]:
    """解析 BV 号 / av 号 / 视频 URL，返回 (bvid, aid)；识别失败返回 ("", 0)。"""
    text = (identifier or "").strip()
    match = _BV_PATTERN.search(text)
    if match:
        return "BV" + match.group(0)[2:], 0
    match = _AV_PATTERN.search(text)
    if match:
        return "", int(match.group(1))
    return "", 0


@dataclass
class BilibiliMessage:
    """B 站私信消息。"""

    talker_id: int
    sender_uid: int
    msg_type: int
    content: str
    timestamp: int
    msg_key: str = ""
    is_self: bool = False

    @property
    def text(self) -> str:
        """纯文本消息的正文；图片等非文本消息返回空串。"""
        if self.msg_type != 1:
            return ""
        try:
            return str(json.loads(self.content).get("content", ""))
        except (ValueError, TypeError, AttributeError):
            return self.content

    @classmethod
    def from_api(cls, raw: Mapping[str, Any], my_uid: int = 0) -> BilibiliMessage:
        content = raw.get("content", "")
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        sender_uid = int(raw.get("sender_uid") or 0)
        talker_id = int(raw.get("receiver_id") or 0) or sender_uid
        message = cls(
            talker_id=talker_id,
            sender_uid=sender_uid,
            msg_type=int(raw.get("msg_type") or 1),
            content=str(content),
            timestamp=int(raw.get("timestamp") or 0),
        )
        message.is_self = bool(my_uid and sender_uid == my_uid)
        message.msg_key = f"{message.talker_id}_{message.timestamp}_{message.content[:20]}"
        return message


@dataclass
class BilibiliSession:
    """B 站私信会话。"""

    talker_id: int
    last_msg: BilibiliMessage | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> BilibiliSession:
        session = cls(talker_id=int(raw.get("talker_id") or 0))
        last_msg = raw.get("last_msg")
        if isinstance(last_msg, dict) and last_msg:
            session.last_msg = BilibiliMessage.from_api(last_msg)
        return session


class BilibiliClient:
    """B 站 Web API 同步客户端。"""

    _MAX_RETRIES = 2
    _RETRY_DELAY_SECONDS = 0.5
    _DEFAULT_WRITE_INTERVAL_SECONDS = 1.0
    _MAX_WRITE_INTERVAL_SECONDS = 60.0

    def __init__(
        self,
        sessdata: str,
        bili_jct: str,
        uid: int = 0,
        *,
        buvid3: str = "",
        logger: Logger | None = None,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        if not sessdata or not bili_jct:
            raise ValueError("SESSDATA 和 bili_jct 不能为空")
        self._sessdata = sessdata
        self._bili_jct = bili_jct
        self._my_uid = int(uid or 0)
        self._buvid3 = buvid3
        self._logger = logger or NullLogger()
        self._timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
            }
        )
        jar = self._session.cookies
        jar.set("SESSDATA", sessdata, domain=".bilibili.com", path="/")
        jar.set("bili_jct", bili_jct, domain=".bilibili.com", path="/")
        if self._my_uid:
            jar.set("DedeUserID", str(self._my_uid), domain=".bilibili.com", path="/")
        if buvid3:
            jar.set("buvid3", buvid3, domain=".bilibili.com", path="/")

        # 并发安全：任务模块可能从多个线程调用同一个客户端。
        self._lock = threading.Lock()
        # 限速：写操作之间的最小间隔，命中风控后自适应放大。
        self._write_interval = self._DEFAULT_WRITE_INTERVAL_SECONDS
        self._last_write_at = 0.0
        # WBI 签名密钥缓存。
        self._wbi_mixin_key = ""
        # 私信去重缓存。
        self._seen_msg_keys: set[str] = set()
        self._last_msg_times: dict[int, int] = defaultdict(int)

    # ── 认证 ──

    @property
    def my_uid(self) -> int:
        """自己的 UID；构造时未给则尝试从 nav 接口取一次。"""
        if self._my_uid > 0:
            return self._my_uid
        info = self.get_my_info()
        try:
            self._my_uid = int(info.get("mid") or 0)
        except (TypeError, ValueError):
            self._my_uid = 0
        return self._my_uid

    def get_my_info(self) -> dict[str, Any]:
        """nav 接口的 data 原样返回（mid/uname/face/isLogin/wbi_img 等）；失败返回空 dict。"""
        try:
            payload = self._fetch("GET", _NAV_URL)
        except BilibiliError as exc:
            self._log_read_error("账号信息读取", exc)
            return {}
        data = payload.get("data")
        if not isinstance(data, dict):
            return {}
        wbi_img = data.get("wbi_img")
        if isinstance(wbi_img, dict):
            self._cache_wbi_keys(wbi_img)
        try:
            mid = int(data.get("mid") or 0)
        except (TypeError, ValueError):
            mid = 0
        if mid > 0:
            self._my_uid = mid
        return data

    def verify_auth(self) -> bool:
        """当前 cookie 是否仍处于登录态。"""
        info = self.get_my_info()
        try:
            mid = int(info.get("mid") or 0)
        except (TypeError, ValueError):
            mid = 0
        return bool(info.get("isLogin") and mid > 0)

    # ── 评论 / 回复 ──

    def get_reply_feed(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取「回复我的」评论通知列表（msgfeed/reply 的 items 原样）。"""
        params = {"platform": "web", "build": 0, "mobi_app": "web"}
        try:
            payload = self._fetch("GET", _REPLY_FEED_URL, params=params)
        except BilibiliError as exc:
            self._log_read_error("评论回复读取", exc)
            return []
        data = payload.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        return [item for item in items or [] if isinstance(item, dict)][: max(int(limit), 0)]

    def get_at_feed(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取「@我的」通知列表（msgfeed/at 的 items 原样）。"""
        params = {"platform": "web", "build": 0, "mobi_app": "web"}
        try:
            payload = self._fetch(
                "GET",
                _AT_FEED_URL,
                params=params,
                # 消息中心接口要求带上它自己的页面上下文，否则可能被风控拒绝。
                headers={
                    "Referer": "https://message.bilibili.com/",
                    "Origin": "https://message.bilibili.com",
                },
            )
        except BilibiliError as exc:
            self._log_read_error("提到我的读取", exc)
            return []
        data = payload.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        return [item for item in items or [] if isinstance(item, dict)][: max(int(limit), 0)]

    def get_video_comments(
        self, oid: int, page: int = 1, *, order: str = "time"
    ) -> list[dict[str, Any]]:
        """获取稿件/动态的评论列表。oid 为稿件 aid；order 为 time 或 hot。"""
        params: dict[str, Any] = {
            "type": 1,
            "oid": int(oid),
            "mode": 2 if order == "time" else 3,
            "next": max(int(page) - 1, 0),
        }
        try:
            params = self._sign_wbi(params)
            payload = self._fetch("GET", _VIDEO_COMMENTS_URL, params=params)
        except BilibiliError as exc:
            self._log_read_error("视频评论读取", exc)
            return []
        data = payload.get("data")
        replies = data.get("replies") if isinstance(data, dict) else None
        return [item for item in replies or [] if isinstance(item, dict)]

    def send_comment_reply(
        self, oid: int, root: int, parent: int, text: str, *, type_: int = 1
    ) -> bool:
        """回复评论。oid=稿件/动态 ID，root=根评论 rpid，parent=被回复评论 rpid。"""
        message = (text or "").strip()
        if not oid or not message:
            self._logger.warning("B站评论回复参数不完整（oid/text 不能为空）")
            return False
        form: dict[str, Any] = {
            "oid": int(oid),
            "type": int(type_),
            "message": message,
            "plat": 1,
            "csrf": self._bili_jct,
        }
        if root:
            form["root"] = int(root)
        if parent:
            form["parent"] = int(parent)
        self._rate_limit_wait()
        try:
            payload = self._fetch(
                "POST",
                _COMMENT_ADD_URL,
                data=form,
                headers={"Referer": "https://www.bilibili.com/"},
            )
        except BilibiliError as exc:
            self._on_write_error("评论回复", exc)
            return False
        data = payload.get("data")
        rpid = data.get("rpid") if isinstance(data, dict) else 0
        self._logger.info(f"B站评论回复成功: oid={oid} root={root} parent={parent} rpid={rpid}")
        return True

    def post_video_comment(
        self, identifier: str, text: str, *, root: int = 0, parent: int = 0
    ) -> bool:
        """给视频发评论/回复：identifier 支持 BV/av/URL，root/parent 为 0 时发顶级评论。"""
        message = (text or "").strip()
        if not message:
            self._logger.warning("B站视频评论参数不完整（text 不能为空）")
            return False
        aid = self._resolve_video_aid(identifier)
        if not aid:
            self._logger.warning(f"B站视频评论失败：无法解析视频号 {identifier!r}")
            return False
        return self.send_comment_reply(aid, root, parent, message, type_=1)

    def like_comment(self, oid: int, rpid: int, type_: int = 1) -> bool:
        """点赞评论。oid=稿件/动态 ID，rpid=评论 ID，type_=评论区类型（视频为 1）。"""
        if not oid or not rpid:
            self._logger.warning("B站评论点赞参数不完整（oid/rpid 不能为空）")
            return False
        form: dict[str, Any] = {
            "oid": int(oid),
            "rpid": int(rpid),
            "action": 1,
            "type": int(type_),
            "csrf": self._bili_jct,
        }
        self._rate_limit_wait()
        try:
            self._fetch("POST", _COMMENT_ACTION_URL, data=form)
        except BilibiliError as exc:
            self._on_write_error("评论点赞", exc)
            return False
        self._logger.info(f"B站评论点赞成功: oid={oid} rpid={rpid}")
        return True

    # ── 搜索 / 视频 / 用户空间 ──

    def search(
        self,
        keyword: str,
        *,
        search_type: str = "video",
        page: int = 1,
        page_size: int = 20,
        order: str = "totalrank",
    ) -> dict[str, Any]:
        """综合搜索，返回 data 原样（视频搜索的结果在 data["result"]）；失败返回空 dict。"""
        text = (keyword or "").strip()
        if not text:
            return {}
        params = self._sign_wbi(
            {
                "search_type": search_type or "video",
                "keyword": text,
                "page": max(int(page), 1),
                "page_size": max(int(page_size), 1),
                "order": order or "totalrank",
            }
        )
        try:
            payload = self._fetch(
                "GET",
                _SEARCH_URL,
                params=params,
                headers={"Referer": "https://search.bilibili.com/"},
            )
        except BilibiliError as exc:
            self._log_read_error("视频搜索", exc)
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def get_video_info(self, identifier: str) -> dict[str, Any]:
        """视频信息（view 接口的 data）；identifier 支持 BV 号 / av 号 / 视频 URL。"""
        bvid, aid = parse_video_id(identifier)
        if not bvid and not aid:
            self._logger.warning(f"B站视频号解析失败: {identifier!r}")
            return {}
        params: dict[str, Any] = {"bvid": bvid} if bvid else {"aid": aid}
        try:
            payload = self._fetch("GET", _VIDEO_VIEW_URL, params=params)
        except BilibiliError as exc:
            self._log_read_error("视频信息读取", exc)
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def like_video(self, identifier: str) -> bool:
        """给视频点赞：identifier 支持 BV/av/URL（BV 会先查 aid）。"""
        aid = self._resolve_video_aid(identifier)
        if not aid:
            self._logger.warning(f"B站视频点赞失败：无法解析视频号 {identifier!r}")
            return False
        form: dict[str, Any] = {"aid": aid, "like": 1, "csrf": self._bili_jct}
        self._rate_limit_wait()
        try:
            self._fetch("POST", _VIDEO_LIKE_URL, data=form)
        except BilibiliError as exc:
            self._on_write_error("视频点赞", exc)
            return False
        self._logger.info(f"B站视频点赞成功: aid={aid}")
        return True

    # ── 直播 ──

    def get_live_room_info(self, room_id: int) -> dict[str, Any]:
        """直播间信息（getInfoByRoom 的 data：room_info/anchor_info 等）；失败返回空 dict。"""
        room_id = _to_int(room_id)
        if room_id <= 0:
            return {}
        try:
            payload = self._fetch(
                "GET", _LIVE_ROOM_INFO_URL, params={"room_id": room_id}
            )
        except BilibiliError as exc:
            self._log_read_error("直播间信息读取", exc)
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def get_live_danmu_info(self, room_id: int) -> dict[str, Any]:
        """弹幕服务器信息（getDanmuInfo 的 data：token/host_list）；失败返回空 dict。"""
        real_room_id = self._resolve_live_room_id(room_id)
        if real_room_id <= 0:
            return {}
        try:
            payload = self._fetch(
                "GET",
                _LIVE_DANMU_INFO_URL,
                params={"id": real_room_id, "type": 0},
                headers={"Referer": f"https://live.bilibili.com/{real_room_id}"},
            )
        except BilibiliError as exc:
            self._log_read_error("直播弹幕服务器信息读取", exc)
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def send_danmaku(self, room_id: int, text: str) -> bool:
        """发送直播弹幕；未登录/参数不完整/接口失败返回 False。"""
        room_id = _to_int(room_id)
        message = (text or "").strip()
        if room_id <= 0 or not message:
            self._logger.warning("B站直播弹幕参数不完整（room_id/text 不能为空）")
            return False
        if not self._sessdata or not self._bili_jct:
            self._logger.warning("B站直播弹幕发送失败：未登录")
            return False
        real_room_id = self._resolve_live_room_id(room_id)
        form: dict[str, Any] = {
            "roomid": real_room_id,
            "msg": message,
            "color": 16777215,
            "mode": 1,
            "fontsize": 25,
            "rnd": int(time.time()),
            "csrf": self._bili_jct,
            "csrf_token": self._bili_jct,
        }
        self._rate_limit_wait()
        try:
            self._fetch(
                "POST",
                _LIVE_SEND_DANMAKU_URL,
                data=form,
                headers={
                    "Referer": f"https://live.bilibili.com/{real_room_id}",
                    "Origin": "https://live.bilibili.com",
                },
            )
        except BilibiliError as exc:
            self._on_write_error("直播弹幕发送", exc)
            return False
        self._logger.info(f"B站直播弹幕已发送: room={real_room_id}")
        return True

    # ── 用户空间 ──

    def get_space_info(self, mid: int) -> dict[str, Any]:
        """用户空间信息（space/wbi/acc/info 的 data）；失败返回空 dict。"""
        if not mid:
            return {}
        params = self._sign_wbi({"mid": int(mid)})
        try:
            payload = self._fetch(
                "GET",
                _SPACE_INFO_URL,
                params=params,
                headers={"Referer": f"https://space.bilibili.com/{int(mid)}"},
            )
        except BilibiliError as exc:
            self._log_read_error("用户空间信息读取", exc)
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def get_user_videos(
        self, mid: int, *, page: int = 1, page_size: int = 20
    ) -> list[dict[str, Any]]:
        """用户投稿视频（space/arc/search 的 vlist）；失败返回空列表。"""
        if not mid:
            return []
        params = {
            "mid": int(mid),
            "ps": max(int(page_size), 1),
            "pn": max(int(page), 1),
            "order": "pubdate",
        }
        try:
            payload = self._fetch(
                "GET",
                _SPACE_VIDEOS_URL,
                params=params,
                headers={"Referer": f"https://space.bilibili.com/{int(mid)}"},
            )
        except BilibiliError as exc:
            self._log_read_error("用户投稿读取", exc)
            return []
        data = payload.get("data")
        listing = data.get("list") if isinstance(data, dict) else None
        vlist = listing.get("vlist") if isinstance(listing, dict) else None
        return [item for item in vlist or [] if isinstance(item, dict)]

    def get_player_info(self, bvid: str, cid: int) -> dict[str, Any]:
        """播放器信息（player/wbi/v2 的 data）；字幕在 data["subtitle"]["subtitles"]。"""
        bvid = (bvid or "").strip()
        if not bvid or not cid:
            return {}
        params = self._sign_wbi({"bvid": bvid, "cid": int(cid)})
        try:
            payload = self._fetch(
                "GET",
                _PLAYER_INFO_URL,
                params=params,
                headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
            )
        except BilibiliError as exc:
            self._log_read_error("播放器信息读取", exc)
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def get_playurl_audio(self, bvid: str, cid: int) -> str | None:
        """DASH 音频流地址：取 bandwidth 最大的一条；失败返回 None。"""
        bvid = (bvid or "").strip()
        if not bvid or not cid:
            return None
        params = self._sign_wbi(
            {"bvid": bvid, "cid": int(cid), "fnval": 16, "fourk": 1}
        )
        try:
            payload = self._fetch(
                "GET",
                _PLAYURL_URL,
                params=params,
                headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
            )
        except BilibiliError as exc:
            self._log_read_error("音频地址读取", exc)
            return None
        data = payload.get("data")
        dash = data.get("dash") if isinstance(data, dict) else None
        audios = dash.get("audio") if isinstance(dash, dict) else None
        best = max(
            (
                item
                for item in audios or []
                if isinstance(item, dict) and (item.get("base_url") or item.get("baseUrl"))
            ),
            key=lambda item: _to_int(item.get("bandwidth")),
            default=None,
        )
        if not best:
            return None
        return str(best.get("base_url") or best.get("baseUrl"))

    def fetch_text(self, url: str) -> str | None:
        """下载文本资源（字幕 JSON 等）；失败返回 None。"""
        if not url:
            return None
        response = self._request("GET", url)
        if response is None or response.status_code >= 400:
            self._logger.warning(f"B站文本下载失败: {url}")
            return None
        try:
            return response.text
        except (requests.RequestException, OSError, ValueError) as exc:
            self._logger.warning(f"B站文本下载失败: {url}（{exc}）")
            return None

    def download_file(self, url: str, path: Path) -> bool:
        """把 url 流式下载到 path；失败返回 False 并清理半成品。"""
        target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # shortcut: 下载期间不持锁（长任务会卡住其它请求），同一 Session 的并发流式读取
            # 未额外保护；需要更强并发保证时给下载单独开 Session。
            with self._lock:
                response = self._session.get(url, stream=True, timeout=self._timeout)
            try:
                if response.status_code >= 400:
                    self._logger.warning(f"B站文件下载失败: HTTP {response.status_code} {url}")
                    return False
                with target.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1 << 16):
                        if chunk:
                            handle.write(chunk)
            finally:
                response.close()
        except (requests.RequestException, OSError) as exc:
            self._logger.warning(f"B站文件下载失败: {url}（{exc}）")
            target.unlink(missing_ok=True)
            return False
        if not target.is_file() or target.stat().st_size <= 0:
            target.unlink(missing_ok=True)
            return False
        return True

    # ── 私信 ──

    def get_sessions(self, count: int = 30) -> list[BilibiliSession]:
        """获取私信会话列表（按最后消息时间倒序）。"""
        params = {
            "session_type": 1,
            "group_fold": 1,
            "unfollow_fold": 0,
            "sort_rule": 2,
            "build": 0,
            "mobi_app": "web",
        }
        try:
            payload = self._fetch("GET", f"{VC_BASE}/session_svr/v1/session_svr/get_sessions", params=params)
        except BilibiliError as exc:
            self._log_read_error("私信会话读取", exc)
            return []
        data = payload.get("data")
        raw_sessions = data.get("session_list") if isinstance(data, dict) else None
        sessions = [
            BilibiliSession.from_api(item)
            for item in raw_sessions or []
            if isinstance(item, dict)
        ]
        sessions.sort(
            key=lambda item: item.last_msg.timestamp if item.last_msg else 0,
            reverse=True,
        )
        return sessions[: max(int(count), 0)]

    def get_messages(self, talker_id: int, size: int = 5) -> list[BilibiliMessage]:
        """获取指定会话的最新消息。"""
        params = {
            "sender_device_id": 1,
            "talker_id": int(talker_id),
            "session_type": 1,
            "size": int(size),
            "build": 0,
            "mobi_app": "web",
        }
        try:
            payload = self._fetch("GET", f"{VC_BASE}/svr_sync/v1/svr_sync/fetch_session_msgs", params=params)
        except BilibiliError as exc:
            self._log_read_error("私信读取", exc)
            return []
        data = payload.get("data")
        raw_messages = data.get("messages") if isinstance(data, dict) else None
        return [
            BilibiliMessage.from_api(item, self._my_uid)
            for item in raw_messages or []
            if isinstance(item, dict)
        ]

    def get_new_messages(self, session: BilibiliSession) -> list[BilibiliMessage]:
        """获取会话里尚未见过、且不是自己发出的新消息。"""
        messages = self.get_messages(session.talker_id, size=3)
        new_messages: list[BilibiliMessage] = []
        for message in messages:
            if not message.msg_key or message.msg_key in self._seen_msg_keys:
                continue
            if self._my_uid and message.sender_uid == self._my_uid:
                continue
            if message.timestamp <= self._last_msg_times.get(session.talker_id, 0):
                continue
            self._seen_msg_keys.add(message.msg_key)
            new_messages.append(message)
        if new_messages:
            self._last_msg_times[session.talker_id] = max(item.timestamp for item in new_messages)
        return new_messages

    def send_message(self, receiver_id: int, text: str) -> bool:
        """发送文本私信。"""
        message = (text or "").strip()
        if not receiver_id or not message:
            self._logger.warning("B站私信参数不完整（receiver_id/text 不能为空）")
            return False
        sender_uid = self.my_uid
        if not sender_uid:
            self._logger.warning("B站私信发送失败：未取到自己的 UID")
            return False
        form: dict[str, Any] = {
            "msg[sender_uid]": sender_uid,
            "msg[receiver_id]": int(receiver_id),
            "msg[receiver_type]": 1,
            "msg[msg_type]": 1,
            "msg[msg_status]": 0,
            "msg[content]": json.dumps({"content": message}, ensure_ascii=False),
            "msg[timestamp]": int(time.time()),
            "msg[new_face_version]": 0,
            "msg[dev_id]": DEFAULT_DEVICE_ID,
            "build": 0,
            "mobi_app": "web",
            "csrf": self._bili_jct,
            "csrf_token": self._bili_jct,
        }
        self._rate_limit_wait()
        try:
            self._fetch(
                "POST",
                f"{VC_BASE}/web_im/v1/web_im/send_msg",
                data=form,
                headers={"Referer": "https://message.bilibili.com/"},
            )
        except BilibiliError as exc:
            self._on_write_error("私信发送", exc)
            return False
        self._logger.info(f"B站私信已发送: receiver={receiver_id}")
        return True

    # ── 动态 ──

    def upload_dynamic_image(self, image_path: str | Path) -> dict[str, Any]:
        """上传一张动态配图，返回 upload_bfs 的 data（含 image_url/image_width/image_height）。"""
        path = Path(image_path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise BilibiliApiError(-1, f"读取图片失败: {path}（{exc}）") from exc
        if not content:
            raise BilibiliApiError(-1, f"图片为空: {path}")
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        self._rate_limit_wait()
        payload = self._fetch(
            "POST",
            _DYNAMIC_IMAGE_UPLOAD_URL,
            data={"category": "daily", "biz": "new_dyn", "csrf": self._bili_jct},
            files={"file_up": (path.name, content, mime_type)},
            headers={"Referer": "https://t.bilibili.com/"},
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("image_url"):
            raise BilibiliApiError(-1, "图片上传成功但未返回 image_url")
        return data

    def publish_dynamic(
        self, text: str, images: Sequence[str | Path] | None = None
    ) -> int:
        """发布一条文本/图文动态，返回 dynamic_id；失败抛 BilibiliApiError。"""
        content = (text or "").strip()
        image_paths = [Path(item) for item in images or ()]
        if not content and not image_paths:
            raise ValueError("动态内容不能为空")
        pics: list[dict[str, Any]] = []
        for path in image_paths:
            info = self.upload_dynamic_image(path)
            pics.append(
                {
                    "img_src": str(info["image_url"]),
                    "img_width": int(info.get("image_width") or 0),
                    "img_height": int(info.get("image_height") or 0),
                    "img_size": info.get("img_size") or 0,
                }
            )
        dyn_req: dict[str, Any] = {
            "content": {
                "contents": (
                    [{"biz_id": "", "type": 1, "raw_text": content}] if content else []
                )
            },
            "scene": 2 if pics else 1,
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            "attach_card": None,
        }
        if pics:
            dyn_req["pics"] = pics
        self._rate_limit_wait()
        payload = self._fetch(
            "POST",
            _DYNAMIC_CREATE_URL,
            params={"csrf": self._bili_jct},
            json_body={"dyn_req": dyn_req},
            headers={"Referer": "https://t.bilibili.com/"},
        )
        data = payload.get("data")
        dynamic_id = 0
        if isinstance(data, dict):
            for key in ("dyn_id", "dyn_id_str", "dynamic_id"):
                value = data.get(key)
                if value in (None, ""):
                    continue
                try:
                    dynamic_id = int(value)
                except (TypeError, ValueError):
                    continue
                if dynamic_id:
                    break
        if not dynamic_id:
            raise BilibiliApiError(-1, "发布动态成功但未返回 dynamic_id")
        self._logger.info(f"B站动态已发布: dynamic_id={dynamic_id}")
        return dynamic_id

    # ── 生命周期 ──

    def close(self) -> None:
        """关闭底层 HTTP 会话。"""
        self._session.close()

    # ── WBI 签名 ──

    def _ensure_wbi_keys(self) -> None:
        if self._wbi_mixin_key:
            return
        self.get_my_info()

    def _cache_wbi_keys(self, wbi_img: Mapping[str, Any]) -> None:
        if self._wbi_mixin_key:
            return
        img_raw = str(wbi_img.get("img_key") or wbi_img.get("img_url") or "")
        sub_raw = str(wbi_img.get("sub_key") or wbi_img.get("sub_url") or "")
        img_key = img_raw.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_raw.rsplit("/", 1)[-1].split(".")[0]
        if len(img_key) < 32 or len(sub_key) < 32:
            self._logger.debug("B站 WBI 密钥提取失败，跳过签名")
            return
        combined = img_key + sub_key
        self._wbi_mixin_key = "".join(
            combined[index] for index in _WBI_MIXIN_KEY_ENC_TAB if index < len(combined)
        )[:32]

    def _sign_wbi(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """给查询参数加 wts + w_rid（拿不到密钥时原样返回）。"""
        self._ensure_wbi_keys()
        if not self._wbi_mixin_key:
            return dict(params)
        signed = {
            str(key): "".join(ch for ch in str(value) if ch not in _WBI_FILTERED_CHARS)
            for key, value in params.items()
        }
        signed["wts"] = str(int(time.time()))
        signed = dict(sorted(signed.items()))
        query = urlencode(signed)
        signed["w_rid"] = hashlib.md5(
            (query + self._wbi_mixin_key).encode()
        ).hexdigest()
        return signed

    # ── 内部：ID 解析 ──

    def _resolve_video_aid(self, identifier: str) -> int:
        """BV/av/URL -> aid；BV 需要查一次 view 接口，解析失败返回 0。"""
        bvid, aid = parse_video_id(identifier)
        if aid:
            return aid
        if not bvid:
            return 0
        return _to_int(self.get_video_info(bvid).get("aid"))

    def _resolve_live_room_id(self, room_id: int) -> int:
        """短号/房间号 -> 真实房间号；查不到时原样返回输入值。"""
        room_id = _to_int(room_id)
        if room_id <= 0:
            return 0
        room = self.get_live_room_info(room_id).get("room_info")
        real_room_id = _to_int(room.get("room_id")) if isinstance(room, dict) else 0
        return real_room_id or room_id

    # ── 内部：HTTP / 错误处理 ──

    def _fetch(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Any = None,
        files: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self._request(
            method,
            url,
            params=params,
            data=data,
            json_body=json_body,
            files=files,
            headers=headers,
        )
        if response is None:
            raise BilibiliApiError(-1, "B站网络请求失败")
        return self._decode(response)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Any = None,
        files: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> requests.Response | None:
        last_error: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                with self._lock:
                    response = self._session.request(
                        method,
                        url,
                        params=params,
                        data=data,
                        json=json_body,
                        files=files,
                        headers=headers,
                        timeout=self._timeout,
                    )
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code < 500:
                    return response
                last_error = BilibiliApiError(-1, f"B站服务异常 HTTP {response.status_code}")
            if attempt < self._MAX_RETRIES:
                time.sleep(self._RETRY_DELAY_SECONDS * (attempt + 1))
        self._logger.warning(f"B站请求失败: {method} {url}（{last_error}）")
        return None

    @staticmethod
    def _decode(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliApiError(-1, "B站响应不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise BilibiliApiError(-1, "B站响应不是 JSON 对象")
        code = int(payload.get("code") or 0)
        if code == 0:
            return payload
        message = str(payload.get("message") or payload.get("msg") or "")
        if code == -101:
            raise BilibiliAuthError(f"B站登录态失效: {message}")
        raise BilibiliApiError(code, message)

    def _rate_limit_wait(self) -> None:
        elapsed = time.monotonic() - self._last_write_at
        if elapsed < self._write_interval:
            time.sleep(self._write_interval - elapsed)
        self._last_write_at = time.monotonic()

    def _on_write_error(self, action: str, error: Exception) -> None:
        if isinstance(error, BilibiliApiError) and error.code in _WRITE_RETRY_CODES:
            self._write_interval = min(
                self._write_interval * 1.5, self._MAX_WRITE_INTERVAL_SECONDS
            )
            self._logger.warning(
                f"B站{action}被限流（{error.code}），写操作冷却提升到 {self._write_interval:.1f}s"
            )
            return
        if isinstance(error, BilibiliAuthError):
            self._logger.warning(f"B站{action}失败: {error}")
            return
        self._logger.warning(f"B站{action}失败: {error}")

    def _log_read_error(self, action: str, error: Exception) -> None:
        if isinstance(error, BilibiliAuthError):
            self._logger.warning(f"B站{action}失败: {error}")
        else:
            self._logger.debug(f"B站{action}失败: {error}")
