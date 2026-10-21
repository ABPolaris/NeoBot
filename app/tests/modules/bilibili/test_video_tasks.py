"""B站视频后台解析：每群并发 3 与通知。"""

from __future__ import annotations

import asyncio

from neobot_app.bilibili.video_tasks import BilibiliVideoTaskManager


class _FakeVideo:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def watch(self, identifier: str, *, max_chars: int, include_comments: int):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.05)
            return {"ok": True, "bvid": "BV1", "title": "视频", "transcript": "内容", "comments": []}
        finally:
            self.active -= 1


class _FakeHub:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish(self, **kwargs):
        self.published.append(str(kwargs.get("content") or ""))
        return True


async def test_video_tasks_limit_three_per_flow_and_notify() -> None:
    video = _FakeVideo()
    hub = _FakeHub()
    manager = BilibiliVideoTaskManager(video_service=video, notification_hub=hub)

    results = [
        await manager.submit(flow_key="group:1", identifier=f"BV{i}", include_comments=3)
        for i in range(4)
    ]
    assert all(item["ok"] for item in results)
    for _ in range(40):
        if len(hub.published) >= 4:
            break
        await asyncio.sleep(0.02)

    assert video.max_active <= 3
    assert len(hub.published) == 4
    status = manager.get_pipeline_status("group:1")
    assert status["active"] == 0
    assert len(status["tasks"]) == 4
