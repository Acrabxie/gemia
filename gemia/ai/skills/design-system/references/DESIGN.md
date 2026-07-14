---
version: alpha
name: Lumeri Video
description: >
  Lumeri Video 的视觉与创作身份母文件。遵循 Google DESIGN.md 规范(token 为唯一
  规范值,正文只讲理据),并在其静态 UI 基础上扩展"时间维度" token,使其适配视频创作。
  本文件由 Gemini 在每次创作会话开始时加载,作为持久、结构化的创作基准。

# ─────────────────────────────────────────────
# 第一层:静态 token(沿用 DESIGN.md 语义)
# ─────────────────────────────────────────────

colors:
  # 品牌身份色(仅用于标题/下三分标/片尾等"图形层",不等于画面调色)
  brand-primary: "#5FC6DE"      # 冰蓝主色
  brand-accent: "#8BD8EA"       # 亮冰蓝(高光/强调)
  brand-tertiary: "#ABE5F1"     # 淡冰蓝(衬底)
  # 文本/字幕
  text-on-light: "#1A1A1A"
  text-on-dark: "#FFFFFF"
  caption-stroke: "rgba(0,0,0,0.75)"

typography:
  # 图形文本层(标题/字幕),非画面内文字
  title:
    fontFamily: Montserrat
    fontSize: 48px
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: -0.01em
  caption-body:
    fontFamily: Inter
    fontSize: 36px
    fontWeight: 500
    lineHeight: 1.2
  caption-annotation:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: 400

frame:
  aspect-horizontal: "16:9"
  aspect-vertical: "9:16"
  aspect-square: "1:1"
  # 安全框(动作/文本可占画面比例)
  action-safe: 0.90
  title-safe: 0.85
  caption-bottom-margin: 0.05     # 字幕距底边 = 5% 画面高
  caption-side-margin: 0.05

grading:
  # 一级校色基线(normative;针对册《调色》只引用不重复数值)
  black-point-floor: 0.03         # 黑点保留 ≥3% 细节,禁止死黑
  white-point-ceiling: 0.98       # 白点留 2% 安全边界,禁止爆白
  midtone-skin-target: [0.45, 0.55]   # 肤色中间调直方图落点
  saturation-global-cap: 0.85     # 全局饱和上限(超即告警)
  skin-saturation-offset: -0.20   # 肤色相对整体额外压饱和
  cross-shot-temp-tolerance: 300  # 跨镜头色温差容差(K)
  lut-strength: 0.75              # LUT 应用强度,非拉满(留逐镜微调余地)
  lut-microtune: 0.25             # 套 LUT 后仍需微调的幅度(曝光/对比/肤色)
  presets:
    cinematic:  { lift: 0.08, gamma: 1.05, gain: 0.95, saturation: 1.12, temp: "5500K" }
    documentary:{ lift: -0.05, gamma: 0.98, gain: 1.02, saturation: 0.95, temp: "6500K" }
    faded-film: { lift: 0.15, gamma: 0.95, gain: 0.95, saturation: 0.90, temp: "5800K" }
    noir:       { lift: 0.0,  gamma: 0.92, gain: 1.05, saturation: 0.80, temp: "5200K" }

depth:
  dof-shallow: { aperture: "f/1.4", use: "人物特写/情感" }
  dof-medium:  { aperture: "f/4.0", use: "对话中景" }
  dof-deep:    { aperture: "f/11",  use: "环境/建立镜头" }
  focus-pull-duration: "0.5s"

# ─────────────────────────────────────────────
# 第二层:时间 token(Lumeri Video 对 DESIGN.md 的扩展)
# 说明:base spec 无此层。消费方遇未知 group 应保留不报错。
# ─────────────────────────────────────────────

