# Gemia 底层架构草案（v0.2）

> 目标：把 Gemia 从“已跑通的最小命令行闭环”推进为“可持续扩展的创作引擎原型”。
>
> 本版为 **v0.2 修订版**，相对 v0.1 重点补强：
> - **AI（Gemini）在各层的明确介入点**
> - **Executor 的 async / polling / 外部 API 模式**
> - **Task Revision（修订链）机制**
> - **Skill Runtime 的 AI 动态沉淀新 Skill 能力**

---

## 1. 设计目标

Gemia 不是传统意义上的单点视频脚本工具，而应当是一个：

- 能把**自然语言创作意图**转成结构化计划的引擎
- 能统一管理**视频 / 图片 / 关键帧 / 风格化结果 / 预览输出**的资产系统
- 能执行**抽帧、风格化、拼接、回写、导出**等多阶段媒体任务
- 能把这些流程沉淀为**可复用 Skill / Pipeline 资产**
- 能被未来的 GUI、控制面板、多 agent 协作系统直接调用

因此，Gemia 的底层架构应优先围绕：

1. **统一计划表示（Plan JSON）**
2. **统一资产管理（Asset Graph / Asset Store）**
3. **统一执行器接口（Executors）**
4. **可复用技能层（Skill Runtime）**
5. **可挂 UI / API 的编排入口（Runtime API）**
6. **统一 AI 调用抽象（AI Adapter / AI Client）**

---

## 2. 总体分层

建议采用以下 6 层架构：

```text
┌──────────────────────────────────────────────┐
│ 6. Product / UI Adapter Layer                │
│    CLI / Dashboard / Future GUI              │
├──────────────────────────────────────────────┤
│ 5. Runtime API / Orchestrator Layer          │
│    task run / revise / poll / save skill     │
├──────────────────────────────────────────────┤
│ 4. Skill Runtime Layer                       │
│    reusable skill json / AI-generated skill  │
├──────────────────────────────────────────────┤
│ 3. Executor Layer                            │
│    local / remote / async / polling          │
├──────────────────────────────────────────────┤
│ 2. Asset Layer                               │
│    asset store / metadata / lineage          │
├──────────────────────────────────────────────┤
│ 1. Intent + Planner Layer                    │
│    NL -> Plan JSON / revise plan             │
└──────────────────────────────────────────────┘
                 ↑
                 │ shared by multiple layers
                 │
        ┌──────────────────────────────┐
        │ AI Adapter Layer             │
        │ gemini_adapter.py / ai_client│
        └──────────────────────────────┘
```

说明：
- 保持原本 6 层主结构不变。
- **AI Adapter Layer 不一定单独成为业务层编号**，更适合作为横切基础设施层，被 Layer 1 / 4 / 5 调用。
- 这样可以避免 Gemini 调用逻辑散落在 Planner、Skill Runtime、Orchestrator 内部。

---

## 3. 各层职责说明

### 3.1 Intent + Planner Layer

职责：
- 接收自然语言请求、模板请求、或 Skill 调用参数
- 生成统一格式的 `Plan JSON`
- 做最小校验与参数补全
- 在用户反馈后生成修订版 Plan

输入示例：
- “把这个视频抽关键帧，做赛博朋克风格化，再输出预览”
- “给我一个 before/after 对比视频”
- “运行 stylize-preview skill”
- “风格太花了，保留主体结构，颜色更克制一点”

输出示例：

```json
{
  "plan_id": "plan_20260329_001",
  "goal": "extract keyframes, stylize, compose preview",
  "inputs": {
    "video": "inputs/demo.mp4"
  },
  "steps": [
    {
      "id": "extract_01",
      "type": "extract_keyframes",
      "params": {
        "fps": 0.2,
        "max_frames": 5
      }
    },
    {
      "id": "stylize_01",
      "type": "stylize_images",
      "params": {
        "style": "cyberpunk cinematic"
      },
      "depends_on": ["extract_01"]
    },
    {
      "id": "compose_01",
      "type": "compose_preview_video",
      "params": {
        "mode": "before_after"
      },
      "depends_on": ["stylize_01"]
    }
  ]
}
```

