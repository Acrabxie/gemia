---
id: ultrasharpen
description: |
  用于超清锐化、恢复细节、增强边缘但保持自然纹理。何时不用我：动作拖影修复用 motion-deblur；分析边缘或直方图用 analysis；艺术化线稿/漫画化用 stylize-art。
triggers:
  primary: [锐化, 超清, 更清晰, ultrasharpen, sharpen, recover detail]
  secondary: [清晰度, 细节, 边缘增强, crisp, detail recovery]
primitives:
  - gemia.video.ultrasharpen.render_ultrasharpen_plan
  - gemia.picture.enhance.image_sharpen
  - gemia.video.effects.video_sharpen
est_tokens: 430
---

# ultrasharpen

## 何时使用
需要提升画面清晰度、边缘细节和自然质感时使用。

## 参数说明
核心参数是 `strength`、`radius`、`preserve_texture`、`artifact_guard`。默认自然锐化。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.ultrasharpen.render_ultrasharpen_plan","args":{"strength":"medium","preserve_texture":true},"input":"$input","output":"$output"}
```

## 边界 / fallback
如果是运动糊，不要只锐化；用 motion-deblur。
