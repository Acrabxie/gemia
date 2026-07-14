---
id: slate-id
description: |
  用于 AI slate、镜头/场记元数据、素材身份识别、镜头编号和可审计 metadata 报告。何时不用我：普通场景剪辑用 timeline-ops；场景检测和高光分析用 analysis；标题卡视觉设计用 html-graphics。
triggers:
  primary: [slate id, AI slate, 场记, 镜头编号, slate metadata]
  secondary: [素材身份, 元数据牌, scene id, take id, metadata card]
primitives:
  - gemia.video.slate_id.render_slate_id_metadata_plan
est_tokens: 400
---

# slate-id

## 何时使用
需要生成或展示可追踪的素材身份、镜头编号和场记元数据时使用。

## 参数说明
核心参数是 `fields`、`format`、`include_timecode`、`output_style`。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.slate_id.render_slate_id_metadata_plan","args":{"include_timecode":true},"input":"$input","output":"$output"}
```

## 边界 / fallback
如果用户要的是视觉标题卡而非元数据身份，用 html-graphics。