关键要求：
- Plan JSON 必须**稳定、可存档、可回放、可调试**
- Planner 不直接做重执行，只负责描述任务
- 后期可替换不同模型或规则引擎，但不改变 Plan schema
- Planner 需要支持 **revision-aware planning**（基于旧计划与反馈生成新计划）

#### v0.2 新增：AI（Gemini）在 Planner 的介入点

Gemini 在这一层至少承担 3 类职责：

1. **意图理解**
   - 将自然语言创作意图解析为结构化任务目标
   - 识别输入资产、输出期望、风格要求、约束条件

2. **Plan JSON 生成**
   - 按固定 schema 生成步骤列表、参数和依赖关系
   - 尽量输出结构化 JSON，而非自由文本说明

3. **Plan Revision**
   - 基于旧 Plan + 任务结果摘要 + 用户反馈生成修订版 Plan

#### v0.2 新增：Plan JSON 是否需要多轮上下文

需要，但**上下文应当被显式结构化传递**，而不是依赖隐式长对话记忆。

建议 Planner 接收以下上下文对象：

```json
{
  "request": "风格太花了，保留主体结构，颜色更克制一点",
  "task_context": {
    "task_id": "task_001",
    "current_revision": 2,
    "latest_plan": {"...": "..."},
    "latest_outputs": ["styled/frame-01.jpg"],
    "latest_summary": "The stylized output is visually strong but oversaturated.",
    "constraints": {
      "preserve_subject": true,
      "avoid_over_stylization": true
    }
  }
}
```

结论：
- **不是让 Gemini 持有无边界会话历史**
- 而是由 Orchestrator 提供一个受控的 `planning_context` 给 Planner
- 这样更适合调试、回放、版本化和未来多模型替换

---

### 3.2 Asset Layer

职责：
- 统一管理输入视频、抽帧结果、风格化结果、预览视频、缩略图、日志等
- 跟踪资产之间的来源关系（lineage）
- 提供可读路径与元数据索引
- 支持 revision 间资产继承与差异比较

建议目录结构：

```text
gemia-mvp/
  inputs/
  outputs/
  keyframes/
  styled/
  previews/
  plans/
  skills/
  cache/
  logs/
  tasks/
```

建议的资产元数据字段：

```json
{
  "asset_id": "asset_keyframe_001",
  "type": "image",
  "path": "keyframes/frame-001.jpg",
  "source": "inputs/demo.mp4",
  "created_by": "extract_keyframes",
  "created_at": "2026-03-29T05:00:00+08:00",
  "task_id": "task_001",
  "revision_id": "rev_001",
  "tags": ["keyframe", "preview-source"],
  "meta": {
    "timestamp_sec": 2.5,
    "width": 1280,
    "height": 720
  }
}
```

核心原则：
- 文件路径是物理层
- 资产元数据是逻辑层
- 后续 UI 看到的应该主要是逻辑资产，而不是散乱文件
- Revision 间允许复用已有资产，但必须明确记录来源 revision

---

### 3.3 Executor Layer

职责：
- 执行 Plan 中定义的每一种 step
- 每种 step 都实现统一接口
- 输出新的资产与执行日志
- 支持本地执行与远程 API 执行
- 支持 async run 与 polling 生命周期

#### v0.2 修订：统一改为 async + 状态轮询模型

执行器不再假设所有步骤都能同步完成。

因为：
- `ffmpeg`、本地图像处理这类是典型 **LocalExecutor**
- `Veo`、`Nano Banana`、远程 Gemini 图像/视频接口这类是典型 **RemoteExecutor**
- 远程任务通常需要：提交 → 轮询 → 下载结果 → 注册资产

建议任务状态：
- `submitted`
- `running`
- `done`
- `failed`
- `cancelled`

#### v0.2 修订后的接口定义

