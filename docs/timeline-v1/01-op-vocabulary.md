# Timeline v1 — M1 Op 词汇表规格

> 拍板依据（2026-06-13，Acrabxie）：细粒度 verb、overlay-as-clip、ripple 默认关、
> v1 = 视频轨 + overlay/text 轨（音频轨与关键帧仅预留字段）。
> 本文是 `lumerai/patches.py` 与 `gemia/project_model.py` 扩展的实现契约。
>
> **M6 修订（2026-06-19）：音频轨进入执行面。** 音频不再是预留字段：`add_track
> kind=audio`、音频 clip 的 insert/move/delete/trim、以及 `gain_db`/`fade_in`/
> `fade_out`/`muted` 音频属性（经 `set_clip_effects`）全部可用，渲染器（`project_export`）
> 第三遍把音频混音并 mux 进导出。下文凡标 “预留/reserved” 的音频条目均以本节修订为准
> （§2.5、§3.4、§3.9、§3.10、§4、§5 已就地更新）。**仍推迟**：轨级 ducking 关系
> （音乐轨自动避让人声轨）与 `render_preview` 预览音频——见 §4。
>
> **M7 修订（2026-06-19）：轨级 ducking + 导出时长。** 新增 `set_track` op /
> `timeline_set_track` verb 与轨级字段 `duck_under`（见 §3.14）：标了 `duck_under = T`
> 的 bed 音频轨，在 trigger 音频轨 `T` 响时自动被压低（渲染器 `sidechaincompress`，
> 复用 `mix_audio` duck 参数）。导出时长改为 = `timeline.duration`（含音频的 master）：
> 合成视频不足处补黑尾，音乐尾巴在黑场上播放而非越界/冻帧。M6 “ducking 推迟” 的表述作废；
> 仅 `render_preview` 预览音频仍推迟。
>
> **M8 修订（2026-06-19）：OTIO 互换文件格式。** `project_export_otio` /
> `project_import_otio` verb 新增 `format` 参数（默认 `otio`，与 M5 行为一致）：
> `otio`(JSON 无损) / `otioz`·`otiod`(无损 bundle，含媒体) / `edl`(cmx_3600) /
> `fcp7`(fcp_xml) / `fcpx`(fcpx_xml)。EDL/FCP 为**有损**且由可选插件提供——
> `pip install lumeri[interop]`（`otio-cmx3600-adapter` / `otio-fcp-adapter` /
> `otio-fcpx-xml-adapter`）；未安装时 verb 抛 `OtioFormatError` 指明缺哪个包，绝不伪造输出。
> 有损导出前会按格式**定义化降级**（见 §9 保真矩阵），不崩溃。这两个 verb 是动作 verb
> （读出/替换），不产生新 patch op。

## 0. 范围与不变量

- 本层是**纯函数层**：输入 project_state dict + ops，输出新 project_state；
  不接线、不渲染、不碰 v3 session。
- 时间单位：秒（float ≥ 0），写入前 `round(x, 6)`；比较用 `EPSILON = 1e-3`。
- **同轨 clip 不允许重叠**（叠层用多条 overlay 轨）。
- 校验失败必须抛 `TimelinePatchError`，**任何 op 失败时调用方（ProjectStore）不落盘**，
  整个 patch 原子生效或原子失败（现有 `apply_patches` 先 apply 后写盘已保证）。
- `apply_timeline_patches` 末尾保留 `_recompute_duration`，并新增整体不变量校验
  `validate_project`（见 §5）。

## 1. 错误模型

```python
class TimelinePatchError(ValueError):
    def __init__(self, code: str, message: str): ...
    # str(e) 必须含 code 与 message，模型/用户可读
```

| code | 含义 |
|---|---|
| `E_OP_UNKNOWN` | 未知 op 名 |
| `E_NOT_FOUND` | clip_id / track_id 不存在 |
| `E_OVERLAP` | 同轨时间重叠（且未授权 ripple 解决） |
| `E_BAD_ARG` | 参数缺失/类型错/取值非法 |
| `E_TRACK_KIND` | 媒体类型与轨道类型不匹配，或 v1 禁用的音频轨 |
| `E_RANGE` | source_in/out 超出资产时长或非法区间 |

## 2. 数据模型变更（`gemia/project_model.py`）

