---
id: face-reshaper
description: |
  用于脸型和五官微调：下颌、眼睛、嘴、鼻、脸宽、表情细节。何时不用我：年龄变化用 face-age；去痘磨皮用 blemish-removal；整体美颜调色用 color-grade 或 blemish-removal。
triggers:
  primary: [脸型, 瘦脸, 五官, 下颌, face reshape, jaw, eyes, mouth]
  secondary: [人脸调整, 鼻子, 眼睛, 嘴巴, portrait reshape]
primitives:
  - gemia.video.face_reshaper.render_face_reshaper_plan
est_tokens: 420
---

# face-reshaper

## 何时使用
用户要求改变脸型或五官比例时使用。

## 参数说明
核心参数是 `region`、`amount`、`subtle`、`identity_preserve`。默认 subtle=true。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.face_reshaper.render_face_reshaper_plan","args":{"region":"jaw","amount":0.15,"subtle":true},"input":"$input","output":"$output"}
```

## 边界 / fallback
皮肤瑕疵不走 reshaper；用 blemish-removal。