```python
from typing import Any, Optional

class BaseExecutor:
    step_type = "base"

    async def validate(self, step: dict, context: dict) -> None:
        ...

    async def submit(self, step: dict, context: dict) -> dict:
        """提交任务，返回 execution record"""
        raise NotImplementedError

    async def poll(self, execution: dict, context: dict) -> dict:
        """查询状态，返回最新 execution record"""
        raise NotImplementedError

    async def finalize(self, execution: dict, context: dict) -> dict:
        """在 done 后收尾：下载结果、注册资产、返回 step result"""
        raise NotImplementedError

    async def run(self, step: dict, context: dict) -> dict:
        """默认统一流程：submit -> poll until terminal -> finalize"""
        execution = await self.submit(step, context)
        while execution["status"] in {"submitted", "running"}:
            execution = await self.poll(execution, context)
        if execution["status"] != "done":
            raise RuntimeError(f"Step failed: {execution}")
        return await self.finalize(execution, context)
```

#### LocalExecutor 基类

```python
class LocalExecutor(BaseExecutor):
    async def poll(self, execution: dict, context: dict) -> dict:
        return execution
```

特征：
- 常常在 `submit()` 内就完成执行
- `poll()` 可以为空实现或直接返回终态
- 适用于 ffmpeg、文件转换、本地拼接等

#### RemoteExecutor 基类

```python
class RemoteExecutor(BaseExecutor):
    poll_interval_sec = 5
    timeout_sec = 600

    async def submit(self, step: dict, context: dict) -> dict:
        ...

    async def poll(self, execution: dict, context: dict) -> dict:
        ...

    async def finalize(self, execution: dict, context: dict) -> dict:
        ...
```

特征：
- `submit()` 只负责向远程服务提交任务
- `poll()` 负责查询远程状态
- `finalize()` 负责取回结果并注册资产

#### RemoteExecutor 示例：Nano Banana / Veo 风格的远程图像生成

```python
class RemoteStylizeExecutor(RemoteExecutor):
    step_type = "stylize_images_remote"

    async def validate(self, step: dict, context: dict) -> None:
        assert "style" in step.get("params", {})

    async def submit(self, step: dict, context: dict) -> dict:
        client = context["ai_client"]
        source_assets = context["resolved_inputs"]
        job = await client.submit_image_stylize_job(
            images=source_assets,
            prompt=step["params"]["style"]
        )
        return {
            "status": "submitted",
            "remote_job_id": job["id"],
            "step_id": step["id"]
        }

    async def poll(self, execution: dict, context: dict) -> dict:
        client = context["ai_client"]
        status = await client.get_job_status(execution["remote_job_id"])
        return {
            **execution,
            "status": status["status"],
            "result_urls": status.get("result_urls", [])
        }

    async def finalize(self, execution: dict, context: dict) -> dict:
        asset_store = context["asset_store"]
        downloaded = []
        for url in execution.get("result_urls", []):
            asset = await asset_store.import_remote_file(
                url=url,
                asset_type="image",
                tags=["styled", "remote-result"]
            )
            downloaded.append(asset)
        return {
            "status": "done",
            "assets": downloaded,
            "logs": [f"Imported {len(downloaded)} stylized images"]
        }
```

#### v0.2 新增：AI（Gemini）在 Executor 的介入点

AI 不仅在 Planner，也可能在 Executor 层被调用：
- 远程图像风格化
- 远程视频生成 / 镜头延展
- 结果评估（例如判断风格是否达标）

但原则上：
- **生成计划** 由 Planner 负责
- **执行媒体操作** 由 Executor 负责
- **AI 结果评估** 可以由单独的 evaluator executor 或 post-process evaluator 负责

---

### 3.4 Skill Runtime Layer

职责：
- 把高频创作流程封装成可复用 Skill
- Skill 不是“硬编码脚本”，而是“参数化 pipeline 模板”
- 让 Gemia 的能力能被资产化、复用、分享
- 支持从已执行任务中 AI 反向沉淀新 Skill

