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

from gemia.device_capabilities import DEVICE_CAPABILITY_NAMES


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
        "Create a new still image with GPT Image 2 through the OpenAI subscription bridge. This capability is exposed only when the selected provider is openai_subscription; it has no other-provider fallback. Returns a new asset_id.",
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
            "verify_motion": {
                "type": "boolean",
                "description": "For video, decode sampled frames and prove real frame-to-frame motion rather than trusting the container. Use for formal production evidence.",
            },
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
        "stock_media",
        "Search or fetch licensed public stock footage from the configured "
        "Pexels/Pixabay connectors. Use action='search' to inspect real options "
        "before spending generation budget, then action='fetch' to import one "
        "usable result into this project with provider, source URL, attribution, "
        "license, and a provenance sidecar. This is free media sourcing, not AI generation.",
        {
            "action": {
                "type": "string",
                "enum": ["search", "fetch"],
                "description": "search returns candidates; fetch downloads and registers one project asset. Default search.",
            },
            "query": {"type": "string", "description": "Concrete visual search, preferably in English, e.g. 'rainy cyberpunk city aerial'."},
            "provider": {"type": "string", "enum": ["auto", "pexels", "pixabay"], "description": "Configured licensed source. Default auto."},
            "media_type": {"type": "string", "enum": ["video", "image"], "description": "Default video."},
            "orientation": {"type": "string", "enum": ["landscape", "portrait", "square"], "description": "Optional source orientation."},
            "limit": {"type": "integer", "description": "Search candidates, default 8 and max 20."},
            "result_id": {"type": "string", "description": "For action='fetch', select the exact id returned by a prior search."},
            "result_index": {"type": "integer", "description": "For action='fetch', select a zero-based candidate index when no stable id is available."},
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
        "Semantic footage search over already available real material (filename + probed visual/dialog labels), ranked by relevance. This is the '先搜真素材' step: fill a shot from raw footage before falling back to generate_video. Returns matching asset_ids ready to use. It is NOT an import or registration tool for a specific external path; use copy_in for that and require asset_registered=true plus asset_id. If it finds nothing, generate the shot instead. Probes frames live so it needs NO prior annotation — complements search_media (which queries saved annotations and returns timecodes). Stronger than search_library (a plain filename/label text match).",
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
            "fit_media": {"type": "boolean", "description": "Fit indexed time-ranged library evidence to the drafted outline. Default false; never generates or purchases media."},
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
                                            "asset_id": {"type": "string", "description": "Optional registered media asset already chosen for this shot."},
                                            "library_asset_id": {"type": "string", "description": "Persistent media-library identity for the chosen source."},
                                            "source_in": {"type": "number", "description": "Chosen source start second from persistent evidence."},
                                            "source_out": {"type": "number", "description": "Chosen source end second from persistent evidence."},
                                            "evidence": {"type": "object", "description": "Traceable persistent evidence identity and ranking explanation."},
                                            "alternatives": {"type": "array", "items": {"type": "object"}, "description": "Up to three traceable candidates not selected."},
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
            "fit_media": {"type": "boolean", "description": "Fit indexed time-ranged library evidence before the single atomic shotlist write. Default false; never generates or purchases media."},
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
                    "library_asset_id": {"type": "string", "description": "Persistent media-library identity for the selected source."},
                    "source_in": {"type": "number", "description": "Chosen source start second from persistent evidence."},
                    "source_out": {"type": "number", "description": "Chosen source end second from persistent evidence."},
                    "evidence": {"type": "object", "description": "Traceable evidence identity and ranking explanation."},
                    "alternatives": {"type": "array", "items": {"type": "object"}, "description": "Up to three non-selected evidence candidates."},
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
        "draft_quanta",
        "Draft a COMPLETE quanta (a discrete video: an ordered tree of content scopes and render states) in one call — the START of any quanta task. Two modes: (1) give a ONE-LINE 'theme' and a structure template — 'pitch' (Hook→Problem→Solution→Highlights→Numbers→CTA), 'report' (conclusions-first analysis), 'teach' (lesson arc); (2) from_shotlist=true converts the CURRENT storyboard into scopes (narration→speaker notes, on-screen text→title block, footage→image blocks, shot durations→state dwell). Drafted blocks have stable ids; bullets/cards are separate grouped leaves with explicit cumulative state visibility. It REPLACES the current quanta (replace=false previews without persisting). A scaffold — refine per node with update_quantum after.",
        {
            "theme": {"type": "string", "description": "One line describing the quanta, e.g. 'Lumeri 产品介绍' or 'Q3 growth review'. Required unless from_shotlist=true."},
            "template": {"type": "string", "enum": ["pitch", "report", "teach"], "description": "Structure for theme mode. Default 'pitch'."},
            "from_shotlist": {"type": "boolean", "description": "true = build the quanta from the current shotlist instead of a theme (video→quanta migration)."},
            "language": {"type": "string", "enum": ["zh", "en"], "description": "Language of the drafted text. Auto-detected if omitted."},
            "replace": {"type": "boolean", "description": "Replace the current quanta (default true). false = return the draft without persisting."},
        },
        [],
    ),
    _tool(
        "set_quanta",
        "Set or replace the WHOLE quanta — a DISCRETE VIDEO: one ordered state tree. For a fresh scaffold prefer draft_quanta; for node edits prefer update_quantum. Two accepted shapes: (a) flat authoring sugar {slides:[…]} (below) — lifts deterministically into the tree, slide→content scope, builds→state children, default_path orders scopes then disappears (DFS leaf order IS the default path); (b) the full tree {root:{children:[…]}} with group nodes (title+children, may nest), content scopes (blocks+state children), and states ({visible_block_ids, dwell_sec, advance:'wait'|'auto', hidden}). Content truth lives in semantic blocks (text/stat/image/shape/group — never pixels). Each state is a FULL cumulative snapshot in visible_block_ids (leaf ids only): monotonic, final state exactly covers every leaf. Any node takes hidden:true — outside the default walk and mp4, reachable only via explicit links (appendix pattern). STRICTLY validated (E_BAD_ARG): duplicate quantum/block ids, nested content scopes, invalid visibility, links mounted on groups, hotspot blocks outside their scope, dangling link targets (every edge enumerated), dwell_sec <= 0. Persisted + undoable.",
        {
            "quanta": {
                "type": "object",
                "description": "The quanta plan.",
                "properties": {
                    "version": {"type": "integer", "description": "IR version (optional; backfilled — stored canonical form is the v2 tree)."},
                    "theme": {
                        "type": "object",
                        "description": "Quanta-level look: one mood for the WHOLE quanta (per-slide moods read as collage).",
                        "properties": {
                            "tokens": {"type": "object", "description": "Design-token overrides (optional)."},
                            "mood": {"type": "string", "description": "Quanta-wide tone, e.g. 'calm-tech', 'confident'."},
                            "aspect": {"type": "string", "description": "Canvas aspect, default '16:9'."},
                        },
                    },
                    "slides": {
                        "type": "array",
                        "description": "Flat authoring sugar: ordered content scopes (each lifts to a tree node; builds lift to state children with document-unique prefixed ids like s1_b1).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Stable quantum id you will reference in update_quantum/links (optional; auto-filled)."},
                                "layout": {"type": "string", "description": "Layout template name, e.g. 'title', 'content', 'stat', 'full-bleed'."},
                                "title": {"type": "string", "description": "Slide heading."},
                                "blocks": {
                                    "type": "array",
                                    "description": "Semantic content blocks — the content truth. kind='text' (text and/or bullets), 'stat' (value+label big number), 'image' (asset_id or query+source), 'shape' (accent shape), 'group' (children: homogeneous sub-blocks, e.g. 3 feature cards).",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string", "description": "Stable id within this slide. Optional input; deterministically backfilled as blk_1 / blk_1_1 by source path."},
                                            "kind": {"type": "string", "enum": ["text", "stat", "image", "shape", "group"]},
                                            "role": {"type": "string", "description": "Slot hint, e.g. 'title', 'body', 'hero', 'cta', 'card'."},
                                            "text": {"type": "string"},
                                            "bullets": {"type": "array", "items": {"type": "string"}},
                                            "style_token": {"type": "string"},
                                            "value": {"type": "string", "description": "stat: the big number, e.g. '97'."},
                                            "label": {"type": "string", "description": "stat: what the number means."},
                                            "asset_id": {"type": "string", "description": "image: a registered asset."},
                                            "source": {"type": "string", "description": "image: how to fill it ('search'/'generate')."},
                                            "query": {"type": "string", "description": "image: search query when not yet filled."},
                                            "shape": {"type": "string", "description": "shape: e.g. 'rect'."},
                                            "fill_token": {"type": "string"},
                                            "children": {"type": "array", "description": "group: nested semantic blocks. Every child also has a stable id; builds reference renderable non-group leaves, never the group id.", "items": {"type": "object"}},
                                        },
                                    },
                                },
                                "notes": {"type": "string", "description": "Speaker notes (the shotlist narration's descendant)."},
                                "builds": {
                                    "type": "array",
                                    "description": "Ordered render states (the discrete video's frames-of-meaning). visible_block_ids is the FULL cumulative leaf-id snapshot for that state (not a delta); it may start empty, must only grow, and the final state must exactly cover every renderable leaf. Missing/wrong-type visibility on legacy input backfills all leaves. dwell_sec (> 0) is the autoplay/mp4 hold; advance 'wait' (default) holds at this state until interaction in presentation mode, 'auto' advances after dwell.",
                                    "items": {"type": "object", "properties": {
                                        "id": {"type": "string"},
                                        "dwell_sec": {"type": "number"},
                                        "advance": {"type": "string", "enum": ["wait", "auto"], "description": "Presentation semantics: wait = hold until interaction (default), auto = advance after dwell."},
                                        "visible_block_ids": {"type": "array", "items": {"type": "string"}, "description": "Unique ids of every leaf visible at this state. Group ids are invalid."}}},
                                },
                                "links": {
                                    "type": "array",
                                    "description": "Interaction out-edges (跃迁); omit for the implicit advance. Mount on content scopes (applies to all its states; advance = the scope's EXIT edge) or on a state (overrides itself). target: 'next', 'quantum:<id>', or 'url:<https url>'. hotspot blocks must exist in this scope.",
                                    "items": {"type": "object", "properties": {
                                        "trigger": {"type": "string", "description": "'advance' or 'hotspot:<leaf block id>'."},
                                        "target": {"type": "string"}}},
                                },
                                "transition": {
                                    "type": "object",
                                    "description": "Transition into this slide. v1: 'cut' (default) or 'fade'.",
                                    "properties": {"kind": {"type": "string", "enum": ["cut", "fade"]}},
                                },
                            },
                        },
                    },
                    "default_path": {"type": "array", "items": {"type": "string"}, "description": "Flat-sugar only: orders the lifted scopes, then disappears — in the tree, DFS leaf order IS the default path; reorder with update_quantum op='move'."},
                    "root": {"type": "object", "description": "Tree shape (alternative to slides): {id:'root', children:[group|content nodes]}. group={id,title,hidden,children}; content={id,layout,title,blocks,notes,links,transition,hidden,children:[states]}; state={id,visible_block_ids,dwell_sec,advance,hidden}."},
                },
            },
        },
        ["quanta"],
    ),
    _tool(
        "update_quantum",
        "The EDIT TREE's single entry point: revise any node of the quanta state tree by document-unique id, one op or an atomic batch. op='patch' (default) merges 'fields' into one node — reword blocks/title/notes, retune state visibility/dwell/advance, change links/layout/transition/hidden; a content node's fields.builds replaces its states wholesale (v1 sugar); 'id'/'children' cannot be patched. op='insert' adds a whole subtree ('quantum') under 'parent_id' at 'index' (states under content scopes; groups/content under groups or root). op='remove' detaches a subtree — if other links point into it the batch is rejected with EVERY dangling edge enumerated; retarget them in the SAME call via 'ops'. op='move' reorders/reparents ('quantum_id' → 'parent_id' + 'index') — this is how you reorder the default path (DFS leaf order). 'ops' runs several edits as ONE atomic undoable patch, order-independent for reference integrity. Persisted + undoable.",
        {
            "op": {"type": "string", "enum": ["patch", "insert", "remove", "move"], "description": "Single-op mode. Default 'patch'."},
            "quantum_id": {"type": "string", "description": "Target node id (patch/remove/move). From get_quanta."},
            "fields": {
                "type": "object",
                "description": "patch: fields to merge, e.g. {title, blocks, notes, builds, links, layout, transition, mood_override, hidden} on a content scope; {visible_block_ids, dwell_sec, advance, hidden} on a state; {title, hidden} on a group.",
                "properties": {
                    "layout": {"type": "string"},
                    "title": {"type": "string"},
                    "blocks": {"type": "array", "items": {"type": "object"}},
                    "notes": {"type": "string"},
                    "mood_override": {"type": "string"},
                    "builds": {"type": "array", "items": {"type": "object"}, "description": "Content scopes: replace the state children wholesale (v1 sugar)."},
                    "links": {"type": "array", "items": {"type": "object"}},
                    "transition": {"type": "object"},
                    "hidden": {"type": "boolean"},
                    "visible_block_ids": {"type": "array", "items": {"type": "string"}},
                    "dwell_sec": {"type": "number"},
                    "advance": {"type": "string", "enum": ["wait", "auto"]},
                },
            },
            "parent_id": {"type": "string", "description": "insert/move: destination parent ('root' or a group id; a content scope id when inserting/moving states)."},
            "index": {"type": "integer", "description": "insert/move: position among the parent's children (default: end)."},
            "quantum": {"type": "object", "description": "insert: the new subtree — a state ({visible_block_ids,dwell_sec,advance}), a content scope ({blocks,…,children:[states]}), or a group ({title,children})."},
            "ops": {"type": "array", "items": {"type": "object"}, "description": "Atomic batch: [{op, …}, …] applied as one undoable patch (e.g. remove a quantum AND retarget its in-edges together)."},
        },
        [],
    ),
    _tool(
        "get_quanta",
        "Read the current quanta state tree: groups as indented sections, each content scope's id, layout, block kinds, state count/dwell, title, notes, non-default links and hidden marks, plus the full tree IR. Call before revising so you use the right quantum ids.",
        {},
        [],
    ),
    _tool(
        "refine_quantum",
        "Revise ONE quantum (a content scope or one of its states) in an assembled quanta and immediately refresh the presentation pager + dedicated Quanta timeline. Rematerialization is subtree-granular: with a current frame cache it rerenders only the containing content scope and reuses every unchanged scope's frames; if the quanta was not assembled or the cache is stale, it safely materializes the whole quanta. Use update_quantum instead while still planning. Unrelated timeline clips survive.",
        {
            "quantum_id": {"type": "string", "description": "The quantum id (content scope or state) from get_quanta."},
            "fields": {
                "type": "object",
                "description": "Fields to merge into this quantum: layout, title, blocks, notes, mood_override, builds, links, transition (content scope) or visible_block_ids, dwell_sec, advance (state).",
                "properties": {
                    "layout": {"type": "string"},
                    "title": {"type": "string"},
                    "blocks": {"type": "array", "items": {"type": "object"}},
                    "notes": {"type": "string"},
                    "mood_override": {"type": "string"},
                    "builds": {"type": "array", "items": {"type": "object"}},
                    "links": {"type": "array", "items": {"type": "object"}},
                    "transition": {"type": "object"},
                },
            },
            "fail_on_overflow": {"type": "boolean", "description": "When true, leave the valid IR edit in place but refuse timeline refresh if any scope still overflows."},
        },
        ["quantum_id", "fields"],
    ),
    _tool(
        "assemble_quanta",
        "Materialize the current quanta into every render-state PNG, register those frames, return a same-origin presentation pager URL, and atomically rebuild dedicated Quanta timeline tracks in DFS leaf order using each state's dwell_sec. Hidden subtrees stay out of the flatten (they are interaction-only). Re-running the same quanta reuses the frame cache and replaces only the dedicated Quanta tracks; unrelated timeline clips survive. MP4 flattening never follows interaction edges: authored fades and discarded interaction links are reported as explicit degradations (fade_to_cut, interaction_flattened).",
        {
            "fail_on_overflow": {"type": "boolean", "description": "When true, refuse to assemble if any text still overflows after the two allowed autofit steps. Default false returns overflow details for refine_quantum."},
        },
        [],
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
        "prepare_roughcut",
        "Prepare imported video/audio for editorial review without changing the timeline. Runs local Whisper transcription, detects pauses and filler words, ranks repeated takes, creates/reuses a low-resolution proxy, and checkpoints each asset so interrupted long batches can resume. This is local and does not spend generation budget. Use before storyboard fitting or timeline editing when the user has raw talking-head, interview, or repeated-take footage.",
        {
            "asset_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Media-library video/audio asset ids.",
            },
            "all": {"type": "boolean", "description": "Prepare all video/audio assets in the media library."},
            "language": {"type": "string", "description": "Spoken language code or auto. Default auto."},
            "create_proxies": {"type": "boolean", "description": "Create/reuse 540p local proxies for video. Default true."},
            "proxy_resolution": {"type": "integer", "description": "Proxy height from 240 to 1080. Default 540."},
            "resume": {"type": "boolean", "description": "Reuse matching completed checkpoints. Default true."},
            "max_assets": {"type": "integer", "description": "Batch cap, maximum 100."},
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
        "Execute a bash command in an isolated sandbox. Workspace directory is fully readable/writable. Outside workspace: files can only be created, not modified/deleted. Credentials (~/.ssh, ~/.config/gcloud, ~/.gemia/config.json) are not readable. Network access denied. Wraps command with sandbox-exec for M1 isolation. Foreground (default) blocks the turn and returns exit_code, stdout_tail, stderr_tail, timed_out, sandbox_enforced, workspace_dir. For commands that may run longer than ~30s (broad find/grep, test suites, installs), set run_in_background=true: returns a job_id IMMEDIATELY, output streams to a log file, and you get NOTIFIED when it finishes — continue other work or end the turn instead of polling. Manage with check_job / wait_for_job / kill_job.",
        {
            "command": {
                "type": "string",
                "description": "Bash command string to execute.",
            },
            "timeout_sec": {
                "type": "number",
                "description": "Timeout in seconds. Foreground: default 30, max 120. Background: default 600, max 3600. Command's whole process group is killed if exceeded.",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Run detached and return a job_id immediately (default false). Use for anything that may exceed ~30s; completion is announced automatically, so do NOT busy-poll.",
            },
        },
        ["command"],
    ),
    # Session-scope file_list/read/write/copy/move removed in favour of
    # the machine-scope equivalents (list_dir, read_file, write_file, copy_in,
    # move_file, organize_files) to reduce tool count. Dispatch map in __init__
    # still routes them if called.
    _tool(
        "get_design_state",
        "Read the persisted RealityContract and compact Creative IR for this ProductionRun. Use it before consequential design work or a targeted revision; it is the bridge from human intent to deterministic project code, not a generated shot-list dump.",
        {
            "document": {
                "type": "string",
                "enum": ["both", "reality_contract", "creative_ir"],
                "description": "Which production fact document to return. Default both.",
            },
        },
        [],
    ),
    _tool(
        "patch_design_state",
        "Apply ONE small, revision-checked JSON-pointer patch to RealityContract or Creative IR. Use repeated semantic patches instead of replacing a giant JSON document. Contract patches define the real deliverable; Creative IR patches define stable beats, design systems, program inputs and the active human revision scope.",
        {
            "document": {
                "type": "string",
                "enum": ["reality_contract", "creative_ir"],
            },
            "operation": {
                "type": "string",
                "enum": ["set", "merge", "remove", "append"],
            },
            "path": {
                "type": "string",
                "description": "Non-root JSON pointer, e.g. /brief, /beats/opening, /systems/motion, /beat_order.",
            },
            "value": {
                "type": ["object", "array", "string", "number", "boolean", "null"],
                "description": "Patch value. Omit only for remove. One patch is capped at 64 KiB.",
            },
            "expected_revision": {
                "type": "integer",
                "description": "Document revision returned by get_design_state; prevents concurrent overwrite.",
            },
        },
        ["document", "operation", "path", "expected_revision"],
    ),
    _tool(
        "build",
        "Run an incrementally authored, multi-file design program from the project workspace, or submit a small compatibility code string. Project entrypoints can import LumenFrame and keep complex timing, layout, motion, typography and rendering logic in deterministic code instead of one giant JSON/tool call. Returns job_id immediately; use check_job or wait_for_job for status.",
        {
            "entrypoint": {
                "type": "string",
                "description": "Preferred: project://design/... path to an existing persistent project program (for example project://design/main.py). Build it incrementally with write_file, then rerun this entrypoint after local revisions. A plain relative path refers only to the current session workspace. Mutually exclusive with code.",
            },
            "code": {
                "type": "string",
                "description": "Compatibility path for a small source string. Mutually exclusive with entrypoint; do not put a whole production or giant generated JSON in this field.",
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
            "device_capabilities": {
                "type": "array",
                "items": {"type": "string", "enum": list(DEVICE_CAPABILITY_NAMES)},
                "description": "Hardware capabilities the program actually needs. CPU and project files are implicit. GPU/media acceleration can run inside the sandbox; capture, peripherals and networking require the user-controlled host permission path and remain subject to macOS consent.",
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
        [],
    ),
    _tool(
        "check_job",
        "Poll a pending job by job_id. Works for build/background-shell jobs (status + log tails + next_offset) and Veo video jobs (status → asset_id when done). Inexpensive. Use to inspect job state without blocking; background completion is announced automatically, so do NOT call this in a tight loop.",
        {
            "job_id": {
                "type": "string",
                "description": "Job identifier returned by build, run_shell (background) or generate_video.",
            },
            "since_offset": {
                "type": "integer",
                "description": "Optional (build/shell only): byte offset from a previous check_job's next_offset — returns only NEW log output since then instead of the default tail.",
            },
        },
        ["job_id"],
    ),
    _tool(
        "wait_for_job",
        "Block until a job completes or max_wait_sec is exceeded. Works for build jobs (polls every 1s) and Veo video jobs (polls every 10s). Returns same shape as check_job plus waited_sec and timed_out flag. For background shell jobs the wait is capped at 60s — prefer ending the turn and letting the completion notification wake you.",
        {
            "job_id": {
                "type": "string",
                "description": "Job identifier returned by build, run_shell (background) or generate_video.",
            },
            "max_wait_sec": {
                "type": "number",
                "description": "Maximum seconds to wait (default 60, clamped to (0, 300]; shell jobs further capped at 60). For Veo jobs use 300.",
            },
        },
        ["job_id"],
    ),
    _tool(
        "kill_job",
        "Stop a running build or background-shell job: SIGKILLs its whole process group. The job's final status becomes failed with error 'killed by kill_job'. Idempotent on jobs that already finished. Not applicable to remote jobs (video).",
        {
            "job_id": {
                "type": "string",
                "description": "Job identifier returned by build or run_shell (background).",
            },
        },
        ["job_id"],
    ),
    _tool(
        "publish_cloud_guide",
        "Upload one declarative Skill, Creator Workflow, or validated Point Library .lus bundle to the signed-in user's Lumeri Skill Cloud. Private is the default. Set visibility='public' and public_confirmed=true ONLY when the user explicitly asks to publish it on the public Lumeri website. Point Library bundles are stored as bytes and never executed by the cloud.",
        {
            "kind": {
                "type": "string",
                "enum": ["skill", "workflow", "point_library"],
                "description": "Use point_library for a validated .lus bundle; skill for one reusable capability; workflow for ordered stages.",
            },
            "id": {
                "type": "string",
                "description": "Stable kebab-case id, at most 64 characters.",
            },
            "version": {
                "type": "string",
                "description": "Exact SemVer core version such as 1.0.0. Content changes require a new version.",
            },
            "title": {"type": "string", "description": "Creator-readable display title."},
            "description": {
                "type": "string",
                "description": "One concise sentence describing when and why to use it.",
            },
            "definition": {
                "type": "object",
                "description": "Machine-readable contract. A skill needs non-empty steps or procedure; a workflow needs non-empty stages.",
                "additionalProperties": True,
            },
            "instructions": {
                "type": "string",
                "description": "Full reusable execution guidance. Never include credentials or private absolute paths.",
            },
            "visibility": {
                "type": "string",
                "enum": ["private", "public"],
                "description": "Default private. Public entries appear in the website Skill catalog.",
            },
            "public_confirmed": {
                "type": "boolean",
                "description": "Must be true for public visibility and only after the user explicitly requested public publishing.",
            },
            "bundle_base64": {
                "type": "string",
                "description": "For point_library only: exact base64-encoded .lus bundle bytes produced by the local package installer.",
            },
        },
        ["kind", "id", "version", "title", "description", "definition", "instructions"],
    ),
    _tool(
        "list_cloud_guides",
        "List Skill Cloud entries available to the signed-in user: their private entries plus the public catalog, without loading full instructions.",
        {
            "kind": {
                "type": "string",
                "enum": ["skill", "workflow", "point_library"],
                "description": "Optional kind filter. Omit to list both.",
            },
        },
        [],
    ),
    _tool(
        "load_cloud_guide",
        "Load one exact account-owned or public Skill/Creator Workflow version, including its definition and instructions.",
        {
            "kind": {"type": "string", "enum": ["skill", "workflow", "point_library"]},
            "id": {"type": "string", "description": "Exact kebab-case id."},
            "version": {"type": "string", "description": "Exact SemVer core version."},
            "content_sha256": {
                "type": "string",
                "description": "Optional exact content digest returned by list_cloud_guides. Pass it for public entries so the selected publisher/version cannot be confused with a same-id entry.",
            },
        },
        ["kind", "id", "version"],
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
        "Call this FIRST, before starting a task, to reuse prior know-how: returns the most relevant saved (distilled) and built-in library skills for your query/task, each with name + when_to_use + recipe steps. Reuse a matching skill instead of re-deriving it. If a tool failure shows that recalled skills are steering the task down the wrong route, audit that result and call recall_skills again with routing_audit; give concrete failure evidence, positive replacement guidance, and only explicitly disproven skill names to avoid. That audit is remembered for the same task query so unchanged recalls do not return the same rejected pile.",
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
            "routing_audit": {
                "type": "object",
                "description": "Optional main-model audit after a failed route. Stored only in this session and applied only to the same normalized query.",
                "properties": {
                    "failure_evidence": {
                        "type": "string",
                        "description": "Required concrete structured failure evidence showing why the previous route was unsuitable. Evidence is recorded but not used as positive search text.",
                    },
                    "guidance": {
                        "type": "string",
                        "description": "Positive description of the alternative capability or approach the sorter should prefer next.",
                    },
                    "avoid_skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact names of previously returned skills that evidence has disproven for this task. Do not list skills merely because one transient call failed.",
                    },
                },
                "required": ["failure_evidence"],
            },
            "reset_routing_guidance": {
                "type": "boolean",
                "description": "Clear the current session's sorter guidance before this recall, for example after conditions changed or the prior route became valid again.",
            },
        },
        [],
    ),
    _tool(
        "remember",
        "Remember a durable fact across sessions. In a Project, scope=auto writes to that Project's shared memory; use scope=global only for facts that apply across every Project. Outside a Project, auto writes global memory. Pass title to update the same fact instead of duplicating it. Never store secrets. Use log_note for short-lived progress.",
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
            "scope": {
                "type": "string",
                "enum": ["auto", "project", "global"],
                "description": "Memory scope. auto (default) means Project memory inside a Project, otherwise global memory.",
            },
        },
        ["content"],
    ),
    _tool(
        "log_note",
        "Append one short breadcrumb. In a Project, scope=auto writes to the shared Project log; scope=global writes the cross-Project daily log. Use for short-lived progress and decisions, not durable facts. Empty or secret-looking notes are skipped.",
        {
            "text": {
                "type": "string",
                "description": "The note to append to today's daily log (collapsed to a single line).",
            },
            "scope": {
                "type": "string",
                "enum": ["auto", "project", "global"],
                "description": "Log scope. auto (default) means Project log inside a Project, otherwise the global daily log.",
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
    # 18 lumen_* convenience verbs (add_layer, set_transform, set_opacity,
    # delete_layer, move_layer, set_visibility, select, set_mask, key,
    # set_range, set_lane, retime_segment, reverse, time_remap, speed_ramp,
    # ripple_delete, merge_compositions, set_work_area) removed from schema.
    # Use lumen_patch with the corresponding ops instead — the full ops
    # vocabulary is in the lumenframe_ops system-prompt section.
    # Dispatch map in __init__.py still routes them if called.
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
    # (lumen_set_range through lumen_set_work_area removed — see comment above)
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
    _tool(
        "lumen_comp_to_timeline",
        "Place the current lumenframe composition ON THE TIMELINE as a normal video clip. Renders the window [t_in, t_out) once to a cached file and inserts it in ONE undoable step. The clip is a LIVE reference: editing the composition later marks it stale, and project_export automatically re-renders it (pass 0). Use this to bring keyframed/masked/blended lumenframe work into the final cut alongside ordinary clips. NOTE the undo asymmetry: undoing timeline steps never restores composition content — if you undo past a refresh, the clip points at the older rendered file (stale but playable) while the composition document stays at its latest state.",
        {
            "t_in": {
                "type": "number",
                "description": "Inclusive start time of the composition window, in seconds.",
            },
            "t_out": {
                "type": "number",
                "description": "Exclusive end time of the window, in seconds. Must be > t_in; clamped to the composition's duration.",
            },
            "track_id": {
                "type": "string",
                "description": "Target video track id. Default V1 (auto-created if missing).",
            },
            "at_time": {
                "type": "number",
                "description": "Place the clip at this timeline time (seconds). Mutually exclusive with at_index; default append after the last clip.",
            },
            "at_index": {
                "type": "integer",
                "description": "Place the clip at this position among the track's clips. Mutually exclusive with at_time.",
            },
            "ripple": {
                "type": "boolean",
                "description": "When placing by at_time, shift later clips to make room instead of failing on overlap. Default false.",
            },
        },
        ["t_in", "t_out"],
    ),
    _tool(
        "vector_motion",
        "Professional vector motion design (logo reveals, brand stings, MG animation, "
        "animated backgrounds) as ONE creative verb. op:'create' takes a creative BRIEF "
        "— {subject:{kind: logo_text|title|mark|abstract, text?, mark?: ring|hex|star|blob|wave|orbit, "
        "preset?, subtitle?}, intent: reveal|intro|loop|transition|outro, style (playful/minimal/"
        "luxury/tech/lumeri or aliases like 'google-like'), feeling:[adjectives], duration (s), "
        "palette, seed, params:{energy|smoothness|playfulness|elegance|complexity|density|"
        "organicness: 0..1}} — plans the choreography (phase arc, staggering, focal order) and adds "
        "the result as an animated html layer in the lumenframe doc. Speak creative language, not "
        "coordinates: say energy 0.8, never x+=20. op:'adjust' re-choreographs an existing vector "
        "layer from human feedback phrases ('more playful', 'less chaotic', '更高级'). op:'catalog' "
        "lists the full vocabulary. Verify with lumen_seek / lumen_render_range. Deterministic per "
        "seed; same brief renders once (content-hash cache).",
        {
            "op": {
                "type": "string",
                "enum": ["create", "adjust", "catalog"],
                "description": "create = brief → new vector layer; adjust = feedback → rebuild an existing layer; catalog = vocabulary.",
            },
            "brief": {
                "type": "object",
                "description": "create only: the creative brief (subject required; everything else optional).",
            },
            "place": {
                "type": "object",
                "description": "create only: layer placement {start (s), lane, name}.",
            },
            "layer_id": {
                "type": "string",
                "description": "adjust only: the vector layer to re-choreograph.",
            },
            "feedback": {
                "type": "array",
                "items": {"type": "string"},
                "description": "adjust only: feedback phrases, e.g. ['more playful', 'less dense'].",
            },
        },
        ["op"],
    ),
    _tool(
        "point_library",
        "Call an installed Lumeri Point Library package through its single semantic host surface. "
        "Use library_id such as 'vector-motion' or 'grade', op:'catalog' first when the vocabulary "
        "is unknown, then op:'create'/'adjust' for Shape A or op:'apply'/'describe' for Shape B. "
        "The package supplies the professional taste floor, deterministic implementation, catalog, "
        "verification evidence, and next step; do not bypass it with raw craft recipes.",
        {
            "library_id": {
                "type": "string",
                "description": "Installed Point Library id, for example vector-motion or grade.",
            },
            "op": {
                "type": "string",
                "enum": ["create", "adjust", "catalog", "apply", "describe"],
                "description": "Library operation. catalog/describe are read-only vocabulary calls.",
            },
            "brief": {
                "type": "object",
                "description": "Semantic creative brief for create/adjust when the selected library uses it.",
            },
            "place": {
                "type": "object",
                "description": "Optional placement for a library that creates a document layer.",
            },
            "layer_id": {
                "type": "string",
                "description": "Existing library-created layer for an adjust operation.",
            },
            "feedback": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Human feedback phrases for a semantic adjustment.",
            },
            "payload": {
                "type": "object",
                "description": "Shape B payload forwarded only through its declared safe ops binding.",
            },
        },
        ["library_id"],
    ),
    # ── creative "point libraries" (second layer; each = ONE creative verb,
    #    op: create|adjust|catalog, speak creative language not raw numbers) ──
    _tool(
        "grade",
        "Creative COLOUR GRADING as one verb. op:'create' takes a BRIEF "
        "{look (neutral|teal_orange|film|bleach_bypass|noir|day_for_night|pastel|"
        "cyberpunk|vintage|clean, or aliases kodak/blockbuster/bw/instagram), "
        "feeling:[adjectives], intensity (0..1), params:{warmth|contrast|saturation|"
        "lift|drama|filmic:0..1}, seed} → a deterministic grade RECIPE (protected "
        "S-curve tone, complementary split toning, skin-safe by default) + a preview "
        "SVG + an ffmpeg_filter string to apply to footage. op:'adjust' folds feedback "
        "('warmer','更电影感') into the recipe. op:'catalog' lists looks. Speak looks, "
        "not curve points.",
        {
            "op": {"type": "string", "enum": ["create", "adjust", "catalog"],
                   "description": "create = brief → grade recipe; adjust = feedback → re-derive; catalog = vocabulary."},
            "brief": {"type": "object", "description": "create/adjust: the grade brief (look + feelings + params)."},
            "feedback": {"type": "array", "items": {"type": "string"},
                         "description": "adjust only: phrases like ['warmer','less contrast','更高级']."},
        },
        ["op"],
    ),
    _tool(
        "kinetic_type",
        "Animated TITLES / KINETIC TYPOGRAPHY as one verb. op:'create' takes a BRIEF "
        "{text or lines:[...], layout (title_card|lower_third|quote|kinetic_lyric|"
        "list_reveal|caption|credits_roll), style (title_hero|editorial|kinetic|"
        "broadcast|lyric|minimal), feeling:[...], reveal (per_word|per_line|typewriter|"
        "mask_wipe|rise_fade|scale_pop), emphasis:[words], duration, palette, seed} → a "
        "typeset, choreographed animated SVG added as an html layer (modular type scale, "
        "TV title-safe margins, pace-timed reveals). Verify with lumen_seek/lumen_render. "
        "op:'adjust' re-typesets a kinetic layer from feedback ('more energetic','更优雅'). "
        "op:'catalog' lists layouts/reveals/styles. Deterministic per seed.",
        {
            "op": {"type": "string", "enum": ["create", "adjust", "catalog"],
                   "description": "create = brief → new title layer; adjust = feedback → rebuild; catalog = vocabulary."},
            "brief": {"type": "object", "description": "create only: the text brief (text or lines + layout)."},
            "place": {"type": "object", "description": "create only: layer placement {start (s), lane, name}."},
            "layer_id": {"type": "string", "description": "adjust only: the kinetic layer to re-typeset."},
            "feedback": {"type": "array", "items": {"type": "string"},
                         "description": "adjust only: phrases like ['more energetic','更优雅']."},
        },
        ["op"],
    ),
    _tool(
        "edit_grammar",
        "EDIT GRAMMAR / cut craft as one verb. op:'create' takes a BRIEF "
        "{clips:[{id, duration, has_action?, tags?, scene?}], style (invisible|energetic|"
        "dreamy|documentary|montage|commercial, aliases mtv/film/music_video), feeling:[...], "
        "seed} → a reasoned CUT PLAN (straight cuts by default; J/L cuts; cut-on-action; a "
        "capped transition budget) of timeline transition/trim ops with a reason each. "
        "op:'adjust' re-plans from feedback ('more energetic','更梦幻'). op:'catalog' lists "
        "styles + transition vocabulary.",
        {
            "op": {"type": "string", "enum": ["create", "adjust", "catalog"],
                   "description": "create = clips → cut plan; adjust = feedback → re-plan; catalog = vocabulary."},
            "brief": {"type": "object", "description": "create/adjust: {clips:[...], style, feeling}."},
            "feedback": {"type": "array", "items": {"type": "string"},
                         "description": "adjust only: phrases like ['more energetic','fewer transitions']."},
        },
        ["op"],
    ),
    _tool(
        "camera",
        "Synthetic CAMERA MOVEMENT as one verb. op:'create' takes a BRIEF {move "
        "(push_in|pull_out|pan_left|pan_right|tilt_up|tilt_down|dolly|reveal|ken_burns|"
        "handheld|float|whip), subject:{x,y in 0..1}, style (locked|cinematic|energetic|"
        "handheld|documentary|epic), feeling:[...], energy, duration, canvas, seed} → an "
        "eased, motivated, frame-safe keyframe TRANSFORM TRACK (subtle by default, never "
        "reveals an edge; organic handheld from seeded noise) to apply via lumen_set_transform. "
        "op:'adjust' re-derives from feedback; op:'catalog' lists moves. Canvas defaults to the doc.",
        {
            "op": {"type": "string", "enum": ["create", "adjust", "catalog"],
                   "description": "create = move → transform track; adjust = feedback → re-derive; catalog = vocabulary."},
            "brief": {"type": "object", "description": "create/adjust: {move, subject, style, duration}."},
            "feedback": {"type": "array", "items": {"type": "string"},
                         "description": "adjust only: phrases like ['slower','more handheld']."},
        },
        ["op"],
    ),
    _tool(
        "compose",
        "COMPOSITION / FRAMING as one verb. op:'create' takes a BRIEF {subjects:[{bbox:"
        "[x,y,w,h] in 0..1, weight?, facing?}], canvas or aspect, framing (thirds|centered|"
        "golden|negative_space|dynamic|tight|wide), intent?, headroom?, horizon?, seed} → a "
        "REFRAME RECIPE (thirds/golden anchor, hard subject containment so the head is never "
        "cropped, honest horizon snapping) + a guide-overlay SVG, to apply via lumen_set_transform. "
        "op:'adjust' re-derives from feedback; op:'catalog' lists framings. Canvas defaults to the doc.",
        {
            "op": {"type": "string", "enum": ["create", "adjust", "catalog"],
                   "description": "create = subjects → reframe recipe; adjust = feedback → re-derive; catalog = vocabulary."},
            "brief": {"type": "object", "description": "create/adjust: {subjects:[...], framing, canvas}."},
            "feedback": {"type": "array", "items": {"type": "string"},
                         "description": "adjust only: phrases like ['tighter','more negative space']."},
        },
        ["op"],
    ),
    _tool(
        "rhythm_edit",
        "Musical RHYTHM EDITING (cut to the beat) as one verb. op:'create' takes a BRIEF "
        "{bpm, time_signature?, sections?:[{name, bars}], clips?:[{id, duration}], style "
        "(on_beat|on_downbeat|on_phrase|syncopated|half_time|double_time|build_drop, alias edm), "
        "feeling:[...], energy, sync, seed} → a BEAT GRID + a phrase-aware beat-aligned CUT PLAN "
        "(accents on strong beats, density following energy, build/drop acceleration). op:'adjust' "
        "re-derives from feedback; op:'catalog' lists sync patterns. Deterministic from bpm.",
        {
            "op": {"type": "string", "enum": ["create", "adjust", "catalog"],
                   "description": "create = bpm → beat grid + cut plan; adjust = feedback → re-derive; catalog = vocabulary."},
            "brief": {"type": "object", "description": "create/adjust: {bpm, sections, style} (bpm required)."},
            "feedback": {"type": "array", "items": {"type": "string"},
                         "description": "adjust only: phrases like ['tighter','more drive']."},
        },
        ["op"],
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
        "get_segment",
        "Read one Clip's expandable SegmentDocument, including its internal timeline, layers, states, revisions and human reservations. Reading a legacy clip is zero-write and returns the implicit single-material structure.",
        {"clip_id": {"type": "string", "description": "Top-level Clip to inspect."}},
        ["clip_id"],
    ),
    _tool(
        "segment_edit",
        "Edit one Clip's SegmentDocument. Always call get_segment first and pass its expected_segment_revision. Agent edits are refused when a human reservation covers the entity_ref; branches never enter the main preview or export.",
        {
            "clip_id": {"type": "string"},
            "action": {"type": "string", "enum": ["set_layer", "set_timeline", "set_state", "state_add", "state_copy", "state_delete", "state_reorder", "branch"]},
            "entity_ref": {"type": "string", "description": "Layer, material or state id being edited."},
            "entity_refs": {"type": "array", "items": {"type": "string"}, "description": "Additional entities touched by the same operation."},
            "changes": {"type": "object"},
            "expected_segment_revision": {"type": "integer"},
            "new_state_id": {"type": "string"},
            "index": {"type": "integer"},
            "client_op_id": {"type": "string"},
        },
        ["clip_id", "action", "expected_segment_revision"],
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
        "Render the current project timeline as a low-res composited proxy and sample actual visual frames from it. Use after timeline/layout edits, before diagnosing visual timing/composition, or when you need to see what the timeline currently looks like. Pass the exact seconds to review in times_sec; returns image asset_ids and attaches a preview thumbnail for the next model turn.",
        {
            "times_sec": {
                "type": "array",
                "items": {"type": "number"},
                "description": "One to twelve exact timeline positions to inspect, in seconds. Default [0].",
            },
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
        "Read a text file. In a Project with a local folder, relative paths use that folder; "
        "without one they use project://edit/. project://edit/... always refers to Lumeri's "
        "private Project directory. Binary files return a short note plus size.",
        {
            "path": {"type": "string", "description": "Project-relative path, project://source/..., project://edit/..., or allowed absolute host path."},
            "max_bytes": {
                "type": "integer",
                "description": "Max bytes to read (default 2000000). Larger files are truncated.",
            },
        },
        ["path"],
    ),
    _tool(
        "write_file",
        "Write or append text. Relative paths use the selected local folder when present, "
        "otherwise the Project's project://edit/ directory. "
        "Mutations are recorded for Project undo/redo. Protected roots and credentials remain forbidden.",
        {
            "path": {"type": "string", "description": "Project-relative path, project://source/..., or project://edit/... path."},
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
        "Recognized media files are registered as session assets. For media "
        "registration, success requires asset_registered=true and a non-empty "
        "asset_id; copied=true or file presence alone is not registration. "
        "Returns {workspace_path, name, size, asset_registered, asset_id?, kind?}.",
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
        "List a directory. Relative paths use the selected local folder when present, "
        "otherwise the Project's project://edit/ directory. Read-only.",
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
        "file_delete",
        "Delete a file or folder inside the bound Project source/edit roots. The "
        "item is moved into Lumeri's private Project history, so the user can undo "
        "or redo it. The selected Project root itself and protected roots are refused.",
        {
            "path": {
                "type": "string",
                "description": "Project-relative path, project://source/..., or project://edit/... path to remove.",
            },
        },
        ["path"],
    ),
    _tool(
        "move_file",
        "Move or rename a file/folder. Inside a bound Project source/edit root, "
        "the Project grant is sufficient and the mutation is undoable. Outside "
        "that boundary, existing host approval and protected-path rules apply.",
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
        "Batch move/rename several files. Project-contained moves use the bound "
        "folder grant and each mutation is undoable; moves outside the Project "
        "keep the existing host approval and protected-path rules.",
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
        "returns a number; panel {fields: {key->spec}, description?} a grouped form. "
        "Custom panels require a host-registered validator and are not available to "
        "model-authored asks. 'options' may be a "
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
                    "(select|multi_select|text|slider|panel) plus that "
                    "type's parameters."
                ),
            },
        },
        ["reason", "title", "controls"],
    ),
    _tool(
        "verify_delivery",
        "Evaluate the ProductionRun against its persisted RealityContract and the exact canonical "
        "preview/export graph, contract-required inspected frames, verified source motion, provenance, "
        "audio, budget and technical gates. Call only after project_export moved the run "
        "to verifying. A passing report moves the run to ready_for_review; it never marks "
        "human acceptance.",
        {
            "export_asset_id": {"type": "string"},
            "preview_asset_id": {"type": "string"},
            "inspection_asset_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
            "review_checks": {
                "type": "object",
                "additionalProperties": {"type": "boolean"},
                "description": "Boolean pass/fail for every agent_review_checks name in the current RealityContract.",
            },
            "review_notes": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Concrete visual observation for every contract-required review check.",
            },
        },
        ["export_asset_id", "preview_asset_id", "inspection_asset_ids", "review_checks", "review_notes"],
    ),
    _tool(
        "spawn_subtasks",
        "Fan out sub-agents that work IN PARALLEL on independent goals and return "
        "structured results. You may start at most 4 direct children per call; "
        "children cannot spawn further children. "
        "Each full-profile child has the SAME local tool set as the parent, including "
        "project-scoped file reads, writes, edits, and creative edits. Use for: "
        "bulk media annotation/indexing, per-beat rough-cut candidate scouting, parallel "
        "library search/probe sweeps, A/B preview variants, any embarrassingly-parallel "
        "workload. For tasks requiring full VIDEO UNDERSTANDING (visual analysis, scene "
        "recognition, frame-level reasoning), set model to a multimodal model such as "
        "'gemini-3.5-flash'. Do NOT use for a single sequential task — call the tools "
        "directly instead.",
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
                                         "enum": ["full", "annotate", "probe"],
                                         "description": "Capability set. Omit or 'full' for the parent's full tool list."},
                        "model": {"type": "string",
                                  "description": "Model override for this child. Use a multimodal model (e.g. 'gemini-3.5-flash') when the task needs to understand video content."},
                        "asset_ids": {"type": "array", "items": {"type": "string"},
                                      "description": "Assets this child is scoped to (informational; echoed into the child prompt)."},
                        "max_steps": {"type": "integer",
                                      "description": "Optional caller-requested model-call limit for this child. Omit to let the child continue until it finishes or hits a failure guard."},
                    },
                    "required": ["goal"],
                },
            },
            "deadline_sec": {"type": "number",
                             "description": "Optional caller-requested wall-clock deadline for the whole batch. Omit to wait for all children."},
        },
        ["subtasks"],
    ),
]