1. `MEDIA_KINDS` 增加 `"text"`。
2. track kind 集合扩为 `{"video", "overlay", "audio"}`；
   `_normalize_tracks` 识别 `overlay`（id 习惯前缀 `OV`，如 `OV1`）。
   **`_default_tracks()` 保持 `[V1, A1]` 不变**（向后兼容）；overlay 轨由
   `add_track` 显式创建（M3 的 verb 层负责"无 overlay 轨时自动建 OV1"，本层不做隐式创建）。
3. clip 规范化新增字段：
   - `keyframes: []`（**仅预留**，normalize 时保证存在且为 list，本层任何 op 不解释它）；
   - `text_config: dict | None`（仅 `media_kind == "text"` 时有意义）：
     `{content: str(必填,非空), font_size: float(默认 64), color: str(#rrggbb,默认 "#ffffff"), position: {x: float, y: float} | None(默认居中), align: "left"|"center"|"right"(默认 "center")}`。
4. `_normalize_timeline_clip` 调整：
   - `media_kind == "text"`：不要求 asset_id（可为 `""`），duration 缺省 `IMAGE_DURATION`，
     source_in/out 固定 0 / duration；
   - `media_kind == "image"`：**仅当 duration 缺失或 ≤0 时**才强制 `IMAGE_DURATION`，
     显式 duration 必须尊重（旧行为是无条件强制——放宽；若全量测试有依赖旧行为的用例，
     修测试并在报告里逐条列出）。
5. ~~音频轨为**预留**：任何 op 以 audio 轨为目标 → `E_TRACK_KIND`。~~
   **M6：音频轨可执行。** 媒体/轨匹配规则：audio clip ⇒ audio 轨；audio 轨拒绝
   video/image/text（`E_TRACK_KIND`）。音频 clip 走 `media_kind == "audio"` 的常规
   normalize（duration、source_in/out 语义同 video）。`gain_db`/`fade_in`/`fade_out`
   作为 effects 键（见 §3.9）。

## 3. Op 词汇表

通用：op 为 dict，`{"op": <name>, ...,"provenance"?: dict}`；`provenance` 若存在则写入受影响 clip。
新 op 用**扁平字段**，不沿用旧的 `data.clip` 包裹（旧格式仅为兼容保留，见 3.1/3.7）。

### 3.1 `insert_clip`（兼容 + 扩展）
- **旧形态**（必须原样兼容，现有 runtime_vnext/orchestrator/测试在用）：
  `{op:"insert_clip", data:{asset?, clip}}` → 行为不变：upsert asset 后 append clip。
- **扩展形态**：`{op:"insert_clip", data:{asset?, clip}, track_id?, at?, ripple?:bool=false}`
  - `track_id`：目标轨；缺省取 `clip.track_id`，再缺省 `V1`。轨不存在 → `E_NOT_FOUND`。
  - `at` 三选一：
    - 缺省 / `"append"`：start = 该轨现有 clip 的最大结束时间（空轨为 0）；
    - `{"time": t}`：start = t；与同轨现有 clip 重叠时，`ripple=false` → `E_OVERLAP`；
      `ripple=true` → 所有 `start >= t` 的同轨 clip 右移 `clip.duration`；
    - `{"index": i}`：按该轨 clip 时序排序后插到第 i 个之前（i ∈ [0, n]），
      start = 原第 i 个 clip 的 start（i==n 时等价 append），其后同轨 clip 右移
      `clip.duration`（index 插入**天然 ripple**，忽略 ripple 参数）。
  - 媒体/轨道匹配：`video` clip → video 轨；`image`/`text` clip → overlay 轨
    （v1 不支持画中画：video clip 上 overlay 轨 → `E_TRACK_KIND`；
    兼容例外：**旧形态** image clip 落 video 轨维持现状不报错，扩展形态严格校验）。
  - clip.id 缺省自动生成 `clip_<uuid8hex>`；duration 缺省取 asset.duration 或
    source_out-source_in；text clip 见 §2.3。

### 3.2 `delete_clip`
`{op:"delete_clip", clip_id, ripple?:bool=false}`
- ripple=false：仅移除；
- ripple=true：同轨 `start > 被删.start` 的 clip 左移 `被删.duration`（闭合空隙）。

### 3.3 `move_clip`
`{op:"move_clip", clip_id, start?, track_id?, ripple?:bool=false}`
- 至少给 start 或 track_id 之一，否则 `E_BAD_ARG`。
- 目标位置与目标轨现有 clip 重叠 → `E_OVERLAP`（v1 不做目的地推挤）。
- `ripple=true` 仅作用于**原位置**：移走后闭合原轨空隙。
- 跨轨：媒体/轨道匹配规则同 3.1。

