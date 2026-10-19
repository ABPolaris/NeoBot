"""渲染层测试：风格选择、文案格式化、模板填充与降级。"""

from __future__ import annotations

from datetime import datetime

import pytest

from bilibili.models import CST
from render import (
    STYLE_ORDER,
    CardRenderer,
    _fill,
    format_live_time,
    format_online,
    host_fragment,
    resolve_style,
)


class TestStyleSelection:
    def test_random_returns_registered_style(self):
        for _ in range(20):
            assert resolve_style("random") in STYLE_ORDER

    def test_empty_and_unknown_fall_back_to_random(self):
        assert resolve_style(None) in STYLE_ORDER
        assert resolve_style("") in STYLE_ORDER
        assert resolve_style("不存在的风格") in STYLE_ORDER

    @pytest.mark.parametrize("name", STYLE_ORDER)
    def test_known_style_is_preserved(self, name):
        assert resolve_style(name) == name
        assert resolve_style(name.upper()) == name


class TestFormatting:
    def test_online_below_ten_thousand(self):
        assert format_online(0) == "0人气"
        assert format_online(9999) == "9999人气"

    def test_online_above_ten_thousand(self):
        assert format_online(12345) == "1.2万人气"
        assert format_online(1000000) == "100.0万人气"

    def test_live_time_none_is_empty(self):
        assert format_live_time(None) == ""

    def test_live_time_today(self):
        now = datetime(2026, 9, 26, 22, 0, tzinfo=CST)
        value = datetime(2026, 9, 26, 20, 30, tzinfo=CST)
        assert format_live_time(value, now=now) == "今天 20:30 开播"

    def test_live_time_yesterday(self):
        now = datetime(2026, 9, 26, 1, 0, tzinfo=CST)
        value = datetime(2026, 9, 25, 20, 30, tzinfo=CST)
        assert format_live_time(value, now=now) == "昨天 20:30 开播"

    def test_live_time_older(self):
        now = datetime(2026, 9, 26, 12, 0, tzinfo=CST)
        value = datetime(2026, 9, 20, 20, 30, tzinfo=CST)
        assert format_live_time(value, now=now) == "09-20 20:30 开播"


class TestFill:
    def test_escapes_user_text(self):
        out = _fill("<p>{{name}}</p>", {"name": "<script>alert(1)</script>"})
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_raw_keys_are_inserted_verbatim(self):
        out = _fill("<div>{{grid}}</div>", {"grid": "<b>x</b>"}, raw_keys=("grid",))
        assert out == "<div><b>x</b></div>"

    def test_missing_key_becomes_empty(self):
        assert _fill("[{{nothing}}]", {}) == "[]"

    def test_unterminated_placeholder_is_kept(self):
        assert _fill("a {{name", {}) == "a {{name"


class TestHostFragment:
    def test_live_host_has_ring_class_and_badge(self):
        html = host_fragment(avatar_src="data:image/png;base64,AA", name="主播", live=True)
        assert "host host-live" in html
        assert "host-badge" in html
        assert "LIVE" in html

    def test_offline_host_has_no_badge(self):
        html = host_fragment(avatar_src="", name="主播", live=False)
        assert "host-live" not in html
        assert "host-badge" not in html
        assert "host-placeholder" in html

    def test_name_is_escaped(self):
        html = host_fragment(avatar_src="", name="<b>x</b>", live=False)
        assert "<b>" not in html


class FakeResult:
    def __init__(self, data: bytes) -> None:
        self.data = data


class FakePort:
    """记录每次渲染的 HTML，并按脚本返回结果或抛异常。"""

    def __init__(self, *, fail_first: bool = False, data: bytes = b"PNG") -> None:
        self.calls: list[str] = []
        self.options: list[object] = []
        self._fail_first = fail_first
        self._data = data

    async def render(self, *, html, options, base_url=None):
        self.calls.append(html)
        self.options.append(options)
        if self._fail_first and len(self.calls) == 1:
            raise RuntimeError("FontLoadError 模拟")
        return FakeResult(self._data)


@pytest.fixture
def templates(tmp_path):
    (tmp_path / "fonts").mkdir()
    (tmp_path / "card_sakura.html").write_text(
        "<html><body><main class=\"card\">{{name}}/{{status_text}}/{{online_text}}</main></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "overview_sakura.html").write_text(
        "<html><body><main class=\"card\">{{title_text}}{{summary_text}}{{grid}}</main></body></html>",
        encoding="utf-8",
    )
    return tmp_path


class TestCardRenderer:
    async def test_render_card_fills_template(self, templates):
        port = FakePort()
        renderer = CardRenderer(templates, screenshots_provider=lambda: port)
        data = await renderer.render_card(
            {"name": "主播A", "status_text": "开播了", "online_text": "1.2万人气"},
            style="sakura",
        )
        assert data == b"PNG"
        assert "主播A" in port.calls[0]
        assert "开播了" in port.calls[0]

    async def test_missing_template_returns_none(self, templates):
        renderer = CardRenderer(templates, screenshots_provider=lambda: FakePort())
        assert await renderer.render_card({}, style="neon") is None

    async def test_port_absent_returns_none(self, templates):
        renderer = CardRenderer(templates, screenshots_provider=lambda: None)
        assert await renderer.render_card({"name": "x"}, style="sakura") is None

    async def test_font_load_failure_falls_back_to_no_wait(self, templates):
        port = FakePort(fail_first=True)
        renderer = CardRenderer(templates, screenshots_provider=lambda: port)
        data = await renderer.render_card({"name": "x"}, style="sakura")
        assert data == b"PNG"
        assert len(port.calls) == 2
        assert port.options[0].wait_for_fonts is True
        assert port.options[1].wait_for_fonts is False

    async def test_all_attempts_failing_returns_none(self, templates):
        port = FakePort(fail_first=True)
        renderer = CardRenderer(templates, screenshots_provider=lambda: port)
        data = await renderer.render_card({"name": "x"}, style="sakura")
        assert data is not None

        class AlwaysFail:
            async def render(self, *, html, options, base_url=None):
                raise RuntimeError("boom")

        renderer2 = CardRenderer(templates, screenshots_provider=lambda: AlwaysFail())
        assert await renderer2.render_card({"name": "x"}, style="sakura") is None

    async def test_render_overview_builds_grid(self, templates):
        port = FakePort()
        renderer = CardRenderer(templates, screenshots_provider=lambda: port)
        data = await renderer.render_overview(
            [
                {"avatar_src": "data:image/png;base64,AA", "name": "甲", "live": True},
                {"avatar_src": "", "name": "乙", "live": False},
            ],
            style="sakura",
            title_text="本群关注的主播",
            summary_text="共 2 位，1 位正在直播",
        )
        assert data == b"PNG"
        html = port.calls[0]
        assert "本群关注的主播" in html
        assert "host-live" in html
        assert "甲" in html and "乙" in html

    async def test_provider_is_called_each_render(self, templates):
        calls = {"n": 0}

        def provider():
            calls["n"] += 1
            return FakePort()

        renderer = CardRenderer(templates, screenshots_provider=provider)
        await renderer.render_card({"name": "x"}, style="sakura")
        await renderer.render_card({"name": "x"}, style="sakura")
        assert calls["n"] == 2

    def test_default_selector_targets_card_root(self, templates):
        renderer = CardRenderer(templates, screenshots_provider=lambda: None)
        assert renderer.selector == ".card"

