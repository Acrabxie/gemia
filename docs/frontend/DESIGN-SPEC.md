# Lumeri 前端设计规范

**版本** v1.2 · 2026-08-05（新增视频空间/时间微调交互内核；用户拍板）
**适用范围** Lumeri 全部产品前端：web v3（`static/v3/`）、CLI（`lumeri-cli`）、桌面壳（Electron）。管产品界面本身，不管成片画面（成片归 `lumeri-design-manuals/DESIGN.md`）。
**文档地位** 本规范是前端视觉与交互的唯一权威口径。与 `lumeri-design-manuals/针对性/05-界面UI.md`（胶囊版 token，已过时）冲突处以本规范为准；`lumeri-cli/docs/tui-design-spec.md` 降为本规范第 16 章的 CLI 实现细则；`06-AI交互准则.md` 保留 agent 行为契约与出片侧管辖，其**前端呈现**条文以本规范第 9 章为准。
**语气约定** "必须/禁止" = 硬规则，违反即缺陷；"应" = 默认做法，偏离需在代码注释里写明理由；"可" = 自由裁量。正文各表与附录 A 冲突时，**以附录 A 为 canonical**。

---

## 0. 总则：Lumeri 的设计立场

1. **最好的科技感受不到科技，最好的设计是它本来就这样。** 界面不炫技、不刷存在感；用户注意力属于他的视频，不属于 UI。
2. **画面是唯一主角。** Lumeri 是视频创作工具，UI 是影棚的黑墙：有层次、有质感，但永远退在用户的画面后面。暗色是第一公民，不是浅色主题的反转。
3. **less is more。** 有更简洁的方案就选更简洁的。一屏一个主 CTA；90% 的界面只用表面梯与灰阶文字；冰蓝只做点睛。
4. **能用图案，尽量用图案。** 图标承义，文字退居 tooltip / `aria-label`。例外：不可逆确认按钮必须保留文字。
5. **系统先行。** 实现 CSS 中的颜色、间距、圆角、字号、时长必须走 token 或档位值；组件固有尺寸（switch 52×32、图标钮 34×34 等）以第 8 章组件表为 canonical，不属 token 强制范围但同样禁止随意偏离。
6. **层级靠弱化次要，不靠强化主要。** 工具顺序：字重（400/500 对 600/700）→ 文字灰阶 → 最后才是字号。禁止靠加框、加色、加动效抬层级。
7. **无障碍是设计约束，不是补丁。** 对比度、焦点、目标尺寸、reduced-motion 在设计阶段锁定（见第 14 章底线表）。
8. **借鉴不抄袭。** 视觉语言以 Material 3 为骨（用户拍板，2026-07-12），以专业剪辑工具（DaVinci / CapCut）为密度参照，但不照搬任何一家的成品皮肤。不抄清单：SF 字体、毛玻璃表面、弹性滚动、ripple 波纹、spring token 数值。
9. **尊重用户的理解力。** 先保证用户看得懂，再删除重复线索；一条清楚的文案或视觉差异已经能表达时，禁止再叠加彩底、描边、图标、标题或二次解释。界面不靠夸大状态证明自己“提醒过”，也不把用户当成需要反复教育的人。

---

## 1. 品牌与色彩

### 1.1 四个合法冰蓝值的定界（硬规则）

| 值 | 名字 | 用在哪 |
|---|---|---|
| `#5FC6DE` | **品牌种子色** | Logo / 品牌资产 / 宣传物料；CLI 终端 accent（truecolor）；MCU 生成色板的唯一 seed |
| `#54d6f3` | **UI 交互主色**（`--m3-primary`） | web / 桌面壳一切界面组件。由 MCU 从种子色生成，不是手调值 |
| `#239FC0` | **白底字标色** | 浅色载体上的**品牌文字**（wordmark、宣传页、文档、未来 light theme 预留） |
| `#1E7A94` | **终端亮底文字 accent** | 仅 CLI：亮色终端背景下的**交互文字**（链接、命令、h1），经 `COLORFGBG` 探测切换。与 `#239FC0` 的分工：一个是品牌字标色，一个是终端交互文字色 |

- 各司其职，禁止互相顶替。界面组件永远消费角色 token（`--m3-*`），**裸 `#5FC6DE` 禁止出现在 web 组件 CSS 里**。
- Logo 渐变三色 `#5FC6DE / #8BD8EA / #ABE5F1`（下带→上带→光点，由下至上渐亮）只属于品牌资产与 CLI splash，禁止进入 UI 组件与转录区。
- **称谓口径**：Lumeri 是母品牌；视频产品对外名为 **Lumeri Video**。header、splash、tagline、空态等界面称谓受此约束（品牌家族语境用 Lumeri，指本产品用 Lumeri Video）。

### 1.2 色板生成（硬规则）

- 全套色板由 `@material/material-color-utilities@0.3.0` 从 seed `#5FC6DE` 生成 dark scheme（**0.4.0 npm 包发布损坏，禁装**）。
- 换主题 = 重新生成 token 层，组件规则一行不改（paint-by-number）。禁止手写 hex 拼 scheme、禁止在生成值上手调。
- 功能色必须经 `Blend.harmonize()` 向 seed 谐调后使用。生成脚本与谐调源值必须归档（现状未归档，见附录 B-24——否则"换主题只换 token 层"的承诺无法兑现）。

### 1.3 色彩角色（现行 dark scheme 权威值）

**交互角色：**

| Token | 值 | 用途 |
|---|---|---|
| `--m3-primary` | `#54d6f3` | 主交互色：filled 按钮底、text 按钮文字、链接、运行态、进度、选中 ring |
| `--m3-on-primary` | `#003640` | primary 填充上的内容 |
| `--m3-primary-container` | `#004e5c` | 用户气泡、plan bar、subagent 分组线 |
| `--m3-on-primary-container` | `#aaedff` | primary-container 上的内容 |
| `--m3-secondary` | `#b2cbd2` | **focus ring 专用**（与 primary 填充的 active 态区分） |
| `--m3-secondary-container` | `#334a50` | tonal 按钮底、选中态（menu row / chip / toggle pill） |
| `--m3-on-secondary-container` | `#cee7ee` | 同上内容 |
| `--m3-tertiary` | `#bec5eb` | 第三色：时间线 text 轨等"既非主也非状态"的分类 |
| `--m3-error` / `--m3-error-container` | `#ffb4ab` / `#93000a` | 错误前景 / 错误容器 |
| `--m3-on-error-container` | `#ffdad6` | 错误容器内容 |

**配对纪律（硬规则）**：`on-X` 只能出现在 `X` 上；`*-container` 禁止用作**文字/图标**颜色（用作分组线、选中 ring 等非文字图形合法）；分隔线必须用 `outline-variant`；**交互组件的边界描边必须用 `outline`**（`outline-variant` 撑不起 3:1 组件边界对比，见 1.4）。

### 1.4 表面梯（tonal ladder）

层级归属靠背景明度阶梯表达，**相互重叠的表面必须取不同梯级**：

| Token | 值 | tone | 分工 |
|---|---|---|---|
| `--m3-surface-lowest` | `#0b0f10` | N4 | 时间线井（最深的"舞台下方"） |
| `--m3-surface` | `#111415` | N6 | app 底、预览舞台 |
| `--m3-surface-low` | `#191c1d` | N10 | header、chat rail、时间线工具栏等贴边 chrome |
| `--m3-surface-container` | `#1d2021` | N12 | 菜单、召唤式面板 |
| `--m3-surface-high` | `#272a2b` | N17 | 卡片、assistant 气泡、对话框 |
| `--m3-surface-highest` | `#323536` | N22 | 输入框、chips、轨道、代码块、toast、tooltip |