最小 Skill JSON 示例：

```json
{
  "skill_id": "stylize_preview_v1",
  "name": "Stylize Preview",
  "version": "0.1.0",
  "description": "Extract keyframes, stylize them, and compose a preview video.",
  "inputs": {
    "video": { "type": "video", "required": true },
    "style": { "type": "string", "required": true }
  },
  "pipeline": [
    {
      "type": "extract_keyframes",
      "params": {
        "fps": 0.2,
        "max_frames": 5
      }
    },
    {
      "type": "stylize_images",
      "params": {
        "style": "$style"
      }
    },
    {
      "type": "compose_preview_video",
      "params": {
        "mode": "before_after"
      }
    }
  ],
  "meta": {
    "created_from_task": "task_001",
    "created_from_revision": "rev_003"
  }
}
```

Skill Runtime 要做的事：
- 加载 Skill JSON
- 做变量替换
- 展开为正式 Plan
- 交给 Runtime API / Orchestrator 执行
- 支持从任务执行记录反向生成 Skill JSON

#### v0.2 新增：AI 动态写入新 Skill

新增建议接口：

```text
save_as_skill(task_id, skill_name, description)
```

接口语义：
- 输入某个已成功任务的执行记录
- 从其最终 revision 对应的 Plan 中抽取可泛化的 pipeline
- 生成一个新的 Skill JSON
- 存储到 `skills/` 目录并写入版本信息

#### AI 如何从已执行的 Plan 反向生成 Skill JSON

建议流程：

1. Orchestrator 获取：
   - 最终 revision 的 Plan
   - 实际成功执行的 steps
   - 输入输出资产摘要
   - 用户给出的目标描述

2. Skill Runtime 调用 Gemini：
   - 判断哪些参数应保留为变量（如 `style`, `video`）
   - 判断哪些参数应固化为默认值（如 `fps=0.2`, `mode=before_after`）
   - 生成 Skill JSON 草案

3. 本地规则校验：
   - 是否缺少 `skill_id/name/version/description/inputs/pipeline`
   - pipeline 中是否引用了不可泛化的临时路径
   - 是否包含敏感上下文（必须去除）

4. 最终保存为 Skill

#### save_as_skill 的接口设计

```python
async def save_as_skill(task_id: str, skill_name: str, description: str) -> dict:
    """
    Return:
    {
      "skill_id": "stylize_preview",
      "version": "0.1.0",
      "path": "skills/stylize_preview@0.1.0.json"
    }
    """
```

#### Skill 版本管理与覆盖策略

建议：
- Skill 使用语义化版本，如 `0.1.0`, `0.2.0`, `1.0.0`
- 默认**不直接覆盖**旧版本，而是新增版本文件
- 引入 alias 文件，例如：
  - `skills/stylize_preview@0.1.0.json`
  - `skills/stylize_preview@0.2.0.json`
  - `skills/stylize_preview.latest.json`

覆盖策略建议：
- 默认：创建新版本 + 更新 `.latest`
- 显式 `overwrite=true` 时：只允许覆盖 `.latest` 引用，不直接抹掉历史版本
- 若 AI 生成内容与现有 Skill 差异极大，应提示创建新 Skill 而非静默覆盖

#### v0.2 新增：AI（Gemini）在 Skill Runtime 的介入点

Gemini 可介入：
- 从自然语言示例生成最初版 Skill
- 从成功任务反向总结 Skill
- 对 Skill 做参数泛化与描述生成
- 对两个相近 Skill 做聚合建议（未来能力）

---

### 3.5 Runtime API / Orchestrator Layer

职责：
- 统一接收任务
- 调度 Planner、Asset Store、Executors、Skill Runtime
- 管理任务状态、revision、日志
- 作为未来 CLI / 控制面板 / Agent 调用入口

建议暴露的最小接口：

