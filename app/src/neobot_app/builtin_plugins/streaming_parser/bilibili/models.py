"""直播接口的响应模型与归一化。

对照参考：amagi packages/response-types/src/generated/bilibili/ 下的
LiveRoomInit_V0 / LiveRoomInfo_V0 / UserLiveStatus_V0。

注意两个容易踩的坑（见 docs/roadmap.md）：
- room_init 的 live_time 是数字时间戳，get_info 的是字符串 "YYYY-MM-DD HH:mm:ss"；
- get_info 没有独立的 cover 字段，封面是 user_cover / keyframe。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

#: B 站服务器时间基准（东八区）；不用 zoneinfo 以免在无 tzdata 的 Windows 上失败
CST = timezone(timedelta(hours=8))

_LIVE_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M")

#: 秒级时间戳的合理区间（1970-01-01 ~ 2100-01-01）。
#: 真实流量实测：room_init 对「没有开播时间」的直播间返回 -62170012800，
#: 这是 .NET DateTime.MinValue 的秒数（0001-01-01），Windows 上直接喂给
#: datetime.fromtimestamp 会抛 OSError [Errno 22]。参考项目没有记录这个值。
_LIVE_TIME_MIN = 0
_LIVE_TIME_MAX = 4102444800


def normalize_live_time(value: Any) -> datetime | None:
    """把两种形态的 live_time 统一成带 +08:00 的 datetime。

    0 / 空串 / "0000-00-00 00:00:00" 都表示「没有开播时间」，返回 None。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= _LIVE_TIME_MIN or value > _LIVE_TIME_MAX:
            # 0 / 负数哨兵（如 -62170012800）/ 明显越界的值都表示「无开播时间」
            return None
        return datetime.fromtimestamp(float(value), tz=CST)
    if isinstance(value, str):
        text = value.strip()
        if not text or text.startswith("0000-00-00"):
            return None
        if text.lstrip("-").isdigit():
            # 有的端点会把时间戳序列化成字符串，宽容处理
            return normalize_live_time(int(text))
        for fmt in _LIVE_TIME_FORMATS:
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=CST)
            except ValueError:
                continue
        raise ValueError(f"无法解析的 live_time: {value!r}")
    raise TypeError(f"live_time 类型不支持: {type(value).__name__}")


def _data_of(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("响应不是 dict")
    data = payload.get("data")
    if data is None:
        raise ValueError("响应缺少 data 字段")
    if not isinstance(data, dict):
        raise TypeError("响应 data 不是 dict")
    return data


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LiveRoomInit:
    """room/v1/Room/room_init 的结果。"""

    room_id: int
    short_id: int
    uid: int
    live_status: int
    live_time: datetime | None
    is_hidden: bool = False
    is_locked: bool = False
    is_portrait: bool = False
    encrypted: bool = False
    room_shield: int = 0
    special_type: int = 0

    @classmethod
    def from_payload(cls, payload: Any) -> LiveRoomInit:
        data = _data_of(payload)
        return cls(
            room_id=_int(data.get("room_id")),
            short_id=_int(data.get("short_id")),
            uid=_int(data.get("uid")),
            live_status=_int(data.get("live_status")),
            live_time=normalize_live_time(data.get("live_time")),
            is_hidden=bool(data.get("is_hidden", False)),
            is_locked=bool(data.get("is_locked", False)),
            is_portrait=bool(data.get("is_portrait", False)),
            encrypted=bool(data.get("encrypted", False)),
            room_shield=_int(data.get("room_shield")),
            special_type=_int(data.get("special_type")),
        )

    @property
    def is_living(self) -> bool:
        """live_status: 0 未开播 / 1 直播中 / 2 轮播。"""
        return self.live_status == 1

    @property
    def has_short_id(self) -> bool:
        """用真实房间号查询时 short_id 为 0，表示该直播间没有短号。"""
        return self.short_id > 0


@dataclass(frozen=True)
class LiveRoomInfo:
    """room/v1/Room/get_info 的结果。"""

    room_id: int
    short_id: int
    uid: int
    title: str
    live_status: int
    live_time: datetime | None
    online: int = 0
    attention: int = 0
    area_id: int = 0
    area_name: str = ""
    parent_area_id: int = 0
    parent_area_name: str = ""
    description: str = ""
    user_cover: str = ""
    keyframe: str = ""
    tags: str = ""

    @classmethod
    def from_payload(cls, payload: Any) -> LiveRoomInfo:
        data = _data_of(payload)
        return cls(
            room_id=_int(data.get("room_id")),
            short_id=_int(data.get("short_id")),
            uid=_int(data.get("uid")),
            title=str(data.get("title") or ""),
            live_status=_int(data.get("live_status")),
            live_time=normalize_live_time(data.get("live_time")),
            online=_int(data.get("online")),
            attention=_int(data.get("attention")),
            area_id=_int(data.get("area_id")),
            area_name=str(data.get("area_name") or ""),
            parent_area_id=_int(data.get("parent_area_id")),
            parent_area_name=str(data.get("parent_area_name") or ""),
            description=str(data.get("description") or ""),
            user_cover=str(data.get("user_cover") or ""),
            keyframe=str(data.get("keyframe") or ""),
            tags=str(data.get("tags") or ""),
        )

    @property
    def is_living(self) -> bool:
        return self.live_status == 1

    @property
    def cover(self) -> str:
        """优先用户自定义封面，回落到关键帧截图（get_info 没有 cover 字段）。"""
        return self.user_cover or self.keyframe


@dataclass(frozen=True)
class UserCard:
    """web-interface/card 的结果：主播昵称、头像与统计。

    卡片渲染需要 face（头像）与 name（昵称）。
    """

    mid: int
    name: str
    face: str = ""
    sign: str = ""
    fans: int = 0
    level: int = 0
    official_type: int = 0
    official_desc: str = ""

    @classmethod
    def from_payload(cls, payload: Any) -> UserCard:
        data = _data_of(payload)
        card = data.get("card") or {}
        if not isinstance(card, dict):
            raise TypeError("user card 的 card 字段不是 dict")
        level_info = card.get("level_info") or {}
        official = card.get("official_verify") or {}
        return cls(
            mid=_int(card.get("mid")),
            name=str(card.get("name") or ""),
            face=str(card.get("face") or ""),
            sign=str(card.get("sign") or ""),
            fans=_int(card.get("fans") or data.get("follower")),
            level=_int(level_info.get("current_level")),
            official_type=_int(official.get("type")),
            official_desc=str(official.get("desc") or ""),
        )


@dataclass(frozen=True)
class UserLiveStatus:
    """room/v1/Room/getRoomInfoOld 的结果（uid → 直播状态）。"""

    live_status: int
    room_status: int
    room_id: int
    title: str = ""
    cover: str = ""
    online: int = 0
    url: str = ""
    round_status: int = 0
    broadcast_type: int = 0

    @classmethod
    def from_payload(cls, payload: Any) -> UserLiveStatus:
        data = _data_of(payload)
        return cls(
            live_status=_int(data.get("liveStatus")),
            room_status=_int(data.get("roomStatus")),
            room_id=_int(data.get("roomid")),
            title=str(data.get("title") or ""),
            cover=str(data.get("cover") or ""),
            online=_int(data.get("online")),
            url=str(data.get("url") or ""),
            round_status=_int(data.get("roundStatus")),
            broadcast_type=_int(data.get("broadcast_type")),
        )

    @property
    def is_living(self) -> bool:
        return self.live_status == 1

    @property
    def has_room(self) -> bool:
        """roomStatus: 1 已开通直播间 / 0 未开通。"""
        return self.room_status == 1 and self.room_id > 0

