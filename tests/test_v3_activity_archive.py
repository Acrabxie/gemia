from pathlib import Path
import json
import subprocess


def test_same_purpose_calls_render_under_one_activity_archive() -> None:
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")
    css = Path("static/v3/v3.css").read_text(encoding="utf-8")

    assert 'const key = activityText ? `purpose:${activityText}` : `category:${category}`;' in source
    assert "if (previous?.key === key)" in source
    assert "if (group.calls.length === 1)" in source
    assert '<details class="activity-archive activity-archive--${phase}"' in source
    assert '<div class="activity-archive-items">${detailHtml}</div>' in source
    assert 'activityText: ""' in source
    assert ".activity-archive[open] > .activity-archive-head::after" in css


def test_grouping_logic_combines_cross_category_calls_with_one_purpose() -> None:
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")
    start = source.index("  function buildCallGroups(turn) {")
    end = source.index("\n  function renderCallGroup(group) {", start)
    build_call_groups = source[start:end]
    script = f"""
const safeActivityText = (value) => String(value || "").trim();
const safeProgressReport = (value) => String(value || "").trim();
const toolCategory = (name) => ({{
  read_file: "文件",
  web_search: "搜索",
  run_shell: "文件",
}}[name] || "执行");
{build_call_groups}
const calls = [
  {{call_id: "1", tool_name: "read_file", activityText: "确认当前工程状态"}},
  {{call_id: "2", tool_name: "web_search", activityText: "确认当前工程状态"}},
  {{call_id: "3", tool_name: "run_shell", activityText: "生成审阅版本"}},
];
const turn = {{
  orderedCallIds: calls.map((call) => call.call_id),
  toolCalls: new Map(calls.map((call) => [call.call_id, call])),
}};
process.stdout.write(JSON.stringify(buildCallGroups(turn).map((group) => ({{
  key: group.key,
  calls: group.calls.map((call) => call.call_id),
}}))));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == [
        {"key": "purpose:确认当前工程状态", "calls": ["1", "2"]},
        {"key": "purpose:生成审阅版本", "calls": ["3"]},
    ]


def test_completed_archives_collapse_but_live_work_stays_open() -> None:
    source = Path("static/v3/v3.js").read_text(encoding="utf-8")

    assert 'const open = phase === "active" || phase === "attention" || phase === "waiting";' in source
    assert '${open ? " open" : ""}' in source


def test_activity_prompt_names_one_shared_batch_purpose() -> None:
    prompt = Path("gemia/prompts/system_v3.md").read_text(encoding="utf-8")

    assert "shared purpose of that whole batch" in prompt
    assert "same immediate goal belong under one activity line" in prompt
