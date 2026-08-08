import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "static/v3/workspace-layout.js"


def run_node(expression: str):
    script = f"const L=require({json.dumps(str(LAYOUT))}); console.log(JSON.stringify({expression}));"
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def test_default_layout_is_deterministic_filled_and_non_overlapping() -> None:
    expression = """(() => {
      const ids=['preview','outline','tasks','timeline'];
      const items=ids.map(id=>({id,...L.DEFAULT_SIZES[id]}));
      const a=L.flowModules(items,{width:1200,height:760,gap:8});
      const b=L.flowModules(items,{width:1200,height:760,gap:8});
      const rowChecks=a.rows.map(row=>{
        const p=row.ids.map(id=>a.placements[id]);
        return {
          left:p[0].x,
          right:p[p.length-1].x+p[p.length-1].width,
          aligned:p.every(x=>x.y===p[0].y&&x.height===p[0].height),
        };
      });
      const last=a.rows[a.rows.length-1];
      return {same:JSON.stringify(a)===JSON.stringify(b),overlap:L.hasOverlap(a.placements),rowChecks,bottom:last.y+last.height,rows:a.rows.map(r=>r.ids)};
    })()"""
    result = run_node(expression)

    assert result == {
        "same": True,
        "overlap": False,
        "rowChecks": [
            {"left": 0, "right": 1200, "aligned": True},
            {"left": 0, "right": 1200, "aligned": True},
        ],
        "bottom": 760,
        "rows": [["preview", "outline", "tasks"], ["timeline"]],
    }


def test_many_orders_and_continuous_sizes_fill_every_row_without_overlap() -> None:
    expression = """(() => {
      let seed=117; const ids=['preview','outline','tasks','timeline','files','history'];
      const rnd=()=>((seed=(seed*48271)%2147483647)/2147483647);
      for(let n=0;n<500;n++) {
        const order=[...ids].sort(()=>rnd()-.5);
        const items=order.map(id=>({id,width:8+rnd()*104,height:8+rnd()*104}));
        const width=420+rnd()*1380, height=360+rnd()*740, gap=rnd()*14;
        const flow=L.flowModules(items,{width,height,gap});
        if(L.hasOverlap(flow.placements)) return {ok:false,reason:'overlap',n};
        for(const row of flow.rows) {
          const p=row.ids.map(id=>flow.placements[id]);
          if(Math.abs(p[0].x)>0.01 || Math.abs(p[p.length-1].x+p[p.length-1].width-width)>0.01)
            return {ok:false,reason:'horizontal-gap',n,row};
        }
        const last=flow.rows[flow.rows.length-1];
        if(Math.abs(last.y+last.height-height)>0.01) return {ok:false,reason:'vertical-gap',n};
      }
      return {ok:true};
    })()"""
    assert run_node(expression) == {"ok": True}


def test_sizes_are_continuous_clamped_and_legacy_grid_values_migrate() -> None:
    result = run_node("({preview:L.clampSize('preview',{width:12.375,height:120}),timeline:L.clampSize('timeline',{width:120,height:4}),panel:L.clampSize('files',{width:4,height:120}),legacy:L.clampSize('outline',{cols:4,rows:3})})")
    assert result == {
        "preview": {"width": 30, "height": 100},
        "timeline": {"width": 100, "height": 18},
        "panel": {"width": 16, "height": 100},
        "legacy": {"width": 33.333, "height": 30},
    }


def test_vertical_drop_promotes_both_modules_to_separate_rows() -> None:
    result = run_node("""(() => {
      const preview=L.fullRowSize('preview', L.DEFAULT_SIZES.preview);
      const outline=L.fullRowSize('outline', L.DEFAULT_SIZES.outline);
      const flow=L.flowModules([
        {id:'preview',...preview},
        {id:'outline',...outline},
      ],{width:900,height:600,gap:8});
      return {preview,outline,rows:flow.rows.map(row=>row.ids)};
    })()""")

    assert result == {
        "preview": {"width": 78, "height": 64},
        "outline": {"width": 78, "height": 64},
        "rows": [["preview"], ["outline"]],
    }


def test_timeline_uses_one_uniform_scale_and_never_crops_its_chrome() -> None:
    result = run_node("""({
      full: L.timelineScale(784, 184),
      proportional: L.timelineScale(627.2, 147.2),
      widthLimited: L.timelineScale(496, 148),
      floor: L.timelineScale(278, 114),
    })""")

    assert result == {
        "full": 1,
        "proportional": 0.8,
        "widthLimited": 0.633,
        "floor": 0.5,
    }
