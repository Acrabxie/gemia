---
id: ad-graphics
description: |
  用于商业广告风格的文字、图案、产品卖点、CTA 卡、价格/促销标签、产品标注和扫光动效。何时不用我：普通字幕或单个标题用 html-graphics；数学/流程 MG 解释用 html-graphics；复杂开放式图层源码扩展用 creative-runtime；只剪辑/转场/调色分别用 timeline-ops、transition、color-grade。
triggers:
  primary: [商业广告, 广告图文, 广告动效, 产品卖点, CTA, cta card, price badge, product callout, shimmer sweep, promo graphics]
  secondary: [卖点, 促销, 价格标签, 按钮, 购买按钮, 号召, 标注, 扫光, 质感文字, 商业感, 广告片, product promo, callout]
primitives:
  - gemia.video.ad_graphics.render_ad_title_pack
  - gemia.video.ad_graphics.render_lower_third
  - gemia.video.ad_graphics.render_cta_card
  - gemia.video.ad_graphics.render_product_callout
  - gemia.video.ad_graphics.render_shimmer_sweep
  - gemia.video.ad_graphics.compose_overlay_on_video
est_tokens: 720
---

# ad-graphics

## 何时使用

用户要“商业广告感”的图文动效时使用：卖点标题、产品标注、CTA 结尾卡、价格/促销标签、扫光文字、lower third、app/product showcase 的短促视觉包装。它偏成片包装，不是通用字幕。

## 工作流

优先把广告效果拆成明确图层和时间段：标题层、卖点层、标注层、CTA 层、扫光/图案层。先渲染真实结果并写 `.ad_graphics.json` / `.ad_composition.html` 旁车；如果 review 后发现现有 primitive 不够，再和 `creative-runtime` 组合生成开发 brief，而不是编造不存在的函数名。

## 参数说明

- `title` / `headline` / `label`：主卖点，尽量短。
- `subtitle` / `body` / `detail`：补充说明，不要塞长段落。
- `cta` / `button_text`：行动按钮，比如“立即体验”“了解更多”。
- `style`：默认 `ice`，也可用 `mono` 或 `night`。
- `duration` / `start_sec`：广告包装出现时间。用户不说时用默认值，不要 ask。
- `point_x` / `point_y`：产品标注指向位置；用户不说时按画面右上黄金点默认。

## 典型 plan 片段

```json
{
  "id": "step_1",
  "function": "gemia.video.ad_graphics.render_ad_title_pack",
  "args": {
    "title": "Lumeri",
    "subtitle": "一句话把素材变成成片",
    "kicker": "NEW WORKFLOW",
    "cta": "立即体验",
    "style": "ice",
    "duration": 3
  },
  "input": "$input",
  "output": "$output",
  "assistant_message": "我会先做一组商业广告式标题包装，把核心卖点压到画面前段。"
}
```

```json
{
  "id": "step_2",
  "function": "gemia.video.ad_graphics.render_product_callout",
  "args": {
    "label": "自动读素材",
    "detail": "先理解画面，再规划剪辑",
    "badge": "AI WORKFLOW",
    "point_x": 0.72,
    "point_y": 0.42,
    "style": "ice"
  },
  "input": "$input",
  "output": "$output",
  "assistant_message": "接着我会给产品重点加一个广告标注，让观众一眼看到卖点。"
}
```

## 边界 / fallback

何时不用我：只加普通字幕、标题卡或 Lottie 小图标时用 `html-graphics`；需要自由搭多层图层或新增微函数时用 `creative-runtime`；只要“加转场/调色”时交给 `transition` 或 `color-grade`。用户没有给完整文案时，基于 request 自己写短广告文案，不要反复 ask。
