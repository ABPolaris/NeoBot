"""UP 投稿更新追踪的预备代码（尚未实装轮询/通知）。

现状：agent 可以用 bilibili_user__user_videos 手动查一次投稿；本模块只提供
不依赖网络/运行时的纯数据与 diff 工具，供后续接入后台轮询时复用。

设计约束见 Reference/_research/bilibili-update-polling.md：
- 首次基线不通知；
- 以 bvid 去重，不按视频总数判断；
- 轮询间隔建议 10-30 分钟，失败指数退避；
- 关注状态需要持久化，不能只放内存。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

DEFAULT_POLL_INTERVAL_SECONDS = 1800
MAX_SEEN_VIDEOS = 500


@dataclass(frozen=True)
class UpVideo:
    """一条 UP 投稿的稳定快照。"""

    bvid: str
    title: str
    author: str
    pubdate: int
    play: int = 0

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}" if self.bvid else ""


def normalize_video(raw: Mapping[str, Any]) -> UpVideo | None:
    """把 space/arc/search 的 vlist 条目归一化；缺 bvid 的条目跳过。"""
    bvid = str(raw.get("bvid") or "").strip()
    if not bvid:
        return None
    try:
        pubdate = int(raw.get("pubdate") or raw.get("created") or 0)
    except (TypeError, ValueError):
        pubdate = 0
    try:
        play = int(raw.get("play") or 0)
    except (TypeError, ValueError):
        play = 0
    return UpVideo(
        bvid=bvid,
        title=str(raw.get("title") or "").strip(),
        author=str(raw.get("author") or "").strip(),
        pubdate=pubdate,
        play=play,
    )


def new_videos(
    videos: Iterable[UpVideo],
    seen_bvids: Iterable[str],
) -> list[UpVideo]:
    """按 bvid 过滤已知投稿；保持输入顺序（调用方通常按 pubdate 倒序）。"""
    seen = {str(bvid) for bvid in seen_bvids}
    result: list[UpVideo] = []
    emitted: set[str] = set()
    for video in videos:
        if not video.bvid or video.bvid in seen or video.bvid in emitted:
            continue
        emitted.add(video.bvid)
        result.append(video)
    return result


def merge_seen(
    existing: Iterable[str],
    discovered: Iterable[str],
    *,
    limit: int = MAX_SEEN_VIDEOS,
) -> list[str]:
    """把新发现的 bvid 并入已读集合，保留最近 limit 条（新在前）。"""
    merged: list[str] = []
    for bvid in list(discovered) + list(existing):
        text = str(bvid or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged[: max(1, int(limit))]


@dataclass
class UpSubscription:
    """本地关注状态；实际轮询器尚未接线。"""

    uid: int
    name: str = ""
    seen_bvids: list[str] = field(default_factory=list)
    notify_targets: list[str] = field(default_factory=list)
    last_checked_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": int(self.uid),
            "name": self.name,
            "seen_bvids": list(self.seen_bvids),
            "notify_targets": list(self.notify_targets),
            "last_checked_at": float(self.last_checked_at),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "UpSubscription | None":
        try:
            uid = int(raw.get("uid") or 0)
        except (TypeError, ValueError):
            return None
        if uid <= 0:
            return None
        seen = [str(item) for item in raw.get("seen_bvids") or [] if str(item).strip()]
        targets = [
            str(item) for item in raw.get("notify_targets") or [] if str(item).strip()
        ]
        return cls(
            uid=uid,
            name=str(raw.get("name") or ""),
            seen_bvids=seen[:MAX_SEEN_VIDEOS],
            notify_targets=targets,
            last_checked_at=float(raw.get("last_checked_at") or 0.0),
        )


def dumps_subscriptions(subscriptions: Iterable[UpSubscription]) -> str:
    """稳定 JSON，供未来状态文件使用。"""
    return json.dumps(
        {"version": 1, "items": [item.to_dict() for item in subscriptions]},
        ensure_ascii=False,
        sort_keys=True,
    )


def loads_subscriptions(text: str) -> list[UpSubscription]:
    """读取未来状态文件；损坏时返回空列表，不抛。"""
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    result: list[UpSubscription] = []
    for item in raw.get("items") or []:
        if isinstance(item, dict):
            subscription = UpSubscription.from_dict(item)
            if subscription is not None:
                result.append(subscription)
    return result
