"""ToolPermissions 自动审查：allow / deny / 凭据覆盖 / 回退。"""

from __future__ import annotations

import json

import pytest

from neobot_app.agent_tools.contracts import AgentToolError, ToolContext
from neobot_app.agent_tools.permissions import ToolPermissions


class _Credentials:
    def __init__(self, allowed: bool = False) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str, bool]] = []

    def consume(self, *, chat_flow: str, action: str, commit: bool = True):
        self.calls.append((chat_flow, action, commit))
        return object() if self.allowed else None


class _Reviewer:
    def __init__(self, decision: str = "allow", *, error: Exception | None = None) -> None:
        self.decision = decision
        self.error = error
        self.calls: list[list[dict]] = []

    async def chat(self, messages: list[dict]) -> dict:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return {"content": json.dumps({"decision": self.decision, "reason": "审查原因"}, ensure_ascii=False)}


def _context() -> ToolContext:
    return ToolContext(owner="owner", chat_flow_id="group:1")


@pytest.mark.asyncio
async def test_authorize_review_allow() -> None:
    credentials = _Credentials()
    reviewer = _Reviewer("allow")
    permissions = ToolPermissions(credentials, reviewer=reviewer)

    await permissions.authorize(
        _context(), "agent_execute", tool_name="run_code", arguments={"code": "print(1)"}
    )

    assert len(reviewer.calls) == 1
    assert credentials.calls == [("group:1", "agent_execute", True)]


@pytest.mark.asyncio
async def test_authorize_review_deny_offers_credential() -> None:
    reviewer = _Reviewer("deny")
    permissions = ToolPermissions(_Credentials(), reviewer=reviewer)

    with pytest.raises(AgentToolError) as exc:
        await permissions.authorize(_context(), "agent_execute", tool_name="run_code", arguments={})

    assert exc.value.code == "AUTO_REVIEW_DENIED"
    assert exc.value.details["request_tool"] == "credential__request"
    assert exc.value.details["request_args"]["action"] == "agent_execute"


@pytest.mark.asyncio
async def test_authorize_credential_override_skips_reviewer() -> None:
    reviewer = _Reviewer("deny")
    permissions = ToolPermissions(_Credentials(allowed=True), reviewer=reviewer)

    await permissions.authorize(_context(), "agent_execute", tool_name="run_code", arguments={})
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_authorize_reviewer_unavailable_falls_back_to_credentials() -> None:
    reviewer = _Reviewer(error=RuntimeError("provider down"))
    permissions = ToolPermissions(_Credentials(), reviewer=reviewer)

    with pytest.raises(AgentToolError) as exc:
        await permissions.authorize(_context(), "agent_shared_write", tool_name="write_file", arguments={})

    assert exc.value.code == "CREDENTIAL_REQUIRED"


@pytest.mark.asyncio
async def test_authorize_can_disable_auto_review() -> None:
    reviewer = _Reviewer("allow")
    permissions = ToolPermissions(_Credentials(), reviewer=reviewer, auto_review=False)

    with pytest.raises(AgentToolError) as exc:
        await permissions.authorize(_context(), "agent_execute", tool_name="run_code", arguments={})

    assert exc.value.code == "CREDENTIAL_REQUIRED"
    assert reviewer.calls == []