### 3.4 `trim_clip`
`{op:"trim_clip", clip_id, source_in?, source_out?, ripple?:bool=false}`
- 仅 video/image/audio clip（text → `E_BAD_ARG`，text 用 `set_clip_time` 改时长）。
  **M6**：audio clip 的 source_in/source_out 语义与 video 完全一致。
- 不变量：`duration = source_out - source_in`（v1 不把 effects.speed 折进时长；
  speed 属预留语义，渲染层后续处理）。
- `0 <= source_in < source_out`；资产 duration 已知（>0）时
  `source_out <= asset.duration + EPSILON`，否则 `E_RANGE`。
- duration 变化 delta ≠ 0 时：
  - ripple=true：同轨后续 clip 平移 delta；
  - ripple=false：原位伸缩，伸长导致重叠 → `E_OVERLAP`。

### 3.5 `split_clip`
`{op:"split_clip", clip_id, at_time, new_clip_id?}`
- `at_time` 为时间线时刻，须严格落在 `(start, start+duration)` 内（含边界 → `E_BAD_ARG`）。
- 前半保留原 clip_id：duration = at_time - start，source_out 相应收缩；
- 后半新 clip：id = `new_clip_id`（缺省自动生成），同 asset_id、同 track，
  start = at_time，source_in = 原 source_in + (at_time - start)，source_out = 原值；
  effects 深拷贝；`transition_after` 归后半，前半置 None。
- **身份铁律**（2026-05-10 教训）：两半都保持 asset_id，身份 = clip_id + source 区间，
  禁止按 asset 去重合并。回归测试必须覆盖"同资产二段 split 后独立 trim 互不影响"。

### 3.6 `set_clip_time`
`{op:"set_clip_time", clip_id, start?, duration?, ripple?:bool=false}`
- `start`：所有 media_kind 可用，语义同 move_clip（不跨轨）；
- `duration`：仅 image/text clip 可用（video → `E_BAD_ARG`，video 改时长走 trim_clip）；
  image clip 同步把 source_out 设为 duration（source_in 固定 0）。
- ripple 语义同 trim_clip。

### 3.7 `replace_clip`（兼容保留）
旧形态原样保留（merge 语义）；merge 后过 §5 整体校验。

### 3.8 `add_transition`
`{op:"add_transition", clip_id, kind:"cut"|"dissolve"|"wipe"|"fade", duration_sec?:float=0.5}`
- `cut` → `transition_after = None`；其余写 `{kind, duration_sec}`，duration_sec > 0。
- 非 cut 要求：同轨按时序存在下一个 clip，且 `|本 clip 结束 - 下个 start| <= EPSILON`
  （不相邻 → `E_BAD_ARG`，提示先 move/ripple 对齐）。
- `duration_sec` 不得超过两侧任一 clip 的 duration（→ `E_BAD_ARG`）。

### 3.9 `set_clip_effects`
`{op:"set_clip_effects", clip_id, effects: dict}`
- **merge** 进 clip.effects（不整体替换）；显式传 `null` 值表示删除该 key。
- v1 允许 key 白名单：`rotation(0|90|180|270), mirrored(bool), muted(bool),
  speed(float>0, 预留——本层只存值), blur_radius(float>=0), opacity(0..1),
  x(float), y(float), scale(float>0)`；
  **M6 新增音频键**：`gain_db(float，dB，可负，默认 0)`、`fade_in(float secs>=0，默认 0)`、
  `fade_out(float secs>=0，默认 0)`；`muted(bool)` 复用既有键（音频 clip：true=不输出该
  clip 声音）。白名单外 key → `E_BAD_ARG`。
- `x/y/scale/opacity` 主要服务 overlay clip（像素坐标，x/y 为左上角，缺省居中）。

### 3.10 `add_track` / `remove_track`
- `{op:"add_track", kind:"video"|"overlay"|"audio", track_id?, name?}`
  - **M6**：`kind=audio` 现已支持（曾 `E_TRACK_KIND` 预留）。track_id 缺省自动
    `V<n>`/`OV<n>`/`A<n>` 取最小未占用序号；默认轨已含 `A1`，故首个显式
    `add_track kind=audio` 通常落 `A2`。重复 track_id → `E_BAD_ARG`。新 video/overlay
    轨排在同 kind 末尾、audio 之前；新 audio 轨排在最末。
- `{op:"remove_track", track_id}`：仅空轨可删（有 clip → `E_BAD_ARG`）；`V1` 不可删。

