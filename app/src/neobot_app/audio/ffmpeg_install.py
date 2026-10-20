"""ffmpeg/ffprobe 按需自动安装（Windows x64）。

本机 PATH 没有 ffmpeg 时，从 BtbN 的 GitHub Release 下载 win64 zip。GitHub 直连
在国内经常超时，所以按 NCD（NapCatQQ-Desktop）的镜像思路把原始 URL 展开成
直连 + 6 个反代前缀，所有候选并发探测首字节、先到先用；探测连接不写盘，winning
镜像再正式下载（换镜像时重下整包，不跨镜像续传，避免拼出错位字节）。GitHub 全挂
时用 gyan.dev 直连兜底（它不是 GitHub 资产，不套镜像）。

装到 <DATA_DIR>/bin/ffmpeg/（不动 PATH、不污染系统），成功写 .installed marker；
之后直接命中，不再联网。任何失败只记日志并返回 None，交给调用方降级。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import NamedTuple

import httpx
from neobot_contracts.ports.logging import Logger, NullLogger

from neobot_app.core.constants import DATA_DIR

#: BtbN 的 master 版 win64 gpl 包（自带 ffmpeg/ffprobe）。
BTBN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
#: GitHub 不通时的直连兜底；包内同样是 bin/ffmpeg.exe + bin/ffprobe.exe。
GYAN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

#: 与 NCD crates/ncd-network/src/mirror.rs 保持一致：空串 = 直连，其余为反代前缀。
DEFAULT_MIRROR_PREFIXES = (
    "",
    "https://gh.ddlc.top",
    "https://gh-proxy.com",
    "https://ghfast.top",
    "https://cors.isteed.cc",
    "https://ghproxy.cc",
    "https://github.akams.cn",
)

#: 单个安装包大小上限：当前 gpl 包约 150 MB，留足余量。
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
#: 单个候选等首字节 / 整包下载的硬超时（秒）。
_PROBE_TIMEOUT = 20.0
_DOWNLOAD_TIMEOUT = 600.0
_EXE_NAMES = ("ffmpeg.exe", "ffprobe.exe")
_MARKER = ".installed"
#: 进程内串行化，避免多个 ASR 请求同时触发下载。
_LOCK = asyncio.Lock()


class FFmpegTools(NamedTuple):
    """解析出的 ffmpeg / ffprobe 绝对路径（ffprobe 可能缺失）。"""

    ffmpeg: Path
    ffprobe: Path | None


def expand_mirror_urls(
    original: str, prefixes: tuple[str, ...] = DEFAULT_MIRROR_PREFIXES
) -> list[str]:
    """把原始 GitHub URL 展开成「直连 + 各镜像前缀」的候选列表。"""
    return [original if not prefix else f"{prefix.rstrip('/')}/{original}" for prefix in prefixes]


async def ensure_ffmpeg(
    *, data_dir: Path | None = None, logger: Logger | None = None
) -> FFmpegTools | None:
    """确保有可用的 ffmpeg：PATH -> 已安装目录 -> 下载安装；失败返回 None（不抛）。"""
    log = logger or NullLogger()
    tools = _system_tools()
    if tools is not None:
        return tools
    tools = _installed_tools(data_dir)
    if tools is not None:
        return tools

    async with _LOCK:
        # 等锁期间别的请求可能已经装好，进来先复查。
        tools = _installed_tools(data_dir) or _system_tools()
        if tools is not None:
            return tools
        try:
            return await _download_and_install(data_dir, log)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 安装只是增强项：任何失败都只降级
            log.warning("ffmpeg 自动安装失败", error_type=type(exc).__name__)
            return None


def _install_dir(data_dir: Path | None) -> Path:
    return (Path(data_dir) if data_dir is not None else DATA_DIR) / "bin" / "ffmpeg"


def _system_tools() -> FFmpegTools | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    ffprobe = shutil.which("ffprobe")
    return FFmpegTools(Path(ffmpeg), Path(ffprobe) if ffprobe else None)


def _installed_tools(data_dir: Path | None) -> FFmpegTools | None:
    target = _install_dir(data_dir)
    ffmpeg = target / _EXE_NAMES[0]
    ffprobe = target / _EXE_NAMES[1]
    if not ffmpeg.is_file() or not ffprobe.is_file():
        return None
    marker = target / _MARKER
    if not marker.is_file():
        # 手工放进来的二进制没有 marker：先验一次可执行，再补 marker 免掉后续每次校验。
        if not (_runnable(ffmpeg) and _runnable(ffprobe)):
            return None
        try:
            marker.write_text("verified", encoding="utf-8")
        except OSError:
            pass
    return FFmpegTools(ffmpeg, ffprobe)


def _runnable(executable: Path) -> bool:
    try:
        completed = subprocess.run(
            [str(executable), "-version"], capture_output=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


async def _download_and_install(data_dir: Path | None, logger: Logger) -> FFmpegTools | None:
    target = _install_dir(data_dir)
    target.mkdir(parents=True, exist_ok=True)

    mirrored = expand_mirror_urls(BTBN_URL)
    winner = await _race_mirrors(mirrored)
    if winner is not None:
        logger.info("ffmpeg 镜像竞速选中", url=winner)
    candidates = ([winner] if winner else []) + [url for url in mirrored if url != winner]
    candidates.append(GYAN_URL)

    part = target.parent / f".ffmpeg-{os.getpid()}.part"
    for url in candidates:
        try:
            await _download(url, part)
            tools = _extract_zip(part, target)
            if (
                tools is None
                or not _runnable(tools.ffmpeg)
                or tools.ffprobe is None
                or not _runnable(tools.ffprobe)
            ):
                raise ValueError("安装包内没有可执行的 ffmpeg/ffprobe")
        except Exception as exc:
            _remove_binaries(target)
            logger.warning(
                "ffmpeg 下载源不可用，换下一个", url=url, error_type=type(exc).__name__
            )
        else:
            try:
                (target / _MARKER).write_text(url, encoding="utf-8")
            except OSError:
                pass
            logger.info("ffmpeg 自动安装完成", path=str(tools.ffmpeg))
            return tools
        finally:
            part.unlink(missing_ok=True)
    return None


async def _race_mirrors(urls: list[str]) -> str | None:
    """并发探测候选首字节，返回最先响应的 URL；其余任务取消。"""
    if not urls:
        return None
    tasks = {asyncio.create_task(_probe_first_byte(url)): url for url in urls}
    try:
        pending: set[asyncio.Task[bool]] = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception() is None and task.result():
                    return tasks[task]
        return None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _probe_first_byte(url: str) -> bool:
    """GET stream，收到首个非空 chunk 即视为可用；不写盘、立刻释放连接。"""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(_PROBE_TIMEOUT)
        ) as client:
            async with client.stream("GET", url) as response:
                if response.is_error:
                    return False
                async for chunk in response.aiter_raw():
                    return bool(chunk)
        return False
    except (httpx.HTTPError, OSError, ValueError):
        return False


async def _download(url: str, dest: Path) -> None:
    """把整包流式写到 .part；超过大小上限或整体超时即失败。"""
    async with asyncio.timeout(_DOWNLOAD_TIMEOUT):
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0),
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                written = 0
                with dest.open("wb") as out:
                    async for chunk in response.aiter_raw():
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise ValueError("ffmpeg 安装包超过大小上限")
                        out.write(chunk)
                if written == 0:
                    raise ValueError("下载内容为空")


def _extract_zip(archive: Path, target: Path) -> FFmpegTools | None:
    """只按 basename 取两个 exe 落盘；目录穿越条目不可能被写出。"""
    target.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # 丢弃路径，只认原始 basename —— 压缩包里的 ../ 或绝对路径都落不到目标外。
            name = info.filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if name not in _EXE_NAMES or name in found:
                continue
            output = target / name
            temp = output.with_name(output.name + ".part")
            with zf.open(info) as source, temp.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            os.replace(temp, output)
            found[name] = output
    if len(found) != len(_EXE_NAMES):
        _remove_binaries(target)
        return None
    return FFmpegTools(found[_EXE_NAMES[0]], found[_EXE_NAMES[1]])


def _remove_binaries(target: Path) -> None:
    for name in _EXE_NAMES:
        (target / name).unlink(missing_ok=True)
        (target / (name + ".part")).unlink(missing_ok=True)
