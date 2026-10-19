"""OneBot 通用动作 Skill 测试：文档生成、参数透传、安全门控与错误映射。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from neobot_app.skills.base import SkillManager
from neobot_app.skills.onebot_skill import (
    OneBotSkill,
    action_safety,
    build_action_docs,
    load_action_docs,
    scan_adapter_actions,
)

SNAPSHOT: dict[str, Any] = {
    "source": "napcat-openapi",
    "version": "4.15.18",
    "actions": {
        "get_group_detail_info": {
            "description": "获取群详细信息",
            "tags": ["群组接口"],
            "payload": {
                "type": "object",
                "properties": {"group_id": {"type": "string", "description": "群号"}},
                "required": ["group_id"],
            },
            "response": {"type": "object"},
            "payloadExample": {"group_id": "123456"},
            "errorExamples": [{"retcode": 1401, "message": "权限不足"}],
        },
        "send_poke": {
            "description": "发送戳一戳",
            "tags": ["核心接口"],
            "payload": {
                "type": "object",
                "properties": {"user_id": {"type": "string", "description": "用户QQ"}},
                "required": ["user_id"],
            },
            "payloadExample": {"user_id": "123456789"},
        },
        "set_group_kick": {
            "description": "群组踢人",
            "tags": ["群组接口"],
            "payload": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "user_id": {"type": "string"},
                },
                "required": ["group_id", "user_id"],
            },
        },
        "create_group_album": {
            "description": "创建群相册（未封装）",
            "tags": ["群组接口"],
            "payload": {"type": "object", "properties": {"group_id": {"type": "string"}}},
        },
    },
}

ADAPTER_ACTIONS = {"get_group_detail_info", "send_group_msg"}


def _docs() -> dict[str, Any]:
    return build_action_docs(snapshot=SNAPSHOT, adapter_actions=ADAPTER_ACTIONS)


def _parse(text: str) -> dict[str, Any]:
    return json.loads(text)


_UNSET = object()


class FakeAdapter:
    def __init__(self, response: Any = _UNSET, error: Exception | None = None) -> None:
        self.response = {"status": "ok", "retcode": 0, "data": {}} if response is _UNSET else response
        self.error = error
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    async def call_api(self, action: str, params: dict[str, Any], timeout: float = 5.0, wait_response: bool = True):
        self.calls.append((action, params, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class FakeCredentials:
    def __init__(self, allowed: bool = False) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str, bool]] = []

    def consume(self, *, chat_flow: str, action: str, commit: bool = True):
        self.calls.append((chat_flow, action, commit))
        return SimpleNamespace(action=action, chat_flow=chat_flow) if self.allowed else None


def _skill(
    *,
    adapter: FakeAdapter | None = None,
    credentials: FakeCredentials | None = None,
) -> OneBotSkill:
    return OneBotSkill(
        adapter=adapter if adapter is not None else FakeAdapter(),
        credential_manager=credentials,
        docs=_docs(),
    )


# ── 文档生成 ──


def test_scan_adapter_actions_reads_request_source() -> None:
    actions = scan_adapter_actions()

    assert {"send_group_msg", "get_group_info", "delete_froup_folder"} <= actions


def test_build_action_docs_merges_snapshot_and_adapter() -> None:
    docs = _docs()

    assert docs["catalog"] == "napcat-openapi 4.15.18"
    detail = docs["actions"]["get_group_detail_info"]
    assert detail["source"] == "napcat"
    assert detail["wrapped"] is True
    assert detail["safety"] == "green"
    assert detail["params"]["required"] == ["group_id"]
    assert detail["example"] == {"group_id": "123456"}
    assert detail["errors"] == [{"retcode": 1401, "message": "权限不足"}]

    unwrapped = docs["actions"]["create_group_album"]
    assert unwrapped["wrapped"] is False

    adapter_only = docs["actions"]["send_group_msg"]
    assert adapter_only["source"] == "adapter"
    assert adapter_only["wrapped"] is True
    assert adapter_only["safety"] == "yellow"


def test_load_action_docs_reads_snapshot_file(tmp_path) -> None:
    path = tmp_path / "napcat.json"
    path.write_text(json.dumps(SNAPSHOT, ensure_ascii=False), encoding="utf-8")

    docs = load_action_docs(path)

    assert docs["actions"]["send_poke"]["description"] == "发送戳一戳"


def test_load_action_docs_falls_back_to_adapter_table(tmp_path) -> None:
    docs = load_action_docs(tmp_path / "missing.json")

    assert docs["catalog"] == "adapter"
    assert docs["actions"]["send_group_msg"]["source"] == "adapter"
    assert "get_group_detail_info" not in docs["actions"]
    assert "NEOBOT_ONEBOT_CATALOG" in docs["hint"]


@pytest.mark.parametrize(
    "action, expected",
    [
        ("get_group_detail_info", "green"),
        ("ocr_image", "green"),
        ("_get_group_notice", "green"),
        ("send_poke", "yellow"),
        ("send_group_msg", "yellow"),
        ("set_group_kick", "red"),
        ("_del_group_notice", "red"),
        ("bot_exit", "red"),
        ("some_new_action", "yellow"),
    ],
)
def test_action_safety(action: str, expected: str) -> None:
    assert action_safety(action) == expected


# ── 文档工具 ──


async def test_doc_tool_returns_schema_and_search_results() -> None:
    skill = _skill()

    exact = _parse(await skill.execute("doc", {"action": "send_poke"}))
    assert exact["ok"] is True
    assert exact["action"]["params"]["required"] == ["user_id"]
    assert exact["action"]["example"] == {"user_id": "123456789"}
    assert exact["action"]["wrapped"] is False

    searched = _parse(await skill.execute("doc", {"keyword": "poke"}))
    assert searched["count"] == 1
    assert searched["actions"][0]["name"] == "send_poke"

    listed = _parse(await skill.execute("doc", {}))
    assert listed["count"] == 5


async def test_doc_tool_reports_unknown_action_with_suggestions() -> None:
    skill = _skill()

    result = _parse(await skill.execute("doc", {"action": "send_pok"}))

    assert result["ok"] is False
    assert result["code"] == "UNKNOWN_ACTION"
    assert "send_poke" in result["suggestions"]


def test_tool_schemas_register_under_onebot_prefix() -> None:
    manager = SkillManager()
    manager.register(_skill())

    names = manager.skill_tool_names("onebot")

    assert set(names) == {"onebot__doc", "onebot__call"}


def test_build_all_skills_registers_onebot_when_adapter_present() -> None:
    from neobot_app.skills import build_all_skills

    manager = build_all_skills(adapter=FakeAdapter())

    assert manager.get("onebot") is not None
    assert set(manager.skill_tool_names("onebot")) == {"onebot__doc", "onebot__call"}


# ── 调用与参数透传 ──


async def test_call_passes_action_and_params_verbatim() -> None:
    adapter = FakeAdapter({"status": "ok", "retcode": 0, "data": {"group_name": "测试群"}})
    credentials = FakeCredentials(allowed=False)
    skill = _skill(adapter=adapter, credentials=credentials)

    result = _parse(
        await skill.execute(
            "call",
            {"action": "get_group_detail_info", "params": {"group_id": 123456}},
        )
    )

    assert result["ok"] is True
    assert result["safety"] == "green"
    assert result["wrapped"] is True
    assert result["result"]["data"] == {"group_name": "测试群"}
    assert adapter.calls[0][0] == "get_group_detail_info"
    assert adapter.calls[0][1] == {"group_id": 123456}
    assert credentials.calls == []


async def test_call_allows_unwrapped_action_with_credential() -> None:
    adapter = FakeAdapter()
    credentials = FakeCredentials(allowed=True)
    skill = _skill(adapter=adapter, credentials=credentials)

    result = _parse(
        await skill.execute(
            "call",
            {"action": "create_group_album", "params": {"group_id": 1}, "pipeline_key": "group:1"},
        )
    )

    assert result["ok"] is True
    assert result["wrapped"] is False
    assert adapter.calls[0][0] == "create_group_album"


async def test_call_allows_unknown_action_after_credential_gate() -> None:
    adapter = FakeAdapter()
    credentials = FakeCredentials(allowed=True)
    skill = _skill(adapter=adapter, credentials=credentials)

    result = _parse(
        await skill.execute(
            "call",
            {"action": "some_new_action", "params": {"x": 1}, "pipeline_key": "group:1"},
        )
    )

    assert result["ok"] is True
    assert result["wrapped"] is False
    assert adapter.calls[0][0] == "some_new_action"


# ── 安全门控 ──


async def test_call_rejects_yellow_action_without_credential() -> None:
    adapter = FakeAdapter()
    credentials = FakeCredentials(allowed=False)
    skill = _skill(adapter=adapter, credentials=credentials)

    result = _parse(
        await skill.execute(
            "call",
            {"action": "send_poke", "params": {"user_id": 1}, "pipeline_key": "group:123"},
        )
    )

    assert result["ok"] is False
    assert result["code"] == "CREDENTIAL_REQUIRED"
    assert result["request_tool"] == "credential__request"
    assert result["request_args"]["action"] == "send_poke"
    assert adapter.calls == []
    assert credentials.calls == [("group:123", "send_poke", True)]


async def test_call_allows_yellow_action_with_credential() -> None:
    adapter = FakeAdapter()
    credentials = FakeCredentials(allowed=True)
    skill = _skill(adapter=adapter, credentials=credentials)

    result = _parse(
        await skill.execute(
            "call",
            {"action": "send_poke", "params": {"user_id": 1}, "pipeline_key": "group:123"},
        )
    )

    assert result["ok"] is True
    assert adapter.calls[0][0] == "send_poke"


async def test_call_rejects_red_action_without_credential() -> None:
    adapter = FakeAdapter()
    credentials = FakeCredentials(allowed=False)
    skill = _skill(adapter=adapter, credentials=credentials)

    result = _parse(
        await skill.execute(
            "call",
            {
                "action": "set_group_kick",
                "params": {"group_id": 1, "user_id": 2},
                "pipeline_key": "group:1",
            },
        )
    )

    assert result["code"] == "CREDENTIAL_REQUIRED"
    assert result["safety"] == "red"
    assert adapter.calls == []


async def test_call_rejects_non_green_action_without_session() -> None:
    adapter = FakeAdapter()
    credentials = FakeCredentials(allowed=True)
    skill = _skill(adapter=adapter, credentials=credentials)

    result = _parse(await skill.execute("call", {"action": "send_poke", "params": {"user_id": 1}}))

    assert result["code"] == "SESSION_REQUIRED"
    assert credentials.calls == []
    assert adapter.calls == []


async def test_call_validates_required_params_before_credential_gate() -> None:
    adapter = FakeAdapter()
    credentials = FakeCredentials(allowed=True)
    skill = _skill(adapter=adapter, credentials=credentials)

    result = _parse(
        await skill.execute(
            "call",
            {"action": "send_poke", "params": {}, "pipeline_key": "group:1"},
        )
    )

    assert result["ok"] is False
    assert result["code"] == "INVALID_PARAMS"
    assert "user_id" in result["error"]
    assert credentials.calls == []
    assert adapter.calls == []


async def test_call_rejects_non_object_params() -> None:
    skill = _skill()

    result = _parse(await skill.execute("call", {"action": "send_poke", "params": "user_id=1"}))

    assert result["code"] == "INVALID_PARAMS"


# ── 错误映射 ──


async def test_call_maps_missing_response_to_backend_unavailable() -> None:
    skill = _skill(adapter=FakeAdapter(response=None))

    result = _parse(
        await skill.execute(
            "call",
            {"action": "get_group_detail_info", "params": {"group_id": 1}},
        )
    )

    assert result["ok"] is False
    assert result["code"] == "BACKEND_UNAVAILABLE"


async def test_call_maps_failed_response_to_api_error() -> None:
    skill = _skill(
        adapter=FakeAdapter(
            {
                "status": "failed",
                "retcode": 1401,
                "message": "权限不足",
                "wording": "没有权限",
            }
        )
    )

    result = _parse(
        await skill.execute(
            "call",
            {"action": "get_group_detail_info", "params": {"group_id": 1}},
        )
    )

    assert result["ok"] is False
    assert result["code"] == "API_ERROR"
    assert result["retcode"] == 1401
    assert result["hint"] == "权限不足"
    assert "权限不足" in result["error"]


async def test_call_maps_adapter_exception_to_adapter_error() -> None:
    skill = _skill(adapter=FakeAdapter(error=RuntimeError("boom")))

    result = _parse(
        await skill.execute(
            "call",
            {"action": "get_group_detail_info", "params": {"group_id": 1}},
        )
    )

    assert result["ok"] is False
    assert result["code"] == "ADAPTER_ERROR"
    assert "boom" in result["error"]
