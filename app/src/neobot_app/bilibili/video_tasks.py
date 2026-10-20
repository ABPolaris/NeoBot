"""B站视频/评论解析后台任务（参考绘图管理器，per-pipeline 并发 3）。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from neobot_contracts.ports.logging import Logger, NullLogger

MAX_ACTIVE_PER_FLOW = 3


@dataclass
class VideoTask:
    task_id: str
    flow_key: str
    identifier: str
    max_chars: int
    include_comments: int
    status: str = "watching"
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class BilibiliVideoTaskManager:
    """把耗时的视频解析放到后台，完成/失败通过通知中心回到聊天流。"""

    def __init__(
        self,
        *,
        video_service: Any = None,
        notification_hub: Any = None,
        logger: Logger | None = None,
        concurrency: int = MAX_ACTIVE_PER_FLOW,
    ) -> None:
        self._video = video_service
        self._hub = notification_hub
        self._logger = logger or NullLogger()
        self._concurrency = max(1, int(concurrency))
        self._tasks: dict[str, VideoTask] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._bg_tasks: set[asyncio.Task] = set()

    def set_video_service(self, service: Any) -> None:
        self._video = service

    async def submit(
        self,
        *,
        flow_key: str,
        identifier: str,
        max_chars: int = 3000,
        include_comments: int = 0,
    ) -> dict[str, Any]:
        if self._video is None:
            return {"ok": False, "error": "B站视频解析服务未配置"}
        text = str(identifier or "").strip()
        if not text:
            return {"ok": False, "error": "identifier 不能为空"}
        task = VideoTask(
            task_id=f"bilivideo_{uuid4().hex[:12]}",
            flow_key=str(flow_key),
            identifier=text,
            max_chars=max(200, min(int(max_chars or 3000), 20000)),
            include_comments=max(0, min(int(include_comments or 0), 20)),
        )
        self._tasks[task.task_id] = task
        bg = asyncio.create_task(self._run(task))
        self._bg_tasks.add(bg)
        bg.add_done_callback(self._bg_tasks.discard)
        return {
            "ok": True,
            "status": "watching",
            "task_id": task.task_id,
            "identifier": task.identifier,
            "message": "视频解析已转入后台，完成后会通知当前聊天流；不要重复提交。",
        }

    def get_pipeline_status(self, flow_key: str) -> dict[str, Any]:
        tasks = [t for t in self._tasks.values() if t.flow_key == flow_key]
        return {
            "active": sum(1 for t in tasks if t.status == "watching"),
            "tasks": [
                {"task_id": t.task_id, "status": t.status, "identifier": t.identifier, "error": t.error}
                for t in tasks[-10:]
            ],
        }

    async def _run(self, task: VideoTask) -> None:
        sem = self._semaphores.setdefault(task.flow_key, asyncio.Semaphore(self._concurrency))
        try:
            async with sem:
                result = await self._video.watch(
                    task.identifier,
                    max_chars=task.max_chars,
                    include_comments=task.include_comments,
                )
            task.result = result if isinstance(result, dict) else {}
            task.status = "completed" if task.result.get("ok") else "failed"
            if task.status == "failed":
                task.error = str(task.result.get("error") or "视频解析失败")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            if task.status == "completed":
                await self._publish(task, "B站视频解析完成")
            else:
                await self._publish(task, "B站视频解析失败")

    async def _publish(self, task: VideoTask, title: str) -> None:
        if self._hub is None:
            return
        result = dict(task.result or {})
        payload = {
            "task_id": task.task_id,
            "identifier": task.identifier,
            "status": task.status,
            "title": title,
            "bvid": result.get("bvid", ""),
            "video_title": result.get("title", ""),
            "source": result.get("source", ""),
            "transcript": str(result.get("transcript", ""))[:6000],
            "comments": result.get("comments", [])[:5],
            "error": task.error,
            "note": "这是后台视频解析结果，不是新任务；需要全文可调用 read_video_transcript。",
        }
        content = title + "\n" + json.dumps(payload, ensure_ascii=False)[:8000]
        try:
            kind, _, conv_id = task.flow_key.partition(":")
            await self._hub.publish(
                source="bilibili_video",
                kind=kind or "private",
                conversation_id=conv_id or task.flow_key,
                content=content,
                manager_name="bilibili_video",
                metadata={"task_id": task.task_id, "bvid": result.get("bvid", "")},
            )
        except Exception as exc:
            self._logger.warning("B站视频解析结果通知失败", task_id=task.task_id, error=str(exc))
