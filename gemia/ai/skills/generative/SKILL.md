---
id: generative
description: |
  用于生成图片、编辑图片、风格迁移、图生视频、生成视频、延展视频和生成 b-roll。何时不用我：已有素材的普通裁剪用 timeline-ops；已有素材整体调色用 color-grade；具体单点滤镜用 stylize-art。
triggers:
  primary: [生成图片, 生成视频, 图生视频, 生成 b-roll, 生成 broll, 生成一段, 延展视频, generate image, generate video, image to video]
  secondary: [b-roll, broll, 生成, 续写, 扩展, generative, edit image, style transfer, veo]
primitives:
  - gemia.picture.generative.generate_image
  - gemia.picture.generative.edit_image
  - gemia.picture.generative.style_transfer
  - gemia.picture.generative.blend_images
  - gemia.video.generative.generate_video
  - gemia.video.generative.generate_video_from_image
  - gemia.video.generative.extend_video
  - gemia.video.generative.generate_broll
est_tokens: 560
---

# generative

## 何时使用
用户要求创建新视觉内容、补充镜头、图生视频或生成 b-roll 时使用。

## 参数说明
核心参数是 `prompt`、`script_text`、`style`、`duration`、`aspect_ratio`、`reference_asset_id`、`seed`。B-roll 下载用 `script_text`，不要把它当已有视频滤镜。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.generative.generate_broll","args":{"script_text":"city night street b-roll","style":"cinematic"},"output":"$step_1"}
```

## 边界 / fallback
不要把“生成一个剪辑方案”当成生成视频；那通常是 timeline-ops + color-grade。
