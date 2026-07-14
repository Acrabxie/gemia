---
id: composite-blend
description: |
  用于参考 DaVinci Resolve 剪辑软件里的 Fusion/Color 工作流：抠像、绿幕、亮度键、形状蒙版、power window、羽化、反选、matte 预览、混合模式、透明合成、双重曝光和局部叠加。何时不用我：复杂多轨 keyframe 流程用 layer-flow；HTML/Lottie 文字叠加用 html-graphics；三维空间合成用 blender-link；单纯整体调色用 color-grade。
triggers:
  primary: [混合模式, 蒙版, 遮罩, 抠像, 绿幕, chroma key, luma key, alpha matte, magic mask, composite blend, double exposure]
  secondary: [合成, 叠加, 羽化, 反选, matte, overlay, alpha, mask, keyer, qualifier, davinci resolve, 达芬奇剪辑软件, fusion page, color page, power window, blend, split screen]
primitives:
  - gemia.picture.composite.create_mask
  - gemia.picture.composite.create_edge_mask
  - gemia.picture.composite.chroma_key
  - gemia.picture.composite.luma_key
  - gemia.picture.enhance.image_composite_alpha
  - gemia.picture.enhance.image_double_exposure
  - gemia.video.masking.render_chroma_key_preview
  - gemia.video.masking.render_luma_key_preview
  - gemia.video.masking.render_shape_mask_preview
  - gemia.video.masking.render_masked_composite
est_tokens: 540
---

# composite-blend

## 何时使用
用户要求把素材通过 mask/key/blend 合到一起时使用。遇到“抠像、绿幕、蒙版、遮罩、羽化、反选、matte、DaVinci Resolve、Fusion page、Color page、power window、局部遮罩、背景替换”时，优先考虑视频级 `gemia.video.masking.*`，不要退回纯文字层。

## 参数说明
核心参数是 `mode`、`key_color`、`tolerance`、`low/high/soft`、`shape`、`center`、`size`、`feather`、`invert`、`background_path`、`background_color`、`matte_view`。缺省时直接执行：绿幕用 green + tolerance 0.28；局部强调用 ellipse + feather；不确定背景时用黑色/暗背景预览。

## 典型 plan 片段
```json
{"id":"step_1","function":"gemia.video.masking.render_chroma_key_preview","args":{"key_color":"green","tolerance":0.28,"feather":1.5,"background_color":"black"},"input":"$input","output":"$output","assistant_message":"我先做一版干净的绿幕抠像预览，保留边缘羽化和黑底检查。","artifact_type":"video"}
```

## 边界 / fallback
多层时间轴动画用 layer-flow；普通 lower-third 不需要 composite primitive。这里的“达芬奇”指 DaVinci Resolve 剪辑软件的能力参照，不是艺术风格；需要自定义复杂节点树时先用 `render_masked_composite` 做小样，不要发明未注册的 Resolve/Fusion primitive 名字。