**文字与轮廓：**

| Token | 值 | 分工 |
|---|---|---|
| `--m3-on-surface` | `#e1e3e4` | 主文字 |
| `--m3-on-surface-variant` | `#bfc8cb` | 次要文字、**占位文字**（对 surface-highest ≈7.2:1） |
| `--system-text` | primary 72% + surface 28% | 会话中的宿主/系统文字；暗底对比度约 6.1:1 |
| `--m3-outline` | `#899295` | 交互组件描边（outlined 按钮/chip、switch 轨）、状态点。**不是文字角色**；禁用文字走 7.3 的透明度方案 |
| `--m3-outline-variant` | `#3f484b` | 分隔线、非交互装饰边（对 surface 仅 ~2:1，禁止承担交互边界） |

**Scrim：** `rgba(0,0,0,0.5)`。刻意偏离 M3 规范的 0.32——近黑底上 0.32 不可感知。此偏离已论证，保持。

> 命名备注：本项目用 `--m3-surface-low` 等短名对应官方 `surface-container-low`。属既成命名，全站一致即可，禁止再引入第三种命名。

### 1.5 功能色与"一色一义"（硬规则）

| Token | 值 | 语义 |
|---|---|---|
| `--m3-primary` | `#54d6f3` | **可交互 / 品牌 / 运行中**。三义合一，专属冰蓝。链接、命令等交互性文字用 primary 是本义，不算"冰蓝强调正文" |
| `--ok` | `#06d6bd` | 成功、完成、FINAL 终态 |
| `--warn` | `#dfcd5d` | 警告、预算、gated 等待、时间线 marker |
| `--err`（=`--m3-error`） | `#ffb4ab` | 失败、破坏性操作 |
| `--ok-tint` / `--warn-tint` | 功能色 15% → transparent | 功能色容器底（banner、状态 chip） |
| `--err-tint` | error 14% → transparent | 同上（14% 为现行实装值，保持） |
| `--tl-playhead` | `#ff4b5c` | 时间线 playhead 专用警示红（剪辑工具通行惯例，**全站唯一非 MCU 体系色**，仅此一处豁免） |

- 每个颜色只许一种含义；**同一含义禁止在两处用两种颜色**（当前双时间线两套轨道色语义是待清算缺陷，见附录 B-1）。
- `--ok` 与品牌同为青侧，因此**状态永远色 + 形双通道**：成功必配 ✓ 形、失败必配 ✗ 形、运行必配动效或进度形。纯颜色传状态即缺陷。（"形"按介质取：web 用 sprite 图标 `i-check`/`i-x-circle`，CLI 用词表 glyph `✔`/`✗`。）
- 冰蓝（primary）填充上禁止白字——用 `--m3-on-primary` 深墨。
- 彩色底上禁止用灰字；用 on-role 或同 hue 调明度。tint 底（深色）上配**功能色本体亮字**，不是深字。
- 纯黑 `#000` 禁止用作文字或背景（scrim/阴影除外）。

### 1.6 主题扩展

- 暗色是第一公民。**light theme 暂不实现**，但架构必须随时可加：组件只消费角色 token，届时由 MCU 同 seed 生成 light scheme 换 token 层即可。
- 禁止 `prefers-color-scheme` 半成品分支（要么完整生成，要么不做）。

---

## 2. 排版

### 2.1 字体栈（权威值）

```css
/* UI 正文 —— 西文在前，中文在后（防中文字体的难看拉丁形抢跑） */
font-family: Roboto, "Helvetica Neue", -apple-system, BlinkMacSystemFont,
  "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;

/* 等宽 —— 必须走 token，禁止内联重复 */
--font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
```

- 全站字体角色 ≤2（正文 + 等宽）。禁止第三种。
- 禁止对汉字施加 faux italic / faux bold；中文强调用字重或 `text-emphasis` 着重号。

### 2.2 字号档（8 档，禁止档外值）

| px | 行高 | 角色 | 对应 M3 |
|---|---|---|---|
| 10 | 1.3 | 时间线轨标、时码、微标签（**必须 ≥500 字重**） | — |
| 11 | 1.3 | pane 标题（caps）、chips、meta、tooltip（**必须 ≥500 字重**） | label-small |
| 12 | 1.4 | 次级按钮、描述、mono 块 | label-medium |
| 13 | 1.4 | 菜单项、placeholder、工具名 | — |
| 14 | 1.45（chrome）/ **1.6（对话长文）** | 正文、按钮 label、composer 输入 | body-medium / label-large |
| 16 | 1.4 | header 标题、**表单文本框**（防 iOS 缩放，见 8/11 章） | title-medium |
| 20 | 1.3 | 对话框标题 | ≈title-large（M3 原值 22，Lumeri 收敛为 20） |
| 24 | 1.25 | 空态 headline | headline-small |

- **声明偏离**：对话长文 14px 低于 Practical Typography 的 15px 正文下限——专业密度工具 + 400px rail 的中文行长约束下的刻意选择，以 1.6 行高补偿。标题档行高取紧（1.25–1.3）按单行场景设计；**可折行的中文标题行高 ≥1.4**。
- **输入框两档裁决**：表单类文本框（登录、设置、对话框内）一律 16px；composer 输入 14px 是唯一豁免（阅读距离近、随长文 grow、非 iOS 场景）。
- **野值消灭**：12.5 → 13；11.5 → 11；10.5 → 10（补 500 字重）或 11；18 → 20；22 → 24。
- 字重白名单：400 / 500 / 600 / 700。600 起算强调；<400 禁止。
- 组件字号一律 px。**唯一例外**：markdown 渲染区内部允许相对档 `0.85 / 0.88 / 0.9 / 1.1 / 1.25 / 1.4em`（内容随容器缩放是合理需求），档外 em 值禁止。
- 数字量值（时码、时长、百分比、文件大小）必须 `font-variant-numeric: tabular-nums`。

### 2.3 中文排版规则（Lumeri 是中文优先产品）

- 对话/阅读性长文行高 1.6（中文正文合法区间 1.5–2.0，禁止套西文的 1.2–1.4）；chrome 短文案 1.3–1.45 可。
- 中文正文容器限宽 17–40 em，上限 48 em；对话气泡区当前 ~400px rail 天然合规，改版时不得突破。
- 标点在源码层写对：中文引号""''、省略号……、破折号——；禁止 `--` 冒充破折号、直引号冒充引号。
- 中西/数字混排：**静态 UI 文案**在源码层执行"汉字与西文、数字之间加一个半角空格；紧邻中文标点处不加"；动态内容（模型输出、用户文本、文件名）不强制。
- `text-align: justify` 对中文安全可用；西文区块 justify 必须配 `hyphens: auto` + `lang`，否则左对齐。
- 全大写/caps 标签配 `letter-spacing: 0.05–0.12em`（现行 pane 标题的做法，保持）。
- 遵守禁则：点号不上行首，开引号不落行尾（浏览器默认行为已覆盖大部分，自绘换行逻辑须遵守）。

---

## 3. 间距与布局

### 3.1 间距档

- **主网格 4px**：`4 / 8 / 12 / 16 / 24 / 32 / 48`。组件外距 > 组件内距；先给足留白再收。
- **密集半档**：`2 / 6 / 10` 仅允许在密集 chrome（时间线、chips、图标钮内距）使用。
- **禁止**：3 / 5 / 7 / 9 / 11 / 13 / 14 / 26 及一切档外值。现存野值列附录 B-6 收敛。

### 3.2 布局骨架

