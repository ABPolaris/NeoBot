"""Cookie 持久化：保存/读取/损坏容忍/清除。"""

from __future__ import annotations

import json

import pytest

from neobot_app.bilibili.cookie_provider import BilibiliCookieStore

_COOKIES = {
    "SESSDATA": "sess-value",
    "bili_jct": "jct-value",
    "DedeUserID": "12345",
    "buvid3": "buvid-value",
}


def test_save_then_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "bilibili" / "cookies.json"
    store = BilibiliCookieStore(path)
    store.save(_COOKIES, refresh_token="refresh-token")

    assert path.is_file()
    loaded = store.load()
    assert loaded is not None
    assert {key: loaded[key] for key in _COOKIES} == _COOKIES
    assert loaded["refresh_token"] == "refresh-token"
    assert loaded["saved_at"].isdigit()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["saved_at"].isdigit()
    assert raw["DedeUserID"] == "12345"


def test_load_missing_file_returns_none(tmp_path) -> None:
    assert BilibiliCookieStore(tmp_path / "cookies.json").load() is None


def test_load_corrupt_json_returns_none(tmp_path) -> None:
    path = tmp_path / "cookies.json"
    path.write_text("{not json", encoding="utf-8")
    assert BilibiliCookieStore(path).load() is None


def test_load_without_required_cookies_returns_none(tmp_path) -> None:
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps({"SESSDATA": "only-sessdata"}), encoding="utf-8")
    assert BilibiliCookieStore(path).load() is None


def test_save_requires_sessdata_and_bili_jct(tmp_path) -> None:
    store = BilibiliCookieStore(tmp_path / "cookies.json")
    with pytest.raises(ValueError):
        store.save({"SESSDATA": "s"})
    assert not (tmp_path / "cookies.json").exists()


def test_clear_removes_file(tmp_path) -> None:
    path = tmp_path / "cookies.json"
    store = BilibiliCookieStore(path)
    store.save(_COOKIES)
    store.clear()
    assert not path.exists()
    store.clear()  # 重复清除不抛错
