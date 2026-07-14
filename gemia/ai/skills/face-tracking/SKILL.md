---
id: face-tracking
description: |
  用于人脸/人物主体跟踪：自动选择最显眼的人脸，生成跟踪预览、轨迹和本地 metadata。何时不用我：改变脸型五官用 face-reshaper；磨皮祛痘用 blemish-removal；只要普通场景检测或元数据用 analysis；要把文字/贴纸跟随人脸时可与 layer-flow 或 html-graphics 组合。
triggers:
  primary: [人脸跟踪, 跟踪人脸, 面部跟踪, face tracking, track face, face track]
  secondary: [主体跟踪, 人物跟踪, 跟踪这张脸, 锁定人脸, 追踪人脸, tracking face]
primitives:
  - gemia.video.face_tracking.render_face_tracking_plan
  - gemia.video.analysis.get_metadata
est_tokens: 430
---

# face-tracking

## 何时使用
用户要求“跟踪脸/人物主体/锁定人脸/生成跟踪轨迹”时使用。默认目标是画面里最显眼的人脸，默认时间范围是选中的时间参考；没有选区时用全片。

## 参数说明
核心参数是 `target`、`time_scope`、`overlay`、`trail`、`frame_step`、`max_long_edge`。如果用户没有指定，使用默认值：`target="most_prominent_face"`、`time_scope="selected_range_or_full_clip"`、`overlay=true`、`trail=true`。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.face_tracking.render_face_tracking_plan","args":{"target":"most_prominent_face","time_scope":"selected_range_or_full_clip","overlay":true,"trail":true},"input":"$input","output":"$output","artifact_type":"video"}
```

## 边界 / fallback
不要因为缺少“哪张脸/哪一段/是否显示轨迹”就 ask；这些都可默认。只有用户要求多人精确身份选择且当前素材无法判断时，才问一次。用户要美颜、瘦脸或年龄变化时不要走本 skill。
