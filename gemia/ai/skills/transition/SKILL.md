---
id: transition
description: |
  用于素材之间的转场和过渡，如溶解、擦除、推拉、淡入淡出、自定义 transition、相机快门和光圈叶片转场。何时不用我：单个片段的裁剪或拼接顺序用 timeline-ops；整体色调风格用 color-grade；MG 标题动画用 html-graphics。
triggers:
  primary: [转场, 过渡, 淡入淡出, 溶解转场, wipe, push transition, transition, fade]
  secondary: [衔接, 切换, 两段素材, between clips, dissolve, 快门, 相机快门, 光圈, 光圈叶片, shutter, camera shutter, aperture, iris]
primitives:
  - gemia.video.transitions.transition_dissolve
  - gemia.video.transitions.transition_wipe
  - gemia.video.transitions.transition_push
  - gemia.video.transitions.transition_custom
  - gemia.video.transitions.transition_shutter
est_tokens: 420
---

# transition

## 何时使用
用户要求片段 A 到片段 B 有视觉过渡效果时使用。默认缺参时优先短溶解。用户提到“相机快门 / 快门 / 光圈叶片 / iris / aperture”时，优先使用 `gemia.video.transitions.transition_shutter`，不要用 HTML/CSS 图层模拟，也不要退回普通 dissolve。

## 参数说明
核心参数是 `duration_sec`、`transition`、`direction`、`mask_fn`。`transition_shutter` 还支持 `blade_count`、`hold_sec`、`edge_highlight` 和 `highlight_strength`：用户要求快门停顿/保持全黑时写 `hold_sec`，用户要求金属高光、叶片边缘、机械质感时写 `edge_highlight: true`。素材选择必须写在 step 的 `input` 字段里，例如 `["$input", "$step_1"]` 或项目中相邻两段素材路径；不要把 `input_a`、`input_b`、`output_path` 放进 `args`。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.transitions.transition_dissolve","args":{"duration_sec":0.5},"input":["/path/a.mp4","/path/b.mp4"],"output":"$output"}
{"id":"step_1","function":"gemia.video.transitions.transition_shutter","args":{"duration_sec":1.0,"blade_count":6},"input":["/path/a.mp4","/path/b.mp4"],"output":"$output"}
{"id":"step_1","function":"gemia.video.transitions.transition_shutter","args":{"duration_sec":1.0,"blade_count":6,"hold_sec":0.1,"edge_highlight":true,"highlight_strength":0.75},"input":["/path/a.mp4","/path/b.mp4"],"output":"$output"}
```

## 边界 / fallback
不要把“需要补充转场类型”直接当 error；若缺少必要目标才返回 ask。用户只说“加转场”时使用默认溶解。
