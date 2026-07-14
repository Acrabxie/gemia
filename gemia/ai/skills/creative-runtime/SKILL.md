---
id: creative-runtime
description: |
  用于开放式创作运行时：让 Lumeri 自主决定是否读视频、如何搭图层、插入文字/HTML/MG、组合素材和生成临时开发补丁 brief。何时不用我：单纯裁剪/加速/拼接用 timeline-ops；单纯调色用 color-grade；只要普通字幕或标题且不涉及多层编排时用 html-graphics。
triggers:
  primary: [创作运行时, creative runtime, prompt-only, 微函数, 自己写微函数, 自己敲代码, 修改底层源码, 开放创作空间, 自主搭图层, 自己插入图层, 自己编辑图层, 代码渲染, review再写代码]
  secondary: [读视频, 图层, 文字图层, html overlay, 自定义图层, 写代码, 源码, 底层代码, 自己选择, 自动编排, layer authoring, review loop, self review, render pass, 小样, 实时预览, 参考资料, reference assets, 渲染结果, 复核后改代码]
primitives:
  - gemia.video.analysis.get_metadata
  - gemia.video.summary.video_summarize
  - gemia.video.layer_flow.render_layer_workflow
  - gemia.video.html_graphics.render_html_graphics_plan
  - gemia.video.subtitles.add_text
  - gemia.video.subtitles.add_lower_third
  - gemia.video.motion_graphics.render_mg_title_card
  - gemia.video.motion_graphics.render_mg_formula_reveal
  - gemia.video.blender_link.render_blender_link_operation
  - gemia.video.creative_runtime.write_development_patch_brief
est_tokens: 760
---

# creative-runtime

## 何时使用

用户希望 Lumeri 不只是套用剪辑/转场，而是像创作 agent 一样自己判断：要不要先读素材，是否需要视频摘要，是否要加文字图层、HTML 图层、MG 图形、空间图层，怎样把多个层组织成一个成片。

## 自主策略

- 先判断是否需要读视频：如果请求依赖画面内容、节奏、主体、字幕位置或空间关系，先用 `get_metadata` 或 `video_summarize`。
- 优先使用一个高层 layer workflow 承接复杂视觉想法：源视频、文字、HTML、图片、蒙版、透明度、位置、关键帧都放进 `overlay_layers`。
- 文字不是只能走字幕：标题、注释、标签、信息面板、lower third、HTML 卡片都可以作为图层处理。
- 对需要三维空间、Blender 或 LumeriLink 的请求，可以把 Blender 能力和图层/HTML 图形组合起来。
- 创作链路优先按 `图层/时间轴 API -> 代码渲染真实结果 -> review/复核 -> 如能力不足写开发 brief` 组织；不要只给抽象建议。
- 参考资料只进入 `reference_assets`；不要默认把它们塞进时间轴。需要正式使用时再生成、抓取、读取或提升为素材。
- 每个可见小效果都应该产出低清 preview/render pass，随后写一句自然语言自审；用户反馈默认绑定到对应图层、时间段或 render pass，只局部重做。
- 每个 step 必须写自然的 `assistant_message`，告诉用户当前在创作什么，不要输出内部推理。

## 参数说明

常用参数：

- `overlay_layers`：图层数组；每层可含 `type`、`text`、`html`、`source`、`position`、`opacity`、`scale`、`z_index`、`start_frame`、`end_frame`、`keyframes`。
- `html` / `html_source`：用于信息面板、标题卡、lower third 或可视化说明。
- `title`、`title_position`、`title_font_size`：快速标题图层。
- `feature_request`、`suggested_files`、`proposed_primitives`：当现有能力不够时写开发补丁 brief。

## 典型 plan 片段

```json
{
  "id": "step_1",
  "function": "gemia.video.summary.video_summarize",
  "args": {},
  "input": "$input",
  "assistant_message": "我先读一下画面内容和节奏，确定文字和图层该放在哪里。"
}
```

```json
{
  "id": "step_2",
  "function": "gemia.video.layer_flow.render_layer_workflow",
  "args": {
    "overlay_layers": [
      {
        "id": "main_title",
        "type": "text",
        "text": "Lumeri",
        "position": [64, 64],
        "font_config": {"size": 52},
        "z_index": 20
      }
    ]
  },
  "input": "$input",
  "output": "$output",
  "assistant_message": "我会把标题作为独立图层压到画面上，而不是只做简单字幕。"
}
```

```json
{
  "id": "step_3",
  "function": "gemia.video.creative_runtime.write_development_patch_brief",
  "args": {
    "feature_request": "新增一个可复用的粒子文字图层 primitive",
    "suggested_files": ["gemia/video/motion_graphics.py", "gemia/ai/skills/creative-runtime/SKILL.md"],
    "proposed_primitives": ["gemia.video.motion_graphics.render_particle_text_layer"],
    "safety_notes": ["普通运行时只写 brief；源码修改交给开发模式确认后执行。"]
  },
  "input": "$input",
  "output": "$output",
  "assistant_message": "现有底层能力不够直接完成这个想法，我先生成一个开发补丁 brief，方便下一步扩展源码。"
}
```

## 边界 / fallback

何时不用我：只要求“裁掉前 3 秒”“加速 2 倍”“合并所有素材”时用 timeline-ops；只要求“暖一点/冷一点/LUT”时用 color-grade；只要求普通字幕且文案明确时用 html-graphics。不要在普通运行中假装已经修改源码；只有调用开发 brief primitive 时才可以说明需要新增底层能力。
