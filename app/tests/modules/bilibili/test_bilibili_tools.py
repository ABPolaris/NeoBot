"""B站工具 Skill 测试：搜索映射、视频信息/看视频/评论、用户与关联QQ主页。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from neobot_app.skills.base import SkillManager
from neobot_app.skills.bilibili_tools import (
    BilibiliSearchSkill,
    BilibiliUserSkill,
    BilibiliVideoSkill,
)

_VIDEO_URL = "https://www.bilibili.com/video/BV1xx411c7mD"


class FakeClient:
    def __init__(self) -> None:
        self.search_calls: list[tuple] = []
        self.info_calls: list[str] = []
        self.comment_calls: list[tuple] = []
        self.video_calls: list[int] = []

    async def search(self, keyword, *, search_type="video", page=1):
        # 与真实 BilibiliClient 一致：当前不支持 order。
        self.search_calls.append((keyword, search_type, page))
        if search_type == "video":
            return {
                "result": [
                    {
                        "type": "video",
                        "title": '<em class="keyword">猫</em>片 &amp; 日常',
                        "bvid": "BV1xx411c7mD",
                        "author": "UP主",
                        "play": 12345,
                        "arcurl": _VIDEO_URL,
                        "description": "视频简介",
                    }
                ],
                "numResults": 1,
            }
        if search_type == "bili_user":
            return {
                "result": [
                    {
                        "type": "bili_user",
                        "mid": 42,
                        "uname": "阿猫",
                        "usign": "签名",
                        "fans": 99,
                        "videos": 8,
                        "level": 6,
                    }
                ]
            }
        return {
            "result": [
                {
                    "type": "article",
                    "id": 777,
                    "title": "专栏标题",
                    "desc": "专栏摘要",
                    "view": 321,
                    "author": "作者",
                }
            ]
        }

    async def get_video_info(self, identifier):
        self.info_calls.append(identifier)
        return {
            "bvid": "BV1xx411c7mD",
            "aid": 170001,
            "title": "视频标题",
            "owner": {"name": "UP主", "mid": 5},
            "stat": {"view": 1000, "like": 20},
            "duration": 61,
            "desc": "视频简介",
            "url": _VIDEO_URL,
        }

    async def get_video_comments(self, oid, page=1, order="hot"):
        self.comment_calls.append((oid, page, order))
        return [
            {
                "rpid": 9,
                "member": {"uname": "甲", "mid": 2},
                "content": {"message": "好看"},
                "like": 3,
                "ctime": 1_700_000_000,
                "rcount": 2,
            }
        ]

    async def get_space_info(self, mid):
        self.video_calls.append(int(mid))
        return {
            "mid": mid,
            "name": "UP主",
            "sign": "签名",
            "level": 6,
            "fans": 100,
            "following": 3,
        }

    async def get_user_videos(self, mid, page=1):
        return {
            "result": [
                {"bvid": "BV1xx411c7mD", "title": "投稿", "author": "UP主", "play": 5}
            ]
        }


class OrderedSearchClient(FakeClient):
    """未来签名：search 支持 order 时应该原样转发。"""

    async def search(self, keyword, *, search_type="video", page=1, order=None):
        self.search_calls.append((keyword, search_type, page, order))
        return {"result": []}


class LegacyClient(FakeClient):
    """并行实现的旧签名：get_user_videos 只接受 mid。"""

    async def get_user_videos(self, mid):
        return [{"bvid": "BV1xx411c7mD", "title": "投稿", "author": "UP主", "play": 5}]


class FakeVideoService:
    def __init__(self, result=None) -> None:
        self.result = result or {}
        self.calls: list[tuple] = []

    async def watch(self, identifier, max_chars=6000, include_comments=0):
        self.calls.append((identifier, max_chars, include_comments))
        return self.result


class FakeArchive:
    def __init__(self, entries=None) -> None:
        self.entries = dict(entries or {})

    async def get(self, table, key):
        value = self.entries.get((table, key))
        return SimpleNamespace(value=value) if value is not None else None


class FakeLinks:
    def __init__(self, links=None) -> None:
        self.links = list(links or [])

    async def find_by_uid(self, uid):
        return [link for link in self.links if link.bilibili_uid == uid]


class FakeProfiles:
    def __init__(self, users=None) -> None:
        self.users = dict(users or {})

    async def get_user(self, user_id):
        return self.users.get(str(user_id))


class FakeUow:
    def __init__(self, links=None, profiles=None) -> None:
        self.bilibili_links = links or FakeLinks()
        self.profiles = profiles or FakeProfiles()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _uow_factory(links=None, profiles=None):
    def make():
        return FakeUow(links=links, profiles=profiles)

    return make


def _service(client) -> SimpleNamespace:
    return SimpleNamespace(available=True, client=client)


# ── 搜索 ──


async def test_search_video_maps_and_cleans_fields() -> None:
    client = FakeClient()
    skill = BilibiliSearchSkill(service=_service(client))

    raw = await skill.execute("search_video", {"keyword": "猫", "order": "click"})
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["total"] == 1
    item = result["items"][0]
    assert item["title"] == "猫片 & 日常"
    assert item["bvid"] == "BV1xx411c7mD"
    assert item["up"] == "UP主"
    assert item["play"] == 12345
    assert item["url"] == _VIDEO_URL
    assert item["desc"] == "视频简介"
    # order 当前不在真实 client.search 签名里：过滤掉而不是 TypeError。
    assert client.search_calls == [("猫", "video", 1)]
    assert "猫片" in raw  # ensure_ascii=False


async def test_search_video_forwards_order_when_client_supports_it() -> None:
    client = OrderedSearchClient()
    skill = BilibiliSearchSkill(client=client)

    await skill.execute("search_video", {"keyword": "猫", "order": "click"})

    assert client.search_calls == [("猫", "video", 1, "click")]


async def test_search_user_and_article() -> None:
    skill = BilibiliSearchSkill(client=FakeClient())

    user = json.loads(await skill.execute("search_user", {"keyword": "猫"}))["items"][0]
    article = json.loads(
        await skill.execute("search_article", {"keyword": "猫"})
    )["items"][0]

    assert user["uid"] == 42
    assert user["name"] == "阿猫"
    assert user["fans"] == 99
    assert user["url"] == "https://space.bilibili.com/42"
    assert article["cv_id"] == 777
    assert article["url"] == "https://www.bilibili.com/read/cv777"


async def test_search_requires_keyword_and_valid_order() -> None:
    skill = BilibiliSearchSkill(client=FakeClient())

    missing = json.loads(await skill.execute("search_video", {}))
    bad_order = json.loads(
        await skill.execute("search_video", {"keyword": "猫", "order": "nope"})
    )

    assert missing["ok"] is False and "keyword" in missing["error"]
    assert bad_order["ok"] is False and "totalrank" in bad_order["error"]


async def test_dormant_service_returns_readable_error() -> None:
    skill = BilibiliSearchSkill(service=SimpleNamespace(available=False, client=None))

    result = json.loads(await skill.execute("search_video", {"keyword": "猫"}))

    assert result["ok"] is False
    assert "未登录" in result["error"]


def test_no_dependency_means_no_tools() -> None:
    assert BilibiliSearchSkill().get_tools() == []
    assert BilibiliVideoSkill().get_tools() == []
    assert BilibiliUserSkill().get_tools() == []


# ── 视频 ──


async def test_video_info_accepts_url() -> None:
    client = FakeClient()
    skill = BilibiliVideoSkill(client=client)

    result = json.loads(await skill.execute("video_info", {"identifier": _VIDEO_URL}))

    assert result["ok"] is True
    assert result["bvid"] == "BV1xx411c7mD"
    assert result["aid"] == 170001
    assert result["up"] == "UP主"
    assert result["duration"] == 61
    assert result["view"] == 1000
    assert client.info_calls == ["BV1xx411c7mD"]


async def test_watch_video_returns_transcript_and_source() -> None:
    video = FakeVideoService(
        {
            "ok": True,
            "bvid": "BV1xx411c7mD",
            "title": "视频标题",
            "owner": "UP主",
            "duration": 61,
            "desc": "简介",
            "url": _VIDEO_URL,
            "source": "asr",
            "transcript": "这是语音识别转写",
            "comments": [],
        }
    )
    skill = BilibiliVideoSkill(video_service=video)

    result = json.loads(
        await skill.execute(
            "watch_video", {"identifier": "BV1xx411c7mD", "max_chars": 1500}
        )
    )

    assert result["ok"] is True
    assert result["source"] == "asr"
    assert result["transcript"] == "这是语音识别转写"
    assert result["title"] == "视频标题"
    assert video.calls == [("BV1xx411c7mD", 1500, 0)]


async def test_watch_video_without_service_and_with_error() -> None:
    no_service = BilibiliVideoSkill(client=FakeClient())
    missing = json.loads(
        await no_service.execute("watch_video", {"identifier": "BV1xx411c7mD"})
    )
    assert missing["ok"] is False and "视频解析服务未配置" in missing["error"]

    failing = BilibiliVideoSkill(
        video_service=FakeVideoService({"ok": False, "error": "没有字幕"})
    )
    failed = json.loads(
        await failing.execute("watch_video", {"identifier": "BV1xx411c7mD"})
    )
    assert failed["ok"] is False and "没有字幕" in failed["error"]


async def test_video_comments_resolves_bv_to_aid() -> None:
    client = FakeClient()
    skill = BilibiliVideoSkill(client=client)

    result = json.loads(
        await skill.execute("video_comments", {"identifier": _VIDEO_URL})
    )

    assert result["ok"] is True
    assert result["oid"] == 170001
    assert result["count"] == 1
    assert result["comments"][0]["uname"] == "甲"
    assert result["comments"][0]["content"] == "好看"
    assert client.comment_calls == [(170001, 1, "hot")]


async def test_video_comments_accepts_av_number_without_extra_lookup() -> None:
    client = FakeClient()
    skill = BilibiliVideoSkill(client=client)

    result = json.loads(
        await skill.execute("video_comments", {"identifier": "av170001", "order": "time"})
    )

    assert result["oid"] == 170001
    assert client.info_calls == []
    assert client.comment_calls == [(170001, 1, "time")]


# ── 用户 ──


async def test_user_info_and_legacy_user_videos() -> None:
    client = LegacyClient()
    skill = BilibiliUserSkill(client=client)

    info = json.loads(
        await skill.execute("user_info", {"uid": "https://space.bilibili.com/42"})
    )
    videos = json.loads(
        await skill.execute("user_videos", {"uid": "42", "page": 2})
    )

    assert info["ok"] is True
    assert info["uid"] == 42
    assert info["name"] == "UP主"
    assert info["fans"] == 100
    assert videos["ok"] is True
    assert videos["videos"][0]["title"] == "投稿"
    assert videos["page"] == 2


async def test_linked_qq_profile_returns_qq_data() -> None:
    links = FakeLinks([SimpleNamespace(bilibili_uid=42, qq_number="10001")])
    profiles = FakeProfiles(
        {"10001": SimpleNamespace(nick_name="小王", remark="老友", profile="常聊天", known_gender="男")}
    )
    archive = FakeArchive(
        {("user_summary", "10001"): "QQ摘要", ("user_profile", "10001"): "完整档案"}
    )
    skill = BilibiliUserSkill(
        client=FakeClient(),
        archive_service=archive,
        uow_factory=_uow_factory(links, profiles),
    )

    result = json.loads(await skill.execute("linked_qq_profile", {"uid": 42}))

    assert result["ok"] is True
    assert result["linked"] is True
    assert result["qq_numbers"] == ["10001"]
    profile = result["profiles"][0]
    assert profile["remark"] == "老友"
    assert profile["nick_name"] == "小王"
    assert profile["user_summary"] == "QQ摘要"
    assert profile["user_profile"] == "完整档案"


async def test_linked_qq_profile_not_linked() -> None:
    skill = BilibiliUserSkill(
        client=FakeClient(),
        uow_factory=_uow_factory(FakeLinks(), FakeProfiles()),
    )

    result = json.loads(await skill.execute("linked_qq_profile", {"uid": 42}))

    assert result["ok"] is True
    assert result["linked"] is False
    assert "还没有关联" in result["message"]


async def test_linked_qq_profile_requires_store() -> None:
    skill = BilibiliUserSkill(client=FakeClient())

    result = json.loads(await skill.execute("linked_qq_profile", {"uid": 42}))

    assert result["ok"] is False
    assert "未配置" in result["error"]


async def test_unknown_tool() -> None:
    result = json.loads(await BilibiliSearchSkill(client=FakeClient()).execute("nope", {}))

    assert result["ok"] is False


async def test_three_skills_register_and_route_together() -> None:
    manager = SkillManager()
    client = FakeClient()
    manager.register(BilibiliSearchSkill(client=client))
    manager.register(BilibiliVideoSkill(client=client))
    manager.register(BilibiliUserSkill(client=client))

    names = {tool["function"]["name"] for tool in manager.get_tools()}
    assert {
        "bilibili_search__search_video",
        "bilibili_video__watch_video",
        "bilibili_user__linked_qq_profile",
    } <= names

    result = json.loads(
        await manager.execute("bilibili_user__user_info", {"uid": "42"})
    )
    assert result["ok"] is True
    assert result["uid"] == 42


async def test_read_video_transcript_from_archive() -> None:
    archive = FakeArchive(
        {("bilibili_video", "BV1xx411c7mD"): "完整转写" * 1000}
    )
    skill = BilibiliVideoSkill(client=FakeClient(), archive_service=archive)
    tools = {tool["function"]["name"] for tool in skill.get_tools()}
    assert "read_video_transcript" in tools

    result = json.loads(
        await skill.execute("read_video_transcript", {"identifier": "BV1xx411c7mD"})
    )
    assert result["ok"] is True
    assert result["found"] is True
    assert result["transcript"] == "完整转写" * 1000


async def test_read_video_transcript_not_found() -> None:
    skill = BilibiliVideoSkill(client=FakeClient(), archive_service=FakeArchive())
    result = json.loads(
        await skill.execute("read_video_transcript", {"identifier": "BV1xx411c7mD"})
    )
    assert result["ok"] is True
    assert result["found"] is False
    assert "没有找到" in result["message"]


# ── 写操作（评论 / 点赞）──


class WriteClient(FakeClient):
    """模拟并行实现的写接口；result=False 表示失败，error 模拟抛异常。"""

    def __init__(self, result: bool = True, error: Exception | None = None) -> None:
        super().__init__()
        self.result = result
        self.error = error
        self.comment_calls: list[tuple] = []
        self.like_calls: list[str] = []
        self.like_comment_calls: list[tuple] = []

    async def post_video_comment(self, identifier, text, *, root=0, parent=0):
        self.comment_calls.append((identifier, text, root, parent))
        if self.error is not None:
            raise self.error
        return self.result

    async def like_video(self, identifier):
        self.like_calls.append(identifier)
        if self.error is not None:
            raise self.error
        return self.result

    async def like_comment(self, oid, rpid, type_=1):
        self.like_comment_calls.append((oid, rpid, type_))
        if self.error is not None:
            raise self.error
        return self.result


def test_video_skill_exposes_write_tools_with_public_action_warning() -> None:
    tools = {
        tool["function"]["name"]: tool["function"]
        for tool in BilibiliVideoSkill(client=FakeClient()).get_tools()
    }

    assert {"comment_video", "like_video", "like_comment"} <= set(tools)
    assert "确认用户意图后再调用" in tools["comment_video"]["description"]
    assert "确认用户意图后再调用" in tools["like_video"]["description"]
    assert "确认用户意图后再调用" in tools["like_comment"]["description"]


async def test_comment_video_posts_top_level_and_reply() -> None:
    client = WriteClient()
    skill = BilibiliVideoSkill(client=client)

    top = json.loads(
        await skill.execute("comment_video", {"identifier": _VIDEO_URL, "text": "好看"})
    )
    reply = json.loads(
        await skill.execute(
            "comment_video",
            {"identifier": "av170001", "text": "同意", "root": 9, "parent": 10},
        )
    )

    assert top["ok"] is True
    assert top["comment"] == "好看"
    assert top["root"] == 0 and top["parent"] == 0
    assert reply["ok"] is True
    assert reply["root"] == 9 and reply["parent"] == 10
    assert client.comment_calls == [
        ("BV1xx411c7mD", "好看", 0, 0),
        ("av170001", "同意", 9, 10),
    ]
    assert "好看" in json.dumps(top, ensure_ascii=False)


async def test_comment_video_failure_and_missing_capability() -> None:
    failing = BilibiliVideoSkill(client=WriteClient(result=False))
    failed = json.loads(
        await failing.execute("comment_video", {"identifier": "av170001", "text": "好"})
    )
    assert failed["ok"] is False
    assert "风控" in failed["error"]

    missing_text = json.loads(
        await failing.execute("comment_video", {"identifier": "av170001"})
    )
    assert missing_text["ok"] is False and "text" in missing_text["error"]

    legacy = BilibiliVideoSkill(client=FakeClient())
    missing = json.loads(
        await legacy.execute("comment_video", {"identifier": "av170001", "text": "好"})
    )
    assert missing["ok"] is False
    assert "post_video_comment" in missing["error"]


async def test_like_video_and_like_comment() -> None:
    client = WriteClient()
    skill = BilibiliVideoSkill(client=client)

    video = json.loads(await skill.execute("like_video", {"identifier": _VIDEO_URL}))
    comment = json.loads(
        await skill.execute("like_comment", {"identifier": "av170001", "rpid": "9"})
    )
    resolved = json.loads(
        await skill.execute("like_comment", {"identifier": _VIDEO_URL, "rpid": 9})
    )

    assert video["ok"] is True and video["liked"] is True
    assert client.like_calls == ["BV1xx411c7mD"]
    assert comment["ok"] is True
    assert comment["oid"] == 170001 and comment["rpid"] == 9
    assert resolved["oid"] == 170001
    assert client.like_comment_calls == [(170001, 9, 1), (170001, 9, 1)]


async def test_like_failure_missing_rpid_and_missing_capability() -> None:
    failing = BilibiliVideoSkill(client=WriteClient(result=False))
    video = json.loads(
        await failing.execute("like_video", {"identifier": "av170001"})
    )
    comment = json.loads(
        await failing.execute("like_comment", {"identifier": "av170001", "rpid": 9})
    )
    assert video["ok"] is False and "风控" in video["error"]
    assert comment["ok"] is False and "风控" in comment["error"]

    bad_rpid = json.loads(
        await failing.execute("like_comment", {"identifier": "av170001", "rpid": 0})
    )
    assert bad_rpid["ok"] is False and "rpid" in bad_rpid["error"]

    legacy = BilibiliVideoSkill(client=FakeClient())
    missing = json.loads(await legacy.execute("like_video", {"identifier": "av170001"}))
    missing_comment = json.loads(
        await legacy.execute("like_comment", {"identifier": "av170001", "rpid": 9})
    )
    assert missing["ok"] is False and "like_video" in missing["error"]
    assert missing_comment["ok"] is False and "like_comment" in missing_comment["error"]

