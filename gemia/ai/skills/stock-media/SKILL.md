---
id: stock-media
description: |
  用于从 Pexels/Pixabay 搜索并抓取可用的公开视频或图片素材，尤其是补 b-roll、背景、氛围镜头和参考画面。何时不用我：用户要求生成全新内容用 generative；已有素材的裁剪、调色、转场分别用 timeline-ops、color-grade、transition；真实用户素材不能被替换为公开素材。
triggers:
  primary: [Pexels, Pixabay, 搜索素材, 抓取素材, 下载素材, 找素材, 找一个视频, 找一段视频, 找段视频, 搜索视频素材, 找公开视频, 素材网站, stock media, stock footage, stock photo]
  secondary: [b-roll, broll, 公共素材, 公开视频, 参考素材, 背景素材, 氛围镜头, 找一段, 素材池]
primitives:
  - gemia.video.stock_media.search_stock_media
  - gemia.video.stock_media.fetch_stock_media
  - gemia.video.stock_media.fetch_pexels_media
  - gemia.video.stock_media.fetch_pixabay_media
est_tokens: 460
---

# stock-media

## 何时使用
用户明确要求从 Pexels、Pixabay、素材网站或公开素材库找一段视频/图片，或要求补充 b-roll/背景素材且没有指定必须由生成模型创作时使用。

## 参数说明
核心参数是 `query`、`provider`、`media_type`、`orientation`、`limit`。默认优先 `media_type: "video"`，`provider: "auto"`，让本地运行时从可用 API key 中选择。若用户点名 Pexels 或 Pixabay，使用对应 `fetch_pexels_media` / `fetch_pixabay_media`。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.stock_media.fetch_stock_media","args":{"query":"city night street b-roll","provider":"auto","media_type":"video","limit":6},"input":"$input","output":"$step_1"}
```

## 边界 / fallback
公开视频素材只能作为可替换 b-roll、背景或参考，不要伪装成用户真实素材。找不到素材时返回 ask 或换 query，不要调用 generative 冒充真实图库素材。