- 桌面双栏：预览舞台 `1fr` + 对话 rail `400px`（grid）。rail 宽度是对话行长的承重墙，改动需重新验算 2.3 的行长约束。
- 断点采用 M3：`600 / 840 / 1200 / 1600px`。窄屏方向：对话 ⇄ 编辑折叠为 tab 切换，时间线横滚 + 捏合缩放（未实现，实现时按此）。
- **320px 宽无横滚**（= 1280px 页面 400% zoom 验收）。时间线属"本质二维内容"享受 WCAG reflow 豁免，但工具栏与属性面板不豁免。
- 宽内容（表格、代码块、时间线）各自 `overflow-x: auto`，body 永不横滚。

### 3.3 z-index 档（token 化，禁止字面量）

| Token | 值 | 层 |
|---|---|---|
| `--z-header` | 20 | 顶栏 |
| `--z-composer` | 30 | 输入壳 |
| `--z-stage-menu` | 31 | 舞台标签栏菜单 |
| `--z-menu` | 45 | plus/slash 菜单 |
| `--z-tray` | 900 | 召唤式面板 |
| `--z-toast` | 950 | toast |
| `--z-dialog` | 1000 | 模态对话框 |
| `--z-tooltip` | 1100 | tooltip（压过一切非模态层） |

组件内部堆叠（≤10）可用局部小值，跨组件层级必须走 token。

---

## 4. 形状（圆角）

**六档 shape scale（现行，权威）：**

| Token | 值 | 用途 |
|---|---|---|
| `--shape-xs` | 4px | 菜单、行内代码 |
| `--shape-sm` | 8px | chips、内部块、时间线 clip、tooltip |
| `--shape-md` | 12px | 卡片、banner、toast |
| `--shape-lg` | 16px | 气泡、召唤式面板 |
| `--shape-xl` | 28px | 对话框、长高后的 composer |
| `--shape-full` | 999px | 按钮、输入壳（stadium 态）、switch、进度条 |

- 禁止第七种**独立**圆角档。
- **嵌套圆角公式（硬规则）**：内半径 = 外半径 − padding。公式派生值（如 28−12=16、16−6=10）**不算新档、豁免于六档禁令**；组件表中的固定圆角是"独立摆放时的默认值"，与公式冲突时**公式优先**。
- 信息密集的卡片/单元格禁止用 lg 及以上（会裁内容、显臃肿）。

---

## 5. 高度与阴影

### 5.1 分工（硬规则）

- **"归属"归表面梯，"漂浮"归阴影。** 一个元素在哪一层，由背景梯级说话；只有确实悬浮于内容之上的元素（菜单、面板、对话框、composer、toast、tooltip）才有阴影。卡片静止无阴影，hover/拖拽才抬升。
- **浮层分隔一律 elevation，禁止描边**（用户拍板，2026-07-12）。给浮层加 1px outline/白边即缺陷。`outlined` 是按钮/chip 的组件变体，不是浮层分隔手段。
- 发丝线 `--hairline`（box-shadow 模拟 1px outline-variant）仅限**贴边 chrome 的分区**（header 底缘、rail 左缘、工具栏边界），禁止用于浮层轮廓或卡片描边。

### 5.2 阴影 token（M3 双层配方：key + ambient）

| Token | 用途 |
|---|---|
| `--elev-1` | 卡片 hover 抬升、tonal 按钮按下 |
| `--elev-2` | 菜单、slash 弹层、tooltip（**近黑调校档**，见附录 A） |
| `--elev-3` | 召唤式面板、对话框、toast（**近黑调校档**） |
| `--elev-4` / `--elev-5` | **仅瞬时态**（拖拽中、hover 抬升）。静止组件用 4/5 即缺陷 |
| `--shadow-float` / `--shadow-float-focus` | composer 专用双层强阴影 |

- **近黑调校**：M3 canonical 阴影对在 N6 底上不可感知（与 scrim 0.5、shadow-float 同一教训），elev-2/3 换用"紧贴接触影 + 宽软环境影"的加强配方（2026-07-13 实拍验证后定档）。
- 静止 elevation ≤3 档："层越少，阴影越有指向力"。
- 暗色阴影参数不得照抄浅色主题（将来做 light theme 时须重配）。
- 阴影质量由实现者自检（截图验证层级可读性），不外抛给用户判断。

---

## 6. 动效

### 6.1 Token

**时长（5 档 + 特例）：**

| Token | 值 | 用途 |
|---|---|---|
| `--dur-xs` | 100ms | 微反馈：hover 态、按钮按下 |
| `--dur-sm` | 150ms | 小控件：switch、chip、图标钮；浮层出场 |
| `--dur-md` | 200ms | 菜单、slash 弹层入场；对话框出场 |
| `--dur-lg` | 250ms | 对话框入场 |
| `--dur-xl` | 300ms | 抽屉、大区域展开 |
| （特例） | 120ms linear | 进度条填充推进 |

**豁免条款**：循环动画周期（spinner、pulse、streaming 光标 blink、indeterminate 滑块）不受 `--dur-*` 档约束，但必须在第 8 章组件表中显式标注周期值，禁止散落无档。

**缓动：**

| Token | 曲线 | 用途 |
|---|---|---|
| `--md-standard` | `cubic-bezier(0.2, 0, 0, 1)` | 全程在屏的移动/形变 |
| `--md-emph-decel` | `cubic-bezier(0.05, 0.7, 0.1, 1)` | 入场（菜单、对话框、面板） |
| `--md-accel` | `cubic-bezier(0.3, 0, 1, 1)` | 出场（时长取入场的 60–85%，落两档间取下档） |
| `--md-emph-accel` | `cubic-bezier(0.3, 0, 0.8, 0.15)` | 大转场出场（新增 token，现代码未用） |

### 6.2 纪律（硬规则）

- **零动画场景**：键盘触发的**高频编辑操作**（裁剪、移动、切换、逐 token streaming）禁止加动画；浮层入退场动效不因触发方式（鼠标/键盘）而异。tooltip 首个有延迟（500ms），同一交互序列内后续 tooltip 零延迟零动画。
- enter 用 decelerate、exit 用 accelerate 且更短；**禁止 enter 用 ease-in**。例外：暂离元素（侧板收起、抽屉关闭，随时召回）出场用 standard 而非 accelerate——加速离场暗示永久消失。
- 组件状态动画用 `transition`（可中断），不用 `keyframes`（不可中断）；循环动画是 keyframes 的唯一合法场景。
- 只动 `transform` / `opacity`；`box-shadow` 过渡仅限低频小面积元素（focus/hover 抬升），并知晓其触发 paint 的代价；禁止动 `width / height / margin / padding`。**披露式容器（时间线抽屉）**：容器尺寸快照切换（单次重排），动效只落在内容的 translateY/opacity 上——384px = 内容实算高度，改抽屉内容必须同步。
- `:active { scale: 0.97 }` 作按压反馈；scale 入场从 ≥0.9 起，禁止 `scale(0)` 弹出。
- 列表 stagger 每项 ~20ms、总时长 ≤500ms。
- `prefers-reduced-motion: reduce` 必须实现：位移/缩放降级为同时长纯 opacity，循环动画停用，且信息必须仍有静态表达。
- 自动动效 >5s 必须可暂停/停止（WCAG 2.2.2）。
- 评估标准：普通用户若经常"注意到"动效，删。

---

## 7. 交互状态

### 7.1 State layer（M3 流派，全站唯一流派）

