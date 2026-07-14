# Lumeri Video · 创作指导手册体系

把 Google 的高赞规范 **[DESIGN.md](https://github.com/google-labs-code/design.md)**(25k⭐)+
**[awesome-design-md](https://github.com/VoltAgent/awesome-design-md)**(96k⭐)+ 专业调色/剪辑手艺,
经多视角(codex 规范架构 · v4pro 手艺 · Antigravity 评审替身)蒸馏,做成给 **Lumeri Video 底层
Gemini** 读的一套持久创作基准。基准对标 DaVinci Resolve / CapCut 专业版。

## 结构(一主 · 一辅 · 六针对)

| 文件 | 角色 | 内容 |
|------|------|------|
| [`DESIGN.md`](DESIGN.md) | **主指导手册** | 视觉/创作身份母文件。YAML front-matter = 唯一规范 token(静态层+时间层);正文 = 理据。Gemini 每次会话加载 |
| [`辅助指导手册.md`](辅助指导手册.md) | **辅助手册** | token schema 说明 · 红旗自检表 · Do's/Don'ts 全表 · 理据库 · lint 校验 · 版本追溯 |
| [`针对性/01-调色.md`](针对性/01-调色.md) | 针对册 | 一级/二级校色 · 肤色线 · LUT · 示波器 · 冷暖分区 |
| [`针对性/02-剪辑节奏.md`](针对性/02-剪辑节奏.md) | 针对册 | ASL/密度 · 切点技法 · 180°轴线 · 卡点 · 转场纪律 |
| [`针对性/03-字幕排版.md`](针对性/03-字幕排版.md) | 针对册 | 字体≤2 · 贴底不居中 · 描边对比 · 缓动 · 竖屏重排 |
| [`针对性/04-构图运镜.md`](针对性/04-构图运镜.md) | 针对册 | 三分线 · 景别多样 · 景深分层 · 运镜有指向 |
| [`针对性/05-界面UI.md`](针对性/05-界面UI.md) | 针对册 | **产品界面**(非成片):表面梯/状态层/焦点键盘/动效 token/字阶/data-state 词表。蒸馏 MD3·HIG·WCAG 2.2·Radix |
| [`针对性/06-AI交互准则.md`](针对性/06-AI交互准则.md) | 针对册 | **Gemini 行为契约**:期望设定/elicit 纪律/存疑降级/失败三件套/plan 闸门/逐 op 撤销/出片无障碍。蒸馏 PAIR·HAX 18 条 |

> 05/06 各带自己的域内 token frontmatter(`ui.*` / `conduct.*`),与 `DESIGN.md` 的成片 token 不重叠——
> DESIGN.md 管**画面**,05 管**界面**,06 管**行为**,三处均为 normative。

## 核心设计:双层 token

DESIGN.md 原生只描述"静止一帧"。视频比静态 UI 多出时间轴,所以本套在 `DESIGN.md` front-matter
里做了**双层扩展**:

- **静态层**(沿用 DESIGN.md):`colors` `typography` `frame` `grading` `depth`
- **时间层**(Lumeri 新增):`motion` `transition` `pacing` `audiosync`
- **组合层**:`components` `quality-gate`

## 三条底线(源自 DESIGN.md 规范)

1. **token 是唯一规范值** —— 所有数值只在 `DESIGN.md` front-matter 定义一次。
2. **正文只讲理据,不重复数值** —— 各册用 `{group.token}` 引用,不复写。
3. **可 lint** —— `npx @google/design.md lint DESIGN.md` 验引用完整性与 WCAG 对比度。

## 给 Gemini 的加载建议

- **常驻**:`DESIGN.md` 正文 + front-matter token 注入 system prompt(会话级持久)。
- **按需**:四本针对册进 skill references / RAG,按当前工作流(调色/剪辑/字幕/构图)召回。
- **出片前**:强制跑 `辅助指导手册.md` 的**红旗自检表**与 front-matter 的 `quality-gate`,命中即打回。

> ⚠ 复审待办:本套"评审视角"由 codex 替 Antigravity 出演(真 AGY/OpenClaw 本机已永久离线)。
> 待非 OpenClaw 的 Antigravity 路由可用时,补一次真机成片复审(重点:情绪一致性、构图美感等机器判不了的维度)。
> 接入路径与安全加固按 Lumeri 惯例功能收尾统一做。