motion:
  easing:
    enter: "cubic-bezier(0, 0, 0.2, 1)"     # ease-out,入场
    exit:  "cubic-bezier(0.4, 0, 1, 1)"     # ease-in,出场
    move:  "cubic-bezier(0.4, 0, 0.2, 1)"   # 标准位移
    dramatic: "cubic-bezier(0.34, 1.56, 0.64, 1)"  # 弹性(慎用)
  text-enter: { min: "0.3s", recommended: "0.4s", max: "0.8s", easing: "{motion.easing.enter}" }
  text-exit:  { min: "0.2s", recommended: "0.3s", max: "0.6s", easing: "{motion.easing.exit}" }
  ken-burns-max: "2.0s"           # 缓推/缓移单次最长
  camera-move-rule: "必须指向画面中一个命名的关键元素"

transition:
  duration: { quick: "0.2s", standard: "0.5s", slow: "1.0s" }
  library: [cut, fade, dissolve, slide, zoom, blur, match-cut, whip-pan]
  budget-ratio: 0.20              # 转场总数 ≤ 镜头数 × 0.20
  default: cut                    # 70% 应为直切

pacing:
  asl-default: "2.5s"             # 平均镜头时长基线
  asl-by-mood:
    interview: [4.0, 6.0]
    narrative: [2.0, 4.0]
    high-energy: [0.8, 2.0]
    contemplative: [5.0, 8.0]
  duration-distribution: { long: 0.10, mid: 0.30, short: 0.40, ultra-short: 0.20 }
  flatness-guard: "禁止 ≥4 个连续镜头时长差 <0.5s"
  cu-max: "3.0s"                  # 特写单镜最长
  high-energy-run-max: "20s"      # 高能快切段最长
  beat-snap-ratio: 0.30           # 约30%切点强踩拍,余70%服从叙述

audiosync:
  lipsync-tolerance-ms: 100       # 字幕/口型对齐容差
  beat-snap-tolerance-ms: 100     # 剪辑点踩拍容差
  music-mood-must-match: true     # 音乐情绪须与画面/文案语气一致
  loudness-target: "-14LUFS"      # 交付响度

# ─────────────────────────────────────────────
# 组件层:把上述 token 组合成可复用镜头/片段模板
# ─────────────────────────────────────────────

components:
  shot-opening:
    duration: "1.5s"
    transition: zoom
    grading: "{grading.presets.cinematic}"
    caption: title
  shot-dialogue:
    duration: "3.0s"
    dof: "{depth.dof-medium}"
    caption-pos: bottom
  shot-closing:
    duration: "1.5s"
    transition: fade
    grading: "{grading.presets.cinematic}"
    caption: call-to-action

# 交付前必过的硬门(详见《辅助指导手册》红旗表)
quality-gate:
  - "饱和 ≤ {grading.saturation-global-cap}"
  - "字幕在 {frame} 安全框内、贴底不居中"
  - "转场数 ≤ 镜头数 × {transition.budget-ratio}"
  - "无 ≥4 连续同时长镜头({pacing.flatness-guard})"
  - "音画对齐 ≤ {audiosync.lipsync-tolerance-ms}ms"
  - "跨镜头色温差 ≤ {grading.cross-shot-temp-tolerance}K"
  - "响度归一化到 {audiosync.loudness-target}"
---

# Lumeri Video · 创作指导手册(主)

> 这是给 **Lumeri Video 底层 Gemini** 读的视觉与创作身份母文件。上方 YAML 是**唯一规范值**;
> 正文只解释"为什么这样定、何时怎么用",**不重复数值**。四本《针对性指导手册》与《辅助
> 指导手册》均引用本文件的 token,不得另立数值。基准对标 DaVinci Resolve / CapCut 专业版——
> 目标不是"能出片",而是"像专业调色师/剪辑师做的"。

## 1. Overview · 视觉身份宣言

Lumeri Video 的成片应给人**"克制的电影感"**:冷静、通透、有呼吸,而非社交平台上那种高饱和、
满屏转场的"AI 感"。四条底层原则贯穿全书:

