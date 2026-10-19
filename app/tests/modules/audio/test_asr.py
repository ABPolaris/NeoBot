"""硅基流动 ASR：请求形态、错误降级与转码边界（不触网、不依赖 ffmpeg）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import neobot_app.audio.asr as asr_module
from neobot_app.audio.asr import ASRService
from neobot_app.config.schemas.bot import ASR as ASRConfig


def _config(**overrides) -> ASRConfig:
    values = {
        "enabled": True,
        "model": "XingChenAGI/XingChenASR-V3.2-Ultra",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "sk-test",
        "language": "zh",
        "timeout_seconds": 5,
        "max_audio_mb": 50,
    }
    values.update(overrides)
    return ASRConfig(**values)


def _make_service(handler, **overrides) -> ASRService:
    return ASRService(_config(**overrides), transport=httpx.MockTransport(handler))


async def _fake_prepare(source, out_dir):
    output = Path(out_dir) / "audio.mp3"
    output.write_bytes(b"fake-mp3")
    return output


def _audio_file(tmp_path: Path) -> Path:
    source = tmp_path / "voice.amr"
    source.write_bytes(b"amr-bytes")
    return source


async def test_transcribe_posts_multipart_and_returns_text(tmp_path, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.read()
        return httpx.Response(200, json={"text": " 你好呀 "})

    service = _make_service(handler)
    monkeypatch.setattr(service, "_prepare_audio", _fake_prepare)
    try:
        result = await service.transcribe(_audio_file(tmp_path))
    finally:
        await service.close()

    assert result == "你好呀"
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/audio/transcriptions"
    assert captured["auth"] == "Bearer sk-test"
    assert "multipart/form-data" in captured["content_type"]
    body = captured["body"].decode("utf-8", errors="replace")
    assert "XingChenAGI/XingChenASR-V3.2-Ultra" in body
    assert 'name="language"' in body
    assert "fake-mp3" in body


async def test_api_key_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SiliconFlow_APIKey", "sk-env")
    monkeypatch.setenv("SiliconFlow_URL", "https://env.example/v1")
    service = ASRService(_config(base_url="", api_key=""))
    try:
        assert service.enabled
        assert str(service._client.base_url) == "https://env.example/v1/"
        assert service._client.headers["authorization"] == "Bearer sk-env"
    finally:
        await service.close()


async def test_missing_api_key_degrades_instead_of_raising():
    service = ASRService(_config(api_key=""))

    assert not service.enabled
    result = await service.transcribe(Path("missing.amr"))

    assert result.startswith("语音识别失败：")


async def test_disabled_returns_chinese_notice():
    service = ASRService(_config(enabled=False))

    result = await service.transcribe(Path("missing.amr"))

    assert "未启用" in result


async def test_existing_but_missing_file_degrades(tmp_path):
    service = _make_service(lambda request: httpx.Response(200, json={"text": "x"}))
    try:
        result = await service.transcribe(tmp_path / "nope.amr")
    finally:
        await service.close()

    assert result.startswith("语音识别失败：")
    assert "不存在" in result


async def test_http_error_body_is_kept_in_chinese_message(tmp_path, monkeypatch):
    service = _make_service(
        lambda request: httpx.Response(401, text="unauthorized: bad key")
    )
    monkeypatch.setattr(service, "_prepare_audio", _fake_prepare)
    try:
        result = await service.transcribe(_audio_file(tmp_path))
    finally:
        await service.close()

    assert result.startswith("语音识别失败：")
    assert "HTTP 401" in result
    assert "bad key" in result


async def test_network_error_degrades(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    service = _make_service(handler)
    monkeypatch.setattr(service, "_prepare_audio", _fake_prepare)
    try:
        result = await service.transcribe(_audio_file(tmp_path))
    finally:
        await service.close()

    assert result.startswith("语音识别失败：")
    assert "网络请求失败" in result


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"result": "no text field"}),
        httpx.Response(200, json={"text": 123}),
    ],
)
async def test_malformed_response_degrades(tmp_path, monkeypatch, response):
    service = _make_service(lambda request: response)
    monkeypatch.setattr(service, "_prepare_audio", _fake_prepare)
    try:
        result = await service.transcribe(_audio_file(tmp_path))
    finally:
        await service.close()

    assert result.startswith("语音识别失败：")


async def test_transcoded_size_limit_is_enforced(tmp_path, monkeypatch):
    async def oversized(source, out_dir):
        output = Path(out_dir) / "audio.mp3"
        output.write_bytes(b"x" * (1024 * 1024 + 1))
        return output

    service = _make_service(
        lambda request: httpx.Response(200, json={"text": "should not reach"}),
        max_audio_mb=1,
    )
    monkeypatch.setattr(service, "_prepare_audio", oversized)
    try:
        result = await service.transcribe(_audio_file(tmp_path))
    finally:
        await service.close()

    assert "MB 上限" in result


async def test_missing_ffmpeg_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_module.shutil, "which", lambda name: None)
    service = _make_service(lambda request: httpx.Response(200, json={"text": "x"}))
    try:
        result = await service.transcribe(_audio_file(tmp_path))
    finally:
        await service.close()

    assert "ffmpeg" in result


async def test_overlong_audio_is_rejected_before_transcode(tmp_path, monkeypatch):
    service = _make_service(lambda request: httpx.Response(200, json={"text": "x"}))
    monkeypatch.setattr(
        service, "_probe_duration", AsyncMock(return_value=asr_module.MAX_AUDIO_SECONDS + 1)
    )
    try:
        result = await service.transcribe(_audio_file(tmp_path))
    finally:
        await service.close()

    assert "分钟上限" in result


async def test_ffmpeg_command_extracts_16k_mono_and_caps_duration(tmp_path, monkeypatch):
    calls = []

    async def fake_probe(source):
        return 10.0

    class _Completed:
        returncode = 0
        stderr = b""

    def fake_run(command, **kwargs):
        calls.append(command)
        output = Path(command[-1])
        output.write_bytes(b"mp3")
        return _Completed()

    monkeypatch.setattr(asr_module, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(asr_module.shutil, "which", lambda name: "/usr/bin/" + name)
    service = _make_service(lambda request: httpx.Response(200, json={"text": "x"}))
    monkeypatch.setattr(service, "_probe_duration", fake_probe)
    try:
        result = await service.transcribe(_audio_file(tmp_path))
    finally:
        await service.close()

    assert result == "x"
    assert len(calls) == 1
    command = calls[0]
    joined = " ".join(command)
    assert "-ac 1" in joined
    assert "-ar 16000" in joined
    assert "-t 3600" in joined
    assert "libmp3lame" in joined


async def test_probe_duration_returns_none_when_ffprobe_missing(monkeypatch):
    monkeypatch.setattr(asr_module.shutil, "which", lambda name: None)

    assert await ASRService._probe_duration(Path("x.mp3")) is None
