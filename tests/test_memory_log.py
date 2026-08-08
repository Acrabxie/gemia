"""Tests for v3 memory injection + auto daily-log + the remember/log_note verbs.

Coverage:
  - format_memory_for_prompt reads a planted MEMORY.md, caps length, and never
    raises on a missing store.
  - The assembled v3 system prompt contains the injected memory block and has
    NO leftover ``{{memory}}`` placeholder.
  - remember persists a fact; assert_memory_safe rejects secret-bearing text.
  - append_daily_entry appends a line to the day file (and creates it).
  - the log_note tool appends to today's log.
  - the remember tool persists via the dispatcher.
  - the activity-reporting protocol text is present in system_v3.md.
  - the new tools are registered in TOOL_NAMES + DISPATCHER + the schema list.

All filesystem state is redirected to ``tmp_path`` by monkeypatching
``memory.memory_root`` — the real ``~/.gemia`` is NEVER touched.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gemia import memory, project_context
from gemia.production_store import ProductionStore
from gemia.tools._context import AssetRegistry, ToolContext


@pytest.fixture
def mem_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the Gemia memory root to a tmp dir for the whole test."""
    root = tmp_path / "gemia_memory"
    monkeypatch.setattr(memory, "memory_root", lambda: root)
    return root


# ── format_memory_for_prompt ──────────────────────────────────────────


def test_format_memory_reads_planted_memory(mem_root: Path) -> None:
    mem_root.mkdir(parents=True, exist_ok=True)
    planted = "# Gemia Durable Memory\n\n- User prefers a DaVinci-grade UI bar.\n"
    memory.durable_memory_path().write_text(planted, encoding="utf-8")

    block = memory.format_memory_for_prompt()
    assert "DaVinci-grade UI bar" in block


def test_format_memory_missing_never_raises(mem_root: Path) -> None:
    # No files planted at all — must return a short placeholder, not raise.
    block = memory.format_memory_for_prompt()
    assert isinstance(block, str)
    assert block.strip() != ""
    assert "no durable memory" in block.lower()


def test_format_memory_is_capped(mem_root: Path) -> None:
    mem_root.mkdir(parents=True, exist_ok=True)
    memory.durable_memory_path().write_text("A" * 50000, encoding="utf-8")

    block = memory.format_memory_for_prompt(max_chars=1000)
    assert len(block) <= 1000


# ── assembled v3 prompt contains injected memory, no leftover placeholder ──


def test_v3_prompt_injects_memory_and_has_no_leftover_placeholder(
    mem_root: Path, tmp_path: Path
) -> None:
    mem_root.mkdir(parents=True, exist_ok=True)
    marker = "MEMORY-MARKER-XYZZY user likes cool grades"
    memory.durable_memory_path().write_text(
        f"# Gemia Durable Memory\n\n- {marker}\n", encoding="utf-8"
    )

    from gemia.agent_loop_v3 import AgentLoopV3

    loop = AgentLoopV3(
        session_id="sess_mem_inject",
        output_dir=tmp_path / "outputs",
        budget_max_usd=1.0,
        budget_max_seconds=60.0,
        gemini_client=object(),  # stub — prompt assembly must not need creds
    )
    msgs = loop.render_messages()
    system = msgs[0]["content"]

    assert "{{memory}}" not in system  # placeholder fully replaced
    assert marker in system  # planted memory is injected
    assert "### Memory" in system  # the section header is present


# ── remember (function + tool) + secret rejection ─────────────────────


def test_remember_fact_persists(mem_root: Path) -> None:
    record = memory.remember_fact(
        "User prefers FileBeam for APK delivery when adb fails.",
        title="APK delivery",
        kind="workflow",
    )
    assert record["action"] == "appended"
    stored = memory.durable_memory_path().read_text(encoding="utf-8")
    assert "FileBeam for APK delivery" in stored
    assert "**APK delivery**" in stored


def test_remember_fact_idempotent_update_by_title(mem_root: Path) -> None:
    memory.remember_fact("First version", title="UI bar")
    memory.remember_fact("Updated version", title="UI bar")
    stored = memory.durable_memory_path().read_text(encoding="utf-8")
    # Only one bullet for this title, carrying the updated text.
    assert stored.count("**UI bar**") == 1
    assert "Updated version" in stored
    assert "First version" not in stored


def test_assert_memory_safe_rejects_secret_text() -> None:
    with pytest.raises(ValueError):
        memory.assert_memory_safe("here is my api_key = sk-abcdef0123456789abcdef")


def test_remember_fact_rejects_secret(mem_root: Path) -> None:
    with pytest.raises(ValueError):
        memory.remember_fact("token = ghp_abcdefghijklmnopqrstuvwxyz0123456789")


