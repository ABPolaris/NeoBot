"""BrowserSkill 进入B站页面时的风险提示测试。"""

from __future__ import annotations

import json

import pytest

from neobot_app.skills.browser_skill import _BILIBILI_WARNING, BrowserSkill, _bilibili_warning

_BILIBILI_URL = "https://www.bilibili.com/video/BV1xx411c7mD"
_EXPECTED = (
    "模拟浏览器操作不带B站cookie且可能触发风控,请不要使用浏览器操作来访问B站; "
    "请使用B站专用工具（bilibili_search/bilibili_video 等）。"
)


class FakeBrowser:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result if result is not None else {"success": True}
        self.calls: list[tuple] = []

    async def list_tabs(self):
        return []

    async def new_tab(self, url=None):
        self.calls.append(("new_tab", url))
        return self.result

    async def navigate(self, url):
        self.calls.append(("navigate", url))
        return self.result


def _skill(browser: FakeBrowser) -> BrowserSkill:
    return BrowserSkill(browser_instance=browser)


def test_warning_text_is_exact() -> None:
    assert _BILIBILI_WARNING == _EXPECTED


def test_instructions_include_bilibili_warning() -> None:
    assert _EXPECTED in BrowserSkill().instructions


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://m.bilibili.com/video/BV1xx411c7mD",
        "https://b23.tv/abc",
        "bilibili.com/video/BV1xx411c7mD",
    ],
)
def test_bilibili_hosts_are_detected(url: str) -> None:
    assert _bilibili_warning(url) == {"warning": _EXPECTED}


@pytest.mark.parametrize(
    "url",
    ["https://example.com/", "https://notbilibili.com/", "https://bilibili.com.evil.com/"],
)
def test_non_bilibili_hosts_are_ignored(url: str) -> None:
    assert _bilibili_warning(url) == {}


async def test_open_bilibili_url_returns_warning() -> None:
    browser = FakeBrowser({"success": True, "url": _BILIBILI_URL, "title": "B站"})
    result = json.loads(await _skill(browser).execute("open", {"url": _BILIBILI_URL}))

    assert result["ok"] is True
    assert result["warning"] == _EXPECTED
    assert browser.calls == [("new_tab", _BILIBILI_URL)]


async def test_open_non_bilibili_url_has_no_warning() -> None:
    browser = FakeBrowser({"success": True, "url": "https://example.com/", "title": "示例"})
    raw = await _skill(browser).execute("open", {"url": "https://example.com/"})
    result = json.loads(raw)

    assert result["ok"] is True
    assert "warning" not in result
    assert "B站" not in raw


async def test_open_final_url_redirected_to_bilibili_returns_warning() -> None:
    browser = FakeBrowser({"success": True, "url": _BILIBILI_URL, "title": "x"})
    result = json.loads(
        await _skill(browser).execute("open", {"url": "https://example.com/go"})
    )

    assert result["url"] == _BILIBILI_URL
    assert result["warning"] == _EXPECTED


async def test_navigate_bilibili_url_returns_warning() -> None:
    browser = FakeBrowser({"success": True, "url": _BILIBILI_URL, "title": "B站"})
    result = json.loads(await _skill(browser).execute("navigate", {"url": _BILIBILI_URL}))

    assert result["ok"] is True
    assert result["warning"] == _EXPECTED
    assert browser.calls == [("navigate", _BILIBILI_URL)]


async def test_navigate_non_bilibili_url_has_no_warning() -> None:
    browser = FakeBrowser({"success": True, "url": "https://example.com/", "title": "示例"})
    result = json.loads(
        await _skill(browser).execute("navigate", {"url": "https://example.com/"})
    )

    assert result["ok"] is True
    assert "warning" not in result


async def test_navigate_final_url_redirected_to_b23_returns_warning() -> None:
    browser = FakeBrowser({"success": True, "url": "https://b23.tv/abc", "title": "x"})
    result = json.loads(
        await _skill(browser).execute("navigate", {"url": "https://example.com/go"})
    )

    assert result["url"] == "https://b23.tv/abc"
    assert result["warning"] == _EXPECTED


async def test_new_tab_bilibili_url_returns_warning() -> None:
    browser = FakeBrowser({"success": True, "url": _BILIBILI_URL, "title": "B站"})
    result = json.loads(await _skill(browser).execute("new_tab", {"url": _BILIBILI_URL}))

    assert result["ok"] is True
    assert result["warning"] == _EXPECTED
