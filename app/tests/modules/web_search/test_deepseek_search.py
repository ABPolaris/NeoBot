"""DeepSeek Search 引擎 / 编排 / 工具接线测试（MockTransport，无网络）。

覆盖：
- 响应字段映射（summary 优先）、质量过滤/排序/cap；
- 402/401 后 available=False、后续请求直接跳过、fallback 文案；
- 显式 engine=deepseek 失败回退 Bing，signals 标注 deepseek_unavailable；
- auto 不调用 deepseek；工具参数透传/描述；status 显示可用性。
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from neobot_app.web_search import engine as engine_module
from neobot_app.web_search.engine import (
    DEEPSEEK_AUTH_ERROR,
    DEEPSEEK_QUOTA_ERROR,
    DeepSeekSearchEngine,
)
from neobot_app.web_search.manager import SearchManager
from neobot_app.web_search.models import SearchResponse, SearchResult
from neobot_app.web_search.session import SearchSession
from neobot_app.web_search_package import (
    WebSearchExecutor,
    _build_search_tool,
    build_web_search_package,
)


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """把 MockTransport 注入引擎内部自建的 httpx.AsyncClient，并记录请求。"""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_record)
    real_client = httpx.AsyncClient

    def _factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(engine_module.httpx, "AsyncClient", _factory)
    return seen


def _payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"results": rows}


async def test_success_maps_fields_and_filters_low_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "title": "低质结果",
            "url": "https://low.example/a",
            "snippet": "低质摘要",
            "rerank_score": 0.2,
            "authority": -1,
            "date": None,
            "site_name": "Low",
        },
        {
            "title": "优质结果",
            "url": "https://good.example/b?utm_source=x",
            "summary": "长摘要（给模型）",
            "snippet": "短摘要",
            "content": "正文",
            "rerank_score": 1.8,
            "authority": 2,
            "date": "2026-10-19T00:00:00Z",
            "site_name": "Good",
        },
    ]
    seen = _install_transport(monkeypatch, lambda request: httpx.Response(200, json=_payload(rows)))
    engine = DeepSeekSearchEngine(api_key="sk-test")
    try:
        resp = await engine.search("NeoBot", 5)
    finally:
        await engine.aclose()

    assert resp.success is True, resp.error
    assert resp.engine == "deepseek"
    assert len(resp.results) == 1
    item = resp.results[0]
    assert item.title == "优质结果"
    assert item.url == "https://good.example/b", "跟踪参数应被剥离"
    assert item.snippet == "长摘要（给模型）", "摘要优先 summary"
    assert item.content == "正文"
    assert item.content_fetched is True
    assert item.published_at == "2026-10-19T00:00:00Z"
    assert item.site_name == "Good"
    assert item.channel == "api"
    assert item.rerank_score == 1.8
    assert item.authority == 2
    assert resp.signals["filtered"] == 1

    request = seen[0]
    assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert json.loads(request.content) == {
        "query": "NeoBot",
        "count": 5,
        "need_content": False,
    }


async def test_results_sorted_by_score_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"title": "a", "url": "https://a.example/1", "summary": "a", "rerank_score": 1.6},
        {"title": "b", "url": "https://b.example/2", "summary": "b", "rerank_score": 2.4},
        {"title": "c", "url": "https://c.example/3", "summary": "c", "rerank_score": 1.7},
    ]
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=_payload(rows)))
    engine = DeepSeekSearchEngine(api_key="k")
    resp = await engine.search("q", 2)

    assert [item.title for item in resp.results] == ["b", "c"]
    assert [item.index for item in resp.results] == [0, 1]


def test_base_url_with_v1_is_not_doubled() -> None:
    engine = DeepSeekSearchEngine(api_key="k", base_url="https://api.deepseek.com/v1/")
    assert engine.endpoint == "https://api.deepseek.com/v1/tools/search"


async def test_402_disables_engine_and_skips_following_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_transport(
        monkeypatch,
        lambda request: httpx.Response(402, json={"error": {"message": "Insufficient Balance"}}),
    )
    engine = DeepSeekSearchEngine(api_key="k", quota_cooldown_seconds=3600)

    first = await engine.search("q", 5)

    assert first.success is False
    assert first.error == DEEPSEEK_QUOTA_ERROR
    assert "额度不足" in first.error
    assert "已回退旧的搜索实现" in first.error
    assert first.signals["deepseek_unavailable"] is True
    assert first.signals["http_status"] == 402
    assert engine.available is False

    calls_after_first = len(seen)
    second = await engine.search("q2", 5)
    assert len(seen) == calls_after_first, "402 后不应再发请求"
    assert second.success is False
    assert second.error == DEEPSEEK_QUOTA_ERROR


async def test_401_disables_engine_with_longer_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_transport(monkeypatch, lambda request: httpx.Response(401, json={"error": {"message": "Bad key"}}))
    engine = DeepSeekSearchEngine(api_key="bad-key")

    resp = await engine.search("q", 5)

    assert resp.success is False
    assert resp.error == DEEPSEEK_AUTH_ERROR
    assert "Key 无效" in resp.error
    assert resp.signals["http_status"] == 401
    assert resp.signals["disabled_for_seconds"] == 300.0
    assert engine.available is False
    await engine.search("q2", 5)
    assert len(seen) == 1, "401 后同样直接跳过"


def _result(engine: str = "bing") -> SearchResult:
    return SearchResult(index=0, title="t", url="https://example.com/a", snippet="s", engine=engine)


def _ok(engine: str) -> SearchResponse:
    return SearchResponse(query="q", results=[_result(engine)], engine=engine)


def _quota_fail() -> SearchResponse:
    return SearchResponse(
        query="q",
        results=[],
        engine="deepseek",
        error=DEEPSEEK_QUOTA_ERROR,
        degraded=True,
        signals={"deepseek_unavailable": True, "http_status": 402},
    )


class _FakeEngine:
    def __init__(self, name: str, responses: list[SearchResponse]) -> None:
        self.name = name
        self._responses = list(responses)
        self.calls = 0
        self.available = True
        self.unavailable_reason = ""

    async def search(self, query: str, num_results: int = 10) -> SearchResponse:
        self.calls += 1
        resp = self._responses.pop(0) if self._responses else _ok(self.name)
        if (resp.signals or {}).get("deepseek_unavailable"):
            self.available = False
            self.unavailable_reason = "额度不足（HTTP 402）"
        return resp

    async def aclose(self) -> None:
        return None


def _manager(
    engines: list[_FakeEngine],
    *,
    max_retries: int = 0,
    **kwargs: Any,
) -> SearchManager:
    manager = SearchManager(
        engines=[engine.name for engine in engines],
        min_delay=0.0,
        max_retries=max_retries,
        browser_fallback=False,
        **kwargs,
    )
    manager._engines = {engine.name: engine for engine in engines}  # type: ignore[assignment]
    return manager


async def test_explicit_deepseek_failure_does_not_fall_back_to_free() -> None:
    deepseek = _FakeEngine("deepseek", [_quota_fail()])
    bing = _FakeEngine("bing", [_ok("bing")])
    manager = _manager([deepseek, bing], deepseek_enabled=True, deepseek_api_key="sk")

    resp = await manager.search_with_fallback("q", engine="deepseek")

    assert resp.success is False
    assert resp.engine == "all"
    assert deepseek.calls == 1
    assert bing.calls == 0, "有 DeepSeek Key 时，显式 deepseek 失败不再回退免费渠道"
    assert [stage["stage"] for stage in resp.signals["stages"]] == ["deepseek"]


async def test_hidden_deepseek_is_skipped_without_request() -> None:
    deepseek = _FakeEngine("deepseek", [])
    deepseek.available = False
    deepseek.unavailable_reason = "额度不足（HTTP 402）"
    bing = _FakeEngine("bing", [_ok("bing")])
    manager = _manager([deepseek, bing], deepseek_enabled=True, deepseek_api_key="sk")

    assert manager.deepseek_available is False
    resp = await manager.search_with_fallback("q", engine="deepseek")

    assert deepseek.calls == 0, "已隐藏的引擎不应再发请求"
    assert resp.engine == "all"
    assert resp.degraded is True
    assert bing.calls == 0, "有 Key 但额度不足时不回退免费渠道"
    assert resp.signals["deepseek_unavailable"] is True
    assert "deepseek" not in manager.available_engines


async def test_auto_uses_deepseek_by_default() -> None:
    deepseek = _FakeEngine("deepseek", [_ok("deepseek")])
    bing = _FakeEngine("bing", [_ok("bing")])
    manager = _manager([deepseek, bing], deepseek_enabled=True, deepseek_api_key="sk")

    resp = await manager.search_with_fallback("q")

    assert resp.engine == "deepseek"
    assert deepseek.calls == 1
    assert bing.calls == 0
    assert [stage["stage"] for stage in resp.signals["stages"]] == ["deepseek"]


def test_key_presence_enables_deepseek_unless_explicitly_disabled() -> None:
    auto = SearchManager(engines=["bing"], browser_fallback=False, deepseek_api_key="sk")
    assert auto.deepseek_available is True

    disabled = SearchManager(
        engines=["bing"],
        browser_fallback=False,
        deepseek_enabled=False,
        deepseek_api_key="sk",
    )
    assert disabled.deepseek_available is False
    assert "未启用" in disabled.deepseek_unavailable_reason


async def test_direct_deepseek_quota_error_does_not_retry() -> None:
    deepseek = _FakeEngine("deepseek", [_quota_fail()])
    manager = _manager([deepseek], max_retries=2, deepseek_enabled=True, deepseek_api_key="sk")

    resp = await manager.search("q", engine="deepseek")

    assert resp.success is False
    assert resp.error == DEEPSEEK_QUOTA_ERROR
    assert deepseek.calls == 1, "额度错误不应触发指数退避重试"


def test_search_tool_exposes_engine_enum_and_paid_description() -> None:
    function = _build_search_tool()["function"]
    engine = function["parameters"]["properties"]["engine"]

    assert engine["enum"] == ["auto", "bing", "duckduckgo", "deepseek"]
    assert "DeepSeek" in engine["description"]
    assert "免费" in engine["description"]
    assert "缺少" in engine["description"]
    assert "DeepSeek" in function["description"]


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def search(self, query: str, num_results: int = 10, *, engine: str | None = None) -> SearchResponse:
        self.calls.append(("search", engine))
        return SearchResponse(query=query, results=[_result(engine or "bing")], engine=engine or "bing")

    async def research(self, topic: str, **kwargs: Any) -> SearchResponse:
        engine = kwargs.get("engine")
        self.calls.append(("research", engine))
        return SearchResponse(query=topic, results=[_result(engine or "bing")], engine=engine or "bing")


async def test_execute_passes_engine_to_session(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = WebSearchExecutor(deepseek_enabled=True, deepseek_api_key="sk")
    session = _RecordingSession()
    monkeypatch.setattr(executor, "_get_session", lambda: session)

    out = await executor.execute("search", {"query": "q", "engine": "deepseek"})
    assert session.calls[-1] == ("search", "deepseek")
    assert "引擎: deepseek" in out

    await executor.execute("search", {"query": "q", "engine": "auto"})
    assert session.calls[-1] == ("search", None)

    await executor.execute("search", {"query": "q", "engine": "not-an-engine"})
    assert session.calls[-1] == ("search", None)

    await executor.execute("search", {"query": "q", "engine": "deepseek", "mode": "news"})
    assert session.calls[-1] == ("research", "deepseek")


async def test_status_shows_deepseek_availability_and_hides_on_quota() -> None:
    session = SearchSession(
        engines=["bing"],
        browser_fallback=False,
        deepseek_enabled=True,
        deepseek_api_key="sk",
    )
    deepseek = _FakeEngine("deepseek", [])
    session._manager._engines["deepseek"] = deepseek  # type: ignore[assignment]

    summary = session.get_summary_for_agent()
    assert 'DeepSeek 搜索: 可用（engine="deepseek"，按次计费）' in summary

    deepseek.available = False
    deepseek.unavailable_reason = "额度不足（HTTP 402）"
    summary = session.get_summary_for_agent()

    assert "DeepSeek 搜索: 不可用" in summary
    assert "额度不足" in summary
    available_line = next(line for line in summary.splitlines() if line.startswith("可用引擎:"))
    assert "deepseek" not in available_line


def test_build_package_passes_deepseek_config() -> None:
    package = build_web_search_package(
        deepseek_enabled=True,
        deepseek_api_key="sk",
        deepseek_base_url="https://ds.example",
        deepseek_timeout_seconds=9.0,
        deepseek_quota_cooldown_seconds=120,
    )

    assert package is not None
    executor = package.executor.__self__
    assert executor._deepseek_enabled is True
    assert executor._deepseek_api_key == "sk"
    assert executor._deepseek_base_url == "https://ds.example"
    assert executor._deepseek_timeout_seconds == 9.0
    assert executor._deepseek_quota_cooldown_seconds == 120
