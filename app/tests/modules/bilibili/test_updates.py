"""UP 投稿更新预备代码：纯函数与状态序列化。"""

from __future__ import annotations

from neobot_app.bilibili.updates import (
    UpSubscription,
    UpVideo,
    dumps_subscriptions,
    loads_subscriptions,
    merge_seen,
    new_videos,
    normalize_video,
)


def test_normalize_video_requires_bvid_and_normalizes_numbers() -> None:
    video = normalize_video(
        {"bvid": "BV1xx", "title": "标题", "author": "UP", "pubdate": "123", "play": "45"}
    )
    assert video == UpVideo("BV1xx", "标题", "UP", 123, 45)
    assert video.url.endswith("/BV1xx")
    assert normalize_video({"title": "没有BV"}) is None


def test_new_videos_filters_seen_and_dedupes() -> None:
    videos = [
        UpVideo("BV2", "新", "UP", 2),
        UpVideo("BV1", "旧", "UP", 1),
        UpVideo("BV2", "重复", "UP", 2),
    ]
    fresh = new_videos(videos, {"BV1"})
    assert [video.bvid for video in fresh] == ["BV2"]


def test_merge_seen_keeps_newest_first_and_limit() -> None:
    assert merge_seen(["old"], ["new", "old"], limit=2) == ["new", "old"]
    assert merge_seen(["a"], ["b"], limit=1) == ["b"]


def test_subscription_roundtrip_and_corrupt_state() -> None:
    subscriptions = [
        UpSubscription(
            uid=42,
            name="UP主",
            seen_bvids=["BV2", "BV1"],
            notify_targets=["group:1"],
            last_checked_at=123.0,
        )
    ]
    restored = loads_subscriptions(dumps_subscriptions(subscriptions))
    assert restored == subscriptions
    assert loads_subscriptions("not json") == []
    assert UpSubscription.from_dict({"uid": 0}) is None
