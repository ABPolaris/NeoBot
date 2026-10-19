"""fix(21):`neobot doctor` 必须分发给体检，而不是直接启动 Bot。"""

from __future__ import annotations

import sys

import neobot_app.cli as cli


def test_doctor_subcommand_dispatches_to_cmd_doctor(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: calls.append("doctor"))
    monkeypatch.setattr(cli, "cmd_run", lambda args: calls.append("run"))
    monkeypatch.setattr(sys, "argv", ["neobot", "doctor"])

    cli.main()

    assert calls == ["doctor"]


def test_no_subcommand_still_starts_bot(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: calls.append("doctor"))
    monkeypatch.setattr(cli, "cmd_run", lambda args: calls.append("run"))
    monkeypatch.setattr(sys, "argv", ["neobot"])

    cli.main()

    assert calls == ["run"]
