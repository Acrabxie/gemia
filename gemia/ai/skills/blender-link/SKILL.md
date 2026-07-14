---
id: blender-link
description: |
  用于 LumeriLink to Blender、空间效果、三维场景、景深网格、视差、体积光和 Blender 后端渲染。何时不用我：二维 HTML/Lottie 标题用 html-graphics；普通叠加抠像用 composite-blend；仅调色用 color-grade。
triggers:
  primary: [LumeriLink, Blender, 空间效果, 三维, 3D, spatial, parallax, volumetric]
  secondary: [景深网格, 视差, hologram, depth, camera move, blender link]
primitives:
  - gemia.video.blender_link.render_blender_link_operation
  - gemia.video.blender_link.render_blender_spatial_scene
  - gemia.video.blender_link.blender_link_capabilities
est_tokens: 500
---

# blender-link

## 何时使用
需要把素材接到 Blender 后端做空间构图、三维相机、体积光或视差场景时使用。

## 参数说明
核心参数是 `operation`、`style`、`camera_motion`、`depth_mode`、`intensity`。优先输出可执行操作计划。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.blender_link.render_blender_link_operation","args":{"operation":"spatial_scene","style":"cinematic"},"input":"$input","output":"$output"}
```

## 边界 / fallback
如果用户只是要平面文字或下三分之一，不要调用 Blender；如果 Blender 不可用，返回可降级的 2D plan。
