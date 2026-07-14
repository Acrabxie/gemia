---
id: lumeri-execution
description: |
  用于 Lumeri 顶层执行总控：preflight、读取项目/媒体库/timeline、确认本轮改动边界、选择阶段、执行、复核和报告。何时不用我：具体裁剪/调色/转场/字幕/Blender 操作应交给对应底层 skill；只需要生成单个 primitive plan 时不要替代底层 primitives skills。
triggers:
  primary: [Lumeri 总控, 执行工作流, preflight, Reference Cut, Timeline Build, Manual Lock, Render Review, public demo]
  secondary: [复核报告, 输出目录, 媒体库检查, 时间轴检查, 模型输入可观察性, 本轮改什么, 不改什么, workflow kit]
primitives:
  - gemia.video.analysis.get_metadata
  - gemia.video.summary.video_summarize
  - gemia.video.timeline.timeline_from_script
  - gemia.video.timeline.concat
  - gemia.video.blender_link.blender_link_status
  - gemia.video.blender_link.blender_link_capabilities
  - gemia.video.review.review_real_media_artifact
est_tokens: 760
---

# lumeri-execution

## 何时使用

当用户要求 Lumeri 以“总控流程”推进项目时使用：先检查状态，再说明本轮改什么、不改什么，然后选择阶段并执行。这个 skill 负责执行纪律，不负责替代底层能力。

## 阶段模式

- `Reference Cut`：整理参考节奏、素材选择和粗剪方向，不追求最终渲染。
- `Timeline Build`：构建或调整时间轴，安排片段、图片 3 秒语义、基础转场和字幕占位。
- `Manual Lock`：结构已锁定，只修改点名对象；禁止重排未点名片段。
- `Render Review`：导出、复核、报告输出路径和剩余依赖/合规边界。

## Preflight 必做

执行前必须检查：

- 项目状态：是否有当前项目、当前阶段、选中 clip、pending ask。
- 媒体库：是否有可用视频/图片/音频资产，素材状态是否 ready。
- timeline：是否有片段、时间参考、锁定范围、图片 3 秒语义。
- planner/model provider：是否可用，是否需要 OpenRouter 或本地 fallback。
- 输出目录：是否可写，是否会覆盖既有输出。
- Blender/LumeriLink：是否需要、是否可用、不可用时如何降级。
- 模型输入可观察性：是否会生成本地可审计输入 TXT，密钥是否仍 redacted。

## 执行前说明

每次执行前必须明确：

- 本轮改什么。
- 本轮不改什么。
- 当前阶段是什么。
- 会调用哪些底层能力。
- 哪些素材、时间范围或输出属于目标。

## 参数说明

核心参数是 `stage`、`target_assets`、`target_clips`、`time_references`、`output_intent`、`review_required`、`blender_required`、`manual_lock_scope`。

## 典型 plan 片段

```json
{"id":"step_1","function":"gemia.video.analysis.get_metadata","args":{},"input":"$input","output":"$output"}
```

```json
{"id":"step_2","function":"gemia.video.timeline.timeline_from_script","args":{"stage":"Timeline Build","respect_manual_lock":true},"input":"$input","output":"$output"}
```

## 执行后报告

每次执行后必须报告：

- 当前阶段
- 改了什么
- 没动什么
- 调用了哪些能力
- 如何验证
- 输出文件在哪里
- 是否进入媒体库
- 是否还有依赖/合规边界未满足

## 边界 / fallback

如果用户只要求具体操作，例如“加转场”“调暖一点”“加字幕”，路由到 transition、color-grade 或 html-graphics。若 preflight 发现媒体库为空、模型 provider 不可用、输出目录不可写或 Blender 不可用且本轮强依赖 Blender，应先 ask 或报告阻塞，不要假装执行成功。