- hover / focus / press / drag = 内容色（on-color）半透明膜叠加：**8% / 10% / 10% / 16%**（取 m3.material.io Expressive 当前值；非 web token 库 v0.192 的 12%），用 `color-mix(in srgb, var(--on-role) N%, var(--container))` 或 `::after + currentColor + opacity` 实现。
- 禁止：换背景色表达 hover；黑/白半透明当状态层；Radix"沿色阶走一步"流派（与 M3 流派互斥，禁止混入）。
- 状态层不得引起布局位移。

### 7.2 焦点

- Focus ring 权威规格：`outline: 3px solid var(--m3-secondary); outline-offset: 2px`，**仅 `:focus-visible`** 触发（鼠标点击不出环）。secondary 角色是刻意选择——与 primary 填充的 active 态区分。
- 填充式文本框（M3 filled field）的焦点指示 = 底线 1px→2px primary，替代外环（M3 模式，合法）。
- `outline: none` 无替代即缺陷。焦点序 = 视觉序；modal 内焦点困笼 + `Esc` 逐层退出 + 关闭后焦点归还触发元素。
- 获焦组件不得被 sticky 元素完全遮挡（WCAG 2.4.11）。

### 7.3 禁用态

- 内容 38% / 容器 12% 透明度双数字表达。禁止换灰色、禁止只降饱和、禁止挪用 `--m3-outline` 当禁用文字色。
- 表单提交按钮**永不禁用**（见第 11 章）；禁用态只用于确实不可用的工具控件。

### 7.4 目标尺寸

| 场景 | 规格 |
|---|---|
| 一切指针目标 | ≥24×24px（WCAG 2.5.8 底线；或 24px 直径圆互不相交豁免） |
| 密集 chrome 图标钮 | 视觉 28–34px + `::before inset:-6px` 扩热区 |
| 主要控件 / 表单输入 | ≥44px 高 |
| 菜单行 | 48px（M3 menu item） |
| 拖拽操作 | **必须配非拖拽替代路径**（WCAG 2.5.7——时间线 clip 移动/裁剪必须有按钮或输入框等价操作） |

---

## 8. 组件规范

> 每个组件：表面 + 形状 + 高度 + 状态层，全部查表取档。新组件先查此表能否组合出来，组合不出再立新档（需过治理流程，第 17 章）。

| 组件 | 规格 |
|---|---|
| **按钮 · filled** | primary/on-primary，`shape-full`，label 14/500；主 CTA 一屏一个 |
| **按钮 · tonal**（默认） | secondary-container 对，`shape-full`；hover/press 状态层，按下 elev-1 |
| **按钮 · outlined** | 透明底 + 1px `--m3-outline` 描边（inset box-shadow 实现），次要动作 |
| **按钮 · text** | 无底色，`--m3-primary` 文字，工具栏内联动作 |
| **图标钮** | 34×34（密集区 28×28），svg 20px（密集区 16px），**无描边**，纯图标必带 `aria-label`/`title`；composer 的 +/send 为 40×40 专属档 |
| **输入壳 composer** | surface-highest + `shape-full`（idle stadium）⇄ `shape-xl`（grown 卡片），`--shadow-float` 悬浮、**无描边**；输入 14px（2.2 豁免）；send 钮 `has-text` 才浮现（scale 0.9→1 + fade）；plus 钮 pulse 周期 1.8s（循环豁免档） |
| **文本框（表单）** | M3 filled：surface-highest、上圆角 4px、底线 1px on-surface-variant → focus 2px primary；高 ≥44px、**字号 16px**；占位文字 on-surface-variant |
| **单选 radio** | 20×20 圆，2px `--m3-outline` 描边；选中 = primary 内点；竖排；设置场景预选当前值 |
| **复选 checkbox** | 18×18，2px 圆角，2px outline 描边；勾选 = primary 底 + on-primary ✓ |
| **下拉 select** | 最后手段（见 11 章）；用原生 `<select>` + 自绘箭头，禁自造浮层仿制 |
| **菜单**（plus/slash） | surface-container + `shape-xs` + elev-2；行高 48px 满宽出血；hover on-surface 8%；分组用分隔线不用标题文字；slash 菜单浮层内缩（左右 4px），描述 ≤10 字；入场 200ms emph-decel |
| **对话框** | surface-high + `shape-xl` + elev-3 + padding 24px；宽度 `min(92vw, 380px)`（表单/确认）或 `min(92vw, 560px)`（内容型）；标题 20/500；按钮右对齐、确认钮最右、确认用 filled（不可逆时）否则 text、取消用 text；长内容面板内滚动（header/footer 固定）；scrim 0.5 + blur(2px)（景深提示，不是面板毛玻璃——禁的是 frosted 表面材质）；scrim 点击关闭，**不可逆确认对话框除外**；入场 250ms emph-decel / 出场 200ms accel |
| **Toast** | surface-highest + `shape-md` + elev-3；舞台底部居中（避开 rail），`--z-toast`；单条显示，新替旧不堆叠；4–6s 自动消退 + 可手动关闭；入场 200ms emph-decel、出场 150ms accel；reduced-motion 纯 fade。**只给导出级事件**（见 9 章） |
| **Tooltip** | surface-highest + `shape-sm` + elev-2；11px/500；首个延迟 500ms、同序列后续零延迟零动画；hover 与 `:focus-visible` 均触发；`--z-tooltip` |
| **召唤式面板 tray** | surface-container + `shape-lg` + elev-3，锚定召唤源方位 |
| **用户气泡** | primary-container 对 + `shape-lg` + 行高 1.6 |
| **assistant 气泡** | surface-high 平面卡（无阴影）+ `shape-lg`；streaming 光标 `▊` blink 周期 1s（循环豁免档） |
| **起手建议 chips**（rail 空态） | ghost user-bubble 形态：surface-high + `shape-lg`、13px、右对齐列、贴 composer 上方；点击即填 composer 并聚焦；有对话后隐藏 |
| **markdown 链接** | `--m3-primary` + 下划线；hover 状态层；应用内不区分 visited |
| **代码块** | surface-highest + `shape-sm` + `--font-mono`；**语法高亮色板 v1 不定义**（见附录 B-23），迁移场景先按无高亮处理 |
| **卡片**（tool/asset/library） | surface-high + `shape-md`，**静止无阴影**、hover 才 elev-1；failed 态 inset error 描边、FINAL 态 inset ok 描边（状态描边是合法例外） |
| **素材卡** | 缩略图 + 人话标题（13/500）+ "类型 · 时长" meta（11px, tabular-nums）；机器 ID/原始文件名退 `title` 悬停；缩略图缺失给类型图标占位（禁黑块）；标签限 2 枚 + `+N`；⚑ 计数仅 >0 显示；动作钮 hover/focus-within 浮现于缩略图右上 |
| **Switch** | M3 52×32：surface-highest 轨 + 2px `--m3-outline` 描边，on = primary 轨 + on-primary 拇指；拇指 16→24 长大。仅用于即时生效的设置项，提交式表单禁用（见 11 章） |
| **Chips**（filter/状态） | `shape-sm`，11px/500；filter chip：outlined（`--m3-outline`）⇄ 选中 tonal；状态 chip 配 tint 底 + **功能色本体亮字** |
| **进度条** | 4px 高 `shape-full`，surface-highest 轨 + primary 填充；填充推进 120ms linear；indeterminate 滑块周期 1.4s（循环豁免档） |
| **会话系统文本** | 无容器、无描边、无图标；14px/1.6，`--system-text` 深蓝。只靠来源色与 Lumeri 正文区分，不再按错误/预算/计划套不同 banner |
| **状态点** | 8×8 圆点，色 + `title`/`aria-label` 词双通道 |
| **时间线抽屉** | 容器 max-height 0⇄384px 快照切换（不动画）；内容 `.ptl` translateY(14px)→0 + fade，250ms emph-decel；**384px = toolbar+内容+动作条实算高度**，改抽屉内容必须同步此值 |
| **滚动条** | 自绘 11px，thumb surface-highest，密集区（时间线）专属 |