- **科技与设计要隐形自然** —— 观众应感到"好看",而非"这是特效/这是 AI"。技巧越高级越不留痕。
- **Less is more** —— 每一个转场、每一次运动、每一处调色都要有理由;能不加就不加。
- **设计正确优先于商业恰当** —— 当"更炫"与"更对"冲突时,选对的那个。
- **尊重疯狂提案** —— 规范是底线不是天花板;明确的艺术意图可以突破条款,但要写清意图。

品牌图形层(标题、下三分标、片尾)使用 Lumeri 冰蓝身份色 `{colors.brand-primary}` 系;
**注意区分"图形层配色"与"画面调色"**——前者是叠加的 UI,后者是镜头本身的 grading,二者
token 分离(`colors` vs `grading`),不可混用。

## 2. Colors · 调色身份(Color & Grade)

调色是"看起来专业"的第一信号。核心是**克制**与**连贯**:

- **先技术后风格**:任何风格化之前,先用一级校色把曝光与白平衡摆正(见《调色》针对册)。
  白平衡错了,后面 LUT 再华丽也救不回来。
- **保住色阶两端**:最暗不低于 `{grading.black-point-floor}`、最亮不高于
  `{grading.white-point-ceiling}`。大片死黑或爆白 = 廉价。
- **饱和有天花板**:全局不超过 `{grading.saturation-global-cap}`;肤色在此基础上再压
  `{grading.skin-saturation-offset}`。过饱和是 AI 出片最常见的廉价源。
- **拒绝 teal-orange 惯性**:蓝橙对撞是缩略图调色技,不是电影调色。用 `{grading.presets}`
  里的成套情绪基调,并让高光/中间调/暗部**独立游走**,而非全片一个色温压死。
- **跨镜头连贯**:同场景镜头间色温差不超过 `{grading.cross-shot-temp-tolerance}`。

## 3. Typography · 字幕与文本层

字幕排版决定"精致度"。规则朴素但必须守:

- **字体家族 ≤ 2**:标题一种(`{typography.title}`)、正文/字幕一种(`{typography.caption-body}`),
  最多再加一种衬线做强调。同帧禁止 3 种以上字体或字重。
- **字幕贴底、不居中**:工业级字幕永远在下方安全区,居中糊在画面中央是无主次意识的表现。
  位置由 `{frame.caption-bottom-margin}` 与 `{frame.caption-side-margin}` 约束,且不得盖住人脸。
- **出入有缓动**:文字进出场用 `{motion.text-enter}` / `{motion.text-exit}` 的 easing,
  禁止 0→1 的生硬跳转(那看起来像数字时钟)。

## 4. Layout · 画幅与安全框(Frame)

- **双幅优先**:横屏 `{frame.aspect-horizontal}` 与竖屏 `{frame.aspect-vertical}` 是主力;
  为竖屏重排构图与字幕,而非把横屏裁一刀。
- **守安全框**:动作在 `{frame.action-safe}` 内、标题在 `{frame.title-safe}` 内。
- **三分而非居中**:关键元素落在三分线(约 1/3 或 2/3 处),连续 ≥3 个居中/对称构图会显得
  "模板生成、无导演意图"(详见《构图运镜》针对册)。

## 5. Elevation & Depth · 景深与层次

用景深制造透视与质感,而非全程一个平面:

- 特写/情感用 `{depth.dof-shallow}`,对话中景用 `{depth.dof-medium}`,环境建立用 `{depth.dof-deep}`。
- 焦点转移(focus pull)以 `{depth.focus-pull-duration}` 为默认节奏,用来引导视线。
- 前景-中景-背景要有分工:前景压虚、焦点锐、背景带虚化以拉开纵深。

## 6. Shapes · 转场语汇(Transitions)

在视频里,DESIGN.md 的 "Shapes(形状)" 对应**转场的形态语汇**:

