"""Security: send-capable tools must never reach sub-agents.

speak and create_pdf both call ctx.bot.* directly. An autonomy/curator sub-agent holding
them could emit an unprompted voice note or PDF that bypasses may_speak_now / quiet hours /
the daily cap. They must be orchestrator_only, while the user-facing orchestrator keeps them.
"""

from __future__ import annotations

import inais.agents  # noqa: F401 — registers agents + common tools at import
from inais.orchestrator import registry


def _names(for_subagent: bool) -> set[str]:
    return {t.name for t in registry.tools_for("study", for_subagent=for_subagent)}


def test_orchestrator_keeps_send_capable_tools():
    main = _names(for_subagent=False)
    assert {"speak", "create_pdf"} <= main


def test_subagents_are_stripped_of_send_capable_tools():
    sub = _names(for_subagent=True)
    assert "speak" not in sub
    assert "create_pdf" not in sub


def test_delegate_stays_orchestrator_only():
    # regression guard: sub-agents must not be able to recurse via delegate
    assert "delegate" not in _names(for_subagent=True)
    assert "delegate" in _names(for_subagent=False)


def test_web_tools_reach_subagents():
    # the browse fix relies on sub-agents keeping web_search/read_url; don't over-strip
    sub = _names(for_subagent=True)
    assert {"web_search", "read_url"} <= sub