```text
run_plan(plan.json)
run_skill(skill.json, inputs)
get_task(task_id)
get_assets(task_id)
poll_task(task_id)
revise_task(task_id, feedback)
save_as_skill(task_id, skill_name, description)
list_skills()
```

建议任务状态：
- `pending`
- `running`
- `succeeded`
- `failed`
- `partial`
- `revising`

#### v0.2 新增：Task Revision 机制

Task 不再只是单次执行记录，而是一个**有版本链的创作容器**。

建议结构：

```json
{
  "task_id": "task_20260329_001",
  "status": "running",
  "title": "stylize preview for demo video",
  "current_revision_id": "rev_002",
  "root_input_assets": ["asset_video_001"],
  "created_at": "2026-03-29T05:00:00+08:00",
  "revisions": [
    {
      "revision_id": "rev_001",
      "parent_revision_id": null,
      "plan_id": "plan_001",
      "status": "succeeded",
      "feedback": null,
      "outputs": ["asset_styled_001"],
      "summary": "Initial cyberpunk stylization completed."
    },
    {
      "revision_id": "rev_002",
      "parent_revision_id": "rev_001",
      "plan_id": "plan_002",
      "status": "running",
      "feedback": "风格太花了，保留主体结构，颜色更克制",
      "outputs": [],
      "summary": null
    }
  ]
}
```

设计意义：
- 用户不是“重新开新任务”，而是在同一创作线程里持续 refine
- UI 可以展示“第 1 版 / 第 2 版 / 第 3 版”
- Skill 沉淀时可以选择最终 revision 或某个最优 revision

#### revise_task(task_id, feedback: str) 接口设计

```python
async def revise_task(task_id: str, feedback: str) -> dict:
    """
    1. Load current task and latest revision
    2. Build planning_context from latest plan + outputs + feedback
    3. Ask Planner (Gemini-assisted) to generate revised plan
    4. Append new revision to task history
    5. Execute the new revision
    """
```

#### Feedback 如何传回 Planner

建议由 Orchestrator 构造一个受控对象，而不是把全部历史文本直接塞给 Gemini：

```json
{
  "task_id": "task_001",
  "base_revision_id": "rev_001",
  "feedback": "风格太花了，保留主体结构，颜色更克制",
  "previous_plan": {"...": "..."},
  "previous_outputs_summary": {
    "assets": ["styled/frame-01.jpg"],
    "evaluation": "Over-saturated, composition preserved"
  },
  "revision_goal": "Produce a toned-down stylized variant"
}
```

然后 Planner 输出：
- 新的 `plan_id`
- 新的 `steps`
- 修改说明（optional）

#### v0.2 新增：AI（Gemini）在 Orchestrator 的介入点

Gemini 不应主导 Orchestrator 的状态机，但可在这些场景被调用：
- 生成 revision plan
- 对任务结果做摘要
- 从任务历史抽取 skill 候选
- 对失败原因做可读解释（非强依赖）

---

### 3.6 Product / UI Adapter Layer

职责：
- 给 CLI、Dashboard、未来图形界面提供调用适配
- 不承担核心业务逻辑
- 只负责输入输出整形、状态展示、用户交互

短期形态：
- CLI：本地验证和开发
- 控制面板：展示任务状态、资产、日志、技能、revision 历史

未来形态：
- 节点式工作流 UI
- 资产时间线 UI
- 风格模板库
- 多 agent 协作面板
- revision diff / before-after 对比视图

原则：
- UI 不直接写死执行逻辑
- 所有真实能力都从 Runtime API 往下调用

---

## 4. AI Adapter（v0.2 新增）

这是本次最关键的补充之一。

### 4.1 为什么要独立 `ai_client.py` / `gemini_adapter.py`

建议必须独立。

原因：
- 避免 Gemini 调用逻辑散落在 Planner、Skill Runtime、Executor 内
- 统一管理模型名、API key、重试、限流、日志、成本、结构化输出校验
- 后续如果从 Gemini 扩展到 OpenRouter / 多模型路由，更容易替换

推荐文件：

