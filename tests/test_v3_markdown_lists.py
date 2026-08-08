import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _render_markdown(markdown: str) -> str:
    source = (ROOT / "static/v3/v3-markdown.js").read_text(encoding="utf-8")
    renderer = source[
        source.index("  function renderMarkdown(src) {") :
        source.index("  function mdTable(lines) {")
    ]
    script = f"""
function escapeHTML(value) {{
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}}
{renderer}
process.stdout.write(renderMarkdown({json.dumps(markdown)}));
"""
    return subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_adjacent_loose_ordered_list_blocks_continue_numbering() -> None:
    html = _render_markdown(
        """因为执行链路没有真正落地：

1. **设计状态写入异常**
第一项的说明。

1. **时间线是空的**
第二项的说明。

1. **缺少实际制作入口**
第三项的说明。"""
    )

    assert html.count('<ol class="md-list"') == 3
    assert '<ol class="md-list"><li><strong>设计状态写入异常</strong>' in html
    assert '<ol class="md-list" start="2"><li><strong>时间线是空的</strong>' in html
    assert '<ol class="md-list" start="3"><li><strong>缺少实际制作入口</strong>' in html


def test_a_paragraph_between_ordered_lists_starts_a_new_sequence() -> None:
    html = _render_markdown(
        """1. 第一组

这是另一段正文。

1. 第二组"""
    )

    assert html.count('<ol class="md-list"><li>') == 2
    assert 'start="2"' not in html
