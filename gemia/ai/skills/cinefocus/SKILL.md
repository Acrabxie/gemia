---
id: cinefocus
description: |
  用于电影感焦点控制：rack focus、点击对焦、景深转移、焦点拉移和主体突出。何时不用我：普通背景虚化滤镜用 blur-defocus；画面锐化或去模糊用 ultrasharpen/motion-deblur；三维空间焦点用 blender-link。
triggers:
  primary: [拉焦, 跟焦, rack focus, cinefocus, click-to-focus, focus pull]
  secondary: [对焦, 景深, aperture, subject focus, cinematic focus]
primitives:
  - gemia.video.cinefocus.render_cinefocus_plan
est_tokens: 430
---

# cinefocus

## 何时使用
需要随时间改变焦点位置、模拟镜头焦点或突出主体时使用。

## 参数说明
核心参数是 `focus_keyframes`、`aperture`、`subject_hint`、`strength`。坐标默认归一化 0..1。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.cinefocus.render_cinefocus_plan","args":{"focus_keyframes":[{"t":0,"x":0.5,"y":0.5}]},"input":"$input","output":"$output"}
```

## 边界 / fallback
用户只是要“背景虚化”且无焦点运动时，用 blur-defocus。
