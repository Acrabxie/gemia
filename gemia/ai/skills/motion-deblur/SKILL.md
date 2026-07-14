---
id: motion-deblur
description: |
  用于运动模糊修复、动作糊、快门拖影、去模糊恢复和视频降噪类修复。何时不用我：正常审美模糊或背景虚化用 blur-defocus；普通增强清晰度用 ultrasharpen；防抖稳定用 timeline/frames 稳定能力而不是本 skill。
triggers:
  primary: [去模糊, 运动模糊修复, 降噪, motion deblur, deblur, denoise, blurry action]
  secondary: [糊了, 拖影, 快门, 模糊修复, recover motion]
primitives:
  - gemia.video.motion_deblur.render_motion_deblur_plan
  - gemia.video.effects.video_denoise
  - gemia.video.effects.video_denoise_hqdn3d
est_tokens: 420
---

# motion-deblur

## 何时使用
当用户指出动态画面糊、运动拖影、需要恢复动作细节时使用。

## 参数说明
核心参数是 `strength`、`quality`、`motion_hint`、`preserve_noise`。默认保守，避免伪影。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.motion_deblur.render_motion_deblur_plan","args":{"strength":"medium"},"input":"$input","output":"$output"}
```

## 边界 / fallback
如果用户要求“虚化背景”，不要去模糊；用 blur-defocus。