**组件级描边的合法清单**（不违反 5.1 无描边铁律）：outlined 按钮/chip 变体（`--m3-outline`）、switch/radio/checkbox 控件描边（`--m3-outline`）、卡片状态描边（failed/FINAL）、md 表格线（`outline-variant`）、focus ring、Google 官方登录钮。此外新增描边需过治理。

---

## 9. Agent 界面语言

Lumeri 是 agent 产品，对话流是第一界面。以下规则来自 06 手册（行为契约）中与前端呈现相关的部分，属本规范管辖：

- **工具卡**：头行 = 状态形 + 工具名 + 参数摘要；带一行"为什么"（可展开看全参）；流内解释 ≤1 行（中文 ≤30 字），长解释进帮助层。
- **状态词表**（色 + 形，查 1.5；形按介质：web=sprite 图标，CLI=glyph）：pending=outline+静止、running=primary+动效、done=ok+✓、failed=err+✗、gated/timeout=warn。
- **运行动效纪律**：每个运行中任务至多一个动效载体（spinner 与 indeterminate 进度条不得同时出现在同一对象上）；CLI 全屏单 spinner（见 16 章）。
- **长任务卡**（>10s 渲染/导出）：running 工具卡内嵌 4px 进度条 + 右侧百分比（`tabular-nums`）+ meta 行粗估时（"约 3 分钟"，拒绝假精确）；完成转 done 态 + 导出级 toast。
- **失败卡三件套**：出了什么事 + 下一步是什么 + 现成按钮，缺一不可；修复提示放最后（视线落点）；禁错误码裸奔、禁玩笑。
- **成功默认静默**；toast 只给导出级事件；模态只配不可逆破坏；可撤销操作零确认弹窗。
- **撤销**：逐 op 撤销 + 整轮回滚，感知 <100ms（乐观 UI，失败回滚）。
- **plan 审批**：批准前枚举具体 op 清单（"裁剪×2、转场×1"），不只散文；按钮文案精简（"批准"非"批准并执行"）。
- **ask/elicit 卡**：2–4 互斥选项 + 1 推荐默认 + 可跳过；每题注明后果；只在轮次边界弹；dismiss 成本 1 击或 Esc，关掉 = 按默认继续；不重复索要已给信息。
- **置信度**：三档 chip（确定/尚可/存疑），禁裸百分比；低置信给 2–3 候选。
- **NEW 徽章**：新能力首现处挂，用过一次或 7 天后自灭；禁静默改默认行为。
- **文案禁词**：技术黑话（LLM/神经网络/magic…）、全能宣称（总是/保证/100%…）。
- **文字精简纪律**（用户拍板 2026-07-13）：状态词进 `title`/`aria`，界面留形；分组标题降级为分隔线；按钮短词；说明性长段删除。a11y 通道（aria-label/title）全程保留。
- **机器话禁令**：内部 ID（asset_…、hash 文件名、session id）、错误码、枚举原文禁止直接示人；退 `title` 悬停或复制动作，卡面只留人话。
- **会话来源即层级**（用户拍板 2026-07-22）：Lumeri 生成的自然语言固定用 `--m3-on-surface` 白字；宿主/系统写入的状态文字（包括错误、预算、计划等待）固定用 `--system-text` 深蓝字、透明背景。来源已经一目了然时，禁止再加红框、彩底、警告图标或重复标题。红色只留给真正需要用户处理的失败对象与破坏性动作，不给普通会话状态。

---

## 10. 状态设计（空态 / 加载 / 骨架 / 乐观更新）

### 10.1 加载时序阈值

| 预计时长 | 规则 |
|---|---|
| <100ms | 无指示；但交互反馈（按下态）必须 100ms 内可感知（纯 CSS） |
| 100ms–1s | **仍无 spinner**——短于 1s 的循环动画只造成闪烁 |
| 1–2s | 过渡带，可轻量指示 |
| 2–10s | 结构可预测（列表/表格/媒体网格）→ skeleton；单一模块（视频播放器）→ spinner |
| >10s | 百分比进度 + 粗估时（形态见 9 章长任务卡）；进度动画先慢后快 |
| 渲染/导出期 | 聊天必须保持可用 |

**裁决规则**：能预估时长的按表分档；**不能预估的一律用 400ms 延迟挂载 spinner 兜底**（400ms 为 Doherty 推导实现值，权威级别低于 100ms/1s/10s 三档，接受兜底场景下 400ms–1s 的短暂指示为代价）。

### 10.2 Skeleton

- 与最终布局精确吻合，占位块定死尺寸或 aspect-ratio，零 CLS。
- 静态内容（页头、label、图标）直接真渲染；禁 frame-only skeleton；禁空视图正中 spinner；禁静态 "Loading..." 文本。
- shimmer 配 reduced-motion 降级为静态灰块。

### 10.3 空态（三分类，写法不同）

- 解剖：短而正向的动作导向标题（"开始创作"而非"你还没有内容"）→ 一行 why/next → 唯一 primary CTA（可指向界面元素，顺带教位置）。
- **首用**：说清这里将出现什么、如何添加；对话空态 = 一行能力说明 + 3–5 条**可点击起手示例**（点了即填入输入框），禁功能长列表导览。
- **搜索无果**：解释原因 + 建议调整关键词/过滤器。
- **错误类**：具体原因 + 出路（权限→申请路径；格式→列出支持格式），平实语言，禁玩笑。
- 空表格/空网格整体替换组件，不留空表头。

### 10.4 乐观更新

- 默认按成功渲染、立即跳转；失败回滚到前一视图 + 失败卡三件套。

---

## 11. 表单

Lumeri 表单面积小（登录、设置），规则按体量分两档。**定界**：本章"表单"指提交式表单；即时生效的设置面板不属表单——Switch 合法（规格见 8 章）、控件应预选当前值。

**微表单**（1–2 字段，如登录框）：允许 placeholder + `aria-label` 极简式（用户拍板 2026-07-13 的文字精简方向）；其余规则照常。

**常规表单**（≥3 字段或含决策）：

1. 一律单列；label 顶置静态；hint 在 label 下输入框上。
2. 只在 submit 校验（唯一例外：字符计数）；`<form novalidate>`；**服务端校验永远存在**（渐进增强兜底）。
3. 错误呈现：定位字段 + 人话说明 + 修正建议（不能只变红）；行内错误放输入框**上方**；**绝不清空已填内容**；字段 ≥3 且多错时用 error summary 模式（置顶、焦点移入、逐条链接可跳、措辞与行内一致）——Lumeri 表单体量小，1–2 错时行内即可。
4. **不禁用提交按钮**；监听 form submit 而非按钮 click（保住回车提交）。
5. 控件选型：单选 → 竖排 radios（提交式不预选；设置回显预选）；select 是最后手段；禁 `<select multiple>`、`datalist`；**提交式表单禁 toggle switch**。
6. 输入框高 ≥44px、字号 16px；宽度暗示答案长度。
7. 必填不打星，标"选填"，更好是删字段（答不出"拿来做什么"就删）。
8. 同会话不重复索要已填信息（WCAG 3.3.7）；登录不得仅依赖认知测试（3.3.8）。

---

## 12. 数据密集界面（时间线 / 媒体库）

