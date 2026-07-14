---
id: analysis
description: |
  用于素材分析、场景检测、元数据、直方图、波形、矢量示波器、高光选择和质量检查。何时不用我：人脸/人物主体跟踪用 face-tracking；用户要直接渲染成片时不要把分析作为主步骤；字幕识别用 html-graphics；slate 身份报告用 slate-id。
triggers:
  primary: [分析, 检测场景, 元数据, 直方图, 波形图, vectorscope, histogram, metadata, scene detect]
  secondary: [识别, 高光, 检查, review, auto highlight, waveform, quality]
primitives:
  - gemia.video.analysis.detect_scenes
  - gemia.video.analysis.scene_detect
  - gemia.video.analysis.get_metadata
  - gemia.video.analysis.auto_highlight
  - gemia.picture.analysis.histogram
  - gemia.picture.analysis.waveform_monitor
  - gemia.picture.analysis.vectorscope
  - gemia.picture.analysis.check_clipping
est_tokens: 500
---

# analysis

## 何时使用
用户明确要求理解、检测、检查或输出分析报告时使用。

## 参数说明
核心参数是 `mode`、`threshold`、`max_segments`、`report_format`。分析输出通常不是最终视频。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.analysis.detect_scenes","args":{"threshold":0.35},"input":"$input"}
```

## 边界 / fallback
如果用户只是说“剪得好看”，不要先分析过度；使用 fallback skills 做可执行编辑。