```text
gemia/
  ai/
    ai_client.py
    gemini_adapter.py
    prompts.py
    schemas.py
```

### 4.2 建议职责划分

#### `ai_client.py`
负责：
- 提供统一高层接口
- 面向 Gemia 内部业务代码调用

例如：

```python
class AIClient:
    async def plan_from_prompt(self, request: str, context: dict) -> dict: ...
    async def revise_plan(self, feedback: str, context: dict) -> dict: ...
    async def summarize_task(self, task: dict) -> str: ...
    async def generate_skill_from_task(self, task: dict, revision: dict) -> dict: ...
    async def evaluate_output(self, assets: list, criteria: dict) -> dict: ...
```

#### `gemini_adapter.py`
负责：
- 真正与 Gemini API 通信
- 底层请求封装、异常处理、轮询、JSON 解析

例如：

```python
class GeminiAdapter:
    async def generate_json(self, system_prompt: str, user_payload: dict, schema: dict) -> dict: ...
    async def generate_text(self, system_prompt: str, user_payload: dict) -> str: ...
    async def submit_media_job(self, payload: dict) -> dict: ...
    async def poll_job(self, job_id: str) -> dict: ...
```

### 4.3 AI 调用在各层的落点总结

| 层 | 是否调用 AI | 用途 |
|---|---|---|
| Intent + Planner | 是 | 自然语言转 Plan、修订 Plan |
| Asset Layer | 否（原则上） | 不直接调用；只存资产与元数据 |
| Executor Layer | 可能 | 远程生成/风格化/结果评估 |
| Skill Runtime | 是 | 从 Plan / Task 反向生成 Skill |
| Orchestrator | 间接 | 通过 AIClient 请求摘要、revision、skill 提炼 |
| UI Adapter | 否 | 不直接调用，走 Runtime API |

---

## 5. 推荐代码结构（v0.2 修订）

建议从当前 MVP 逐步重构到如下结构：

```text
gemia-mvp/
  README.md
  architecture.md
  gemia_mvp.py                # 过渡期入口

  gemia/
    __init__.py
    planner.py
    orchestrator.py
    asset_store.py
    task_store.py
    schemas.py

    ai/
      ai_client.py
      gemini_adapter.py
      prompts.py
      schemas.py

    executors/
      __init__.py
      base.py
      local.py
      remote.py
      extract_keyframes.py
      stylize_images.py
      compose_preview_video.py
      ffmpeg_edit.py
      export_artifacts.py
      evaluators.py

    skills/
      runtime.py
      loader.py
      versioning.py

    utils/
      ffmpeg.py
      files.py
      time.py
      logger.py
      ids.py

  inputs/
  outputs/
  keyframes/
  styled/
  previews/
  plans/
  skills/
  logs/
  cache/
  tasks/
```

---

## 6. 关键数据对象（v0.2 修订）

### 6.1 Plan
负责描述任务编排。

新增建议字段：
- `based_on_revision`
- `planning_context_summary`
- `planner_meta`（记录模型、时间、策略）

### 6.2 Step
负责描述单步执行动作。

新增建议字段：
- `executor_mode`: `local | remote`
- `retry_policy`
- `timeout_sec`

### 6.3 Asset
负责描述任何输入、中间结果、输出。

新增建议字段：
- `task_id`
- `revision_id`
- `lineage`

### 6.4 Task
负责追踪一个有 revision 历史的创作线程。

### 6.5 Revision（v0.2 新增）
负责追踪 Task 的某一次修订执行。

### 6.6 Skill
负责描述可复用工作流模板。

建议关系：

```text
Skill -> expands to -> Plan
Plan -> contains -> Steps
Step -> consumes/produces -> Assets
Task -> contains -> Revisions
Revision -> executes -> Plan
Revision -> records -> Logs + Outputs + Feedback
```

---

## 7. 第一阶段落地顺序（v0.2 修订）

不建议一口气“大而全”，建议按以下顺序推进：