def test_remember_tool_dispatch_persists(mem_root: Path) -> None:
    from gemia.tools import DISPATCHER

    result = asyncio.run(
        DISPATCHER["remember"](
            {"content": "User is haibogavin@example test fact.", "title": "Handle"},
            None,
        )
    )
    assert result["remembered"] is True
    stored = memory.durable_memory_path().read_text(encoding="utf-8")
    assert "test fact" in stored


# ── append_daily_entry (function + log_note tool) ─────────────────────


def test_append_daily_entry_creates_and_appends(mem_root: Path) -> None:
    day = "2026-06-28"
    out = memory.append_daily_entry("did a useful thing", day=day)
    assert out["written"] is True
    path = memory.daily_path(day)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "did a useful thing" in text

    # A second entry appends, not overwrites.
    memory.append_daily_entry("did another thing", day=day)
    text2 = path.read_text(encoding="utf-8")
    assert "did a useful thing" in text2
    assert "did another thing" in text2


def test_append_daily_entry_collapses_newlines(mem_root: Path) -> None:
    out = memory.append_daily_entry("line one\nline two", day="2026-06-28")
    assert out["written"] is True
    assert "\n" not in out["entry"].split("] ")[-1]  # single logical line
    assert "line one line two" in out["entry"]


def test_append_daily_entry_skips_secret(mem_root: Path) -> None:
    out = memory.append_daily_entry("password = supersecretvalue12345", day="2026-06-28")
    assert out["written"] is False
    assert out.get("reason") == "secret"


def test_log_note_tool_appends(mem_root: Path) -> None:
    from gemia.tools import DISPATCHER

    result = asyncio.run(
        DISPATCHER["log_note"]({"text": "breadcrumb from the agent"}, None)
    )
    assert result["logged"] is True
    path = memory.daily_path()
    assert path.exists()
    assert "breadcrumb from the agent" in path.read_text(encoding="utf-8")


def test_project_memory_and_log_are_shared_and_injected(
    mem_root: Path, tmp_path: Path
) -> None:
    from gemia.agent_loop_v3 import AgentLoopV3
    from gemia.tools import DISPATCHER

    store = ProductionStore(tmp_path / "lumeri-data")
    store.create_project("project-context", name="Context")
    ctx = ToolContext(
        session_id="session-context",
        output_dir=tmp_path / "session-output",
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        extra={"production_store": store, "project_id": "project-context"},
    )

    remembered = asyncio.run(
        DISPATCHER["remember"](
            {"content": "Keep the opening restrained", "title": "Opening"},
            ctx,
        )
    )
    logged = asyncio.run(
        DISPATCHER["log_note"]({"text": "first assembly completed"}, ctx)
    )
    assert remembered["scope"] == "project"
    assert logged["scope"] == "project"
    assert not memory.durable_memory_path().exists()

    loop = AgentLoopV3(
        session_id="session-context-prompt",
        output_dir=tmp_path / "loop-output",
        budget_max_usd=1.0,
        budget_max_seconds=60.0,
        gemini_client=object(),
        extra={"production_store": store, "project_id": "project-context"},
    )
    system = loop.render_messages()[0]["content"]
    assert "Global memory (applies across all Projects)" in system
    assert "Keep the opening restrained" in system
    assert "first assembly completed" in system
    assert "only for this Project" in system
    assert "Current Project: Context" in system
    assert "one long-lived workspace" in system
    assert "`project://edit/` is Lumeri's private editing root" in system
    assert "Never carry this Project's private context into another Project" in system


def test_project_memory_is_shared_across_its_sessions_and_isolated_between_projects(
    mem_root: Path, tmp_path: Path
) -> None:
    from gemia.agent_loop_v3 import AgentLoopV3

    store = ProductionStore(tmp_path / "lumeri-data")
    source_a = tmp_path / "film-a-source"
    source_a.mkdir()
    store.create_project("project-a", name="Film A", source_root=source_a)
    store.create_project("project-b", name="Film B")
    project_context.remember_fact(store, "project-a", "Use a restrained blue grade")
    project_context.remember_fact(store, "project-b", "Use a warm amber grade")

    def render(project_id: str, session_id: str) -> str:
        loop = AgentLoopV3(
            session_id=session_id,
            output_dir=tmp_path / session_id,
            budget_max_usd=1.0,
            budget_max_seconds=60.0,
            gemini_client=object(),
            extra={"production_store": store, "project_id": project_id},
        )
        return loop.render_messages()[0]["content"]

    project_a_first = render("project-a", "film-a-session-1")
    project_a_second = render("project-a", "film-a-session-2")
    project_b = render("project-b", "film-b-session-1")

    assert "Current Project: Film A" in project_a_first
    assert "`project://source/` is the bound source folder" in project_a_first
    assert str(source_a.resolve()) not in project_a_first
    assert "Use a restrained blue grade" in project_a_first
    assert "Use a restrained blue grade" in project_a_second
    assert "Use a warm amber grade" not in project_a_first
    assert "Use a warm amber grade" not in project_a_second

    assert "Current Project: Film B" in project_b
    assert "No source folder is bound" in project_b
    assert "Use a warm amber grade" in project_b
    assert "Use a restrained blue grade" not in project_b


