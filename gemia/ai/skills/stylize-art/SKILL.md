---
id: stylize-art
description: |
  用于单点艺术化滤镜：油画、水彩、像素化、霓虹、漫画、素描、VHS、故障和半调。何时不用我：整体冷暖、曝光、LUT 用 color-grade；基于文本生成全新视觉内容用 generative；抠像混合用 composite-blend。
triggers:
  primary: [油画, 水彩, 像素化, 艺术滤镜, 漫画, 素描, neon glow, pixelate, watercolor, oil paint]
  secondary: [风格化, VHS, 故障, glitch, sketch, cartoon, halftone, retro]
primitives:
  - gemia.picture.enhance.image_oil_paint
  - gemia.picture.enhance.image_watercolor
  - gemia.picture.enhance.image_pixelate
  - gemia.picture.enhance.image_neon_glow
  - gemia.picture.enhance.image_cartoon
  - gemia.picture.enhance.image_sketch
  - gemia.picture.enhance.image_color_halftone
  - gemia.picture.enhance.image_glitch_datamosh
  - gemia.picture.generative.style_transfer
est_tokens: 540
---

# stylize-art

## 何时使用
用户点名某种具体艺术滤镜或单点视觉风格转换时使用。

## 参数说明
核心参数是 `style`、`strength`、`palette`、`preserve_subject`。默认保留主体可读性。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.picture.enhance.image_watercolor","args":{"strength":0.6},"input":"$input","output":"$output"}
```

## 边界 / fallback
“赛博朋克调色”更偏 color-grade；“生成赛博朋克城市背景”更偏 generative。
