"""硅基流动语音识别（OpenAI 兼容 /audio/transcriptions）。

对外只暴露一个 async 方法 `transcribe`：成功返回识别文本，**任何失败都降级为
一句中文错误说明**而不是抛异常 —— ASR 只是视频/语音链路的增强项，失败不应该
打断调用方，也不该把底层异常细节（URL、Key、堆栈）泄露到提示词里。

链路：任意音视频 -> ffmpeg 转 16k 单声道 mp3（上传体积可控）-> multipart POST
-> 取 JSON 的 text 字段。本机没有 ffmpeg 时先尝试 :mod:`.ffmpeg_install` 自动安装。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from neobot_contracts.ports.logging import Logger, NullLogger

from neobot_app.audio.ffmpeg_install import FFmpegTools, ensure_ffmpeg
from neobot_app.config.schemas.bot import ASR as ASRConfig
from neobot_app.config.schemas.env import EnvConfig

#: 注入点：测试传一个不联网的 resolver，生产走 ensure_ffmpeg。
FFmpegResolver = Callable[[], Awaitable[FFmpegTools | None]]

#: 供应商限制：单次音频不超过 1 小时、50 MB（大小上限由配置 max_audio_mb 覆盖）。
MAX_AUDIO_SECONDS = 3600
#: env 里没配 SiliconFlow_URL 时的兜底地址（与 env.py 的默认值一致）。
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
#: 转码目标码率：16k 单声道 32kbps，1 小时约 14 MB，稳定低于上传上限。
_TRANSCODE_BITRATE = "32k"
_ERROR_PREFIX = "语音识别失败："


class ASRService:
    """硅基流动 ASR 客户端（OpenAI 兼容接口，默认模型为免费模型）。"""

    def __init__(
        self,
        config: ASRConfig,
        *,
        logger: Logger | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        ffmpeg_resolver: FFmpegResolver | None = None,
    ) -> None:
        self._config = config
        self._logger = logger or NullLogger()
        self._ffmpeg_resolver = ffmpeg_resolver or self._default_ffmpeg_resolver
        self._base_url = self._resolve_base_url(config)
        self._api_key = self._resolve_api_key(config)
        self._enabled = bool(config.enabled) and bool(self._base_url) and bool(self._api_key)
        if config.enabled and not self._enabled:
            self._logger.warning(
                "ASR 已配置启用但缺少 SiliconFlow_URL / SiliconFlow_APIKey，语音识别不可用"
            )
        # 只有真正可用时才建连接池：disabled 时不占用连接资源。
        self._client = (
            httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=httpx.Timeout(float(config.timeout_seconds), connect=10.0),
                transport=transport,
            )
            if self._base_url
            else None
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def transcribe(self, source: str | Path) -> str:
        """把本地音视频文件转写为文本；失败返回中文错误说明（不抛异常）。"""
        if not self._config.enabled:
            return "语音识别未启用（config 的 [asr].enabled=false）"
        if not self._enabled or self._client is None:
            return f"{_ERROR_PREFIX}未配置 SiliconFlow_APIKey 或 SiliconFlow_URL"

        path = Path(source).expanduser()
        if not path.is_file():
            return f"{_ERROR_PREFIX}音频文件不存在：{path.name}"

        with tempfile.TemporaryDirectory(prefix="neobot_asr_") as tmp:
            prepared = await self._prepare_audio(path, Path(tmp))
            if isinstance(prepared, str):
                return prepared
            try:
                payload = prepared.read_bytes()
            except OSError as exc:
                return f"{_ERROR_PREFIX}读取转码后音频失败（{type(exc).__name__}）"
            max_bytes = max(1, int(self._config.max_audio_mb)) * 1024 * 1024
            if len(payload) > max_bytes:
                return f"{_ERROR_PREFIX}音频超过 {self._config.max_audio_mb} MB 上限"
            return await self._request_transcription(prepared, payload)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ── HTTP ──

    async def _request_transcription(self, audio: Path, payload: bytes) -> str:
        assert self._client is not None
        data = {"model": self._config.model}
        if self._config.language:
            data["language"] = self._config.language
        try:
            response = await self._client.post(
                "/audio/transcriptions",
                data=data,
                files={"file": (audio.name, payload, "audio/mpeg")},
            )
        except httpx.TimeoutException:
            self._logger.warning("ASR 请求超时", model=self._config.model)
            return f"{_ERROR_PREFIX}请求超时，请稍后重试"
        except httpx.HTTPError as exc:
            self._logger.warning("ASR 请求失败", error_type=type(exc).__name__)
            return f"{_ERROR_PREFIX}网络请求失败（{type(exc).__name__}）"

        if response.is_error:
            body = response.text
            if len(body) > 300:
                body = body[:300] + "..."
            self._logger.warning(
                "ASR 接口返回错误", status_code=response.status_code, body=body
            )
            return f"{_ERROR_PREFIX}HTTP {response.status_code}：{body}".strip()

        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            return f"{_ERROR_PREFIX}接口返回的不是 JSON"
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            return f"{_ERROR_PREFIX}接口返回中缺少 text 字段"
        return text.strip()

    # ── ffmpeg 转码 ──

    async def _prepare_audio(self, source: Path, out_dir: Path) -> Path | str:
        """转 16k 单声道 mp3；返回转换后路径，失败返回中文错误说明。"""
        # 先看时长：即使机器没装 ffmpeg，超长音频也应该报“时长超限”而不是误导成环境问题。
        duration = await self._probe_duration(source)
        if duration is not None and duration > MAX_AUDIO_SECONDS:
            return f"{_ERROR_PREFIX}音频超过 {MAX_AUDIO_SECONDS // 60} 分钟上限"
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if ffmpeg is None or ffprobe is None:
            tools = await self._ffmpeg_resolver()
            if tools is not None:
                ffmpeg = ffmpeg or str(tools.ffmpeg)
                ffprobe = ffprobe or (str(tools.ffprobe) if tools.ffprobe else None)
                # 下载装好的 ffprobe 不在 PATH 里：补一次时长探测，超长仍按上限拒绝。
                if duration is None and ffprobe is not None:
                    duration = await _run_ffprobe(ffprobe, source)
                    if duration is not None and duration > MAX_AUDIO_SECONDS:
                        return f"{_ERROR_PREFIX}音频超过 {MAX_AUDIO_SECONDS // 60} 分钟上限"
        if ffmpeg is None:
            return f"{_ERROR_PREFIX}未找到 ffmpeg，自动安装也失败（请安装后加入 PATH）"
        output = out_dir / "audio.mp3"
        # -t 兜底：探测不到时长（流式/损坏容器）时也只会截前 1 小时再上传。
        command = [
            ffmpeg, "-nostdin", "-y", "-i", str(source), "-vn", "-ac", "1",
            "-ar", "16000", "-t", str(MAX_AUDIO_SECONDS),
            "-c:a", "libmp3lame", "-b:a", _TRANSCODE_BITRATE, str(output),
        ]
        try:
            completed = await asyncio.to_thread(
                subprocess.run, command, capture_output=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            return f"{_ERROR_PREFIX}音频转码超时"
        except OSError as exc:
            return f"{_ERROR_PREFIX}无法启动 ffmpeg（{type(exc).__name__}）"
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            detail = detail.splitlines()[-1][:200] if detail else "无输出"
            self._logger.warning("ASR 音频转码失败", detail=detail)
            return f"{_ERROR_PREFIX}音频转码失败（{detail}）"
        return output

    @staticmethod
    async def _probe_duration(source: Path) -> float | None:
        """ffprobe 取时长（秒）；不可用时返回 None，由 -t 兜底。"""
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return None
        return await _run_ffprobe(ffprobe, source)

    async def _default_ffmpeg_resolver(self) -> FFmpegTools | None:
        return await ensure_ffmpeg(logger=self._logger)

    # ── 配置 ──

    @staticmethod
    def _resolve_base_url(config: ASRConfig) -> str:
        base_url = (config.base_url or "").strip()
        if not base_url:
            base_url = EnvConfig.get_api_platform_url("SiliconFlow") or DEFAULT_BASE_URL
        return base_url.rstrip("/")

    @staticmethod
    def _resolve_api_key(config: ASRConfig) -> str:
        api_key = (config.api_key or "").strip()
        if not api_key:
            api_key = EnvConfig.get_api_platform_key("SiliconFlow") or ""
        return api_key


async def _run_ffprobe(ffprobe: str, source: Path) -> float | None:
    """执行 ffprobe 取时长；任何失败都返回 None（时长探测只是可选的提前拒绝）。"""
    command = [
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(source),
    ]
    try:
        completed = await asyncio.to_thread(
            subprocess.run, command, capture_output=True, timeout=30
        )
        return float(completed.stdout.decode().strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def build_asr_service(config: Any, *, logger: Logger | None = None) -> ASRService:
    """按配置构建 ASR 服务（wiring 入口，供 bootstrap 调用）。"""
    asr_config = getattr(config, "asr", None)
    if asr_config is None:
        from neobot_app.config.schemas.bot import ASR as _ASR

        asr_config = _ASR()
    return ASRService(asr_config, logger=logger)