### 3.11 `set_timeline_format`
`{op:"set_timeline_format", fps?, width?, height?}`
- fps ∈ [1,120]；width/height ∈ [16,7680] 整数。仅改 timeline 字段，render_settings 不动
  （导出设置归 M4）。

### 3.12 `add_marker`
`{op:"add_marker", time, label?, marker_id?}` → append 进 `timeline.markers`：
`{id, time, label}`；time ≥ 0。

### 3.13 `upsert_asset`
`{op:"upsert_asset", asset: {...}}` — 把现有 `_upsert_asset` 提升为一等 op（行为不变）。

### 3.14 `set_track`（M7）
`{op:"set_track", track_id, duck_under?:<audio track_id>|null}`
- 设置轨级选项；当前仅 `duck_under`（轨级 ducking 关系）。
- 校验：`track_id` 存在且为 **audio** 轨（否则 `E_TRACK_KIND`）；`duck_under` 非空时必须
  指向一个**存在的 audio 轨**（缺失 → `E_NOT_FOUND`，非音频 → `E_TRACK_KIND`），且 ≠ 自身
  （`E_BAD_ARG`），并且不得形成环（A→B→A，`E_BAD_ARG`）。`null`/`""` 清除关系。
- 语义：标了 `duck_under = T` 的轨是 **bed**（如音乐），当 trigger 轨 `T`（如人声）响时被
  侧链压低。渲染层在导出时按轨分组子混音，对 bed 子混音用 `sidechaincompress`
  （`threshold=0.05:ratio=8:attack=20:release=400`，复用 `mix_audio` duck 参数）以 trigger
  子混音为侧链，再 `amix` 所有轨。**无 `duck_under` 配置时导出与 M6 扁平 `amix` 完全一致。**

## 4. 不做的事（v1 明确排除）

- ~~音频轨执行面（任何 audio 轨 op 一律 `E_TRACK_KIND`）。~~ **M6 已落地**（见顶部修订）。
- ~~音频 ducking 关系（推迟）。~~ **M7 已落地**：`set_track`/`timeline_set_track` +
  `duck_under`（见 §3.14）。
- **`render_preview` 预览音频（仍推迟）**：低清代理仍为纯视频（静音），避免破坏 proxy 契约；
  最终音频以 `project_export` 为准。
- keyframes 解释/插值（仅 normalize 保留字段）。
- 画中画（video clip 上 overlay 轨）。
- 目的地推挤式 move、自动吸附、磁性时间线。

> **M7-A 导出时长契约**：`project_export` 的成片时长 = `timeline.duration`（`_recompute_duration`
> 已含音频 clip，即 audio-inclusive master）。合成视频短于 master 时补黑尾到 master；音频更短则
> 自然以静音收尾。无音频 / 音频不超过视频的项目，master == 视频末端，导出与既往逐帧一致。

## 5. 整体校验 `validate_project(project) -> None`

apply 全部 ops 后执行；违反任一条抛 `TimelinePatchError`：
- 同轨无重叠（按 track 分组排序后两两比较，EPSILON 容差）；
- 每个 clip 的 track_id 存在于 timeline.tracks；
- video/image/audio clip：`source_out - source_in` 与 duration 偏差 ≤ EPSILON
  （image 的 legacy 强制时长豁免：source 区间 == [0, IMAGE_DURATION] 时不查）；
- text clip：text_config.content 非空。

## 6. 测试要求（`tests/test_timeline_patches.py`，新文件）

1. 每个 op：≥1 happy path + ≥1 典型错误（断言 error code）。
2. ripple 矩阵：insert@time / delete / trim / set_clip_time 各 × {true, false}。
3. split 身份回归：同资产 split 成 A/B 后分别 trim，互不影响、asset_id 相同、id 不同。
4. 向后兼容：旧形态 `insert_clip`/`replace_clip` op dict 原样通过
   （现有 `test_lumerai_runtime_kernel.py` 相关用例必须全绿，不许改它）。
5. 原子性：一个 patch 内 op2 失败 → 抛错，且传入的原 project_state dict 未被污染
   （调用方拿旧状态重试安全）。
6. ProjectStore 往返：apply 新 ops → `undo_to_seq` 回退 → 状态与回退点 snapshot 一致。
7. 全量 `pytest -q` 无新增失败（已知 stock_fill 2 failed 除外）。

## 9. OTIO 互换格式保真矩阵（M8）

格式 token → OTIO adapter / 扩展名，以及往返保真度：

