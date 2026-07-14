---
id: layer-flow
description: |
  用于 layer-first 多图层视频计划：类似 DaVinci Resolve 时间线/节点合成里的空画布或源视频覆盖层、关键帧透明度/位置、素材堆叠和复杂合成流程。何时不用我：单一混合模式或抠像用 composite-blend；普通转场用 transition；单纯调色用 color-grade。
triggers:
  primary: [图层流程, 多图层, layer flow, layer-first, keyframed opacity, overlay layers]
  secondary: [叠加, 覆盖, 层级, keyframe, position, opacity, edit page, fusion page, resolve timeline]
primitives:
  - gemia.video.layer_flow.render_layer_workflow
est_tokens: 460
---

# layer-flow

## 何时使用
用户要求多个素材按层叠、位置、透明度和时间轴关系组合时使用。

## 参数说明
核心参数是 `overlay_layers`、`keyframes`、`blend_mode`、`timing`、`canvas`。空画布创作允许 `input` 为 `$input` 且项目 input_path 为空，但必须提供 title 或 overlay_layers。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.layer_flow.render_layer_workflow","args":{"overlay_layers":[{"type":"text","text":"Lumeri","position":[96,96],"z_index":10}],"canvas":{"width":1920,"height":1080,"fps":30,"total_frames":90}},"input":"$input","output":"$output"}
```

## 边界 / fallback
不要直接使用底层 compositing graph/preview primitive；planner 只调用稳定 workflow primitive。
