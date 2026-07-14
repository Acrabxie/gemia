---
id: face-age
description: |
  用于人脸年龄变化：变年轻、变老、年龄渐变和保持身份的年龄调整。何时不用我：脸型五官调整用 face-reshaper；去痘磨皮用 blemish-removal；普通肖像调色用 color-grade。
triggers:
  primary: [年龄, 变年轻, 变老, face age, younger, older]
  secondary: [人脸年龄, age transform, aging, de-aging]
primitives:
  - gemia.video.face_age.render_face_age_plan
est_tokens: 410
---

# face-age

## 何时使用
用户明确要求对人物年龄做视觉变化时使用。

## 参数说明
核心参数是 `target_age`、`direction`、`identity_preserve`、`strength`。默认保留身份和自然肤质。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.face_age.render_face_age_plan","args":{"direction":"younger","identity_preserve":true},"input":"$input","output":"$output"}
```

## 边界 / fallback
只说“修脸”不代表年龄变化；优先 face-reshaper 或 blemish-removal。