def test_internal_chat_container_is_not_treated_as_a_project(
    mem_root: Path, tmp_path: Path
) -> None:
    from gemia.agent_loop_v3 import AgentLoopV3
    from gemia.tools import DISPATCHER

    store = ProductionStore(tmp_path / "lumeri-data")
    store.create_project("v3-internal-chat")
    project_context.remember_fact(
        store,
        "v3-internal-chat",
        "legacy internal marker must stay outside the prompt",
    )
    ctx = ToolContext(
        session_id="plain-chat",
        output_dir=tmp_path / "plain-chat-output",
        registry=AssetRegistry(),
        emit_progress=lambda _update: None,
        extra={"production_store": store, "project_id": "v3-internal-chat"},
    )
    remembered = asyncio.run(
        DISPATCHER["remember"]({"content": "Creator prefers concise replies"}, ctx)
    )
    assert remembered["scope"] == "global"

    loop = AgentLoopV3(
        session_id="plain-chat",
        output_dir=tmp_path / "plain-chat-loop",
        budget_max_usd=1.0,
        budget_max_seconds=60.0,
        gemini_client=object(),
        extra={"production_store": store, "project_id": "v3-internal-chat"},
    )
    system = loop.render_messages()[0]["content"]
    assert "independent Chat with an internal session workspace" in system
    assert "Creator prefers concise replies" in system
    assert "legacy internal marker must stay outside the prompt" not in system


def test_project_turn_auto_log_also_keeps_global_log(
    mem_root: Path, tmp_path: Path
) -> None:
    from gemia.agent_loop_v3 import AgentLoopV3

    store = ProductionStore(tmp_path / "lumeri-data")
    store.create_project("project-log", name="Logs")
    loop = AgentLoopV3(
        session_id="session-project-log",
        output_dir=tmp_path / "loop-output",
        budget_max_usd=1.0,
        budget_max_seconds=60.0,
        gemini_client=object(),
        extra={"production_store": store, "project_id": "project-log"},
    )
    loop._pinned_intent = "make a careful rough cut"
    loop._auto_log_turn(tools_succeeded=2, tools_failed=0, assets_produced=1)
    assert "careful rough cut" in memory.daily_path().read_text(encoding="utf-8")
    assert "careful rough cut" in project_context.format_for_prompt(store, "project-log")


# ── activity-reporting protocol present in the prompt ─────────────────


def test_narration_directive_present_in_prompt() -> None:
    """The rewritten prompt must keep the announce-before-acting guarantee.

    The old 'Narrate before you act' directive became the Activity reporting
    protocol: one <activity> line before each meaningful batch of tool calls.
    """
    tpl = (
        Path(__file__).resolve().parent.parent
        / "gemia"
        / "prompts"
        / "system_v3.md"
    ).read_text(encoding="utf-8")
    assert "## Activity reporting protocol" in tpl
    # The announce-before-acting + single-line constraint is load-bearing.
    assert "emit exactly one line" in tpl
    assert "Before each meaningful batch of tool calls" in tpl
    # The tag format the host parses.
    assert "<activity>" in tpl


def test_activity_prompt_requires_initial_direction_and_flexible_updates() -> None:
    tpl = (
        Path(__file__).resolve().parent.parent
        / "gemia"
        / "prompts"
        / "system_v3.md"
    ).read_text(encoding="utf-8")
    section = tpl.split("## Activity reporting protocol", 1)[1].split("\n---", 1)[0]
    folded = " ".join(section.casefold().split())

    assert "first user-visible output after the user's message" in folded
    assert "briefly describe the overall direction of the work" in folded
    assert "meaningful phase change" in folded
    assert "noticeable wait" in folded
    assert "use judgment rather than a fixed cadence" in folded
    assert "report merely because another tool call happened" in folded
    assert "at most 3 per turn" not in folded


def test_user_facing_language_softly_prefers_plain_terms() -> None:
    tpl = (
        Path(__file__).resolve().parent.parent
        / "gemia"
        / "prompts"
        / "system_v3.md"
    ).read_text(encoding="utf-8")
    assert "Prefer plain creative language" in tpl
    assert "unless they are genuinely needed" in tpl
    assert "This is a preference, not a" in tpl
    assert "ban: when technical terms are necessary" in tpl


# ── new tools registered ──────────────────────────────────────────────


def test_new_tools_registered() -> None:
    from gemia.tools import DISPATCHER, TOOL_NAMES
    from gemia.tools._schema import TOOL_NAMES as SCHEMA_NAMES

    for name in ("remember", "log_note"):
        assert name in TOOL_NAMES
        assert name in SCHEMA_NAMES
        assert name in DISPATCHER
        assert callable(DISPATCHER[name])
