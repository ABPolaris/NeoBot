"""ffmpeg 自动安装：镜像展开、并发竞速、换源、zip 提取与安装缓存（不联网）。"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

import httpx
import pytest
from neobot_contracts.ports.logging import NullLogger

import neobot_app.audio.ffmpeg_install as ffmpeg_install
from neobot_app.audio.ffmpeg_install import (
    BTBN_URL,
    DEFAULT_MIRROR_PREFIXES,
    FFmpegTools,
    ensure_ffmpeg,
    expand_mirror_urls,
)


def _archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("ffmpeg-build/bin/ffmpeg.exe", b"fake-ffmpeg")
        zf.writestr("ffmpeg-build/bin/ffprobe.exe", b"fake-ffprobe")
        zf.writestr("ffmpeg-build/bin/ffplay.exe", b"fake-ffplay")
        zf.writestr("ffmpeg-build/README.txt", b"readme")
        zf.writestr("../../escape.exe", b"escape")
    return buffer.getvalue()


def test_mirror_urls_match_ncd_prefixes():
    urls = expand_mirror_urls(BTBN_URL)

    assert len(urls) == len(DEFAULT_MIRROR_PREFIXES) == 7
    assert urls[0] == BTBN_URL
    assert urls[1] == "https://gh.ddlc.top/" + BTBN_URL
    assert all(url.endswith(BTBN_URL) for url in urls[1:])


def test_mirror_urls_strip_trailing_slash():
    urls = expand_mirror_urls(
        "https://github.com/x.zip", ("", "https://m.com/", "https://n.com")
    )

    assert urls == [
        "https://github.com/x.zip",
        "https://m.com/https://github.com/x.zip",
        "https://n.com/https://github.com/x.zip",
    ]


async def test_race_picks_first_responding_mirror(monkeypatch):
    cancelled = []

    async def fake_probe(url):
        if url == "slow":
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                cancelled.append(url)
                raise
            return True
        if url == "dead":
            return False
        return True

    monkeypatch.setattr(ffmpeg_install, "_probe_first_byte", fake_probe)

    winner = await ffmpeg_install._race_mirrors(["slow", "dead", "fast"])

    assert winner == "fast"
    assert cancelled == ["slow"]


async def test_install_switches_to_next_candidate_after_download_failure(tmp_path, monkeypatch):
    mirrors = expand_mirror_urls(BTBN_URL)
    tried = []

    async def fake_race(urls):
        return urls[1]

    async def fake_download(url, dest):
        tried.append(url)
        if len(tried) == 1:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))
        dest.write_bytes(_archive())

    monkeypatch.setattr(ffmpeg_install, "_race_mirrors", fake_race)
    monkeypatch.setattr(ffmpeg_install, "_download", fake_download)
    monkeypatch.setattr(ffmpeg_install, "_runnable", lambda path: True)

    tools = await ffmpeg_install._download_and_install(tmp_path, NullLogger())

    assert tools is not None
    target = tmp_path / "bin" / "ffmpeg"
    assert tools.ffmpeg == target / "ffmpeg.exe"
    assert tools.ffprobe == target / "ffprobe.exe"
    assert tried == [mirrors[1], mirrors[0]]
    assert (target / ffmpeg_install._MARKER).read_text(encoding="utf-8") == mirrors[0]


def test_extract_zip_only_takes_the_two_exes(tmp_path):
    archive = tmp_path / "ffmpeg.zip"
    archive.write_bytes(_archive())
    target = tmp_path / "out"

    tools = ffmpeg_install._extract_zip(archive, target)

    assert tools is not None
    assert sorted(path.name for path in target.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]
    assert tools.ffmpeg.read_bytes() == b"fake-ffmpeg"
    assert not (tmp_path / "escape.exe").exists()


async def test_existing_installed_binaries_skip_download(tmp_path, monkeypatch):
    target = tmp_path / "bin" / "ffmpeg"
    target.mkdir(parents=True)
    (target / "ffmpeg.exe").write_bytes(b"x")
    (target / "ffprobe.exe").write_bytes(b"x")
    (target / ffmpeg_install._MARKER).write_text("installed", encoding="utf-8")
    monkeypatch.setattr(ffmpeg_install.shutil, "which", lambda name: None)

    async def forbidden(*args, **kwargs):
        raise AssertionError("不应触发下载")

    monkeypatch.setattr(ffmpeg_install, "_download_and_install", forbidden)

    tools = await ensure_ffmpeg(data_dir=tmp_path)

    assert tools == FFmpegTools(target / "ffmpeg.exe", target / "ffprobe.exe")


async def test_unmarked_binaries_are_validated_before_use(tmp_path, monkeypatch):
    target = tmp_path / "bin" / "ffmpeg"
    target.mkdir(parents=True)
    (target / "ffmpeg.exe").write_bytes(b"x")
    (target / "ffprobe.exe").write_bytes(b"x")
    checked = []
    monkeypatch.setattr(ffmpeg_install.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        ffmpeg_install, "_runnable", lambda path: checked.append(path) or True
    )

    tools = await ensure_ffmpeg(data_dir=tmp_path)

    assert tools is not None
    assert len(checked) == 2
    assert (target / ffmpeg_install._MARKER).is_file()


async def test_all_sources_failing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg_install.shutil, "which", lambda name: None)
    tried = []

    async def no_winner(urls):
        return None

    async def failing_download(url, dest):
        tried.append(url)
        raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    monkeypatch.setattr(ffmpeg_install, "_race_mirrors", no_winner)
    monkeypatch.setattr(ffmpeg_install, "_download", failing_download)

    assert await ensure_ffmpeg(data_dir=tmp_path) is None

    assert tried == expand_mirror_urls(BTBN_URL) + [ffmpeg_install.GYAN_URL]
    assert not (tmp_path / "bin" / "ffmpeg" / ffmpeg_install._MARKER).exists()


async def test_download_rejects_empty_and_oversized_bodies(tmp_path, monkeypatch):
    class _Stream:
        def __init__(self, chunks):
            self._chunks = chunks

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        async def aiter_raw(self):
            for chunk in self._chunks:
                yield chunk

    class _Client:
        def __init__(self, chunks):
            self._chunks = chunks

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args):
            return _Stream(self._chunks)

    monkeypatch.setattr(ffmpeg_install, "MAX_DOWNLOAD_BYTES", 1)
    monkeypatch.setattr(
        ffmpeg_install.httpx, "AsyncClient", lambda **kwargs: _Client([b"xx"])
    )
    with pytest.raises(ValueError):
        await ffmpeg_install._download("https://example/x.zip", tmp_path / "a.part")

    monkeypatch.setattr(ffmpeg_install.httpx, "AsyncClient", lambda **kwargs: _Client([]))
    with pytest.raises(ValueError):
        await ffmpeg_install._download("https://example/x.zip", tmp_path / "b.part")


async def test_system_ffmpeg_is_preferred(monkeypatch):
    monkeypatch.setattr(
        ffmpeg_install.shutil,
        "which",
        {"ffmpeg": "/opt/ffmpeg", "ffprobe": "/opt/ffprobe"}.get,
    )

    tools = await ensure_ffmpeg()

    assert tools == FFmpegTools(Path("/opt/ffmpeg"), Path("/opt/ffprobe"))
