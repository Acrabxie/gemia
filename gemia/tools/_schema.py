"""Lumeri v3 verb schemas.

Pure data. Defines the creative-action tools the model can call. Each
schema follows the OpenAI function-calling shape OpenRouter accepts for
Gemini 3.1 Pro:

    {
        "type": "function",
        "function": {
            "name": "edit_video",
            "description": "...",
            "parameters": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }
    }

Asset references are always ``asset_id`` strings (e.g. ``v_001``,
``img_003``). Never file paths. The host owns id allocation and path
resolution.
"""
from __future__ import annotations

from typing import Any


def _tool(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_ASSET_ID = {
    "type": "string",
    "description": "An asset_id from the session registry (e.g. v_001, img_003).",
}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _tool(
        "generate_image",
        "Create a new still image from a text prompt. Returns a new asset_id.",
        {
            "prompt": {"type": "string", "description": "What the image should depict."},
            "aspect_ratio": {
                "type": "string",
                "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"],
                "description": "Frame aspect ratio.",
            },
            "style": {"type": "string", "description": "Optional aesthetic hint (e.g. 'cinematic, soft light')."},
            "reference_asset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional reference assets for image-to-image guidance.",
            },
        },
        ["prompt"],
    ),
    _tool(
        "generate_video",
        "Submit a Vertex AI Veo video generation job. Returns a job_id immediately (non-blocking). Use check_job(job_id) to poll or wait_for_job(job_id) to block until the video asset is ready. Expensive — prefer search_library or local editing when existing footage can satisfy the task.",
        {
            "prompt": {"type": "string", "description": "What the clip should show."},
            "duration_sec": {"type": "number", "description": "Target duration in seconds (max 8)."},
            "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"]},
            "reference_asset_id": {"type": "string", "description": "Optional starting image for image-to-video."},
            "camera": {"type": "string", "description": "Optional motion hint (e.g. 'slow dolly in')."},
        },
        ["prompt"],
    ),
    _tool(
        "generate_audio",
        "Create new music or sound from a text prompt using Vertex AI Lyria. Returns an audio asset_id after writing the generated WAV to the session workspace. Lyria currently returns a fixed short clip; use edit/mix tools afterward for timeline placement.",
        {
            "prompt": {"type": "string", "description": "What the audio should sound like."},
            "duration_sec": {"type": "number", "description": "Requested duration hint. Vertex Lyria may ignore this and return its fixed clip length."},
            "mood": {"type": "string", "description": "Optional mood (e.g. 'tense, low')."},
            "bpm": {"type": "number", "description": "Optional tempo target."},
        },
        ["prompt"],
    ),
    _tool(
        "narrate",
        "Turn a line of script into spoken voiceover (human-voice text-to-speech). Use this for narration/口播/解说 — generate_audio only makes music. Returns an audio asset_id and its measured duration, so you can pace the cut to the voiceover. Pass a system voice name (English 'Ava'/'Samantha', Chinese 'Tingting'/'Meijia') or omit for the system default.",
        {
            "text": {"type": "string", "description": "The script line(s) to speak."},
            "voice": {"type": "string", "description": "System TTS voice name. Omit for the system default."},
            "rate": {"type": "integer", "description": "Speaking rate in words per minute (80–400, default 175)."},
        },
        ["text"],
    ),
    _tool(
        "subtitle",
        "Put a timed subtitle track on a video and return a NEW video asset_id. source='text' (default): caption from words you already have (a narration script or explicit cues) — split evenly across the clip, exact wording, always works. source='transcribe': recover the words from the video's own speech via Whisper (needs openai-whisper). Use burn=false to add a toggleable soft-subtitle track instead of hard-coding it.",
        {
            "asset_id": {"type": "string", "description": "The video asset to caption."},
            "source": {"type": "string", "enum": ["text", "transcribe"], "description": "Where the words come from. Default 'text'."},
            "text": {"type": "string", "description": "The subtitle text (source='text'). Split into timed cues across the clip."},
            "cues": {"type": "array", "description": "Optional explicit cues [{start,end,text}] instead of auto-splitting text.",
                     "items": {"type": "object", "properties": {
                         "start": {"type": "number"}, "end": {"type": "number"}, "text": {"type": "string"}}}},
            "language": {"type": "string", "description": "Language code for transcribe (e.g. 'en', 'zh') or soft-track tag."},
            "burn": {"type": "boolean", "description": "Burn into the picture (default true) or mux a toggleable soft track (false)."},
            "style": {"type": "object", "description": "Optional look: font_size, font_color, outline_color, outline_width, margin_v."},
        },
        ["asset_id"],
    ),
    _tool(
        "animate_captions",
        "Add per-word animated captions (karaoke/word-pop, TikTok/Reels style) to a video and return a NEW video asset_id. Unlike `subtitle` (a static line track), each word highlights as it lands. Supply `text` (words spread evenly across the clip — no ASR needed) or explicit `word_timings` for exact sync. Renders per-frame, so it's slower — use it for the hero caption pass, not every clip.",
        {
            "asset_id": {"type": "string", "description": "The video asset to caption."},
            "text": {"type": "string", "description": "The words to animate (distributed evenly across the clip)."},
            "word_timings": {"type": "array", "description": "Optional exact per-word timing [{word,start_seconds,end_seconds}] instead of even distribution.",
                             "items": {"type": "object", "properties": {
                                 "word": {"type": "string"}, "start_seconds": {"type": "number"}, "end_seconds": {"type": "number"}}}},
            "preset": {"type": "string", "enum": ["karaoke_pop", "quiet_captions"], "description": "Animation style. Default karaoke_pop (active word pops); quiet_captions is subtler."},
            "font_size": {"type": "integer", "description": "Caption font size in px. Default 54."},
            "active_color": {"type": "string", "description": "Color of the currently-spoken word. Default yellow."},
            "inactive_color": {"type": "string", "description": "Color of the other words. Default white."},
        },
        ["asset_id"],
    ),
    _tool(
        "edit_image",
        "Transform an existing image. Returns a new asset_id. Use "
        "operation='remove_background' to cut out the person/subject with real ML "
        "matting (clean alpha on any background — this is the '抠像/抠图/keying' verb).",
        {
            "asset_id": _ASSET_ID,
            "operation": {
                "type": "string",
                "enum": ["crop", "rotate", "resize", "blur", "denoise", "remove_background"],
            },
            "params": {
                "type": "object",
                "description": (
                    "Operation-specific parameters. rotate:{angle_deg}; crop:{x,y,w,h}; "
                    "resize:{w,h}; blur:{radius}; denoise:{strength}. "
                    "remove_background:{background?, feather?, matte_only?} — background "
                    "null→transparent PNG cutout, or a colour name/[r,g,b]/asset_id/path to "
                    "composite the subject over; feather softens the edge (px); "
                    "matte_only=true returns the grayscale alpha mask."
                ),
            },
        },
        ["asset_id", "operation"],
    ),
    _tool(
        "edit_video",
        "Transform an existing video. Operations: trim (cut a segment), concat (join clips end-to-end), reverse (play backward), speed (change playback speed). Returns a new asset_id.",
        {
            "asset_id": _ASSET_ID,
            "operation": {
                "type": "string",
                "enum": ["trim", "concat", "reverse", "speed"],
            },
            "trim": {
                "type": "object",
                "description": "For trim. {start_sec, end_sec}. end_sec=null means 'to end of clip'.",
                "properties": {
                    "start_sec": {"type": "number"},
                    "end_sec": {"type": ["number", "null"]},
                },
            },
            "concat_with": {
                "type": "array",
                "items": {"type": "string"},
                "description": "For concat. Additional asset_ids to append after asset_id, in order.",
            },
            "speed_factor": {
                "type": "number",
                "description": "For speed. >1 faster, <1 slower.",
            },
        },
        ["asset_id", "operation"],
    ),
    _tool(
        "composite",
        "Combine two visual layers into one. Returns a new asset_id.",
        {
            "base_asset_id": _ASSET_ID,
            "overlay_asset_id": _ASSET_ID,
            "mode": {"type": "string", "enum": ["alpha", "blend", "screen", "multiply"]},
            "opacity": {"type": "number", "description": "0..1"},
            "position": {
                "type": "object",
                "description": "Optional pixel offset {x, y} of the overlay's top-left.",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            },
            "scale": {"type": "number", "description": "Optional overlay scale factor."},
        },
        ["base_asset_id", "overlay_asset_id", "mode"],
    ),
    _tool(
        "color_grade",
        "Apply one of the named color looks to a video. Returns a new asset_id.",
        {
            "asset_id": _ASSET_ID,
            "look": {
                "type": "string",
                "enum": ["warm", "cool", "vintage", "cinematic", "teal_orange", "neutral"],
                "description": "The color look to apply. color_grade does color looks only — there is no grayscale/black-and-white and no mirror/flip.",
            },
            "intensity": {
                "type": "number",
                "description": "0..1, default 1.0. Lower values blend the graded look with the original.",
            },
        },
        ["asset_id", "look"],
    ),
    _tool(
        "adjust_media",
        "Direct lightweight photo-app style adjustments for an image or video: brightness, contrast, saturation, exposure, and gamma. Returns a new asset_id. Use this for precise basic corrections instead of pretending a color_grade preset is the same thing.",
        {
            "asset_id": _ASSET_ID,
            "brightness": {
                "type": "number",
                "description": "Brightness offset from -1.0 to 1.0. 0 keeps original brightness.",
            },
            "contrast": {
                "type": "number",
                "description": "Contrast multiplier from 0.0 to 3.0. 1 keeps original contrast.",
            },
            "saturation": {
                "type": "number",
                "description": "Saturation multiplier from 0.0 to 3.0. 1 keeps original saturation; 0 is grayscale.",
            },
            "exposure": {
                "type": "number",
                "description": "Exposure change in stops from -5.0 to 5.0. 0 keeps original exposure.",
            },
            "gamma": {
                "type": "number",
                "description": "Gamma from 0.1 to 10.0. 1 keeps original gamma.",
            },
        },
        ["asset_id"],
    ),
    _tool(
        "paint_overlay",
        "Draw a visible annotation as a transparent PNG and place it on the current timeline as an undoable overlay clip. Use for circles, arrows, boxes, highlights, strokes, and short labels. Coordinates are normalized 0..1 canvas coordinates. First version is static/keyframed only; it does not track moving objects.",
        {
            "shape": {
                "type": "string",
                "enum": ["stroke", "rect", "ellipse", "circle", "arrow", "highlight", "text"],
                "description": "What to draw.",
            },
            "points": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "number"}},
                "description": "Normalized points [[x,y], ...]. Stroke/arrow need 2+; rect/ellipse/highlight can use two opposite corners.",
            },
            "rect": {
                "type": "array",
                "items": {"type": "number"},
                "description": "Optional normalized [x0,y0,x1,y1] bounds for rect/ellipse/highlight.",
            },
            "text": {"type": "string", "description": "For shape=text."},
            "color": {"type": "string", "description": "#rrggbb, default #ff3030."},
            "width": {"type": "number", "description": "Stroke/outline width in pixels."},
            "opacity": {"type": "number", "description": "0..1 clip opacity."},
            "feather": {"type": "number", "description": "Optional alpha feather/blur in pixels."},
            "time_range": {
                "type": "array",
                "items": {"type": "number"},
                "description": "Optional [start_sec,end_sec].",
            },
            "start_sec": {"type": "number", "description": "Timeline start second. Alias: at_time."},
            "at_time": {"type": "number", "description": "Alias for start_sec."},
            "end_sec": {"type": "number", "description": "Timeline end second."},
            "duration": {"type": "number", "description": "Overlay duration when end_sec/time_range is omitted. Default 3s."},
            "track_id": {"type": "string", "description": "Optional overlay track id. If omitted, Lumeri picks a non-overlapping OV track."},
        },
        ["shape"],
    ),
    _tool(
        "paint_mask_effect",
        "Apply a local static mask effect to an image or video asset and return a new asset_id without modifying the source. Use for regional blur, mosaic, dim outside, highlight, and local brightness/contrast/saturation/exposure/gamma adjustments. First version is static/keyframed only; it does not track moving objects.",
        {
            "asset_id": _ASSET_ID,
            "effect": {
                "type": "string",
                "enum": ["blur", "mosaic", "dim_outside", "highlight", "adjust"],
            },
            "mask": {
                "type": "object",
                "description": "Mask shape: {shape:'rect|ellipse|circle|polygon|stroke', points:[[x,y],...], rect:[x0,y0,x1,y1], feather, invert, width}. Coordinates are normalized 0..1.",
            },
            "params": {
                "type": "object",
                "description": "Effect params: blur radius, mosaic block_size, highlight/dim amount+color, or adjust brightness/contrast/saturation/exposure/gamma.",
            },
            "start_sec": {"type": "number", "description": "Video only: first second where the effect applies. Default 0."},
            "end_sec": {"type": "number", "description": "Video only: effect stops before this second. Omit for the rest of the asset."},
        },
        ["asset_id", "effect", "mask"],
    ),
    _tool(
        "add_overlay",
        "Burn a text caption, image overlay, or subtitle onto a video. Returns a new asset_id.",
        {
            "asset_id": _ASSET_ID,
            "kind": {"type": "string", "enum": ["text", "image", "subtitle"]},
            "text": {"type": "string", "description": "For kind=text or kind=subtitle."},
            "overlay_asset_id": {"type": "string", "description": "For kind=image. The asset to overlay."},
            "position": {
                "type": "string",
                "enum": [
                    "top_left", "top_center", "top_right",
                    "center_left", "center", "center_right",
                    "bottom_left", "bottom_center", "bottom_right",
                ],
                "description": "Anchor position on the frame.",
            },
            "start_sec": {"type": "number", "description": "When the overlay appears."},
            "end_sec": {"type": "number", "description": "When the overlay disappears. Omit for whole clip."},
            "font_size": {"type": "integer", "description": "For kind=text. Default 32."},
            "font_color": {"type": "string", "description": "For kind=text. e.g. 'white', '#ffcc00'."},
        },
        ["asset_id", "kind"],
    ),
    _tool(
        "arrange_timeline",
        "Sequence multiple clips in order, with optional transitions. Returns a new asset_id (the assembled timeline).",
        {
            "asset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Clips to place on the timeline, in playback order.",
            },
            "transitions": {
                "type": "array",
                "description": "Optional transitions between adjacent clips.",
                "items": {
                    "type": "object",
                    "properties": {
                        "between_index": {"type": "integer", "description": "0-based index of the cut (between clip i and i+1)."},
                        "kind": {"type": "string", "enum": ["cut", "dissolve", "wipe", "fade"]},
                        "duration_sec": {"type": "number"},
                    },
                },
            },
        },
        ["asset_ids"],
    ),
    _tool(
        "mix_audio",
        "Combine multiple audio tracks. Returns a new asset_id.",
        {
            "asset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Audio assets to combine.",
            },
            "mode": {"type": "string", "enum": ["concat", "mix", "duck"]},
            "levels_db": {
                "type": "array",
                "items": {"type": "number"},
                "description": "Optional per-track gain in dB. Same length as asset_ids.",
            },
        },
        ["asset_ids", "mode"],
    ),
    _tool(
        "edit_audio",
        "Lightweight standalone audio preprocessing for one audio asset: volume gain and fade in/out. Returns a new audio asset_id. Use this before timeline placement when a raw audio asset itself needs adjustment.",
        {
            "asset_id": _ASSET_ID,
            "gain_db": {
                "type": "number",
                "description": "Volume gain in decibels, -60..36. 0 keeps original level.",
            },
            "fade_in_sec": {
                "type": "number",
                "description": "Fade-in duration in seconds from the beginning of the asset.",
            },
            "fade_out_sec": {
                "type": "number",
                "description": "Fade-out duration in seconds ending at the asset duration.",
            },
        },
        ["asset_id"],
    ),
    _tool(
        "transform_geometry",
        "Geometric operations on an image or video frame (crop, rotate, scale, perspective warp). Returns a new asset_id.",
        {
            "asset_id": _ASSET_ID,
            "operation": {
                "type": "string",
                "enum": ["crop", "rotate", "scale", "perspective"],
            },
            "params": {
                "type": "object",
                "description": "Operation-specific parameters (e.g. {angle_deg: 15} or {x, y, w, h} for crop).",
            },
        },
        ["asset_id", "operation"],
    ),
    _tool(
        "smart_reframe",
        "Adapt an image or video to a target canvas such as 9:16, 1:1, 4:5, or 16:9 using deterministic center-crop or fit-pad scaling. Returns a new asset_id. Use for fast social-format reframing; pass anchor_x/anchor_y when analysis suggests the subject is off-center.",
        {
            "asset_id": _ASSET_ID,
            "target": {
                "type": "string",
                "enum": [
                    "vertical_9_16",
                    "9:16",
                    "tiktok",
                    "reels",
                    "shorts",
                    "square_1_1",
                    "1:1",
                    "portrait_4_5",
                    "4:5",
                    "landscape_16_9",
                    "16:9",
                    "custom",
                ],
                "description": "Target aspect/canvas preset. Use custom with explicit width and height.",
            },
            "width": {"type": "integer", "description": "Optional target width; required for target=custom."},
            "height": {"type": "integer", "description": "Optional target height; required for target=custom."},
            "mode": {
                "type": "string",
                "enum": ["center_crop", "fit_pad"],
                "description": "center_crop fills the target by cropping overflow; fit_pad preserves all pixels with padding.",
            },
            "anchor_x": {
                "type": "number",
                "description": "Crop anchor from 0 left to 1 right. Default 0.5 center.",
            },
            "anchor_y": {
                "type": "number",
                "description": "Crop anchor from 0 top to 1 bottom. Default 0.5 center.",
            },
            "background": {
                "type": "string",
                "description": "Pad color for fit_pad, e.g. black, white, or #101010.",
            },
        },
        ["asset_id"],
    ),
    _tool(
        "extract_frame",
        "Pull a single still frame from a video at a given timestamp. Returns a new image asset_id.",
        {
            "asset_id": _ASSET_ID,
            "time_sec": {"type": "number", "description": "Timestamp to extract from."},
        },
        ["asset_id", "time_sec"],
    ),
    _tool(
        "probe_media",
        "Cheap ffprobe-style physical metadata for a registered asset. Returns duration_ms, width, height, fps, codecs, channel count, sample rate, stream count, and file size. Use before trim/reframe/audio edits when exact media properties matter; it does not do semantic visual analysis.",
        {
            "asset_id": _ASSET_ID,
        },
        ["asset_id"],
    ),
    _tool(
        "analyze_media",
        "Examine an asset and get a short text description and a thumbnail back. Use this when you want to actually see what an earlier action produced — for self-review or to decide a next step. Costs tokens; do not call as a default pre-flight.",
        {
            "asset_id": _ASSET_ID,
            "focus": {
                "type": "string",
                "description": "Optional aspect to focus on (e.g. 'color palette', 'subject motion', 'visible text').",
            },
        },
        ["asset_id"],
    ),
    _tool(
        "get_safe_areas",
        "Return conservative canvas safe-area boxes and avoid zones for social/video layouts. Use before placing captions, titles, logos, or UI-sensitive overlays on vertical/square deliverables.",
        {
            "platform": {
                "type": "string",
                "enum": [
                    "generic_vertical",
                    "generic",
                    "tiktok",
                    "reels",
                    "instagram",
                    "instagram_reels",
                    "shorts",
                    "youtube_shorts",
                    "square_feed",
                    "square",
                ],
                "description": "Platform/layout preset. Unknown values fall back to generic_vertical.",
            },
            "width": {"type": "integer", "description": "Canvas width in pixels. Default comes from the preset."},
            "height": {"type": "integer", "description": "Canvas height in pixels. Default comes from the preset."},
        },
        [],
    ),
    _tool(
        "inspect_lottie",
        "Render one exact frame from a Lottie/dotLottie asset and return it as an image asset plus a thumbnail. Use before placing motion graphics or when checking icon/logo animation timing.",
        {
            "asset_id": _ASSET_ID,
            "frame": {"type": "integer", "description": "0-based Lottie frame to render. Mutually exclusive with time_sec."},
            "time_sec": {"type": "number", "description": "Animation time in seconds. Alias: time."},
            "time": {"type": "number", "description": "Alias for time_sec."},
            "width": {"type": "integer", "description": "Output width in pixels. Defaults to the Lottie width."},
            "height": {"type": "integer", "description": "Output height in pixels. Defaults to the Lottie height."},
        },
        ["asset_id"],
    ),
    _tool(
        "search_library",
        "Search the user's existing asset library by text query, including persisted media annotation labels/tags/notes. Returns a list of matching asset_ids with brief descriptions.",
        {
            "query": {"type": "string", "description": "Free-text search."},
            "kind": {"type": "string", "enum": ["image", "video", "audio", "any"]},
            "limit": {"type": "integer", "description": "Max results. Default 10."},
        },
        ["query"],
    ),
    _tool(
        "search_media",
        "Semantic search over persistent media annotations (vision captions, "
        "subjects, actions, on-screen text, tags — Chinese and English). Returns "
        "matching assets WITH time ranges so timeline/cut tools can act on them "
        "directly. Free and fast. Results are registered as session asset_ids. If "
        "unindexed_count > 0, consider annotate_media (paid) to index the rest.",
        {
            "query": {"type": "string", "description": "Free text, zh or en, e.g. '海边日落 无人机' or 'woman talking to camera'."},
            "kind": {"type": "string", "enum": ["video", "image", "audio", "any"]},
            "limit": {"type": "integer", "description": "Max assets. Default 8, max 20."},
        },
        ["query"],
    ),
    _tool(
        "assemble_shotlist",
        "Lay the filled storyboard onto the timeline: for every shot that has an asset_id, append a clip trimmed to its planned duration, align its on_screen_text as an overlay, apply its transition, and mark it placed. Call after filling shots (search_media / generate_*). Unfilled shots are skipped and reported. Use rebuild=true to clear the timeline and reassemble after revising the plan. Then inspect_timeline to review, or export to render.",
        {
            "rebuild": {"type": "boolean", "description": "Clear the current timeline and reassemble from scratch. Default false (append newly-filled shots)."},
        },
        [],
    ),
    _tool(
        "search_frames",
        "Semantic footage search over real material (filename + probed visual/dialog labels), ranked by relevance. This is the '先搜真素材' step: fill a shot from raw footage before falling back to generate_video. Returns matching asset_ids ready to use. If it finds nothing, generate the shot instead. Probes frames live so it needs NO prior annotation — complements search_media (which queries saved annotations and returns timecodes). Stronger than search_library (a plain filename/label text match).",
        {
            "query": {"type": "string", "description": "What the shot should show, e.g. 'city sunrise timelapse aerial'."},
            "kind": {"type": "string", "enum": ["video", "image", "any"], "description": "Media kind to search. Default video."},
            "limit": {"type": "integer", "description": "Max results. Default 5."},
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional extra local files/folders to include in the search."},
        },
        ["query"],
    ),
    _tool(
        "draft_shotlist",
        "Turn a ONE-LINE theme into a complete promo storyboard in one call: scenes -> shots with durations, on-screen text, voiceover (narration), mood tags, and per-shot search_query, following a proven structure. Use this to START outline-driven editing from just a sentence, then fill each shot (search_frames/search_media/generate_*) and assemble_shotlist. It REPLACES the current shotlist (set replace=false to preview without persisting). A scaffold — refine wording/footage/timings after.",
        {
            "theme": {"type": "string", "description": "One line describing the video, e.g. '一款帮你专注的极简待办 App' or 'A minimalist focus timer'."},
            "template": {"type": "string", "enum": ["promo", "story"], "description": "Structure. 'promo' = Hook→Problem→Solution→Highlights→CTA (default). 'story' = Setup→Rising→Turn→Climax→Resolution."},
            "target_duration_sec": {"type": "number", "description": "Total target length in seconds. Default 30."},
            "style": {"type": "string", "description": "Optional look/tone, e.g. 'cinematic promo, warm'."},
            "language": {"type": "string", "enum": ["zh", "en"], "description": "Language of the drafted text. Auto-detected from the theme if omitted."},
            "replace": {"type": "boolean", "description": "Replace the current shotlist (default true). false = return the draft without persisting."},
        },
        ["theme"],
    ),
    _tool(
        "set_shotlist",
        "Draft or replace the whole storyboard (shotlist) for outline/storyboard-driven editing. Turn the user's brief/outline into scenes → shots; each shot states what it should show, how long, on-screen text, and how to source footage. This is a PLAN, not the timeline — nothing renders until you assemble_shotlist. Prefer source='search' (find real footage) and only source='generate' when nothing fits. Persisted + undoable.",
        {
            "shotlist": {
                "type": "object",
                "description": "The storyboard plan.",
                "properties": {
                    "logline": {"type": "string", "description": "One-line summary of the video."},
                    "style": {"type": "string", "description": "Look/tone, e.g. 'cinematic promo, warm'."},
                    "target_duration_sec": {"type": "number", "description": "Optional total target length."},
                    "scenes": {
                        "type": "array",
                        "description": "Ordered scenes, each a group of shots.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Stable scene id (optional; auto-filled)."},
                                "title": {"type": "string"},
                                "shots": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string", "description": "Stable shot id you will reference in update_shot (optional; auto-filled)."},
                                            "description": {"type": "string", "description": "What this shot shows (the visual intent)."},
                                            "duration_sec": {"type": "number", "description": "Planned on-screen duration. Default 3."},
                                            "on_screen_text": {"type": "string", "description": "Optional title/caption burned over this shot."},
                                            "narration": {"type": "string", "description": "Optional voiceover line for this shot (script the narrate tool speaks). Not burned on screen."},
                                            "mood": {"type": "string", "description": "Optional emotion/tone tag, e.g. 'energetic','tense','hopeful','calm','inviting'."},
                                            "source": {"type": "string", "enum": ["search", "generate", "unset"], "description": "How to fill this shot. Prefer 'search'."},
                                            "search_query": {"type": "string", "description": "Query for search_frames/search_media when source='search'."},
                                            "transition_after": {
                                                "type": "object",
                                                "description": "Transition INTO the next shot (omit or kind='cut' for a hard cut).",
                                                "properties": {
                                                    "kind": {"type": "string", "enum": ["cut", "dissolve", "wipe", "fade"]},
                                                    "duration_sec": {"type": "number"},
                                                },
                                            },
                                            "notes": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
        ["shotlist"],
    ),
    _tool(
        "update_shot",
        "Revise ONE shot in the current shotlist by id without resending the whole plan. Use to mark a shot filled (set asset_id + source) after search/generate, to mark it placed (clip_id) after assembly, or to retime/reword it. The shot 'id' itself cannot be changed. Persisted + undoable.",
        {
            "shot_id": {"type": "string", "description": "The shot's id (from set_shotlist / get_shotlist)."},
            "fields": {
                "type": "object",
                "description": "Fields to merge, e.g. {asset_id, source, status, duration_sec, on_screen_text, narration, mood, search_query, description, transition_after, notes}.",
                "properties": {
                    "asset_id": {"type": "string"},
                    "source": {"type": "string", "enum": ["search", "generate", "unset"]},
                    "status": {"type": "string", "enum": ["draft", "filled", "placed"]},
                    "duration_sec": {"type": "number"},
                    "on_screen_text": {"type": "string"},
                    "narration": {"type": "string"},
                    "mood": {"type": "string"},
                    "search_query": {"type": "string"},
                    "description": {"type": "string"},
                    "notes": {"type": "string"},
                },
            },
        },
        ["shot_id", "fields"],
    ),
    _tool(
        "get_shotlist",
        "Read the current storyboard (shotlist) with each shot's id, status, planned duration, source, asset_id, and clip_id. Call before revising shots or assembling so you use the right shot ids.",
        {},
        [],
    ),
    _tool(
        "refine_shot",
        "Edit ONE already-assembled shot in place WITHOUT reassembling the timeline. Pick exactly one operation: retime (duration_sec), replace footage (asset_id — must be a registered asset, preserves position), recaption (on_screen_text, '' clears it), or remove (remove=true drops the shot's clip). Reuses timeline ops with ripple so neighbors reflow. The shot must already be placed (assemble_shotlist) — otherwise it returns guidance to assemble first. Cheaper than assemble_shotlist(rebuild=true) for a one-shot tweak.",
        {
            "shot_id": {"type": "string", "description": "The shot's id (from get_shotlist)."},
            "duration_sec": {"type": "number", "description": "RETIME: new on-screen duration for this shot."},
            "asset_id": {"type": "string", "description": "REPLACE: new footage asset (registered). Keeps the shot's position + duration."},
            "on_screen_text": {"type": "string", "description": "RECAPTION: new burned caption; empty string removes it."},
            "remove": {"type": "boolean", "description": "REMOVE: true drops this shot's clip (and caption) from the cut."},
        },
        ["shot_id"],
    ),
    _tool(
        "annotate_media",
        "Create persistent Gemini-style annotations for media-library assets. Use this for long videos or bulk footage triage before searching, cutting, or assembling. Writes asset-level tags plus timecoded review markers.",
        {
            "asset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Media-library asset ids, e.g. asset_0123abcd....",
            },
            "all": {
                "type": "boolean",
                "description": "If true, annotate a batch from the media library instead of explicit asset_ids.",
            },
            "query": {
                "type": "string",
                "description": "Optional media-library query when all=true.",
            },
            "kind": {
                "type": "string",
                "enum": ["video", "image", "audio", "any"],
                "description": "Media kind for all=true. Default video.",
            },
            "mode": {
                "type": "string",
                "enum": ["quick", "detailed"],
                "description": "quick creates fewer markers; detailed samples more ranges.",
            },
            "max_assets": {"type": "integer", "description": "Batch cap. Default is the explicit list length or 20 for all=true."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags/taxonomy to add to generated annotations.",
            },
            "language": {
                "type": "string",
                "description": "Language for labels/notes. Use the user's prompt language, e.g. zh or en.",
            },
            "replace_existing": {
                "type": "boolean",
                "description": "Replace existing Gemini-source annotations for these assets. Default true.",
            },
        },
        [],
    ),
    _tool(
        "get_media_annotations",
        "Read persistent annotations for one media-library asset: asset summary, tags, labels, and timecoded markers.",
        {
            "asset_id": {"type": "string", "description": "Media-library asset id, e.g. asset_0123abcd...."},
        },
        ["asset_id"],
    ),
    _tool(
        "write_media_annotation",
        "Create or update one persistent annotation on a media-library asset. Use this to mark useful ranges, problems, subjects, quality notes, or cut candidates.",
        {
            "asset_id": {"type": "string", "description": "Media-library asset id, e.g. asset_0123abcd...."},
            "annotation_id": {"type": "string", "description": "Optional existing annotation id to update."},
            "scope": {"type": "string", "enum": ["asset", "time_range", "frame"]},
            "start_sec": {"type": "number", "description": "Start timestamp for time_range annotations."},
            "end_sec": {"type": "number", "description": "End timestamp for time_range annotations."},
            "frame": {"type": "integer", "description": "Optional exact frame number."},
            "label": {"type": "string", "description": "Short marker title."},
            "note": {"type": "string", "description": "Longer annotation note."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "category": {"type": "string", "description": "Category such as summary, segment, cut_candidate, quality, warning."},
            "confidence": {"type": "number", "description": "0..1 confidence."},
            "language": {"type": "string", "description": "Language for label/note."},
            "metadata": {"type": "object", "description": "Optional structured metadata."},
        },
        ["asset_id", "label"],
    ),
    _tool(
        "export",
        "Final encode of an asset at requested quality and format. Returns the path to the exported file as a new asset_id.",
        {
            "asset_id": _ASSET_ID,
            "format": {"type": "string", "enum": ["mp4", "mov", "webm", "gif"]},
            "quality": {
                "type": "string",
                "enum": ["4k", "1080p", "720p", "480p", "draft"],
            },
            "platform": {
                "type": "string",
                "enum": ["youtube", "instagram", "tiktok", "twitter", "prores", "generic"],
                "description": "Optional platform preset for additional tuning.",
            },
            "codec": {
                "type": "string",
                "enum": ["h264", "h265"],
                "description": "Video codec for mp4/mov (default h264). h265/HEVC gives a smaller file at similar quality; ignored for webm/gif.",
            },
            "video_bitrate": {
                "type": "string",
                "description": "Optional target video bitrate (e.g. '8M', '800k'). Switches from quality-based CRF to constrained bitrate; use for a fixed delivery size.",
            },
            "color": {
                "type": "string",
                "enum": ["auto", "bt709"],
                "description": "Tag Rec.709 primaries/transfer/matrix (bt709) for broadcast-correct HD color, or leave auto (default).",
            },
            "fps": {
                "type": "number",
                "description": "Optional output frame-rate override (1-120).",
            },
            "audio_bitrate": {
                "type": "string",
                "description": "Optional audio bitrate override (e.g. '192k', '256k').",
            },
        },
        ["asset_id", "format", "quality"],
    ),
    _tool(
        "web_search",
        "Search the public web from the host side and return compact result titles, URLs, snippets, and a saved JSON path. Use this to discover current sources before opening a page or fetching a file. The sandbox remains network-denied; raw HTML is not returned. "
        "BYOK: pluggable search engines, each reading its key from ~/.gemia/config.json — "
        "tavily (tavily_api_key), serper (serper_api_key), brave (brave_api_key), exa (exa_api_key), "
        "google_cse (google_cse_key + google_cse_id), bing (bing_api_key). "
        "searxng is a keyless self-hosted metasearch engine (set searxng_url, optional searxng_api_key) — the recommended free default. "
        "With provider=auto (default) a configured paid key wins first (order: tavily, serper, brave, exa, google_cse, bing), then searxng if searxng_url is set, "
        "else duckduckgo (no config) is used. Set config search_provider to force one engine globally, or pass provider per call. "
        "If a configured provider errors, the result falls back to duckduckgo and includes a 'fallback' note; the served engine is reported in 'provider'.",
        {
            "query": {
                "type": "string",
                "description": "Search query, e.g. 'current YouTube Shorts safe zone dimensions'.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return. Defaults to a full result page (10); clamped to 1..10.",
            },
            "provider": {
                "type": "string",
                "enum": [
                    "auto",
                    "tavily",
                    "serper",
                    "brave",
                    "exa",
                    "google_cse",
                    "bing",
                    "searxng",
                    "duckduckgo",
                ],
                "description": "Which search engine to use. 'auto' (default) picks the first configured paid BYOK provider, then searxng (if searxng_url is set), else duckduckgo. Paid providers need their key(s) in ~/.gemia/config.json; searxng needs searxng_url (keyless, self-hosted); duckduckgo needs nothing.",
            },
        },
        ["query"],
    ),
    _tool(
        "web_open",
        "Read an https:// web page from the host side and return cleaned text, title, source, and a saved JSON path. Use fetch instead for binary/media downloads. The sandbox remains network-denied; raw HTML is not returned.",
        {
            "url": {
                "type": "string",
                "description": "https:// page URL to read as text.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum cleaned text characters to return. Default 6000; clamped to 500..12000.",
            },
        },
        ["url"],
    ),
    _tool(
        "fetch",
        "Download a file from an https:// URL into the session workspace. Returns asset_id (if media), path, size_bytes, content_type, summary. Enforces https-only; blocks http://, file://, and other schemes. Network access is restricted to the host; the sandbox has no internet. Supports optional filename customization.",
        {
            "url": {
                "type": "string",
                "description": "https:// URL to download from. Must be https (no http, file://, etc.).",
            },
            "dest_name": {
                "type": "string",
                "description": "Optional: target filename in the workspace (default: url basename). Sanitized to prevent path traversal.",
            },
        },
        ["url"],
    ),
    _tool(
        "run_shell",
        "Execute a bash command in an isolated sandbox. Workspace directory is fully readable/writable. Outside workspace: files can only be created, not modified/deleted. Credentials (~/.ssh, ~/.config/gcloud, ~/.gemia/config.json) are not readable. Network access denied. Wraps command with sandbox-exec for M1 isolation. Returns exit_code, stdout_tail, stderr_tail, timed_out, sandbox_enforced, workspace_dir.",
        {
            "command": {
                "type": "string",
                "description": "Bash command string to execute.",
            },
            "timeout_sec": {
                "type": "number",
                "description": "Timeout in seconds (default 30, max 120). Command killed if it exceeds this duration.",
            },
        },
        ["command"],
    ),
    _tool(
        "file_list",
        "List files in a directory. Relative paths resolve inside the session workspace. Credential paths are hidden.",
        {
            "path": {
                "type": "string",
                "description": "Directory path. Relative paths are inside the session workspace. Default '.'.",
            },
            "max_entries": {
                "type": "integer",
                "description": "Maximum entries to return, clamped to 1..500. Default 100.",
            },
        },
        [],
    ),
    _tool(
        "file_read",
        "Read a UTF-8 text file. Relative paths resolve inside the session workspace. Credential paths are blocked.",
        {
            "path": {"type": "string", "description": "File path to read."},
            "max_bytes": {
                "type": "integer",
                "description": "Maximum bytes to read; default 512000, max 2000000.",
            },
        },
        ["path"],
    ),
    _tool(
        "file_write",
        "Write a UTF-8 text file. Inside workspace, full permission including overwrite when overwrite=true. Outside workspace, only creates a NEW file under approved create roots; never overwrites existing outside files or credential paths.",
        {
            "path": {"type": "string", "description": "Target file path. Relative paths are inside the session workspace."},
            "content": {"type": "string", "description": "UTF-8 text content to write."},
            "overwrite": {
                "type": "boolean",
                "description": "Allow replacing an existing file inside the workspace. Ignored outside workspace, where overwrites are refused.",
            },
        },
        ["path", "content"],
    ),
    _tool(
        "file_copy",
        "Copy a file. Sources may be workspace or readable non-credential files. Destinations inside workspace may overwrite with overwrite=true; outside workspace only NEW files under approved create roots are allowed.",
        {
            "source": {"type": "string", "description": "Source file path."},
            "dest": {"type": "string", "description": "Destination file path."},
            "overwrite": {
                "type": "boolean",
                "description": "Allow replacing an existing file inside the workspace only.",
            },
        },
        ["source", "dest"],
    ),
    _tool(
        "file_move",
        "Move a file. Sources may be workspace or readable non-credential files. Destinations inside workspace may overwrite with overwrite=true; outside workspace only NEW files under approved create roots are allowed.",
        {
            "source": {"type": "string", "description": "Source file path."},
            "dest": {"type": "string", "description": "Destination file path."},
            "overwrite": {
                "type": "boolean",
                "description": "Allow replacing an existing file inside the workspace only.",
            },
        },
        ["source", "dest"],
    ),
    _tool(
        "file_delete",
        "Delete a file inside the session workspace. Deleting outside workspace is refused.",
        {
            "path": {"type": "string", "description": "Workspace file path to delete."},
        },
        ["path"],
    ),
    _tool(
        "build",
        "Async submit code to a sandboxed subprocess. Supports Python (default), Node.js, Bash, Go, Ruby, Rust. Executes immediately in a new process group with workspace full r/w and network denied. Returns job_id immediately; use check_job or wait_for_job to poll status. Perfect for long-running code, iteration loops (see→modify→rerun), and skill development.",
        {
            "code": {
                "type": "string",
                "description": "Source code to execute in sandbox (language determined by 'language' parameter).",
            },
            "language": {
                "type": "string",
                "enum": ["python3", "node", "bash", "go", "ruby", "rust"],
                "description": "Programming language (default 'python3'). Choose based on your intent: python3 for data/media work, node for glue code with types, bash for system commands and pipelines.",
            },
            "filename": {
                "type": "string",
                "description": "Output script filename (default 'script.py'). Must be a simple name without path separators.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command-line arguments to pass to the script.",
            },
            "timeout_sec": {
                "type": "number",
                "description": "Timeout in seconds (default 120, clamped to (0, 600]). Process killed if exceeded.",
            },
            "note": {
                "type": "string",
                "description": "Optional human-readable description for the job.",
            },
        },
        ["code"],
    ),
    _tool(
        "check_job",
        "Poll a pending job by job_id. Works for build jobs (status + log tails) and Veo video jobs (status → asset_id when done). Inexpensive. Use to inspect job state without blocking.",
        {
            "job_id": {
                "type": "string",
                "description": "Job identifier returned by build or generate_video.",
            },
        },
        ["job_id"],
    ),
    _tool(
        "wait_for_job",
        "Block until a job completes or max_wait_sec is exceeded. Works for build jobs (polls every 1s) and Veo video jobs (polls every 10s). Returns same shape as check_job plus waited_sec and timed_out flag.",
        {
            "job_id": {
                "type": "string",
                "description": "Job identifier returned by build or generate_video.",
            },
            "max_wait_sec": {
                "type": "number",
                "description": "Maximum seconds to wait (default 60, clamped to (0, 300]). For Veo jobs use 300.",
            },
        },
        ["job_id"],
    ),
    _tool(
        "save_skill",
        "After completing a reusable multi-step task, call save_skill to DISTILL it into a durable skill so it can be reused in future sessions. Capture name + when_to_use (the trigger) + steps (the recipe) + notes. Idempotent: re-saving the same name UPDATES it (no duplicates). (Backward-compat: if you instead pass 'source', it archives that workspace file as a skill via the build artifact path.)",
        {
            "name": {
                "type": "string",
                "description": "Human-readable skill name; also the idempotent key (re-saving updates).",
            },
            "when_to_use": {
                "type": "string",
                "description": "When this skill applies — the trigger / situation that should recall it.",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The reusable recipe: ordered steps/ops to reproduce the task.",
            },
            "notes": {
                "type": "string",
                "description": "Optional caveats, defaults, or extra guidance.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional keyword tags to improve recall.",
            },
            "source": {
                "type": "string",
                "description": "Backward-compat only: workspace-relative file to archive as a skill (e.g. 'builds/build_abc/script.py').",
            },
            "description": {
                "type": "string",
                "description": "Optional description (used by the artifact/source path).",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Artifact path only: if true, replace existing skill of same name. Default false.",
            },
        },
        ["name"],
    ),
    _tool(
        "recall_skills",
        "Call this FIRST, before starting a task, to reuse prior know-how: returns the most relevant saved (distilled) and built-in library skills for your query/task, each with name + when_to_use + recipe steps. Reuse a matching skill instead of re-deriving it.",
        {
            "query": {
                "type": "string",
                "description": "Free-text describing the task you are about to do (matched against skill name, when_to_use, tags, steps, notes).",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of skills to return (default 5).",
            },
            "include_library": {
                "type": "boolean",
                "description": "Also search built-in library skills, not just user-distilled ones. Default true.",
            },
        },
        [],
    ),
    _tool(
        "remember",
        "Call this to REMEMBER a durable user fact or preference across sessions — a standing constraint, a stable preference, a name/handle, a recurring workflow choice. The fact is written to durable memory (MEMORY.md) and shown back to you in the 'What you remember' section of future sessions. Pass a 'title' to make it idempotent: re-remembering the same title UPDATES the note instead of duplicating it. Do NOT store secrets, tokens, passwords, or API keys — those are rejected. For short-lived per-turn progress, use log_note instead, not remember.",
        {
            "content": {
                "type": "string",
                "description": "The durable fact/preference to remember, in plain text.",
            },
            "title": {
                "type": "string",
                "description": "Optional short label/key. Re-remembering with the same title UPDATES the existing note (no duplicates).",
            },
            "kind": {
                "type": "string",
                "description": "Optional category hint, e.g. 'preference', 'constraint', 'fact', 'workflow'.",
            },
        },
        ["content"],
    ),
    _tool(
        "log_note",
        "Append a short one-line note to TODAY'S daily log (a running breadcrumb of progress/decisions). Use for short-lived, in-this-session context worth recording — not for durable facts (use remember for those). Best-effort: empty or secret-looking notes are skipped, not stored. The host already auto-logs a turn summary at turn end; use this to add your own extra breadcrumb mid-turn.",
        {
            "text": {
                "type": "string",
                "description": "The note to append to today's daily log (collapsed to a single line).",
            },
        },
        ["text"],
    ),
    # ── lumenframe layer document verbs ──────────────────────────────────
    # The session owns ONE lumenframe document (layer tree). These verbs
    # expose the LayerPatch vocabulary: low-level lumen_patch for raw ops,
    # or convenience verbs (add_layer, set_transform, etc.) wrapping it.
    _tool(
        "get_lumenframe",
        "Inspect the session lumenframe layer document: layer tree (id, type, name, visibility), selection, and canvas settings.",
        {
            "history": {
                "type": "integer",
                "description": "Reserved for future patch history (currently 0).",
            },
        },
        [],
    ),
    _tool(
        "lumen_patch",
        "Low-level: apply a raw LayerPatch (one or more ops atomically). Each op names its type (add_layer, set_transform, set_opacity, delete_layer, move_layer, set_visibility, select, etc.) and carries the required arguments. Refer to lumenframe.catalog for the complete op vocabulary.",
        {
            "ops": {
                "type": "array",
                "items": {"type": "object"},
                "description": "List of LayerPatch operations: [{op: 'add_layer', type: 'video', ...}, {op: 'set_transform', layer_id: '...', x: 100}, ...].",
            },
        },
        ["ops"],
    ),
    _tool(
        "lumen_add_layer",
        "Convenience verb: create a new layer (video/image/text/shape/audio/adjustment/solid/null/composition).",
        {
            "type": {
                "type": "string",
                "enum": ["video", "image", "text", "shape", "audio", "adjustment", "solid", "null", "composition"],
                "description": "Layer type.",
            },
            "name": {"type": "string", "description": "Optional layer name."},
            "parent_id": {"type": "string", "description": "Optional parent layer id (default: root)."},
            "index": {"type": "integer", "description": "Optional insert position (default: end)."},
            "at_time": {"type": "number", "description": "Optional start time on parent timeline."},
        },
        ["type"],
    ),
    _tool(
        "lumen_set_transform",
        "Convenience verb: move/scale/rotate a layer. Anchor-relative, canvas-centre origin.",
        {
            "layer_id": {"type": "string", "description": "Layer to transform."},
            "x": {"type": "number", "description": "Canvas x offset (px from centre)."},
            "y": {"type": "number", "description": "Canvas y offset."},
            "scale": {"type": "number", "description": "Uniform scale (overrides scale_x/scale_y)."},
            "scale_x": {"type": "number", "description": "Horizontal scale."},
            "scale_y": {"type": "number", "description": "Vertical scale."},
            "rotation": {"type": "number", "description": "Rotation in degrees."},
            "anchor_x": {"type": "number", "description": "Anchor point x (0..1)."},
            "anchor_y": {"type": "number", "description": "Anchor point y (0..1)."},
        },
        ["layer_id"],
    ),
    _tool(
        "lumen_set_opacity",
        "Convenience verb: set layer opacity.",
        {
            "layer_id": {"type": "string", "description": "Layer id."},
            "opacity": {"type": "number", "description": "Opacity value 0..1."},
        },
        ["layer_id", "opacity"],
    ),
    _tool(
        "lumen_delete_layer",
        "Convenience verb: delete one or more layers.",
        {
            "layer_id": {"type": "string", "description": "Layer id to delete (or use layer_ids for multiple)."},
            "layer_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Multiple layer ids to delete.",
            },
        },
        [],
    ),
    _tool(
        "lumen_move_layer",
        "Convenience verb: reparent, reorder (z), retime, or relane a layer.",
        {
            "layer_id": {"type": "string", "description": "Layer to move."},
            "parent_id": {"type": "string", "description": "Optional new parent."},
            "index": {"type": "integer", "description": "Optional new z-order within parent."},
            "lane": {"type": "string", "description": "Optional new lane hint."},
            "start": {"type": "number", "description": "Optional new start time on parent."},
        },
        ["layer_id"],
    ),
    _tool(
        "lumen_set_visibility",
        "Convenience verb: show or hide a layer.",
        {
            "layer_id": {"type": "string", "description": "Layer id."},
            "visible": {"type": "boolean", "description": "True to show, false to hide."},
        },
        ["layer_id", "visible"],
    ),
    _tool(
        "lumen_select",
        "Convenience verb: change the current selection.",
        {
            "layer_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Layer ids to select (empty list + mode='clear' clears selection).",
            },
            "mode": {
                "type": "string",
                "enum": ["replace", "add", "toggle", "clear"],
                "description": "Selection mode. Default 'replace'.",
            },
        },
        [],
    ),
    _tool(
        "lumen_set_mask",
        "Attach, replace, or clear a layer mask. Supports vector shape masks (rectangle/ellipse/polygon/path/bezier), pixel/alpha masks from an asset or inline alpha data, and alpha/luma track mattes.",
        {
            "layer_id": {"type": "string", "description": "Layer to mask."},
            "clear": {"type": "boolean", "description": "If true, remove the layer mask."},
            "kind": {
                "type": "string",
                "enum": ["shape", "pixel", "alpha_matte", "luma_matte"],
                "description": "Mask kind. shape is vector; pixel uses an alpha image/array; matte kinds borrow another layer.",
            },
            "shape": {
                "type": "object",
                "description": "For kind=shape. Examples: {type:'ellipse', cx:.5, cy:.5, rx:.3, ry:.3}; {type:'polygon', points:[[...]]}; {type:'path'|'bezier', points:[[...]], samples:64, fill_rule:'evenodd'}. Coordinates are normalized [0,1].",
            },
            "asset_id": {"type": "string", "description": "For kind=pixel. Image asset whose alpha/luma/r/g/b channel becomes the mask."},
            "alpha": {
                "type": "array",
                "description": "For kind=pixel. Inline 2D alpha matrix in [0,1] or [0,255]. Prefer asset_id for large masks.",
                "items": {"type": "array", "items": {"type": "number"}},
            },
            "channel": {"type": "string", "enum": ["alpha", "luma", "red", "green", "blue"], "description": "Pixel mask channel. Default alpha."},
            "source_layer_id": {"type": "string", "description": "For alpha_matte/luma_matte. Sibling layer to use as matte source."},
            "threshold": {"type": "number", "description": "Optional pixel-mask threshold."},
            "softness": {"type": "number", "description": "Optional threshold softness."},
            "invert": {"type": "boolean", "description": "Invert mask coverage."},
            "feather": {"type": "number", "description": "Soft edge as fraction of min canvas dimension."},
        },
        ["layer_id"],
    ),
    _tool(
        "lumen_key",
        "Apply a keying effect to a layer. Use chroma/advanced_chroma for green/blue screen and luma for brightness-based matte. Produces transparent pixels in the layer render.",
        {
            "layer_id": {"type": "string", "description": "Layer to key."},
            "method": {"type": "string", "enum": ["chroma", "advanced_chroma", "luma"], "description": "Keying method. Default advanced_chroma."},
            "key_color": {"type": "string", "description": "For chroma methods, key colour like #00FF00 or #0000FF."},
            "threshold": {"type": "number", "description": "Basic chroma/luma threshold."},
            "similarity": {"type": "number", "description": "Advanced chroma similarity threshold; lower is stricter."},
            "softness": {"type": "number", "description": "Soft-edge width."},
            "spill": {"type": "number", "description": "0..1 despill amount for advanced chroma."},
            "edge_blur": {"type": "number", "description": "Optional matte edge blur in pixels."},
            "mode": {"type": "string", "enum": ["key_dark", "key_bright"], "description": "For luma key. key_dark removes darker pixels; key_bright removes brighter pixels."},
            "replace_existing": {"type": "boolean", "description": "Default true: remove earlier keying effects on this layer before adding the new one."},
            "params": {"type": "object", "description": "Advanced method-specific params; explicit top-level fields override these."},
        },
        ["layer_id"],
    ),
    _tool(
        "lumen_render",
        "Render the current lumenframe document to a video (MP4) or still frame (PNG). Missing media assets degrade gracefully. Returns asset_id for playback or analysis via analyze_media.",
        {
            "format": {
                "type": "string",
                "enum": ["video", "frame"],
                "description": "Output format. Default 'video' (MP4); 'frame' renders a single PNG.",
            },
            "frame_index": {
                "type": "integer",
                "description": "For format='frame', which frame to render (0-based). Default 0.",
            },
        },
        [],
    ),
    _tool(
        "lumen_set_range",
        "Place an existing layer on an exact frame range. Use when the user says 'put this from frame A to B' or when frame-accurate placement matters.",
        {
            "layer_id": {"type": "string", "description": "Layer to place."},
            "frame_in": {"type": "number", "description": "Inclusive timeline start frame."},
            "frame_out": {"type": "number", "description": "Exclusive timeline end frame; must be > frame_in."},
        },
        ["layer_id", "frame_in", "frame_out"],
    ),
    _tool(
        "lumen_set_lane",
        "Assign an existing layer to a lane/track. Higher lanes composite above lower lanes.",
        {
            "layer_id": {"type": "string", "description": "Layer to relane."},
            "lane": {"type": "integer", "description": "Lane index. 0 is the default; higher lanes are above."},
        },
        ["layer_id", "lane"],
    ),
    _tool(
        "lumen_retime_segment",
        "Speed-change only a sub-range of a layer. The tool splits the segment, applies speed, and ripples the tail so there is no gap/overlap.",
        {
            "layer_id": {"type": "string", "description": "Layer containing the segment."},
            "t0": {"type": "number", "description": "Segment start time in seconds. Use either t0/t1 or frame0/frame1."},
            "t1": {"type": "number", "description": "Segment end time in seconds."},
            "frame0": {"type": "integer", "description": "Segment start frame. Use instead of t0."},
            "frame1": {"type": "integer", "description": "Segment end frame. Use instead of t1."},
            "speed": {"type": "number", "description": "Playback speed for the segment; > 0. 0.5 slow, 2 fast."},
        },
        ["layer_id", "speed"],
    ),
    _tool(
        "lumen_reverse",
        "Reverse a whole layer or only a selected sub-range. Duration is preserved.",
        {
            "layer_id": {"type": "string", "description": "Layer to reverse."},
            "t0": {"type": "number", "description": "Optional segment start seconds; provide with t1."},
            "t1": {"type": "number", "description": "Optional segment end seconds; provide with t0."},
            "frame0": {"type": "integer", "description": "Optional segment start frame; provide with frame1."},
            "frame1": {"type": "integer", "description": "Optional segment end frame; provide with frame0."},
        },
        ["layer_id"],
    ),
    _tool(
        "lumen_time_remap",
        "Attach an explicit output-time to source-time remap curve for speed ramps, freeze frames, loops, and custom timing.",
        {
            "layer_id": {"type": "string", "description": "Layer to retime."},
            "keyframes": {
                "type": "array",
                "description": "List of {t, value, interp?}; t is output seconds, value is source seconds, interp is linear|hold.",
                "items": {"type": "object"},
            },
            "extrapolate": {"type": "string", "enum": ["hold", "loop", "pingpong"], "description": "Behavior outside the curve. Default hold."},
        },
        ["layer_id", "keyframes"],
    ),
    _tool(
        "lumen_speed_ramp",
        "Apply a named speed-ramp preset while preserving layer duration.",
        {
            "layer_id": {"type": "string", "description": "Layer to ramp."},
            "preset": {"type": "string", "enum": ["hero", "montage", "bullet", "ease_in", "ease_out"], "description": "Ramp profile."},
            "extrapolate": {"type": "string", "enum": ["hold", "loop", "pingpong"], "description": "Optional remap extrapolation."},
        },
        ["layer_id", "preset"],
    ),
    _tool(
        "lumen_ripple_delete",
        "Delete a layer and close the gap by shifting later same-lane siblings left.",
        {
            "layer_id": {"type": "string", "description": "Layer to delete."},
        },
        ["layer_id"],
    ),
    _tool(
        "lumen_merge_compositions",
        "Merge source composition timelines into another composition. mode=append places them after existing content; mode=overlay stacks them at their local times.",
        {
            "source_ids": {"type": "array", "items": {"type": "string"}, "description": "Source composition layer ids."},
            "into_id": {"type": "string", "description": "Target composition id, often the root id."},
            "mode": {"type": "string", "enum": ["append", "overlay"], "description": "Merge mode. Default overlay."},
            "offset": {"type": "number", "description": "Optional seconds offset."},
            "keep_sources": {"type": "boolean", "description": "Keep empty source containers instead of removing them."},
        },
        ["source_ids", "into_id"],
    ),
    _tool(
        "lumen_set_work_area",
        "Set or clear the canvas work area. lumen_render_range can default to this range when bounds are omitted.",
        {
            "t_in": {"type": "number", "description": "Work-area start seconds."},
            "t_out": {"type": "number", "description": "Work-area end seconds; must be > t_in."},
            "clear": {"type": "boolean", "description": "If true, clear the work area and ignore t_in/t_out."},
        },
        [],
    ),
    _tool(
        "lumen_seek",
        "Seek/locate the current lumenframe document to a specific moment. Use when the user says 'go to', 'show me at', 'what's happening at <time>', 'jump to frame N', or wants to inspect/preview a single instant. Reports the timeline state at that moment (which layers are active and how they are placed/sampled) AND renders that exact frame to a preview image asset. Pass exactly one of seconds or frame.",
        {
            "seconds": {
                "type": "number",
                "description": "Time on the document timeline to seek to, in seconds. Mutually exclusive with 'frame'.",
            },
            "frame": {
                "type": "integer",
                "description": "Explicit frame index to seek to (0-based). Mutually exclusive with 'seconds'.",
            },
        },
        [],
    ),
    _tool(
        "lumen_render_range",
        "Render or EXPORT only a time range [t_in, t_out) of the current lumenframe document, not the whole thing. Use when the user wants just a slice/segment/clip — 'render seconds 1 to 2.5', 'export the part from 0:05 to 0:10', 'preview the middle section'. Set export=true to write that range to a video (MP4) asset; otherwise returns a short preview (rendered frame count plus a representative middle-frame preview image asset). Times are seconds; require t_in < t_out.",
        {
            "t_in": {
                "type": "number",
                "description": "Inclusive start time of the range, in seconds.",
            },
            "t_out": {
                "type": "number",
                "description": "Exclusive end time of the range, in seconds. Must be greater than t_in.",
            },
            "step": {
                "type": "integer",
                "description": "Stride between rendered frames (>= 1). Default 1.",
            },
            "export": {
                "type": "boolean",
                "description": "If true, export the range to an MP4 video asset; otherwise render a short in-memory preview and report the frame count. Default false.",
            },
        },
        ["t_in", "t_out"],
    ),
    # ── timeline document verbs (timeline v1, 2026-06-13 design) ──────
    # The session owns ONE persistent timeline (tracks + clips). These verbs
    # are fine-grained on purpose: each call = one logged, undoable patch.
    # Times are seconds. clip_ids come from the Timeline section / get_timeline.
    _tool(
        "get_timeline",
        "Inspect the session timeline document: tracks, clips (id, track, start/end, source range, effects), duration, and optionally recent patch history. Use when the prompt's Timeline section is not detailed enough.",
        {
            "history": {
                "type": "integer",
                "description": "How many recent patches to include (0-20). Default 0.",
            },
        },
        [],
    ),
    _tool(
        "timeline_insert_clip",
        "Place a clip on the timeline. Video assets go on video tracks (V1...); image assets and text titles go on overlay tracks (OV1..., auto-created); audio assets go on audio tracks (A1..., auto-created). Default position appends to the end of the track. Returns the new clip_id plus the updated timeline.",
        {
            "asset_id": {
                "type": "string",
                "description": "Asset to place (video, image, or audio). Omit when inserting text.",
            },
            "text": {
                "type": "object",
                "description": "Text title/caption clip instead of an asset. {content (required), font_size, color '#rrggbb', position {x,y} px top-left (default centered), align}.",
                "properties": {
                    "content": {"type": "string"},
                    "font_size": {"type": "number"},
                    "color": {"type": "string"},
                    "position": {"type": "object"},
                    "align": {"type": "string", "enum": ["left", "center", "right"]},
                },
            },
            "track_id": {
                "type": "string",
                "description": "Target track (e.g. V1, OV1). Default: V1 for video, first overlay track for image/text.",
            },
            "at_time": {
                "type": "number",
                "description": "Place at this timeline second. Overlap on the track is an error unless ripple=true (then later clips shift right).",
            },
            "at_index": {
                "type": "integer",
                "description": "Insert before the Nth clip of the track (0-based); later clips shift right. Mutually exclusive with at_time.",
            },
            "source_in": {"type": "number", "description": "Video/audio only: source trim-in second. Default 0."},
            "source_out": {"type": "number", "description": "Video/audio only: source trim-out second. Default full asset."},
            "duration": {"type": "number", "description": "Image/text only: how long it stays on screen. Default 3s."},
            "ripple": {"type": "boolean", "description": "Default false: never shifts other clips unless you set true."},
        },
        [],
    ),
    _tool(
        "timeline_delete_clip",
        "Remove a clip from the timeline. ripple=true closes the gap by shifting later clips on the same track left.",
        {
            "clip_id": {"type": "string", "description": "Clip to delete."},
            "ripple": {"type": "boolean", "description": "Default false (leaves a gap)."},
        },
        ["clip_id"],
    ),
    _tool(
        "timeline_move_clip",
        "Move a clip to a new start time and/or another compatible track. The destination must be free (no overlap). ripple=true closes the gap left at the origin.",
        {
            "clip_id": {"type": "string"},
            "start": {"type": "number", "description": "New timeline start second."},
            "track_id": {"type": "string", "description": "Optional destination track of the same kind."},
            "ripple": {"type": "boolean", "description": "Default false."},
        },
        ["clip_id"],
    ),
    _tool(
        "timeline_trim_clip",
        "Change which part of the source media a video/image clip shows (source_in/source_out, seconds). Clip duration follows the new range. ripple=true shifts later clips on the track by the duration change.",
        {
            "clip_id": {"type": "string"},
            "source_in": {"type": "number"},
            "source_out": {"type": "number"},
            "ripple": {"type": "boolean", "description": "Default false: extending into a neighbour is an error instead."},
        },
        ["clip_id"],
    ),
    _tool(
        "timeline_split_clip",
        "Cut a clip in two at a timeline second strictly inside it. The first half keeps the clip_id; the new second-half clip_id is returned. Both halves keep the same asset.",
        {
            "clip_id": {"type": "string"},
            "at_time": {"type": "number", "description": "Timeline second to cut at (inside the clip)."},
        },
        ["clip_id", "at_time"],
    ),
    _tool(
        "timeline_set_clip_time",
        "Set a clip's start (any clip) and/or on-screen duration (image/text clips only — trim video clips with timeline_trim_clip instead).",
        {
            "clip_id": {"type": "string"},
            "start": {"type": "number"},
            "duration": {"type": "number", "description": "Image/text clips only."},
            "ripple": {"type": "boolean", "description": "Default false."},
        },
        ["clip_id"],
    ),
    _tool(
        "timeline_add_transition",
        "Set the transition from a clip into the NEXT adjacent clip on the same track. kind=cut removes any transition. Non-cut kinds need the two clips to touch (no gap) — align them first.",
        {
            "clip_id": {"type": "string", "description": "The earlier clip of the pair."},
            "kind": {"type": "string", "enum": ["cut", "dissolve", "wipe", "fade"]},
            "duration_sec": {"type": "number", "description": "Transition length, default 0.5. Must fit inside both clips."},
        },
        ["clip_id", "kind"],
    ),
    _tool(
        "timeline_set_clip_effects",
        "Merge effects onto one clip. Visual keys: rotation (0/90/180/270), mirrored, blur_radius, opacity (0-1), x, y (px, overlay placement), scale, speed (reserved). Audio keys: gain_db (volume change in dB, +/-), fade_in, fade_out (seconds), muted (drop this clip's audio). Set a key to null to clear it.",
        {
            "clip_id": {"type": "string"},
            "effects": {
                "type": "object",
                "description": "Subset of allowed effect keys to merge.",
            },
        },
        ["clip_id", "effects"],
    ),
    _tool(
        "timeline_add_track",
        "Add a video, overlay, or audio track. track_id defaults to the next free V<n>/OV<n>/A<n>.",
        {
            "kind": {"type": "string", "enum": ["video", "overlay", "audio"]},
            "track_id": {"type": "string"},
            "name": {"type": "string"},
        },
        ["kind"],
    ),
    _tool(
        "timeline_set_track",
        "Set track-level options. Ducking: pass duck_under=<audio track id> to make this audio track a music bed that automatically ducks (lowers) whenever the trigger track (e.g. a voiceover track) is loud; pass duck_under=null to clear it. Both tracks must be audio; no self-reference or cycles.",
        {
            "track_id": {"type": "string", "description": "The audio track to configure."},
            "duck_under": {
                "type": ["string", "null"],
                "description": "Audio track id whose loudness ducks this track (sidechain trigger). null clears the relationship.",
            },
        },
        ["track_id"],
    ),
    _tool(
        "timeline_undo",
        "Rewind the last N timeline changes (each timeline_* call is one step). Discarded patches are kept for audit.",
        {
            "steps": {"type": "integer", "description": "1-10, default 1."},
        },
        [],
    ),
    _tool(
        "inspect_timeline",
        "Render the current project timeline as a low-res composited proxy and sample actual visual frames from it. Use after timeline/layout edits, before diagnosing visual timing/composition, or when you need to see what the timeline currently looks like. Accepts either a single time/frame or a range; returns image asset_ids and attaches a preview thumbnail for the next model turn.",
        {
            "time_sec": {"type": "number", "description": "Single timeline time to inspect, in seconds. Alias: time."},
            "time": {"type": "number", "description": "Alias for time_sec."},
            "frame": {"type": "integer", "description": "Single 0-based project frame to inspect. Mutually exclusive with time_sec/time."},
            "start_sec": {"type": "number", "description": "Start second for range sampling. Alias: start."},
            "end_sec": {"type": "number", "description": "End second for range sampling, exclusive. Alias: end."},
            "start": {"type": "number", "description": "Alias for start_sec."},
            "end": {"type": "number", "description": "Alias for end_sec."},
            "start_frame": {"type": "integer", "description": "Start frame for range sampling."},
            "end_frame": {"type": "integer", "description": "End frame for range sampling, exclusive."},
            "max_frames": {"type": "integer", "description": "Maximum sampled frames for a range, clamped to 1..12. Default 1."},
            "label": {"type": "string", "description": "Short label for render artifacts (default 'inspect')."},
        },
        [],
    ),
    _tool(
        "render_preview",
        "Render the current timeline document into a low-res proxy MP4 and register it as a new video asset. Use for a playable preview asset; use inspect_timeline when you need the model to see composited frames directly. Final quality comes from export.",
        {
            "label": {"type": "string", "description": "Short label for the render manifest (default 'preview')."},
        },
        [],
    ),
    _tool(
        "project_export",
        "Export the full project timeline as a final-quality MP4. Composes video track clips in timeline order and overlays image/text clips on top. Returns a registered asset_id for the output file.",
        {
            "quality": {
                "type": "string",
                "enum": ["4k", "1080p", "720p", "480p", "draft"],
                "description": "Output quality preset. Default '1080p'.",
            },
            "label": {"type": "string", "description": "Short label for the export manifest (default 'export')."},
        },
        [],
    ),
    _tool(
        "project_export_otio",
        "Export the current project timeline to an OpenTimelineIO-family interchange file for DaVinci Resolve, Premiere, Final Cut and other NLEs. format defaults to 'otio' (lossless JSON). otioz/otiod also bundle the media; edl/fcp7/fcpx are lossy and need the optional interop plugins. Returns the written file as a registered asset_id.",
        {
            "format": {
                "type": "string",
                "enum": ["otio", "otioz", "otiod", "edl", "fcp7", "fcpx"],
                "description": "Interchange format. otio=lossless JSON (default); otioz/otiod=lossless bundle with media; edl/fcp7/fcpx=lossy NLE formats (need lumeri[interop]).",
            },
            "label": {"type": "string", "description": "Base filename label (without extension). Default 'project'."},
        },
        [],
    ),
    _tool(
        "project_import_otio",
        "Import an OpenTimelineIO-family interchange file and replace the current session project's timeline. format defaults to 'otio'; use the same token the file was written with. The prior state can be restored with timeline_undo.",
        {
            "otio_path": {"type": "string", "description": "Absolute path to the interchange file to import."},
            "format": {
                "type": "string",
                "enum": ["otio", "otioz", "otiod", "edl", "fcp7", "fcpx"],
                "description": "Interchange format of the file (default 'otio').",
            },
        },
        ["otio_path"],
    ),
    # ── file-management verbs (host-side, OUTSIDE the session workspace) ──
    # Like fetch/web_search these reach the user's real machine. Every write/
    # move/copy target and source is run through _safe_path (refuses system
    # dirs, credential/secret files, .git internals). move_file/organize_files
    # require explicit user approval via the ask mechanism before moving.
    _tool(
        "read_file",
        "Read a text file from the user's machine (OUTSIDE the session "
        "workspace). Returns {path, text, truncated, size}. Binary files return "
        "a short note plus size instead of raw bytes. Read-only.",
        {
            "path": {"type": "string", "description": "Absolute or ~-relative host path to read."},
            "max_bytes": {
                "type": "integer",
                "description": "Max bytes to read (default 2000000). Larger files are truncated.",
            },
        },
        ["path"],
    ),
    _tool(
        "write_file",
        "Write (overwrite) or append a text file on the user's machine (OUTSIDE "
        "the session workspace). Creates parent directories. Allowed without "
        "approval. Returns {path, bytes_written}. Refuses system/credential "
        "paths.",
        {
            "path": {"type": "string", "description": "Absolute or ~-relative host path to write."},
            "content": {"type": "string", "description": "Text content to write."},
            "append": {
                "type": "boolean",
                "description": "If true, append instead of overwriting. Default false.",
            },
        },
        ["path", "content"],
    ),
    _tool(
        "copy_in",
        "Copy an external file INTO the session workspace so it can be edited "
        "safely without touching the original. If the file is already at the "
        "target workspace path, it is treated as already copied and registered. "
        "Recognized media files are registered as session assets. Returns "
        "{workspace_path, name, size, asset_id?, kind?}.",
        {
            "path": {"type": "string", "description": "Host path of the external file to copy in."},
            "as_name": {
                "type": "string",
                "description": "Optional filename to use inside the workspace (basename only).",
            },
            "overwrite": {
                "type": "boolean",
                "description": "If true, replace an existing different workspace file with the same target name. Not needed when path already points at that exact workspace file.",
            },
        },
        ["path"],
    ),
    _tool(
        "list_dir",
        "List a directory on the user's machine (OUTSIDE the session "
        "workspace). Returns {path, entries:[{name, is_dir, size}], truncated}. "
        "Read-only.",
        {
            "path": {"type": "string", "description": "Host directory path to list."},
            "max_entries": {
                "type": "integer",
                "description": "Max entries to return (default 500).",
            },
        },
        ["path"],
    ),
    _tool(
        "move_file",
        "MOVE/RENAME a file on the user's machine (OUTSIDE the session "
        "workspace). This REQUIRES EXPLICIT USER APPROVAL: an approval prompt "
        "is shown and awaited before anything moves. On approval returns "
        "{status:'moved', src, dst}; otherwise {status:'declined'} and nothing "
        "is moved. Refuses system/credential paths.",
        {
            "src": {"type": "string", "description": "Host path of the file to move."},
            "dst": {"type": "string", "description": "Destination host path (new name/location)."},
            "timeout": {
                "type": "number",
                "description": "Optional seconds to wait for approval before declining.",
            },
        },
        ["src", "dst"],
    ),
    _tool(
        "organize_files",
        "Batch MOVE/RENAME several files at once with ONE approval listing all "
        "moves. REQUIRES EXPLICIT USER APPROVAL before any move happens. On "
        "approval executes each move and returns {status:'completed', moved, "
        "results}; on decline returns {status:'declined'} and moves nothing. "
        "Every src/dst is safety-checked first; a refusal aborts the batch.",
        {
            "moves": {
                "type": "array",
                "description": "List of {src, dst} pairs to move.",
                "items": {
                    "type": "object",
                    "properties": {
                        "src": {"type": "string"},
                        "dst": {"type": "string"},
                    },
                },
            },
            "timeout": {
                "type": "number",
                "description": "Optional seconds to wait for approval before declining.",
            },
        },
        ["moves"],
    ),
    _tool(
        "align_audio",
        "Align multiple audio/video assets to a reference using cross-correlation. "
        "Detects time offsets and provides natural language suggestions for syncing. "
        "Read-only analysis; produces no new assets.",
        {
            "reference_asset_id": {
                "type": "string",
                "description": "ID of the reference audio/video asset (required).",
            },
            "asset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of asset IDs to align against the reference (required).",
            },
            "max_offset_sec": {
                "type": "number",
                "description": "Optional maximum allowed offset in seconds.",
            },
        },
        ["reference_asset_id", "asset_ids"],
    ),
    _tool(
        "detect_beats",
        "Detect beats, tempo, and optionally onsets in an audio/video asset. "
        "Provides beat times and suggested cut points for timeline editing. "
        "Read-only analysis; produces no new assets.",
        {
            "asset_id": {
                "type": "string",
                "description": "ID of the audio/video asset (required).",
            },
            "include_onsets": {
                "type": "boolean",
                "description": "Include onset detection in results (default false).",
            },
            "cut_every": {
                "type": "integer",
                "description": "Take every Nth beat/onset (default 1).",
            },
        },
        ["asset_id"],
    ),
    _tool(
        "elicit",
        "Ask the user a structured question and wait for their answer before "
        "continuing. Use this when a creative/destructive choice is genuinely the "
        "user's to make and you cannot infer it. Creative preferences with explicit "
        "defaults are resolved by the host without asking. Renders rich controls in "
        "the UI and returns the "
        "validated answer as this tool's result; do NOT proceed on assumptions when "
        "an elicit is warranted. Each control is keyed; 'controls' maps control_key "
        "-> spec. Control types: select {options} single-choice; multi_select "
        "{options, min?, max?} returns a list; text {placeholder?, multiline?, "
        "pattern?, min_length?, max_length?}; slider {min, max, step?, default?} "
        "returns a number; panel {fields: {key->spec}, description?} a grouped form; "
        "custom_panel {schema} an extensible schema-driven form. 'options' may be a "
        "list of strings or of {label, value} objects.",
        {
            "reason": {
                "type": "string",
                "enum": [
                    "missing_source",
                    "irreversible_action",
                    "external_paid_unrequested",
                    "sensitive_identity_privacy_copyright",
                    "multi_target_ambiguity",
                    "user_requested_choice",
                    "creative_preference",
                ],
                "description": "Required policy reason for interrupting the user.",
            },
            "title": {"type": "string", "description": "Short question title shown to the user."},
            "description": {"type": "string", "description": "Optional longer explanation."},
            "controls": {
                "type": "object",
                "description": (
                    "Map of control_key -> control spec. Each spec has a 'type' "
                    "(select|multi_select|text|slider|panel|custom_panel) plus that "
                    "type's parameters."
                ),
            },
            "timeout": {
                "type": "number",
                "description": "Optional seconds to wait before falling back to control defaults.",
            },
        },
        ["reason", "title", "controls"],
    ),
    _tool(
        "spawn_subtasks",
        "Fan out 1-4 bounded sub-agents that work IN PARALLEL on independent goals and "
        "return structured results. Each child runs a restricted tool profile, cannot "
        "ask the user, cannot spawn further children, and draws cost/time from THIS "
        "session's budget. Use for: bulk media annotation/indexing, per-beat rough-cut "
        "candidate scouting, parallel library search/probe sweeps, A/B preview variants. "
        "Do NOT use for a single sequential task — call the tools directly instead.",
        {
            "subtasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string",
                                 "description": "Self-contained instruction for this child; include asset_ids explicitly."},
                        "tool_profile": {"type": "string",
                                         "enum": ["annotate", "probe"],
                                         "description": "Host-fixed capability set the child runs with."},
                        "asset_ids": {"type": "array", "items": {"type": "string"},
                                      "description": "Assets this child is scoped to (informational; echoed into the child prompt)."},
                        "max_cost_usd": {"type": "number",
                                         "description": "Optional per-child spend ceiling; host clamps to the fair slice."},
                    },
                    "required": ["goal", "tool_profile"],
                },
            },
            "deadline_sec": {"type": "number",
                             "description": "Shared wall-clock deadline for the whole batch (default 240, max 480)."},
        },
        ["subtasks"],
    ),
]


TOOL_NAMES: list[str] = [t["function"]["name"] for t in TOOL_SCHEMAS]


__all__ = ["TOOL_SCHEMAS", "TOOL_NAMES"]