- **密度是显式参数**：行高档 24 / 32 / 40 / 48px（默认 40；刻意不设 Carbon 的 64 两行档），换档必须联动工具栏、checkbox、表头整棵组件树；**密度收垂直，水平 padding 恒 16px**。
- 营销级 token（大留白、大圆角）禁止直接套进时间线/媒体库——密度是独立分层参数。
- 时码、时长、文件大小：右对齐 + `tabular-nums` + `--font-mono`；文本左对齐；**禁止任何列居中**；列头对齐跟随内容。
- 行分隔 ≤1px 浅灰或无分隔；交互列表禁 zebra；行 hover 常开（辅助横向扫描）。
- sticky 表头/首列；用户的列宽/列序/显隐定制**跨会话持久化**且必须给"恢复默认"。
- 批量选中浮出操作条时，行内单项操作必须禁用（两通道互斥）。
- 排序图标只在被排序列常驻。
- 时间线专业化基线（对齐 DaVinci/CapCut）：真实缩放滚动、playhead 细而醒目（`--tl-playhead` 专用红，见 1.5）、marker 用 warn 系、snap 有微反馈、trim 手柄可抓、clip 选中双圈 ring（1.5px primary + 3px primary-container）、拖拽全部配非拖拽替代。
- **视频微调交互内核**：预览对象、时间线 clip、对象旁微调条和可独立打开的“属性”模块必须共享同一选择与编辑状态；空间用画布左上角为原点的对象中心点绝对坐标，时间用帧对齐。完整键盘、吸附、提交、冲突和无障碍契约见 [`INTERACTION-KERNEL.md`](./INTERACTION-KERNEL.md)，该文档是本章的强制实现细则。
- **动态密度容错（v1.1 起草义务，Antigravity 指出的盲区）**：长 UUID/多标签/时码在 400px rail 内的溢出、换行、截断策略必须在组件规格里显式给出。
- 高密度是专业工具的合法审美；慢的"美"界面是低密度界面。

---

## 13. 图标

- **唯一来源**：gemia 仓 `static/v3/icons.svg` sprite（`i-*` 界面图标 + `b-*` 品牌件），`<use href="#i-xxx">` 引用。手写内联 path 是缺陷（已清零，2026-07-13）。**跨仓使用**（CLI 仓 preview.html 等）以 gemia 侧为 source of truth，复制件须有构建期 hash 校验的同步机制（见 B-14）。
- 网格 24×24（绘制区 3–21）、描边 1.8、`currentColor`（品牌件例外）；母题"一横一点"每图标至多一处。
- 尺寸档：12 / 16 / 20 / 24px，禁档外值。
- 纯图标控件必带 `aria-label`/`title`；状态图标 = 形 + 色双通道。
- 禁 emoji 入 UI（web 与 CLI 同禁：cell 宽漂移 + 风格失控）。

---

## 14. 无障碍底线（WCAG 2.2 AA + Lumeri 自选项）

| 项 | 数值 | 级别 |
|---|---|---|
| 正文对比度 | ≥4.5:1 **硬门槛**；7:1 为观察目标（脚本报告记录，不达标不算缺陷） | AA 底线（7:1 自选目标） |
| 大字对比度 | ≥3:1；大字 = ≥24px 或 ≥18.66px bold（14pt，勿再写 18/18.5px） | AA 底线 |
| UI 组件边界/图标/状态 | ≥3:1 对相邻色（outline-variant 分隔线为非交互装饰，豁免） | AA 底线 |
| 指针目标 | ≥24×24px（详见 7.4 分档） | AA 底线 |
| 焦点可见 | `:focus-visible` ring，指示对背景 ≥3:1 | AA 底线 |
| Reflow | 320px 无双向滚动（时间线豁免，工具栏不豁免） | AA 底线 |
| 文本间距 | 用户强制 line-height 1.5×、段后距 2×、letter-spacing 0.12×、word-spacing 0.16× 字号后不裂版 → 文字容器不定死高 | AA 底线 |
| 拖拽替代 | 一切拖拽配单指针替代 | AA 底线 |
| 闪烁 | ≤3 次/秒 | A 底线 |
| 自动动效 | >5s 可停 | A 底线 |
| reduced-motion | 必须实现（视 AAA 2.3.3 为必做） | Lumeri 自选 |
| 键盘 | 全功能可达、无 trap、焦点序=视觉序 | A 底线 |
| ARIA | 原生 HTML 优先；no ARIA better than bad ARIA | 哲学 |

验收手段：对比度脚本自查（正文级 ≥4.5 全过为门槛，7:1 达成率仅记录）+ 键盘走查 + 400% zoom 走查 + reduced-motion 开关走查。

---

## 15. 反模式速查（见即修；括号内为依据章节）

仅靠 font-size 拉层级(0.6)｜纯黑/纯白文字(1.5)｜em 组件字号(2.2)｜彩底灰字(1.5)｜冰蓝强调正文——链接/命令等交互文字除外(1.5)｜浮层描边/白边(5.1)｜到处画 1px 线(5.1)｜一句状态再叠彩底/描边/图标/标题(0.9, 9)｜会话系统状态套红色警报框(9)｜`outline:none` 无替代(7.2)｜div 装按钮(14)｜正数 tabindex(14)｜placeholder 当 label——微表单例外(11)｜blur 时校验(11)｜禁用提交按钮(11)｜<1s spinner(10.1)｜空视图正中 spinner(10.2)｜frame-only skeleton(10.2)｜`scale(0)` 弹出(6.2)｜enter 用 ease-in(6.2)｜动 width/height/margin(6.2)｜纯颜色传状态(1.5)｜modal 焦点不困不还(7.2)｜自动动效 >5s 无停止(6.2)｜裸 hex/档外字面量(0.5, 17.1)｜同义两色/一色两义(1.5)｜静止组件用 elev-4/5(5.2)｜密集卡大圆角(4)｜数字列比例字体/居中(12)｜错误码裸奔(9)｜机器 ID 示人(9)｜错误文案开玩笑(10.3)｜emoji 进 UI(13)。

---

## 16. TUI 介质转译（CLI 章）

CLI 与 web 功能对等（用户既定方针），但介质不同，**转译精神而非照搬数值**。本章为权威口径，`lumeri-cli/docs/tui-design-spec.md` 为其实现细则。

### 16.1 介质映射表

| web 概念 | 终端等价物 |
|---|---|
| 表面梯 + 阴影 | 缩进脊柱（marker 第 0 列 / 内容第 2 列 / 结果第 5 列）+ 留白；**框只给交互焦点面**（InputBox、AskPrompt、modal，全屏 ≤3 框，`round` 样式） |
| 字号/字重阶梯 | SGR attributes：bold=标题/强调、dim=次级、inverse=选中。层级靠 attribute 不靠色相 |
| `--m3-primary` | 唯一 hex accent `#5FC6DE`（truecolor）→ 256 色 `80` → 16 色 `cyan`；亮底文字形态 `#1E7A94`（`COLORFGBG` 探测） |
| 功能色 | ANSI 色名 green/red/yellow（把色度交给用户终端主题）；色永不独行，必伴 glyph 或词 |
| state layer / hover | 无指针悬停；选中 = inverse 反白 |
| 图标 | glyph 白名单（16.2），语义 = glyph + 词 + 色三通道 |
| 动效 | 单 spinner（状态行独占，80ms tick、400ms 延迟挂载——循环豁免档）+ 进度条；scrollback 禁重绘、字节稳定；非 TTY 零动画 |
| disabled 38% | dim（纪律：dim 不与色叠加、不独载语义） |
| 行长 | 散文 ≤88 cell；列宽按显示 cell 计（CJK=2）；chrome 用 `…` 截断永不折行 |

