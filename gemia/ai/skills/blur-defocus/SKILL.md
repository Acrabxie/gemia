---
id: blur-defocus
description: |
  用于审美模糊、背景虚化、景深、tilt-shift、镜头模糊和柔焦。何时不用我：画面本来糊了需要修复用 motion-deblur；清晰锐化用 ultrasharpen；动态焦点拉移用 cinefocus。
triggers:
  primary: [虚化, 背景虚化, 模糊背景, 景深, blur, defocus, bokeh, tilt-shift]
  secondary: [柔焦, dreamy blur, lens blur, selective blur]
primitives:
  - gemia.picture.enhance.defocus_background
  - gemia.picture.enhance.image_blur
  - gemia.picture.enhance.image_bokeh_blur
  - gemia.picture.enhance.image_lens_blur
  - gemia.picture.enhance.image_selective_blur
  - gemia.picture.enhance.image_tilt_shift
  - gemia.video.effects.blur_background
  - gemia.video.effects.video_blur
  - gemia.video.effects.video_dreamy_blur
  - gemia.video.effects.video_tilt_shift
est_tokens: 520
---

# blur-defocus

## 何时使用
用户想让背景或局部更柔和、更有景深、更像镜头虚化时使用。

## 参数说明
核心参数是 `region`、`radius`、`strength`、`subject_hint`、`falloff`。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.picture.enhance.defocus_background","args":{"strength":0.6},"input":"$input","output":"$output"}
```

## 边界 / fallback
“去模糊”“恢复清晰”不是本 skill；转到 motion-deblur 或 ultrasharpen。
