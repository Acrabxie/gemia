---
id: timeline-ops
description: |
  用于时间线结构编辑：裁剪、截取、加速、慢放、倒放、拼接、合并、旋转、冻结帧和按脚本整理片段。何时不用我：只改画面色彩用 color-grade；只加素材之间的视觉过渡用 transition；只做字幕或标题用 html-graphics。
triggers:
  primary: [裁剪, 裁前, 截取, 加速, 慢动作, 合并, 拼接, 插入, 插到中间, 放到中间, 倒放, 时间范围, 图片按, 图片做成, 3 秒视频, 3s视频, trim, cut, speed, concat, reverse]
  secondary: [时间轴, 片段, 区间, 秒, 视频, clip, timeline, retime]
primitives:
  - gemia.video.timeline.cut
  - gemia.video.timeline.ripple_trim
  - gemia.video.timeline.speed
  - gemia.video.timeline.speed_ramp
  - gemia.video.timeline.concat
  - gemia.video.timeline.nest_clips
  - gemia.video.timeline.reverse
  - gemia.video.timeline.rotate_video
  - gemia.video.timeline.flip_video
  - gemia.video.timeline.freeze_frame
  - gemia.video.timeline.timeline_from_script
est_tokens: 520
---

# timeline-ops

## 何时使用
用户要求改变片段时长、顺序、速度、方向、时间范围或基础几何方向时使用。

## 参数说明
核心参数是 `start_sec`、`end_sec`、`speed`、`clips`、`angle`、`direction`。时间引用优先来自 `project_state.timeReferences`。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.timeline.cut","args":{"start_sec":0,"end_sec":10},"input":"$input","output":"$output"}
```

## 边界 / fallback
如果用户只说“更好看”但没有明确时间线动作，可退到 color-grade 或 ask；如果要求两个片段之间的视觉衔接，用 transition。
