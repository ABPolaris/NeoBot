"""模板契约测试：模板占位符必须与渲染端提供的键严格对齐。

这些测试在保护『模板作者』与『渲染代码』之间的接口：任何一侧改了键名，
这里会先失败，而不是等到运行时渲染出一张缺字段的卡片。
"""

from __future__ import annotations

import pathlib

import pytest

from render import STYLE_ORDER

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / 'templates'

#: CardRenderer.render_card 会提供的键（render.py 的 values 字典 + font_style）
CARD_KEYS = {
    'font_style',
    'host_label',
    'cover_src',
    'avatar_src',
    'status_text',
    'name',
    'title',
    'area_name',
    'online_text',
    'live_time_text',
    'room_id',
}

#: CardRenderer.render_overview 会提供的键
OVERVIEW_KEYS = {'font_style', 'title_text', 'summary_text', 'grid'}


def placeholders(path: pathlib.Path) -> set[str]:
    """扫描 {{key}}，与 render._fill 的解析方式保持一致。"""
    text = path.read_text(encoding='utf-8')
    keys: set[str] = set()
    index = 0
    while True:
        start = text.find('{{', index)
        if start < 0:
            break
        end = text.find('}}', start + 2)
        if end < 0:
            break
        keys.add(text[start + 2 : end].strip())
        index = end + 2
    return keys


@pytest.mark.parametrize('style', STYLE_ORDER)
def test_every_style_ships_card_and_overview(style):
    assert (TEMPLATES / ('card_' + style + '.html')).is_file()
    assert (TEMPLATES / ('overview_' + style + '.html')).is_file()


@pytest.mark.parametrize('style', STYLE_ORDER)
def test_card_templates_use_only_known_placeholders(style):
    keys = placeholders(TEMPLATES / ('card_' + style + '.html'))
    unknown = keys - CARD_KEYS
    assert not unknown, f'card_{style}.html 用了渲染端不提供的占位符: {sorted(unknown)}'
    assert 'font_style' in keys
    assert 'name' in keys


@pytest.mark.parametrize('style', STYLE_ORDER)
def test_overview_templates_use_only_known_placeholders(style):
    keys = placeholders(TEMPLATES / ('overview_' + style + '.html'))
    unknown = keys - OVERVIEW_KEYS
    assert not unknown, f'overview_{style}.html 用了渲染端不提供的占位符: {sorted(unknown)}'
    assert 'grid' in keys


@pytest.mark.parametrize('style', STYLE_ORDER)
def test_templates_define_the_selector_root(style):
    # 渲染端用 element 模式截取 .card，模板必须提供这个根元素
    for name in ('card_' + style + '.html', 'overview_' + style + '.html'):
        text = (TEMPLATES / name).read_text(encoding='utf-8')
        assert 'class="card"' in text, name + ' 缺少 .card 根元素'


@pytest.mark.parametrize('style', STYLE_ORDER)
def test_templates_are_self_contained(style):
    # 截图端口用 CDP setDocumentContent 写进 about:blank，外部资源一律加载不到，
    # 所以模板里不允许出现外链或脚本。
    for name in ('card_' + style + '.html', 'overview_' + style + '.html'):
        text = (TEMPLATES / name).read_text(encoding='utf-8').lower()
        assert '<script' not in text, name + ' 不允许包含脚本'
        assert 'http://' not in text and 'https://' not in text, name + ' 不允许外链资源'
        assert '@import' not in text, name + ' 不允许 CSS @import'


def test_bundled_font_matches_template_font_family():
    fonts = sorted((TEMPLATES / 'fonts').glob('*.woff2'))
    assert fonts, 'templates/fonts 下应有随包分发的中文字体'
    stack = (TEMPLATES / 'card_sakura.html').read_text(encoding='utf-8')
    assert '"FusionPixel"' in stack


def test_font_license_is_shipped():
    # 字体是 OFL 许可的第三方资源，必须随包附带许可文本
    licenses = list((TEMPLATES / 'fonts').glob('*LICENSE*')) + list(
        (TEMPLATES / 'fonts').glob('*OFL*')
    )
    assert licenses, '字体许可文本缺失'

