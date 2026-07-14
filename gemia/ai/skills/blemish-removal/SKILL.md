---
id: blemish-removal
description: |
  用于皮肤瑕疵处理：痘印、斑点、轻微磨皮、肤质清理和保留真实纹理。何时不用我：脸型五官调整用 face-reshaper；年龄变化用 face-age；全片色调统一用 color-grade。
triggers:
  primary: [瑕疵, 痘, 痘印, 磨皮, blemish, acne, skin cleanup]
  secondary: [皮肤, 斑点, 肤质, beauty cleanup, portrait retouch]
primitives:
  - gemia.video.blemish.render_blemish_removal_plan
  - gemia.picture.enhance.image_detect_faces
est_tokens: 420
---

# blemish-removal

## 何时使用
用户要求去掉皮肤瑕疵、轻微磨皮或肖像清理时使用。

## 参数说明
核心参数是 `strength`、`preserve_texture`、`face_hint`、`region`。默认保留纹理。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.blemish.render_blemish_removal_plan","args":{"strength":"light","preserve_texture":true},"input":"$input","output":"$output"}
```

## 边界 / fallback
不要把“调亮肤色”误判为瑕疵去除；那是 color-grade。
