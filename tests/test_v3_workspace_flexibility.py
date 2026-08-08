import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "static/v3/workspace-layout.js"


def run_node(expression: str):
    script = f"const L=require({json.dumps(str(LAYOUT))}); console.log(JSON.stringify({expression}));"
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def test_every_module_kind_can_reach_full_width_and_height() -> None:
    # "灵活调整大小": panels are no longer capped below the full-width regime,
    # so any module can be stretched to own a row / the full board height.
    result = run_node("{panel: L.LIMITS.panel, timeline: L.LIMITS.timeline}")

    assert result["panel"]["maxWidth"] == 100
    assert result["panel"]["maxHeight"] == 100
    assert result["timeline"]["maxHeight"] == 100


def test_module_head_sheds_chrome_instead_of_wrapping() -> None:
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    # Head text stays on one line and ellipsizes; narrow containers drop the
    # meta caption and then the refresh button via container queries.
    assert "container-type: inline-size" in css
    assert "min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" in css
    assert "@container (max-width: 300px)" in css
    assert "@container (max-width: 190px)" in css


def test_stage_tab_strip_scrolls_instead_of_overflowing() -> None:
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")

    assert "flex-wrap: nowrap; min-width: 0;" in css
    assert "overflow-x: auto; scrollbar-width: none;" in css
    assert ".stage-tab-list::-webkit-scrollbar { display: none; }" in css


def test_timeline_chrome_and_tracks_share_the_module_scale() -> None:
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert "--timeline-ui-scale: 1" in css
    assert "calc(38px * var(--timeline-ui-scale))" in css
    assert "calc(26px * var(--timeline-ui-scale))" in css
    assert "WorkspaceLayout.timelineScale(place.width, place.height)" in source
    assert "const timelineTrackHeight = () => TL_TRACK_H * timelineUiScale()" in source


def test_timeline_typography_does_not_shrink_with_module_geometry() -> None:
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert re.search(
        r"(?:font|font-size)\s*:[^;\n]*var\(--timeline-ui-scale\)",
        css,
    ) is None
    assert "h = ruler.clientHeight || TL_RULER_H" in source
    assert "ruler.height = Math.max(1, Math.round(h * dpr))" in source