TOOL_SCHEMAS.extend([
    _tool(
        "install_point_library",
        "Install and atomically activate a local or cloud-loaded Point Library .lus bundle.",
        {
            "path": {"type": "string", "description": "Local .lus bundle path."},
            "bundle_base64": {"type": "string", "description": "Exact bundle bytes returned by load_cloud_guide."},
        },
        [],
    ),
    _tool(
        "list_point_libraries",
        "List installed and built-in Point Library packages with version, active state, catalog count, and verification status.",
        {},
        [],
    ),
    _tool(
        "rollback_point_library",
        "Activate one exact previously installed Point Library version without deleting package bytes.",
        {
            "library_id": {"type": "string", "description": "Point Library id."},
            "version": {"type": "string", "description": "Exact installed SemVer version to activate."},
        },
        ["library_id", "version"],
    ),
    _tool(
        "publish_point_library",
        "Upload one locally validated Point Library .lus bundle to the signed-in user's Skill Cloud. Private is the default; public requires explicit confirmation.",
        {
            "path": {"type": "string", "description": "Local .lus bundle path."},
            "visibility": {"type": "string", "enum": ["private", "public"]},
            "public_confirmed": {"type": "boolean", "description": "Required true only after an explicit public-publishing request."},
        },
        ["path"],
    ),
])

TOOL_NAMES: list[str] = [t["function"]["name"] for t in TOOL_SCHEMAS]


__all__ = ["TOOL_SCHEMAS", "TOOL_NAMES"]