### 16.2 CLI 专属纪律

- **glyph 白名单**：`⏺ ⎿ › • → ✔ ✗ ─ ❯ █ ░`——均经实测宽度锁定（`✽`/`●`/`⏸` 已因宽度歧义剔除）。词表外**禁 emoji 与未经实测的 EAW-Ambiguous 字符**；对齐关键列的宽度按实测 cell 宽计算，不凭 Unicode 属性推断。
- **转录区 accent 白名单**（枚举制，不称"唯一"）：① running 活性标记（⏺）；② banner 前导 glyph（ask/plan）；③ 文字形态 accentText——h1、链接、slash 命令名（交互性文字，与 web"链接用 primary"同义对齐）；④ inline code（tui-design-spec 已规划未落地）。**正文散文禁 accent**。白名单外新增须过治理。
- 单屏净调色板 = default + dim + bold + 1 accent + ≤3 ANSI 状态名；"90% 灰 + 一点 accent"。
- splash 渐变三色（accent→`#8BD8EA`→`#ABE5F1`）只在 splash，禁入转录区。
- 降级阶梯逐档可用：truecolor → 256 → 16 → `NO_COLOR`（attributes 仍在）→ `TERM=dumb`/非 TTY（纯 ASCII 顺序日志）。

### 16.3 组件对照表（web → TUI）

| web 组件 | TUI 形态 |
|---|---|
| 用户气泡 | `›`（dim）前缀 + 默认色正文 |
| assistant 气泡 | 无前缀散文，宽 ≤88 cell；markdown：h1=bold+accentText、行内 code=bold、链接=accentText+下划线+dim URL、代码块=缩进 2 格无框、引用=dim 左线 |
| 工具卡 | `⏺`（状态色）+ 工具名 bold + dim 参数括号；`⎿` 子行接结果/错误；子 agent `├─`/`│` 树形，每子 ≤4 行 + `… +N more` |
| 状态词表 | pending=dim ⏺、running=accent ⏺、done=green ✔、failed=red ✗、gated/timeout=yellow |
| 失败卡 | 原因在前、dim 细节、修复提示放最后（终端视线落在输出末尾）；带 errorCode/recovery/hint 子行 |
| 进度条 | accent `█` 填充 + dim `░` 轨道 + 右对齐 `NN%`；窄屏 10 cell，常规 20 cell |
| Banner | tone glyph（turn_error=`✗`red、budget=`⏺`warn、ask/plan=`•`accent）+ bold 标题 + dim 正文；色只上前导 glyph |
| 菜单/自动补全 | 无框，选中行 inverse + `❯`（accent），窗口 ≤6 行 + `↑/↓ N more` |
| ask/elicit 卡 | round 框 accent（交互焦点面），问题 bold、选项 dim；radio 选项化（`❯ (•)`）为 tui-design-spec 已规划未落地项 |
| Toast / 状态点 | 状态行右侧词条（`⏺` 状态色 + 词）；live 态不渲染——安静是默认态 |

### 16.4 已知债

- `web/preview.html`（CLI 侧 + gemia 侧两份，内容还不同步）整页仍是旧琥珀 `#E6B450` 体系 + One-Dark 状态色 + 1px 边框分隔——Lumeri 全线**体量最大**的未迁移遗留，列附录 B-4（P1 同级中最优先）。
- tui-design-spec 已写未落地四项：AskPrompt radio 选项化、结果 4 行折叠（`ctrl+o` 展开）、inline code accent 化、**ASCII fallback map**。

---

## 17. 治理

### 17.1 Token 纪律

- 三层结构：**ref**（MCU 生成的 tonal palette，只存在于生成脚本）→ **sys**（`--m3-*` 与功能/形状/动效 token，`:root` 单一来源）→ **component**（只查表）。业务规则只消费 sys 层。
- 禁止：组件规则里裸 hex（Google 官方登录钮配色是唯一豁免；`--tl-playhead` 已收编为 token）、档外字面量间距/圆角/字号、JS 里字面量色板（时间线 clip 色必须回 CSS token，见 B-1）。
- 新增 token 需先证明现有档位组合不出来；新增即写进本规范附录 A。

### 17.2 新组件上线自检（8 问）

1. 所有颜色都是角色 token？on-X 配对正确？
2. 圆角/间距/字号/时长全部在档（或嵌套公式派生）？
3. 状态层 8/10/10/16 + disabled 38/12？
4. 纯键盘走得通？focus ring 可见？Esc 行为正确？
5. 目标 ≥24px？拖拽有替代？
6. 对比度抽查过（正文 4.5、组件边界 3.0）？
7. reduced-motion 降级正确？
8. 图标走 sprite？纯图标带 aria-label？状态色形双通道？

### 17.3 文档关系与变更

- 本规范是唯一权威；`05-界面UI.md` 的 token 层按本规范回写更新（见 B-9）；`tui-design-spec.md` 从属第 16 章；`06-AI交互准则.md` 保留 agent 行为契约与出片侧管辖，其前端呈现条文以第 9 章为准，冲突时从本规范。
- 规范条文变更（档位、硬规则）需用户过目；组件规格微调在"应"级内可自决并回写。
- web 与 CLI 功能对等：凡引入新界面能力，两端同步设计（介质转译按第 16 章）。

---

## 附录 A · Token 速查（canonical；正文与此冲突时以此为准。标 ★ = 尚未建，其余为 v3.css 实存）

```css
:root {
  /* 色彩 — MCU 0.3.0, seed #5FC6DE, dark */
  --m3-primary:#54d6f3; --m3-on-primary:#003640;
  --m3-primary-container:#004e5c; --m3-on-primary-container:#aaedff;
  --m3-secondary:#b2cbd2; --m3-secondary-container:#334a50; --m3-on-secondary-container:#cee7ee;
  --m3-tertiary:#bec5eb;
  --m3-error:#ffb4ab; --m3-error-container:#93000a; --m3-on-error-container:#ffdad6;
  --m3-surface-lowest:#0b0f10; --m3-surface:#111415; --m3-surface-low:#191c1d;
  --m3-surface-container:#1d2021; --m3-surface-high:#272a2b; --m3-surface-highest:#323536;
  --m3-on-surface:#e1e3e4; --m3-on-surface-variant:#bfc8cb;
  --m3-outline:#899295; --m3-outline-variant:#3f484b;
  --m3-scrim:rgba(0,0,0,.5);

  /* 功能色（Blend.harmonize 产物；源值归档见 B-24） */
  --ok:#06d6bd; --warn:#dfcd5d; --err:var(--m3-error);
  --ok-tint:color-mix(in srgb,var(--ok) 15%,transparent);
  --warn-tint:color-mix(in srgb,var(--warn) 15%,transparent);
  --err-tint:color-mix(in srgb,var(--m3-error) 14%,transparent);
  --tl-playhead:#ff4b5c;                                   /* ★ 收编现存字面量 */

  /* 排版 */
  --font-mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;

  /* 形状 */
  --shape-xs:4px; --shape-sm:8px; --shape-md:12px;
  --shape-lg:16px; --shape-xl:28px; --shape-full:999px;

  /* 高度（elev-2/3 = 近黑调校档，2026-07-13 定） */
  --elev-1:0 1px 2px rgba(0,0,0,.3),0 1px 3px 1px rgba(0,0,0,.15);
  --elev-2:0 2px 6px rgba(0,0,0,.42),0 8px 24px rgba(0,0,0,.38);
  --elev-3:0 3px 10px rgba(0,0,0,.46),0 14px 40px rgba(0,0,0,.44);
  --elev-4:0 2px 3px rgba(0,0,0,.3),0 6px 10px 4px rgba(0,0,0,.15);   /* 瞬时态 */
  --elev-5:0 4px 4px rgba(0,0,0,.3),0 8px 12px 6px rgba(0,0,0,.15);   /* 瞬时态 */
  --shadow-float:0 2px 8px rgba(0,0,0,.40),0 18px 44px rgba(0,0,0,.52);
  --shadow-float-focus:0 3px 10px rgba(0,0,0,.44),0 22px 52px rgba(0,0,0,.58);
  --hairline:0 1px 0 0 var(--m3-outline-variant);

  /* 动效 */
  --dur-xs:100ms; --dur-sm:150ms; --dur-md:200ms; --dur-lg:250ms; --dur-xl:300ms;  /* ★ */
  --md-standard:cubic-bezier(.2,0,0,1);
  --md-emph-decel:cubic-bezier(.05,.7,.1,1);
  --md-accel:cubic-bezier(.3,0,1,1);
  --md-emph-accel:cubic-bezier(.3,0,.8,.15);               /* ★ 替换 0 引用的 --md-decel */

  /* 层 */
  --z-header:20; --z-composer:30; --z-stage-menu:31; --z-menu:45;
  --z-tray:900; --z-toast:950; --z-dialog:1000; --z-tooltip:1100;    /* toast/tooltip ★ */
}
```

