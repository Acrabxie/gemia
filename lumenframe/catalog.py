"""Self-describing catalogue of the lumenframe editing vocabulary.

One source of truth for *what the editor can do*, consumed by two audiences:

* the **agent** — :func:`describe_ops` renders a compact prompt block so natural
  language can be turned into LayerPatches;
* **third-party authors** — :func:`op_catalog` returns structured metadata they
  can introspect (and extend by registering their own ops).

A test asserts every registered core op has a catalogue entry, so the vocabulary
and its documentation can never silently drift apart.
"""
from __future__ import annotations

import json
from typing import Any

from lumenframe.registry import list_ops, op_source

# group -> human label, used only for ordering / display
_GROUP_LABELS: dict[str, str] = {
    "layer": "Layer management",
    "time": "Time",
    "transform": "Transform & compositing",
    "interlayer": "Inter-layer (masks / clipping / adjustments)",
    "effects": "Effects & colour",
    "text": "Text styling",
    "audio": "Audio properties",
    "animation": "Animation presets",
    "keyframes": "Keyframes",
    "template": "Scene templates",
    "element": "Graphic elements",
}

#: Core op metadata. ``args`` lists the meaningful keys; ``*`` marks required.
CORE_OPS_CATALOG: list[dict[str, Any]] = [
    # ── layer management ──
    {"op": "add_layer", "group": "layer", "args": ["type*", "id", "parent_id", "index", "at_time", "lane", "...fields"],
     "summary": "Create a new layer (video/image/text/shape/audio/adjustment/...) under a parent (default root).",
     "example": {"op": "add_layer", "type": "text", "id": "title", "at_time": 0.0, "text": "Hello"},
     "errors": ["E_NOT_FOUND when parent_id does not exist", "E_CONTAINER when parent_id cannot hold children"]},
    {"op": "add_gradient", "group": "layer", "args": ["id", "mode(linear|radial)", "stops*[[pos0..1,'#rrggbb'],...]", "angle(deg, linear)", "center[cx,cy]/radius(0..1, radial)", "parent_id", "index", "at_time", "duration"],
     "summary": "Create a gradient fill layer (linear: angle; radial: center+radius) defined by colour stops, per the shared layer schema.",
     "example": {"op": "add_gradient", "id": "bg", "mode": "linear", "stops": [[0.0, "#000000"], [1.0, "#3344ff"]], "angle": 90},
     "errors": ["E_ARG when stops is missing / has fewer than 2 entries, a stop is malformed, or mode is not linear/radial", "E_NOT_FOUND when parent_id does not exist", "E_CONTAINER when parent_id cannot hold children"]},
    {"op": "add_shape", "group": "layer", "args": ["id", "kind*(rect|ellipse|polygon|line)", "fill('#rrggbb'|null)", "stroke{color,width}", "rect[x0,y0,x1,y1]|cx,cy,rx,ry|points[[x,y],...]", "radius(px, rect)", "parent_id", "index", "at_time", "duration"],
     "summary": "Create a vector shape layer (rect/ellipse/polygon/line) with fill + optional stroke, per the shared layer schema; coords normalised to canvas [0,1].",
     "example": {"op": "add_shape", "id": "box", "kind": "rect", "fill": "#ff0044", "rect": [0.1, 0.1, 0.9, 0.9], "radius": 12},
     "errors": ["E_ARG when kind is missing/unknown, or a polygon/line lacks >= 2 points", "E_NOT_FOUND when parent_id does not exist", "E_CONTAINER when parent_id cannot hold children"]},
    {"op": "delete_layer", "group": "layer", "args": ["layer_id*|layer_ids*"],
     "summary": "Remove one or more layers; mattes pointing at them are cleared.",
     "example": {"op": "delete_layer", "layer_id": "clip1"},
     "errors": ["E_ARG when neither layer_id nor layer_ids is given", "E_NOT_FOUND when the layer id does not exist", "E_ROOT when targeting the root composition"]},
    {"op": "duplicate_layer", "group": "layer", "args": ["layer_id*", "name", "offset_time"],
     "summary": "Deep-copy a layer (fresh ids) right after the original.",
     "example": {"op": "duplicate_layer", "layer_id": "clip1", "offset_time": 2.0},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "select", "group": "layer", "args": ["layer_ids*", "mode(replace|add|toggle|clear)"],
     "summary": "Change the editor selection.",
     "example": {"op": "select", "layer_ids": ["clip1"], "mode": "replace"},
     "errors": ["E_NOT_FOUND when a selected layer id does not exist", "E_ARG when mode is unknown"]},
    {"op": "move_layer", "group": "layer", "args": ["layer_id*", "parent_id", "index", "lane", "start", "delta_start"],
     "summary": "Universal move: reparent, reorder (z), retime and relane in one op.",
     "example": {"op": "move_layer", "layer_id": "clip1", "parent_id": "comp1", "index": 0},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when a referenced layer does not exist", "E_ROOT when moving the root composition", "E_CONTAINER when the new parent cannot hold children", "E_CYCLE when moving a layer into itself or a descendant"]},
    {"op": "reorder_layer", "group": "layer", "args": ["layer_id*", "to(top|bottom|forward|backward)|index|delta"],
     "summary": "Pure z-order change within the current parent.",
     "example": {"op": "reorder_layer", "layer_id": "clip1", "to": "top"},
     "errors": ["E_ARG when none of to / index / delta is given", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "group_layers", "group": "layer", "args": ["layer_ids*", "name", "into_id"],
     "summary": "Wrap sibling layers into a new composition (precompose).",
     "example": {"op": "group_layers", "layer_ids": ["clip1", "clip2"], "name": "Group"},
     "errors": ["E_ARG when layer_ids is empty", "E_NOT_FOUND when a layer id does not exist", "E_GROUP_PARENT when the layers do not share one parent"]},
    {"op": "ungroup_layer", "group": "layer", "args": ["layer_id*"],
     "summary": "Dissolve a composition, lifting its children to the parent.",
     "example": {"op": "ungroup_layer", "layer_id": "comp1"},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist", "E_ROOT when targeting the root composition", "E_TYPE when the layer is not a composition"]},
    {"op": "merge_layers", "group": "layer", "args": ["layer_ids*", "name"],
     "summary": "Flatten layers into one composition marked for raster baking.",
     "example": {"op": "merge_layers", "layer_ids": ["clip1", "clip2"], "name": "Merged"},
     "errors": ["E_ARG when layer_ids is empty", "E_NOT_FOUND when a layer id does not exist", "E_GROUP_PARENT when the layers do not share one parent"]},
    {"op": "merge_compositions", "group": "layer",
     "args": ["source_ids*", "into_id*", "mode(overlay|append)", "offset(seconds)", "keep_sources"],
     "summary": "Merge the children of one or more source compositions into a target composition — overlay (same time) or append (after its extent). Lifted subtrees get fresh ids; mattes/parent refs follow; empty source comps are removed (keep_sources to keep them).",
     "example": {"op": "merge_compositions", "source_ids": ["scene_b"], "into_id": "scene_a", "mode": "append"},
     "errors": ["E_ARG when source_ids is empty, into_id arg is missing or not a composition, a source is not a composition, a source is into_id or the root, mode is unknown, or into_id sits inside a source", "E_NOT_FOUND when into_id or a source id does not exist"]},
    {"op": "rename_layer", "group": "layer", "args": ["layer_id*", "name*"], "summary": "Rename a layer.",
     "example": {"op": "rename_layer", "layer_id": "clip1", "name": "Intro"},
     "errors": ["E_ARG when layer_id or name is missing", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "set_visibility", "group": "layer", "args": ["layer_id*", "visible*"], "summary": "Show / hide a layer.",
     "example": {"op": "set_visibility", "layer_id": "clip1", "visible": False},
     "errors": ["E_ARG when layer_id or visible is missing", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "set_lock", "group": "layer", "args": ["layer_id*", "locked*"], "summary": "Lock / unlock a layer.",
     "example": {"op": "set_lock", "layer_id": "clip1", "locked": True},
     "errors": ["E_ARG when layer_id or locked is missing", "E_NOT_FOUND when the layer id does not exist"]},
    # ── time ──
    {"op": "set_time", "group": "time", "args": ["layer_id*", "start", "duration"],
     "summary": "Set a layer's start and/or duration on its parent timeline.",
     "example": {"op": "set_time", "layer_id": "clip1", "start": 1.0, "duration": 4.0},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist", "E_RANGE when duration is negative"]},
    {"op": "trim", "group": "time", "args": ["layer_id*", "edge(in|out)*", "to|delta"],
     "summary": "Trim one edge; source in/out follows so media stays in sync.",
     "example": {"op": "trim", "layer_id": "clip1", "edge": "out", "to": 5.0},
     "errors": ["E_ARG when layer_id is missing or edge is not in/out", "E_NOT_FOUND when the layer id does not exist", "E_RANGE when the trim would collapse the layer"]},
    {"op": "split", "group": "time", "args": ["layer_id*", "at_time*"],
     "summary": "Cut a layer in two at a time, splitting the source range.",
     "example": {"op": "split", "layer_id": "clip1", "at_time": 2.5},
     "errors": ["E_ARG when layer_id or at_time is missing", "E_NOT_FOUND when the layer id does not exist", "E_RANGE when at_time is not inside the layer"]},
    {"op": "set_speed", "group": "time", "args": ["layer_id*", "speed*"],
     "summary": "Retime a layer; duration recomputes from the source range.",
     "example": {"op": "set_speed", "layer_id": "clip1", "speed": 2.0},
     "errors": ["E_ARG when layer_id or speed is missing", "E_NOT_FOUND when the layer id does not exist", "E_SPEED when speed is not greater than 0"]},
    {"op": "set_time_remap", "group": "time",
     "args": ["layer_id*", "keyframes*[{t(output_sec),value(source_sec),interp(linear|hold)}]", "extrapolate(hold|loop|pingpong)"],
     "summary": "Speed-ramp/freeze a layer by mapping output time to source time; clears constant speed.",
     "example": {"op": "set_time_remap", "layer_id": "clip1",
                 "keyframes": [{"t": 0.0, "value": 0.0, "interp": "linear"},
                               {"t": 2.0, "value": 1.0, "interp": "linear"}],
                 "extrapolate": "hold"},
     "errors": ["E_ARG when layer_id or keyframes is missing, keyframes is empty, a point lacks t/value, or interp/extrapolate is unknown", "E_NOT_FOUND when the layer id does not exist", "E_RANGE when two keyframes at the same output t map to different source times"]},
    {"op": "retime_segment", "group": "time", "args": ["layer_id*", "t0*(output seconds)", "t1*(output seconds)", "speed*(>0)"],
     "summary": "Apply a constant speed to ONLY the sub-range [t0,t1] of a layer, frame-accurately: snaps t0/t1 to frame boundaries, splits the layer at both edges, and set_speed on the isolated middle piece (pure split+set_speed sugar, no compile change).",
     "example": {"op": "retime_segment", "layer_id": "clip1", "t0": 2.0, "t1": 6.0, "speed": 2.0},
     "errors": ["E_ARG when layer_id/t0/t1 is missing or speed <= 0", "E_NOT_FOUND when the layer id does not exist", "E_RANGE when [t0,t1] is empty or not within the layer's [start, start+duration]"]},
    {"op": "reverse", "group": "time", "args": ["layer_id*", "t0(output seconds)|frame0(frame)", "t1(output seconds)|frame1(frame)"],
     "summary": "Play a layer (or just the sub-segment [t0,t1]) BACKWARDS, frame-accurately: composes a time_remap so output time samples the source from source_out down to source_in (linear, nearest-frame). Whole-layer reverse preserves duration and round-trips (reverse twice = original); a sub-segment splits at t0/t1 and reverses only the middle (no ripple needed).",
     "example": {"op": "reverse", "layer_id": "clip1"},
     "errors": ["E_ARG when layer_id is missing, or only one of t0/t1 is given", "E_NOT_FOUND when the layer id does not exist", "E_RANGE when [t0,t1] is empty/inverted or not within the layer's [start, start+duration]"]},
    {"op": "ripple_delete", "group": "time", "args": ["layer_id*"],
     "summary": "Delete a layer AND close the gap (DaVinci-style ripple): every LATER same-lane sibling (start >= the deleted clip's start) slides earlier by the deleted duration, so doc_duration shrinks. Contrast delete_layer, which leaves the gap. Other lanes are untouched.",
     "example": {"op": "ripple_delete", "layer_id": "clip1"},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist", "E_ROOT when targeting the root composition"]},
    {"op": "set_lane", "group": "time", "args": ["layer_id*", "lane*(int)"],
     "summary": "Assign an existing layer to a timeline lane/track (int): a higher lane composites above a lower one, ties keep tree order, lane 0 is the default tree order. The explicit, single-purpose way to put a layer on a second timeline (move_layer can also relane).",
     "example": {"op": "set_lane", "layer_id": "clip1", "lane": 2},
     "errors": ["E_ARG when layer_id or lane is missing, or lane is not an integer", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "set_range", "group": "time", "args": ["layer_id*", "frame_in*(int frame)", "frame_out*(int frame, > frame_in)"],
     "summary": "Frame-native one-shot placement: set a layer's start+duration from a frame window via the canvas timebase (start=to_seconds(frame_in), duration=to_seconds(frame_out-frame_in)), snapped to frame boundaries. Frame-thinking counterpart to set_time; leaves source range/speed untouched.",
     "example": {"op": "set_range", "layer_id": "clip1", "frame_in": 30, "frame_out": 90},
     "errors": ["E_ARG when layer_id/frame_in/frame_out is missing or frame_out <= frame_in", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "crossfade", "group": "time", "args": ["from_id*", "to_id*", "duration(default 0.5)", "at(output seconds, default from-layer start)"],
     "summary": "Cross-dissolve A->B: writes opacity keyframes so 'from' fades 1->0 while 'to' fades 0->1 over duration starting at 'at'. Opacity keyframes only, no compile change.",
     "example": {"op": "crossfade", "from_id": "clip1", "to_id": "clip2", "duration": 1.0, "at": 4.0},
     "errors": ["E_ARG when from_id or to_id is missing", "E_NOT_FOUND when from_id or to_id does not exist", "E_RANGE when duration <= 0"]},
    # ── transform & compositing ──
    {"op": "set_transform", "group": "transform", "args": ["layer_id*", "x", "y", "scale|scale_x|scale_y", "rotation", "anchor_x", "anchor_y"],
     "summary": "Move / scale / rotate a layer (anchor-relative, canvas-centre origin).",
     "example": {"op": "set_transform", "layer_id": "clip1", "x": 100, "y": -50, "scale": 1.5, "rotation": 15},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "set_opacity", "group": "transform", "args": ["layer_id*", "opacity*(0..1)"], "summary": "Set layer opacity.",
     "example": {"op": "set_opacity", "layer_id": "clip1", "opacity": 0.5},
     "errors": ["E_ARG when layer_id or opacity is missing", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "set_blend_mode", "group": "transform", "args": ["layer_id*", "blend_mode*"], "summary": "Set the blend mode.",
     "example": {"op": "set_blend_mode", "layer_id": "clip1", "blend_mode": "screen"},
     "errors": ["E_ARG when layer_id or blend_mode is missing", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "set_blend", "group": "transform", "args": ["layer_id*", "mode*(normal|multiply|screen|overlay|darken|lighten|color_dodge|color_burn|hard_light|soft_light|difference|exclusion|add|subtract)"],
     "summary": "Set a layer's blend mode, validated up front against the supported set (a typo raises instead of silently degrading to normal).",
     "example": {"op": "set_blend", "layer_id": "clip1", "mode": "screen"},
     "errors": ["E_ARG when layer_id or mode is missing, or mode is not a supported blend mode", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "pip", "group": "transform", "args": ["layer_id*", "corner(br|bl|tr|tl)|x,y", "scale(default 0.3)", "margin(frac, default 0.04)", "radius(px)", "border{color,width}", "shadow(bool)", "blur_background(px, default 0=off)"],
     "summary": "Picture-in-picture: scale the layer down and tuck it into a corner (or explicit x/y) with a rounded-rect mask; optional border/shadow adds one helper shape layer beneath it. blur_background>0 adds a gaussian_blur to every sibling below the pip so the inset pops in focus. Pure transform+mask+effect, no compile change.",
     "example": {"op": "pip", "layer_id": "clip1", "corner": "br", "scale": 0.3, "margin": 0.04, "radius": 24, "blur_background": 8},
     "errors": ["E_ARG when layer_id is missing or corner is unknown", "E_NOT_FOUND when the layer id does not exist", "E_RANGE when scale <= 0, radius < 0, or blur_background < 0"]},
    {"op": "focus_pull", "group": "transform", "args": ["layer_id*", "blur*(px)"],
     "summary": "Rack-focus / depth-of-field: keep layer_id sharp and add a gaussian_blur (blur px) to every sibling in the same composition, so the focused layer is the only crisp element. Pure effect-chain edit, no compile change.",
     "example": {"op": "focus_pull", "layer_id": "clip1", "blur": 12},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist", "E_RANGE when blur <= 0"]},
    {"op": "flip_layer", "group": "transform", "args": ["layer_id*", "direction(horizontal|vertical|both)"],
     "summary": "Upsert a mirror/flip effect on the layer's effect chain.",
     "example": {"op": "flip_layer", "layer_id": "clip1", "direction": "horizontal"},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist"]},
    # ── inter-layer ──
    {"op": "set_mask", "group": "interlayer", "args": ["layer_id*", "mask{kind(shape|pixel|alpha_matte|luma_matte), source_layer_id, asset_id, alpha[][], channel, shape{type(rectangle|ellipse|polygon|path|bezier), x0,y0,x1,y1|rect|cx,cy,rx,ry|points[[x,y]]|contours[], radius, fill_rule}, invert, feather}|null"],
     "summary": "Attach a vector shape mask (rectangle/ellipse/polygon/path/bezier in normalized [0,1] coords), a direct pixel/alpha mask, an alpha/luma track matte, or clear it (null).",
     "example": {"op": "set_mask", "layer_id": "clip1", "mask": {"kind": "shape", "shape": {"type": "rectangle", "x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.9}}},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist", "E_MASK when the mask kind is unknown or a matte source is missing/self-referential"]},
    {"op": "clip_to_below", "group": "interlayer", "args": ["layer_id*", "enabled"],
     "summary": "Clip a layer to the layer beneath it (clipping mask).",
     "example": {"op": "clip_to_below", "layer_id": "clip1", "enabled": True},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "add_adjustment_layer", "group": "interlayer", "args": ["parent_id", "index", "name", "effects[]"],
     "summary": "Insert an adjustment layer whose effects apply to the layers below.",
     "example": {"op": "add_adjustment_layer", "name": "Grade", "effects": [{"type": "color_grade", "params": {"contrast": 1.2}}]},
     "errors": ["E_NOT_FOUND when parent_id does not exist", "E_CONTAINER when the parent cannot hold children"]},
    # ── effects & colour ──
    {"op": "add_effect", "group": "effects", "args": ["layer_id*", "effect{type*, params, id}", "index"],
     "summary": "Append an effect to a layer's effect chain.",
     "example": {"op": "add_effect", "layer_id": "clip1", "effect": {"type": "gaussian_blur", "params": {"radius": 8}}},
     "errors": ["E_ARG when layer_id is missing or the effect has no type", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "remove_effect", "group": "effects", "args": ["layer_id*", "effect_id*"], "summary": "Remove an effect by id.",
     "example": {"op": "remove_effect", "layer_id": "clip1", "effect_id": "fx1"},
     "errors": ["E_ARG when layer_id or effect_id is missing", "E_NOT_FOUND when the layer or effect id does not exist"]},
    {"op": "set_effect_params", "group": "effects", "args": ["layer_id*", "effect_id*", "params*", "merge"],
     "summary": "Update an effect's parameters (merge by default).",
     "example": {"op": "set_effect_params", "layer_id": "clip1", "effect_id": "fx1", "params": {"radius": 12}},
     "errors": ["E_ARG when layer_id/effect_id/params is missing or params is not an object", "E_NOT_FOUND when the layer or effect id does not exist"]},
    {"op": "color_grade", "group": "effects", "args": ["layer_id*", "brightness", "contrast", "saturation", "exposure", "temperature", "tint", "highlights", "shadows", "gamma", "lift", "gain"],
     "summary": "Upsert a single colour-grade effect (DaVinci-style controls).",
     "example": {"op": "color_grade", "layer_id": "clip1", "contrast": 1.2, "saturation": 1.1},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "add_transition", "group": "effects", "args": ["layer_id*", "kind*(fade|dissolve|wipe_l|wipe_r|wipe_u|wipe_d|slide)", "duration", "at(in|out|both)"],
     "summary": "Attach an in/out transition to a layer; rendered at compile (fade/dissolve ramp opacity, wipe_* reveal a growing band, slide translates the content).",
     "example": {"op": "add_transition", "layer_id": "clip1", "kind": "fade", "duration": 0.5, "at": "in"},
     "errors": ["E_ARG when layer_id or kind is missing or at is invalid", "E_NOT_FOUND when the layer id does not exist"]},
    # ── text styling ──
    {"op": "set_text", "group": "text", "args": ["layer_id*", "text", "font", "font_size", "color", "align", "stroke", "shadow", "background", "line_spacing"],
     "summary": "Set text properties (only provided keys are updated); works with text layers.",
     "example": {"op": "set_text", "layer_id": "title", "text": "Hello", "font_size": 64, "color": "#ffffff"},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist"]},
    # ── audio properties ──
    {"op": "set_volume", "group": "audio", "args": ["layer_id*", "volume*"],
     "summary": "Set audio layer volume (linear, 0..1+, default 1.0).",
     "example": {"op": "set_volume", "layer_id": "music", "volume": 0.8},
     "errors": ["E_ARG when layer_id or volume is missing", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "set_audio_fade", "group": "audio", "args": ["layer_id*", "fade_in", "fade_out", "shape(linear|exp|log)"],
     "summary": "Set fade-in/out durations (seconds) and fade curve shape (linear|exp|log) on an audio layer; durations stay scalar, shape is stored as fade_in_shape/fade_out_shape for a downstream mixer.",
     "example": {"op": "set_audio_fade", "layer_id": "music", "fade_in": 0.5, "fade_out": 1.0, "shape": "exp"},
     "errors": ["E_ARG when layer_id is missing or shape is not linear/exp/log", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "mute_layer", "group": "audio", "args": ["layer_id*", "muted(default true)"],
     "summary": "Mute or unmute a layer.",
     "example": {"op": "mute_layer", "layer_id": "music", "muted": True},
     "errors": ["E_ARG when layer_id is missing", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "duck_audio", "group": "audio", "args": ["layer_id*", "target_id*", "amount(dB<=0 or 0..1 gain, default -12)", "attack(s, default 0.05)", "release(s, default 0.3)"],
     "summary": "Store a sidechain-ducking descriptor (props.ducking={target_id,amount,attack,release}) so a downstream mixer lowers this layer's level while the target layer is audible (CapCut/DaVinci auto-duck).",
     "example": {"op": "duck_audio", "layer_id": "music", "target_id": "title", "amount": -12, "attack": 0.05, "release": 0.3},
     "errors": ["E_ARG when layer_id or target_id is missing", "E_NOT_FOUND when the layer id or target_id does not exist", "E_RANGE when attack or release is negative"]},
    # ── animation presets ──
    {"op": "animate_layer", "group": "animation", "args": ["layer_id*", "preset*", "duration", "easing"],
     "summary": "Generate keyframes using a preset: fade_in, fade_out, fly_in/out_*, zoom_in/out, ken_burns.",
     "example": {"op": "animate_layer", "layer_id": "clip1", "preset": "fade_in", "duration": 0.5},
     "errors": ["E_ARG when layer_id or preset is missing, or the preset is unknown", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "animate_text", "group": "animation", "args": ["layer_id*", "preset*(fade_in_words|pop|wave|rise)", "duration", "easing"],
     "summary": "Animate a text layer with a CapCut-style title preset (pop/wave/rise/fade_in_words); emits opacity/transform keyframes only.",
     "example": {"op": "animate_text", "layer_id": "title", "preset": "pop", "duration": 0.5},
     "errors": ["E_ARG when layer_id or preset is missing, the preset is unknown, or duration is not positive", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "speed_ramp", "group": "time", "args": ["layer_id*", "preset*(montage|hero|bullet|ease_in|ease_out)", "extrapolate(hold|loop|pingpong)"],
     "summary": "Apply a named speed-ramp preset by emitting a time_remap curve (hero=slow middle, montage=fast middle); preserves output duration.",
     "example": {"op": "speed_ramp", "layer_id": "clip1", "preset": "hero"},
     "errors": ["E_ARG when layer_id or preset is missing or the preset is unknown", "E_NOT_FOUND when the layer id does not exist", "E_RANGE when the layer has no duration to ramp"]},
    {"op": "apply_template", "group": "template", "args": ["template*", "params{...}"],
     "summary": "Stamp a named scene template (title_card / bullet_list / stat_card / …) into its layers via the normal op dispatch — see the 'Scene templates' block for the full catalogue and each one's params.",
     "example": {"op": "apply_template", "template": "bullet_list", "params": {"heading": "Agenda", "items": ["Why now", "The plan", "Next steps"]}},
     "errors": ["E_ARG when template is missing, unknown, or params is not an object / has bad keys", "E_NOT_FOUND when a referenced layer does not exist"]},
    {"op": "apply_element", "group": "element", "args": ["element*", "params{...}"],
     "summary": "Stamp a named graphic element (arrow / …) as an OVERLAY into its layers via the normal op dispatch — see the 'Element library' block for the full catalogue and each one's params.",
     "example": {"op": "apply_element", "element": "arrow", "params": {"x": 0, "y": 0, "length": 400, "angle_deg": 90}},
     "errors": ["E_ARG when element is missing, unknown, or params is not an object / has bad keys", "E_NOT_FOUND when a referenced layer does not exist"]},
    # ── keyframes ──
    {"op": "set_keyframe", "group": "keyframes", "args": ["layer_id*", "property*", "t*", "value*", "interp"],
     "summary": "Add or replace a keyframe on a property (e.g. transform.x, opacity).",
     "example": {"op": "set_keyframe", "layer_id": "clip1", "property": "opacity", "t": 0.0, "value": 0.0, "interp": "linear"},
     "errors": ["E_ARG when layer_id/property/t is missing or interp is unknown", "E_NOT_FOUND when the layer id does not exist"]},
    {"op": "remove_keyframe", "group": "keyframes", "args": ["layer_id*", "property*", "t*"],
     "summary": "Remove a keyframe at a time.",
     "example": {"op": "remove_keyframe", "layer_id": "clip1", "property": "opacity", "t": 0.0},
     "errors": ["E_ARG when layer_id/property/t is missing", "E_NOT_FOUND when the layer, track, or keyframe does not exist"]},
    {"op": "set_expression", "group": "animation", "args": ["layer_id*", "property*", "expression*"],
     "summary": "Bind a time-driven expression to a layer property (e.g., opacity, rotation).",
     "example": {"op": "set_expression", "layer_id": "clip1", "property": "opacity", "expression": "0.5 + 0.2 * sin(time * 8)"},
     "errors": ["E_ARG when layer_id/property is missing or expression is empty", "E_NOT_FOUND when the layer id does not exist", "E_UNSAFE when the expression is invalid or not allowed"]},
]

_BY_OP: dict[str, dict[str, Any]] = {entry["op"]: entry for entry in CORE_OPS_CATALOG}


def op_catalog() -> list[dict[str, Any]]:
    """Structured metadata for every registered op (core entries + extensions).

    Extension ops with no hand-written entry still appear with a generic
    placeholder so the agent at least knows they exist.
    """
    out: list[dict[str, Any]] = []
    for name in list_ops():
        entry = dict(_BY_OP.get(name, {"op": name, "group": "extension", "args": [], "summary": ""}))
        entry["source"] = op_source(name) or "extension"
        out.append(entry)
    return out


def describe_ops() -> str:
    """Compact, grouped, human-readable vocabulary block for prompt injection.

    Each op line is followed (when the catalogue provides them) by a copy-paste
    ``Example:`` op dict and an ``Errors:`` list of the codes that op can raise,
    so the agent sees a concrete, structurally-valid call and its main failure
    modes inline. A trailing :func:`error_catalog` block decodes every code.
    """
    entries = op_catalog()
    by_group: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_group.setdefault(entry.get("group", "extension"), []).append(entry)
    lines: list[str] = ["lumenframe LayerPatch ops — {\"version\":1,\"ops\":[{\"op\":...}]}:"]
    ordered_groups = list(_GROUP_LABELS) + sorted(g for g in by_group if g not in _GROUP_LABELS)
    for group in ordered_groups:
        items = by_group.get(group)
        if not items:
            continue
        lines.append(f"\n[{_GROUP_LABELS.get(group, group.title())}]")
        for entry in sorted(items, key=lambda e: e["op"]):
            args = ", ".join(entry.get("args", []))
            lines.append(f"  {entry['op']}({args}) — {entry.get('summary', '')}")
            example = entry.get("example")
            if example:
                lines.append(f"    Example: {json.dumps(example, ensure_ascii=False)}")
            errors = entry.get("errors")
            if errors:
                lines.append(f"    Errors: {'; '.join(errors)}")
    # The scene-template component library — stamped via apply_template. Listed
    # here (lazily imported to avoid an import cycle) so the agent sees the whole
    # library of ready-made, styled scenes right beside the raw op vocabulary.
    try:
        from lumenframe.templates import describe_templates

        lines.append("\n[Scene template library]")
        lines.append(describe_templates())
    except Exception:  # pragma: no cover - templates are optional at import time
        pass
    # The graphic-element overlay library — stamped via apply_element. Same lazy
    # import + tolerant guard as the scene templates above.
    try:
        from lumenframe.elements import describe_elements

        lines.append("\n[Element library]")
        lines.append(describe_elements())
    except Exception:  # pragma: no cover - elements are optional at import time
        pass
    # Decode every error code once at the end so the agent can self-correct.
    lines.append("\n[Error codes]")
    for code, meaning in error_catalog().items():
        lines.append(f"  {code}: {meaning}")
    return "\n".join(lines)


#: Stable LayerPatchError codes -> short, agent-facing meaning. Kept in lock-step
#: with the per-op handler hints in :mod:`lumenframe.ops`; a test asserts the two
#: cover the same codes so the catalogue can never silently drift from the ops.
_ERROR_CATALOG: dict[str, str] = {
    "E_PATCH": "patch envelope is wrong; send {\"version\": 1, \"ops\": [...]}",
    "E_OP": "each op must be a JSON object with an 'op' key",
    "E_OP_UNKNOWN": "unknown op name; list available ops before patching",
    "E_ARG": "a required argument is missing or null for this op",
    "E_NOT_FOUND": "the layer/effect/keyframe id does not exist; inspect current state first",
    "E_RANGE": "value out of allowed range (e.g. negative duration, split outside the layer)",
    "E_SPEED": "speed must be greater than 0",
    "E_ROOT": "this op cannot target the root composition",
    "E_CONTAINER": "that parent layer cannot hold children",
    "E_CYCLE": "cannot move a layer into itself or one of its descendants",
    "E_GROUP_PARENT": "all selected layers must share the same parent",
    "E_TYPE": "the layer type is unknown or not valid for this op",
    "E_MASK": "mask/matte source is missing, self-referential, or unknown",
    "E_UNSAFE": "the expression is invalid or not allowed",
    "E_DOC": "the document is missing its root composition",
    "E_ID": "every layer needs an id",
    "E_DUP_ID": "duplicate layer id in the resulting document",
}


def error_catalog() -> dict[str, str]:
    """Map every stable ``LayerPatchError`` code to a short, agent-facing meaning.

    These are the codes surfaced by :func:`lumenframe.ops.validate_patch` /
    :func:`lumenframe.ops.apply_layer_patch`; pairing them with the per-op
    ``errors`` lists lets the agent decode a failure and self-correct without a
    round-trip.
    """
    return dict(_ERROR_CATALOG)


#: Documentation for each built-in effect type.
#:
#: ``types`` is the tuple of effect-type strings that share this row (aliases,
#: e.g. ``("mirror", "flip")``). The display name joins them with ``|``. This is
#: the single source for the prompt block *and* the drift guard: the union of
#: all ``types`` here must equal the dispatch table ``lumenframe.compile.EFFECTS``
#: keys (asserted by ``tests/test_lumenframe_effects_table.py``), so the
#: vocabulary and its documentation can never silently drift from the renderer.
EFFECTS_CATALOG: list[dict[str, Any]] = [
    {"types": ("gaussian_blur",), "args": "radius(float)",
     "desc": "Gaussian blur applied to RGB with premultiplied alpha."},
    {"types": ("color_grade",),
     "args": "brightness, contrast, saturation, exposure, temperature, tint, highlights, shadows, gamma, lift, gain",
     "desc": "DaVinci-style colour grading."},
    {"types": ("brightness",), "args": "value(float)", "desc": "Adjust brightness."},
    {"types": ("contrast",), "args": "value(float)", "desc": "Adjust contrast."},
    {"types": ("saturation",), "args": "value(float)",
     "desc": "Adjust saturation (0=grey, 1=normal, >1=saturated)."},
    {"types": ("invert",), "args": "", "desc": "Invert RGB channels; alpha unchanged."},
    {"types": ("grayscale",), "args": "amount(0..1, default 1.0)",
     "desc": "Blend towards greyscale (0=color, 1=grey)."},
    {"types": ("mirror", "flip"), "args": "direction(horizontal|vertical|both, default horizontal)",
     "desc": "Flip/mirror the frame."},
    {"types": ("crop",), "args": "x0, y0, x1, y1 (normalized [0,1], default 0,0,1,1)",
     "desc": "Crop to rectangle; outside alpha=0."},
    {"types": ("vignette",), "args": "amount(0..1, default 0.5)",
     "desc": "Radial edge darkening with gaussian falloff."},
    {"types": ("sharpen",), "args": "amount(float, default 1.0)", "desc": "Unsharp mask sharpening."},
    {"types": ("hue_rotate",), "args": "degrees or value(float)", "desc": "Rotate hue in HSV space."},
    {"types": ("chroma_key",),
     "args": "key_color(#hex or [r,g,b], default #00FF00), threshold(0..1, default 0.4), softness(0..1, default 0.1)",
     "desc": "Distance-based alpha keying; pixels near key_color become transparent."},
    {"types": ("advanced_chroma_key",),
     "args": "key_color, similarity(default 0.22), softness(default 0.12), spill/despill(0..1), edge_blur(px)",
     "desc": "HSV-aware chroma key with soft edges and despill for green/blue screen footage."},
    {"types": ("luma_key",),
     "args": "threshold(0..1), softness(0..1), mode(key_dark|key_bright)",
     "desc": "Brightness-based matte; removes dark or bright pixels into alpha."},
    {"types": ("curves",),
     "args": "channel(r|g|b|rgb|luma, default rgb), points([[x,y],...] in [0,1], default identity)",
     "desc": "DaVinci-style monotone tone curve (LUT) per channel; identity points are a no-op."},
]


def effect_types() -> set[str]:
    """The canonical set of built-in effect type strings (aliases included).

    Derived from :data:`EFFECTS_CATALOG`; the drift guard asserts this equals the
    dispatch table ``lumenframe.compile.EFFECTS`` keys.
    """
    return {t for entry in EFFECTS_CATALOG for t in entry["types"]}


def describe_effects() -> str:
    """List of built-in and supported effect types for the effect chain."""
    lines = ["Effect types (used in add_effect / set_effect_params):"]
    for entry in EFFECTS_CATALOG:
        name = "|".join(entry["types"])
        lines.append(f"  {name}: {entry['args']}")
        lines.append(f"    {entry['desc']}")
    return "\n".join(lines)


def transition_kinds() -> set[str]:
    """The transition kinds the renderer can synthesise (``add_transition`` kind).

    Derived from the renderer's ``lumenframe.compile.TRANSITION_KINDS`` so the
    documented vocabulary can never claim a kind the compiler doesn't draw.
    """
    from lumenframe.compile import TRANSITION_KINDS

    return set(TRANSITION_KINDS)