### Phase 1：文档与 schema 固化
- [x] 写 `architecture.md`
- [ ] 固化 `Plan JSON` schema
- [ ] 固化 `Skill JSON` schema
- [ ] 固化 `Task / Revision` schema
- [ ] 固化 `Executor execution record` schema

### Phase 2：AI 与执行器抽象
- [ ] 抽 `ai_client.py`
- [ ] 抽 `gemini_adapter.py`
- [ ] 抽 `BaseExecutor / LocalExecutor / RemoteExecutor`
- [ ] 把现有抽帧、风格化、FFmpeg 执行逻辑拆成 executors

### Phase 3：最小运行时
- [ ] 实现 `run_plan()`
- [ ] 实现 `run_skill()`
- [ ] 实现 `poll_task()`
- [ ] 实现 `revise_task()`
- [ ] 写 task / revision log / status

### Phase 4：产品化最小展示
- [ ] 生成 before/after 预览视频
- [ ] 增加 `skills/stylize_preview_v1.json`
- [ ] 实现 `save_as_skill()` 最小版
- [ ] 在 README 中写清最小可演示路径

### Phase 5：为 UI 和控制面板预留接口
- [ ] 输出任务状态 JSON
- [ ] 输出 revision 历史 JSON
- [ ] 输出资产列表 JSON
- [ ] 输出技能列表 JSON

---

## 8. 当前最值得优先做的 4 件事（v0.2）

### 优先级 1：抽出 `ai_client.py` + `gemini_adapter.py`
因为如果 AI 调用不先统一，后面 Planner / Skill / Revision 会很快散掉。

### 优先级 2：实现 async `BaseExecutor` / `RemoteExecutor`
因为 Veo / Nano Banana / 远程 Gemini 媒体任务必须靠这个骨架承接。

### 优先级 3：补上 Task Revision 机制
因为创作系统的真实交互不是“一次生成结束”，而是持续修订。

### 优先级 4：实现 `save_as_skill()` 最小版
因为这决定 Gemia 是否真的会“越做越会”。

---

## 9. 一句话定义 Gemia 底层（v0.2）

**Gemia 的底层，不是一个视频脚本集合，而是一个由 AI 参与规划、由异步执行器驱动、支持修订链与技能沉淀的轻量媒体创作运行时。**

---

## 10. v0.2 变更说明

相对 v0.1，以下部分已修订或新增：

1. **第 2 节 总体分层**
   - 新增横切式 `AI Adapter Layer` 说明

2. **第 3.1 节 Intent + Planner Layer**
   - 明确 Gemini 在规划、修订中的介入点
   - 新增多轮上下文的显式传递方式

3. **第 3.2 节 Asset Layer**
   - 新增 revision 资产归属字段
   - 增加 `tasks/` 目录

4. **第 3.3 节 Executor Layer**
   - 从同步接口改为 async + submit/poll/finalize 模型
   - 区分 `LocalExecutor` 与 `RemoteExecutor`
   - 增加 RemoteExecutor 示例

5. **第 3.4 节 Skill Runtime Layer**
   - 新增 `save_as_skill()`
   - 新增 AI 从任务反向生成 Skill 的流程
   - 新增 Skill 版本管理策略

6. **第 3.5 节 Runtime API / Orchestrator Layer**
   - 新增 `poll_task()` 与 `revise_task()`
   - 新增 Task Revision 结构设计

7. **第 4 节 AI Adapter**
   - 整体新增，明确 `ai_client.py` / `gemini_adapter.py` 设计

8. **第 5~8 节**
   - 同步更新代码结构、数据对象、落地顺序与优先级

---

## 11. 下一步建议

基于 v0.2，下一步最合理的是立即补 4 个文件：

1. `gemia/ai/ai_client.py`
2. `gemia/ai/gemini_adapter.py`
3. `gemia/executors/base.py`
4. `gemia/orchestrator.py`

这样 Gemia 就会从“有架构文档”进一步升级成“有正式 runtime 骨架的原型系统”。