CLI：accent `#5FC6DE`（256→`80`，16→`cyan`）、accentText 亮底 `#1E7A94`、状态 = ANSI green/red/yellow。
品牌：logo 三色 `#5FC6DE/#8BD8EA/#ABE5F1`、白底字标 `#239FC0`、白底 App 图标压深档（光点 `#8BD8EA`/上带 `#5FC6DE`/下带 `#239FC0`）。

## 附录 B · 现状偏离与落地清单（规范 ≠ 立即重构令；动手时按此清算）

> **落地状态（2026-07-13 观感手术已执行）**：U1–U8 全部落地；B-2/B-3/B-5/B-7/B-11 完成；B-6 字号野值已收敛（间距野值残余待收）；抽屉动效已重构；elev-2/3 已换近黑调校配方；--z-*/--font-mono token 已建。未动：B-1/B-10/B-12（时间线色簇与双实现）、B-4（preview.html）、B-8（时长 token 化）、B-14 及 P2 清洁项。证据见 shared daily 2026-07-13。

**P0 — 语义级冲突：**
1. 双时间线两套轨道色语义（`.pt-*` 的 ok/primary/tertiary vs v3.js `TL_CLIP_COLOR` 14 个字面量 hex）→ 统一为一套 token 化 clip 色，JS 色板回迁 CSS。
2. ✅ `--font-mono` 定义并替换全部内联 mono 栈（28 处）。
3. ✅ `--cedge` 死写入已删除。

**P1 — 品牌/一致性：**
4. `preview.html` 两份（CLI 侧 + gemia 侧）整页旧琥珀体系 → 按本规范重绘并两侧同步。**体量最大，P1 中最优先。**
5. ✅ 内联 path 图标（plus-menu 5 项 + composer +/send）已迁 sprite；icons.svg 新增 `i-list-check`/`i-shield`。
6. 档位收敛：✅ 字号野值（12.5/11.5/10.5/13.5/18/22）已灭；⬜ 间距野值（3/5/7/9/11/13/14/26/30）待收；⬜ auth 输入 14→16px 待改；⬜ 11px 处补 ≥500 字重待查。
7. ✅ z-index 跨组件层级已 token 化（--z-*）。
8. ⬜ 动效时长字面量（0.1s–0.3s 散布）→ `--dur-*` token 化。
9. ⬜ `05-界面UI.md` frontmatter 回写为本规范 token。
10. ⬜ ptl 区字面量色簇收敛：playhead → `--tl-playhead`、marker → warn 系派生、clip 高光/lane 分隔 rgba 簇 → token 化或删。
11. ✅ outlined 描边角色升级：filter chips（model/setup/search）已升 `--m3-outline`；rail 钮改无描边图标钮形态。
12. ⬜ 双时间线实现去留：`.ptl` 为主，`.pt` 定性 fallback 共享 token；择期合并。
13. ✅ Switch on 态三连重复块合并（三选择器并列，2026-07-13 review2 后）。
14. ⬜ icons.svg 跨仓同步机制（gemia 为 source of truth + 构建期 hash 校验）。

**P1+ · 观感手术（2026-07-13 实拍审计 + Antigravity 复核；已全部落地 ✅）：**
- U1 ✅ 去边框：rail 钮/退出登录/setup 全家（供应商卡、输入、动作钮、添加模型钮、下拉）/ask 输入组 → filled/text 形态；描边只留第 8 章合法清单。
- U2 ✅ 素材库去机器话：hash → "未命名视频"、ID 退 title、"类型 · 时长" meta、黑块 → 类型图标占位。
- U3 ✅ 空态：舞台加副行 + rail 四条可点起手 chips（点击即填 composer）。
- U4 ✅ 层次：elev-2/3 近黑调校 + hairline 提档 + rail 分线加强（表面梯值未动）。
- U5 ✅ 素材卡收敛：动作 hover 浮现、标签限 2+N、⚑ 仅 >0 显示（Antigravity 修正：8 标签撑爆容器已防）。
- U6 ✅ slash 菜单：浮层内缩 + 描述 ≤10 字。
- U7 ✅ 账户框：登出降 text button。
- U8 ✅ 称谓：title/h1/登录标题改 Lumeri Video。
- 优先序修正（已采纳）：z-token 化与抽屉动效重构与 U1 同批完成。
- 规范盲区回应：12 章已立"动态密度容错"v1.1 义务。
- **Antigravity 复评二轮（7.8/6.5/5.5，各 +1.3/+1.5/+1.5）后的收尾修复**：U1 死角 `.model-add-search-wrap`/`.model-add-custom` 残留 border → inset box-shadow；U2 回归 图片显示 "0.0s" → 图片不显时长、视频/音频改 `formatMediaDuration`（"15 秒"/"2:05"）；U2 `libraryDisplayName` 正则收紧（纯 hex+含数字才判机器 id，避免可读文件名误判）；图标描边 plus/send 2→1.8。头号残项仍是时间线双系统+JS 写死色簇（B-1/10/12）。

**P2 — 清洁：**
15. 死别名清理：`--bg/--surface-1..4/--text*/--brand*(除 deep,glow)/--shadow-1..3/--r-*/--ease-*/--pending/--running/--m3-outline-mid/--md-decel` 等 0 引用 token。
16. markdown 区 `var(--x, fallback)` 防御式兜底统一去 fallback。
17. 注释修正：auth 区"32% scrim"与实际 0.5 不符；L47"v3.js reads some of these (--ok)"与实情不符。
18. 未用角色（on-secondary/tertiary-container 等）在 :root 注释标记"预留"。
19. `.status-pill` 名实不符（实为 dot）→ 改名。
20. 组件级 padding 冗余写法清理。
21. `.library-small-btn` padding 级联补丁 → 随图标钮档位化一并理顺。
22. tui-design-spec 四项未落地（radio 选项化、4 行折叠、inline code accent、ASCII fallback map）。
23. 代码高亮色板：从 MCU tonal palette / 功能色派生一套并写入附录 A（在此之前代码块按无高亮处理）。
24. MCU 生成脚本 + `Blend.harmonize` 源值归档进 gemia docs（兑现"换主题只换 token 层"）。
