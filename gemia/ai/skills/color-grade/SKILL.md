---
id: color-grade
description: |
  用于参考 DaVinci Resolve Color page 的整体调色、冷暖、曝光、对比、饱和度、LUT、胶片色和统一多个素材色彩。何时不用我：具体油画/水彩/像素化等艺术滤镜用 stylize-art；文本生成新视觉或图生视频用 generative；局部抠像/蒙版/合成用 composite-blend。
triggers:
  primary: [调色, 冷色, 暖色, 色调, 曝光, 对比度, 饱和度, LUT, color grade, color grading, exposure]
  secondary: [好看, 高级感, 统一色彩, 电影感, resolve color, color page, cyberpunk, vintage, warm, cool, contrast]
primitives:
  - gemia.picture.color.color_grade
  - gemia.picture.color.adjust_exposure
  - gemia.picture.color.adjust_temperature
  - gemia.picture.color.lift_gamma_gain
  - gemia.picture.color.log_to_linear
  - gemia.picture.color.color_space_convert
  - gemia.picture.color.apply_3d_lut
  - gemia.picture.enhance.color_balance
  - gemia.picture.enhance.color_lookup
  - gemia.picture.enhance.hdr_grade
  - gemia.picture.enhance.image_adjust_hsl
  - gemia.picture.enhance.image_contrast
est_tokens: 560
---

# color-grade

## 何时使用
需要整体观感、色彩一致性或冷暖曝光调整时使用。视频素材可直接套 picture primitive，Lumeri 会按帧应用。

## 参数说明
核心参数是 `preset`、`exposure`、`temperature`、`contrast`、`saturation`、`lut_name`。缺省时保持自然，不要过度处理。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.picture.color.color_grade","args":{"preset":"warm"},"input":"$input","output":"$output"}
```

## 边界 / fallback
“赛博朋克氛围合成”若要求生成新画面，使用 generative；“像素化/油画”用 stylize-art。