- **默认直切**:`{transition.default}`;约 70% 的镜头衔接应是无转场的直切。
- **转场有预算**:全片转场总数 ≤ 镜头数 × `{transition.budget-ratio}`。转场是噪音,越少越贵气。
- **每个转场要有理由**:只在(a)音乐节拍切换 (b)叙述段落切换 (c)时空大跳 时使用;
  "形状好看就用"是 PPT 感的根源。禁止连续两个转场无间断。

---
## ⏱ 时间维度扩展(Lumeri Video 特有 · base spec 无此三章)

> DESIGN.md 原生只描述"静止的一帧"。视频比静态 UI 多出**时间轴**——运动、节奏、音画。
> 以下三章补齐这层,是让成片"活起来且不业余"的关键。

## 7. Motion & Timing · 运动与时基

- **每个动画都有时长带**:如文字入场 `{motion.text-enter}` 给出 min/recommended/max,
  太快生硬、太慢无力。
- **运动必须有指向**:相机推拉/摇移必须`{motion.camera-move-rule}`——指向画面中一个**具名**
  的关键元素;无目的的 Zoom/Dolly 让观众困惑"镜头为什么动"。Ken Burns 单次 ≤ `{motion.ken-burns-max}`。
- **曲线不用 linear**:位移用 `{motion.easing.move}`,弹性 `{motion.easing.dramatic}` 慎用。

## 8. Pacing & Rhythm · 节奏与韵律(P0 · 最大杠杆)

节奏差异是"看起来专业"最直接的信号,也是 AI 出片最容易露怯的地方:

- **ASL 随情绪走**:基线 `{pacing.asl-default}`,按 `{pacing.asl-by-mood}` 因段调整
  (访谈慢、高能快、沉静更慢)。
- **时长要有分布**:按 `{pacing.duration-distribution}`(长/中/短/超短)铺开,制造山谷与山峰;
  触发 `{pacing.flatness-guard}` 即判"节奏平坦",重剪。
- **高能不过载**:快切段 ≤ `{pacing.high-energy-run-max}`,特写单镜 ≤ `{pacing.cu-max}`,
  高能段后必须给缓冲长镜"呼吸"。

## 9. Audio-Visual Sync · 音画契约(P1)

- **对齐容差**:字幕/口型 ≤ `{audiosync.lipsync-tolerance-ms}`,剪辑点踩拍 ≤ `{audiosync.beat-snap-tolerance-ms}`。
- **关键点踩拍**:章节起头、高潮、收尾的镜头切换必须落在音乐节拍上。
- **情绪一致**:`{audiosync.music-mood-must-match}`——欢快音乐配沉重文案是割裂。
- **响度交付**:终混归一化到 `{audiosync.loudness-target}`。

---

## 10. Components · 镜头/片段/序列组件

把上述 token 组合成可复用模板,减少每次从零决策:开场 `{components.shot-opening}`、
对话 `{components.shot-dialogue}`、收尾 `{components.shot-closing}`。Gemini 生成序列时优先
从组件起手,再按具体内容微调 20-30%。

## 11. Do's and Don'ts · 创作禁区与最佳实践

**Do ✅**
- 全片一套视觉身份 token(色温/字体/描边)贯穿首尾。
- 冷暖分区、成段过渡,而非随镜乱飘。
- 景别有跨度(近/中/远、俯/平/仰交替)。
- 关键转场踩音乐节拍;为重要信息留呼吸空间。

**Don't ❌**
- ✗ 同帧 3+ 字体 / 字幕居中糊中央 / 过饱和(>`{grading.saturation-global-cap}`)。
- ✗ 转场超预算 / 连续两转场 / 运动无指向。
- ✗ 节奏平均(触发 `{pacing.flatness-guard}`)/ 死黑爆白 / 音画错位 >100ms。
- ✗ 全程 teal-orange / 构图永远居中对称。

> 出片前 Gemini 必须逐条跑 `quality-gate`(见 front-matter)与《辅助指导手册》的**红旗自检表**;
> 命中任一红旗即打回重做,不得交付。