| token | adapter | 扩展名 | 媒体 | 保真度 | 依赖 |
|---|---|---|---|---|---|
| `otio` | otio_json | `.otio` | 仅引用 | **无损**：全结构 + `lumeri` 元数据（effects、`duck_under`、text_config…） | 核心 |
| `otioz` | otioz | `.otioz` | **打包**（zip） | **无损** + 媒体 | 核心 |
| `otiod` | otiod | `.otiod` | **打包**（目录） | **无损** + 媒体 | 核心 |
| `edl` | cmx_3600 | `.edl` | 仅引用 | **有损**：单视频轨的剪切点/时码/clip 名；丢 overlay、音频增益/淡入淡出、ducking、effects、文字 | `lumeri[interop]` |
| `fcp7` | fcp_xml | `.xml` | 仅引用 | **有损**：视频/音频轨 + 基础结构；丢文字(generator)、富元数据 | `lumeri[interop]` |
| `fcpx` | fcpx_xml | `.fcpxml` | 仅引用 | **有损**：同上 | `lumeri[interop]` |

- **可选 extra**：EDL/FCP adapter 是 OTIO 拆分出的独立 pip 包，非核心。`pip install
  lumeri[interop]` 启用；未装时 `project_export_otio`/`project_import_otio` 抛
  `OtioFormatError` 指明缺哪个包，不静默降级、不伪造。
- **有损前置降级**（`_simplify_for_lossy`，不崩溃）：`edl` 仅保留首条视频轨的视频剪切；
  `fcp7`/`fcpx` 丢弃文字(generator)clip、保留媒体 clip。富结构请用 `otio`/`otioz`/`otiod`。
- **媒体打包**：`otioz`/`otiod` 用 `MissingIfNotFile` 策略——存在的媒体打进 bundle，
  缺失/非本地文件的引用被跳过而非报错。bundle 内 `target_url` 为 host 真实路径，仅存在于
  host 写出的文件里，对模型/SSE 只暴露 asset_id。

## 10. 用户直接编辑（Direct-Edit UI v1，DE 里程碑）

用户可在 web 时间线面板直接拖拽/裁剪/分割/删除 clip，与 AI 并列编辑**同一个**文档。
**架构铁律：用户操作编译成与模型 verb 完全相同的 patches.py op，经同一 ProjectStore 补丁日志
应用——单一真源，无并行编辑/状态通路。** host 不增加智能（无关键词检测、无计划改写）。

### 端点 `POST /sessions/{id}/timeline/op`
- 入参：`{op, clip_id, ...params}`，`op ∈ {move, trim, split, delete, set_time, set_effects}`，
  外加 `{op:"undo", steps?}`。
- 映射到 §3 的 op：`move`→`move_clip`(start/track_id)、`trim`→`trim_clip`(source_in/out)、
  `split`→`split_clip`(at_time)、`delete`→`delete_clip`、`set_time`→`set_clip_time`(start/duration)、
  `set_effects`→`set_clip_effects`(effects)、`undo`→`ProjectStore.undo`（同 `timeline_undo` verb）。
- 应用方式：`ctx.project.apply_ops([op])`——与 verb 同一调用；因此 `ProjectHandle.on_patch`
  自动发 `timeline_op` SSE 事件，所有客户端（含 AI 视图）即时刷新。每次用户操作 = 一个
  TimelinePatch，可经 `timeline_undo` 撤销。
- 校验：复用 `TimelinePatchError`；非法编辑返回 4xx + 类型化 `E_*` code（如 `E_OVERLAP`/`E_RANGE`），
  与非法 verb 调用一视同仁。`ripple` 默认 False。
- 来源标记：op 带 `provenance={"source":"user_direct_edit"}`，补丁日志据此区分用户/模型编辑。
- 纪律：端点只收 `clip_id`/`track_id`/时间/effects，**不暴露本地文件路径**。

### 前端（static/v3）
- body 拖拽 = move；右把手 = trim（视频/音频改 source_out，图片/文字改 duration）；
  点击选中；Delete 键/按钮 = delete；Split 按钮（当前在选中 clip 中点分割）；Undo 按钮。
- 乐观更新 → POST → 从响应/SSE 对账（reconcile）；吸附到邻接 clip 边缘 + 0.1s 网格。
- **共存**：AI 回合进行中（`turnInProgress`）禁用直接编辑（select-only），回合结束恢复；
  补丁日志为串行化裁决者。
- **推迟（follow-up）**：左把手 trim（需 move+trim 两 op 一补丁 + 重叠安全定序）；
  split-at-playhead（待面板加 playhead，当前用选中 clip 中点）。
