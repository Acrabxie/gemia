import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "static/v3/workspace-layout.js"


def run_node(expression: str):
    script = f"const L=require({json.dumps(str(LAYOUT))}); console.log(JSON.stringify({expression}));"
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def test_layout_exports_flow_constants_for_side_by_side_drops() -> None:
    result = run_node("{rowLimit: L.ROW_FILL_LIMIT, fullWidth: L.FULL_WIDTH_THRESHOLD}")
    assert result == {"rowLimit": 136, "fullWidth": 78}


def test_shrunken_full_width_pair_shares_one_row() -> None:
    # After a horizontal drop the pair is capped below FULL_WIDTH_THRESHOLD and
    # scaled into ROW_FILL_LIMIT, so even preview+timeline share one row.
    expression = """(() => {
      const items=[{id:'preview',width:67.7,height:64},{id:'timeline',width:67.7,height:36}];
      const flow=L.flowModules(items,{width:1200,height:760,gap:8});
      return {rows:flow.rows.map(r=>r.ids),overlap:L.hasOverlap(flow.placements)};
    })()"""
    result = run_node(expression)
    assert result == {"rows": [["preview", "timeline"]], "overlap": False}


def test_horizontal_module_drop_pairs_width_weights() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert "dropHorizontal = Math.abs(dx) > Math.abs(dy)" in source
    assert "if (dropHorizontal) ensureSideBySide(draggedModule, dropTarget)" in source
    assert "function ensureSideBySide(" in source


def test_vertical_module_drop_forces_rows_and_uses_vertical_cue() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert "else ensureStacked(draggedModule, dropTarget)" in source
    assert "function ensureStacked(" in source
    assert 'target.dataset.dropAxis = dropHorizontal ? "horizontal" : "vertical"' in source
    assert '.workspace-module[data-drop-axis="vertical"].is-drop-before' in css
    assert '.workspace-module[data-drop-axis="vertical"].is-drop-after' in css


def test_stage_tabs_are_draggable_for_reorder() -> None:
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert 'data-tab-drag="${k}"' in source
    assert 'stageTabList?.addEventListener("dragstart"' in source
    assert 'stageTabList?.addEventListener("drop"' in source
    assert ".stage-tab.is-drop-before" in css
    assert ".stage-tab.is-drop-after" in css
