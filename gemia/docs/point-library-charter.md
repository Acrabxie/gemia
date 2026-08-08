# 《Lumeri 第二层创作库标准（点库宪章）》

> 版本：**v1.3（RATIFIED · 生效）** · 生效日期：2026-07-15 · 初稿 2026-07-14（4 层心智模型经用户当日批准；v1.1 依五镜对抗评审整改；v1.2 落地 §13.1 五项生效门并转正；v1.3 于 2026-07-17 新增 §14 技能层边界仲裁 + AP34 + `E_LUS_CRAFT_NUMBERS` 守卫落地，经用户批准，MINOR）
> **生效状态（重要）**：**§13.1 的生效条件（§10 三失败模式的红-build 级门全部落地并转绿）已满足——本宪章自 2026-07-15 起为 RATIFIED（生效）。** 五项生效门（FM1 Layer A/B、FM2 可寻址 round-trip、FM3 库侧 + host 侧、§13.1(5) 引用完整性 meta 门）均 **【已存在】**、本地 acceptance run 全绿、且**逐一对抗验证**（漏装 / 截断 id / 丢弃诚实部分 → 各触发 RED）。自此本宪章可作为"某库是否达标"的权威依据。
> **区分（重要）**：宪章 RATIFIED ≠ 每条 per-library 门已存在。文中仍标 **【待落地 · TO BE CREATED】** 的多为**每库自带**的验收门（taste-floor / determinism / no-raw-numbers / global-token-uniqueness 等）——它们由**每个新库在其自己的 PR 里创建并转绿**（§8 模板逐项落实），不是宪章生效的前置。宪章生效只要求三失败模式的**通用**门存在（已达）。
> **实现更新（2026-07-15）**：三失败模式门全部落地并对抗验证——FM1 `tests/test_tool_catalog_contract.py`（Layer A）+ `tests/test_library_verb_manifest.py`（Layer B，文件系统自动发现孤儿 `dispatch`）；FM2 `tests/test_addressability_roundtrip.py`（layer-tree 展示 id 逐字可解析，截断即 RED）；FM3 `tests/test_library_ledger_contract.py`（库侧：成功带 asset+`next`、错误 typed+recoverable）+ `tests/test_v3_ledger_partial_disclosure.py`（host 侧：recoverable 失败降级为诚实部分答案，非 opaque 硬停）；§13.1(5) `tests/test_charter_integrity.py`（引用完整性）。详见 §6.3 / §10 / §13.1。
> 适用范围：所有 Lumeri 家族的第二层创作库（点库 / point-library），跨两条正交轴——**形态**（Shape A / Shape B）与**类别**（SYNTHESIS 合成型 / TRANSFORM 变换型），四象限均在本宪章覆盖内。
> 参考实现（reference impl）：`vector-motion`（`lumenframe/vector/*.py` + `gemia/tools/vector_motion.py`，163 tests，已过对抗评审；**Shape A × SYNTHESIS** 参考）；`lumenframe/templates` + `lumenframe/elements`（**Shape B × 绘图 profile** 参考实现）。
> 本文件意在成为**可被 lint/CI 机器校验的契约**，不是风格建议。每条规则尽量写成"某个测试会 assert 什么"，并标注该测试**是否已存在**。凡与本宪章冲突的实现、分支或 PR，一律视为**未完成工作（incomplete work）**，不得合并。
> 约定：正文用中文；`code identifiers`、文件路径、token 名（`energy`、`stagger_spread`、`apply_element`、`E_NOT_FOUND` 等）一律保持原形，不翻译。所有路径均在 `/Volumes/Extreme SSD/lumeri` 之下（`~/Code/lumeri-design-manuals/` 除外）。
> 排除范围：依用户全局 `CLAUDE.md`，`examples/` 目录不参与本宪章任何架构推导。
> **合并门（merge gate）**：验收信号走**本地 acceptance run**＝`pytest` + `npm test`（GitHub CI 非主信号）。本宪章所称"红 build / CI RED"一律指**本地 acceptance run 失败**。执行合并的 agent **必须在合并前跑通 acceptance run**；未跑 = 未完成工作。禁止在未跑 acceptance run 的情况下声称任一门"绿"。

---

## §0 心智模型：四层，以及点库为什么是"全部胜负手"

### §0.1 四层心智模型（每个 builder 必须先内化）

| 层 | 名称 | 内容 | 品味属性 |
|---|---|---|---|
| Layer 0 | physics（物理层） | `ffmpeg` / `PIL`·`cv2` / `Chromium`·`HyperFrames` / `lumenframe` compile+render | 无品味，纯执行 |
| Layer 1 | primitives（积木层） | `lumenframe` ops、`add_shape`、`set_keyframe`、timeline patch | **图灵完备但品味中立**：模型每次调用都要自己堆积木、自己供给全部品味 → 不可靠、业余 |
| **Layer 2** | **CREATIVE LIBRARIES（点库）** | 每个库**关闭一个创作域**，把专业手艺（编舞 / easing 词汇 / 风格原型 / 构图规则 / 节奏 / 调色 preset）硬编码成结构 | **品味从"祈祷模型有"变为"结构上无从逃逸"**，业余输出不可达 |
| Layer 3 | intent（意图层） | 模型只表达意图 | 纯语义 |

### §0.2 点库的一句话定义（canonical）

> **点库（第二层创作库）** 是一个**软件单元**，它**恰好关闭一个创作域**（见 §0.6 的域定义与边界判据），把该域的专业手艺——choreography / easing vocabulary / style archetypes / composition rules / pacing / grading——硬编码成**不可逃逸的结构**；于是 agent 只用创作语言表达语义意图（**永不出现夹带手艺的原始数字**），并且**无法产出业余结果**——因为品味（stagger / easing / negative-space / focal-order / skin-tone-preservation …）不是每次调用去**选择**，而是被库**强制（ENFORCED）**的。它是把品味从"祈祷模型有品味"（Layer 1，品味中立的积木）搬到"结构上无从逃避"（Layer 2）的那台机器。这正是让输出"感觉高端"的机制。`vector-motion` 是第一个这样的库，也是参考模板。

### §0.3 为什么这是全部胜负手（the whole game）

Layer 1 图灵完备但品味中立，堆积木等于在**每一次调用**都把手艺问题重新抛回模型，回归到模型均值 → 业余。点库之所以高级，是因为手艺成了**软件的属性**，而不是 prompt 或模型的属性。本宪章其余每条原则，要么 (i) 让品味地板保持**结构性**（创作语言接口、命名目录、只关一个域、结构化编码手册、确定性、**每域自声明的地板断言集**），要么 (ii) 让库**诚实且真正上线**，使地板真的抵达 live 模型（逐字可寻址 + 完整可枚举、build==install 机器校验、可验证交付物 + 可恢复错误）。

### §0.4 合格点库的六点契约（source of truth：`vector-motion`）

任何点库必须同时满足，缺一不合格：

1. **说创作 / 语义语言，永不暴露夹带手艺的原始数字**（`energy 0..1`，不是 `x+=20`；放置类裸量的界定见 §0.7 与 P2）。
2. **具备结构性品味地板（STRUCTURAL TASTE FLOOR）**：手艺由该域**自声明的地板断言集**强制（motion 库＝choreography；grading 库＝色度守恒；type 库＝可读性驻留），不是每次调用的选项；地板要高到"坏输出很难产出"，并由**对抗评审加固**（而非声明）。
3. **风格原型（style archetypes）**：一个词重塑全局。
4. **确定性**：同一 brief → 同一输出（逐字节）。
5. **一个 agent 面，禁止扁平工具增殖**（`update_quantum` 模式）。
6. **骑在既有 render / primitive 层之上，不 fork 物理层，不新增 render capability**。

### §0.5 生命周期脊柱（本宪章的骨架）

builder 沿五阶段行走，**每阶段以一道硬 GATE 收尾，未过不得进入下一阶段**：

```
定义(GATE 0) → 设计与制作(GATE 1) → 安装(GATE 2) → 验收(GATE 3) → 演进(GATE 4)
```

各 GATE 的 PASS 判据集中在 §7；每条原则、接口、制作、安装规则都标注 `(← P#)` 可回溯至 §3 的宪法条款。

### §0.6 创作域（creative domain）的定义与边界判据（架构的原子单位）—— 新增

P6「恰好关闭一个创作域」若不先定义"域"就无法裁决。**定义**：

> **一个创作域 = 一组必须协同变动（co-vary）才能显得专业的手艺决策的最小闭包。** 两个能力属于**同一个域**，当且仅当"为其一选定某种品味"会**约束**另一个的品味取值（选择耦合）。若两能力的品味可各自独立选定而互不牵制，它们属于**不同域**。

**边界 / 泄漏判据（GATE 0 的可操作检查）**：一个域被**闭合**，当且仅当——该域内**没有任何**承载手艺的决策被暴露为一个**模型可见的可选参数、且其默认值会使地板失效**。存在这样一条路径 = **半开泄漏（leak）**，GATE 0 FAIL。

**可测形式**（域泄漏 proxy，PR 评审 + 机器 lint）：`test_no_floor_disabling_default`【待落地】——枚举库的模型面参数，断言**没有**承载该域手艺的参数其默认值属于"地板失效值"集合（例：`easing='linear'`、`stagger=0`、`saturation_cap=∞`、`dwell=0`）。评审同时人工确认无"半开"的域切分。

### §0.7 库类别（library class）：SYNTHESIS vs TRANSFORM —— 与形态正交的第二轴（新增）

形态（A/B）决定**如何上线**；类别决定**如何制作**。二者正交，四象限都合法。

| 类别 | 定义 | 原材料 | 主输入 | 制作标准 | 例 |
|---|---|---|---|---|---|
| **SYNTHESIS（合成型）** | 从语义 brief **生成**全新内容 | 语义轴 / archetype | `subject`（要生成什么） | §5A（IR→behaviours→styles→params→choreography→api）或 §5B（Shape B 宏组合） | `vector-motion`、`kinetic-type`、title cards |
| **TRANSFORM（变换型）** | **变换**一个既有 asset 的像素 / 采样 | LUT / tone-curve / lift-gamma-gain / filter | `target asset_id`（要变换谁） | §5T（非破坏 param/node 栈 + measurement readback + 像素验证） | `grading`、`stabilize`、`retime`、`color-match` |

**判类问句**：`"本库是从 brief 无中生有地合成内容（→SYNTHESIS），还是对一个已存在的 asset 做变换（→TRANSFORM）？"`
类别决定：主输入字段（`subject` vs `target asset_id`，见 P4/§4.1.3）、地板断言集（§0.6 各域自声明）、digest 形态（plan digest vs measurement/scope readback，见 §4.2）、以及 §5 走哪一支。**§5 的分层生成管线（IR/behaviours/choreography/phase-arc）是 SYNTHESIS 的制作标准，TRANSFORM 库不受其约束、走 §5T。**

### §0.8 生效前置声明

本宪章的强制力来自其被机器校验的检查。**任何在 §12 标 【待落地】 的检查，在其转绿之前不构成合并门**；对应原则以指导效力存在，评审可指出但不据以判"未完成"。见 §13 生效条件与治理。

---

## §1 库的定义：两种合法形态

点库有且只有两种形态；本宪章对两者都给出定义、接口契约与安装契约，并在 §2 给出选型规则。形态与 §0.7 的类别正交。

### Shape A —— tool-verb library（工具动词库，如 `vector_motion`）
一个**顶层 agent 工具**。它引入真正的新词汇（自己的 op verb），带专用 `_schema`（参数校验），是引擎必须知道如何执行的**不可约原语**（经 `_dispatch` 直接改文档），有独立的 budget cost、plan-mode 变更语义、router pack 归属，参与完整的 ~105 工具面。安装需触碰 **9 个点**（§6.2）。它**扩大引擎的表面**。

### Shape B —— ops-catalog library（算子目录库，如 `lumenframe/templates` + `lumenframe/elements`）
**不是**顶层工具。整库是 (1) 一批纯 `(**params) -> list[op_dict]` 宏函数的 `TEMPLATES`/`ELEMENTS` 注册表，(2) 一份平行的 `*_CATALOG` 元数据，(3) 恰好**一个**通用 `apply_<lib>` op 去展开+重派发，(4) 一段 `describe_<lib>()` 注入进 `describe_ops()`。一个 Shape-B **条目**没有 `_schema`（其 Python 签名 + `TypeError->E_ARG` 捕获就是全部校验故事）、没有新 op verb、没有 router pack、没有 client 改动。它**扩大一个库，而非引擎**。

> **`elements` 是 `templates` 的更严子变体**：同一套机制，额外强制"必须是纯 overlay（不画满帧背景）"这一不变量，由 corner-alpha render 测试把关。`templates` 是同一形态去掉该约束（可占满整帧）。**`templates` 与 `elements` 是共享机制的两个库**（各自一个 `apply_*` op、各自一段 bracketed 注入、各自一个 namespace），不是"一个库两个面"。两个 apply op（`apply_template`/`apply_element`）刻意互为镜像（`_op_apply_element` docstring：`Mirrors _op_apply_template exactly`）——证明该形态对每个新库都可按库复用。

### GATE 0 —— 定义门（必须全绿方可进入设计）
- [ ] 该库**关闭恰好一个创作域**（按 §0.6 定义）；跑通 §0.6 泄漏判据，无"半开"泄漏。`(← P6)`
- [ ] 六点契约逐条可满足（哪怕尚未实现，也能陈述如何满足）。
- [ ] **形态已判定**（A 或 B），并书面写下 §2 决策规则命中的那一条。
- [ ] **类别已判定**（SYNTHESIS 或 TRANSFORM，§0.7），并据此选定 §5A / §5B / §5T 制作支线。
- [ ] 该库对应设计手册中哪一份原料已指明，且已声明其域触及 DESIGN.md 的哪几层（static / TIME）（见 P7）。
- [ ] **本域地板断言集已声明**（§0.6 + P1 + §8 模板"地板断言 = ____"），非空且非平凡。

---

## §2 形态选择决策规则（每个新库必须先回答）

### §2.1 主判据：工具身份（tool identity），而非 op-可约性

**核心问句（canonical）**：
> **"agent 需要把它当作一个带独立身份的动作（ACTION：自己的 budget cost + plan-mode gating + `create/adjust/catalog` + brief 校验 + `next` 验证指针）来调用，还是当作一套从中挑选、展开为既有 ops 的词汇（VOCABULARY / 目录）？"**
> **动作且需独立工具身份 → Shape A；词汇 / 目录 → Shape B。**

**为什么主判据不是"能否用既有 ops 表达"**：`vector_motion`——canonical Shape A 库——其结果只 emit **一个既有 op**（`apply_layer_patch add_layer type:'html'`，§5A.7/§5A.10）。若以"能否用既有 ops 表达"为主判据，它会被误判为 Shape B。它是 Shape A 的真正原因是**它需要工具身份**：顶层 verb、独立 budget cost、plan-mode gating、`create/adjust/catalog` 三态、brief 校验、`next` 指针。**用主判据走一遍 `vector_motion`：它是一个 agent 主动发起、需独立预算与 plan 语义的 ACTION → Shape A。** 参考库因此验证规则，而非打破规则。

### §2.2 破平局（tie-breaker，仅在主判据不决时用）
1. 若这功能跑起来**只会结果为既有 ops** 且**不需要独立工具身份** → Shape B（catalog 条目，绝不做顶层 verb）。
2. 若它引入引擎必须**结构性校验**的新 required-arg 契约（一个 `_schema`）→ Shape A。
3. 若"加十个"应当花费**零**新 op verb 且**零**客户端改动 → Shape B（这份扁平性就是它的全部意义）；若每个实例需要独立的 budget/plan 身份 → Shape A。
4. Shape B 内 element vs template：纯 overlay 图形 → element（corner-alpha 门）；可自画地面的整场景 → template。

### §2.3 强制覆写：TIME 层域必为 Shape A（或强制内嵌 choreography 引擎）
任何**创作域落在 TIME 层**的库（motion / easing / pacing / transition-timing / camera-movement），其地板需要 §5A 的 behaviour/choreography 引擎来强制（无 linear easing、enforced stagger、focal order、留白 hold）。纯 `params->[ops]` 的 Shape B 宏**只能硬编码作者手打的 easing，无法强制地板**。**故：TIME 层域的库必须 Shape A**；若确要做成 Shape B，则必须在宏内**强制内嵌等价的 choreography 引擎**并同样过 G1。§2.1 的 op-可约性启发式在此**被覆写**。（这修正了旧 §11.5 把 `kinetic-type`/`camera-movement` 轻率归 B 的倾向。）

### §2.4 默认取向 + less-is-more↔invisible-tech 取舍（显式声明）
**默认 Shape B**（更少 install 点、更小表面、更 less-is-more，P4），仅当库确需自己的工具身份才升级 Shape A。**取舍须知（P4 vs P2）**：选 Shape B 换来更小表面，但接受其 placement 走**裸坐标接口**（`x/y/width/height` px、`start/duration` s）。**这只在裸量是"放置"而非"手艺本身"时可接受**：静态 overlay 的位置合法地是空间量（P2 对此有显式 Shape-B 豁免，见 §4.3/P2）；但**当数字本身就是手艺**（motion 的 easing/时序、pacing 的节拍），必须 Shape A、用语义轴（§5A.3），不得以 Shape B 裸量绕过。判据：**"这个裸量只是把东西放哪，还是它就是手艺？放置 → B 可；手艺 → 必 A（语义轴）。"**

### §2.5 Shape A 的正当理由**不含**"新 render capability"（与 P8 对齐）
Shape A 的正当理由**限于**：新的**文档状态 mutation / 新持久字段 / 新 query 动词**，且**骑既有 render/compile 路径**。**点库永不新增 render capability**（P8 ADD-ONLY）。`vector_motion` 新增的是**新 authoring**（骑既有 renderer），而非新 renderer。（删除旧 §2 中"新 render 能力 → A"的措辞。）

### §2.6 类别路由（正交，§0.7）
形态判完后，按 §0.7 判 SYNTHESIS / TRANSFORM，据此走 §5A（合成 Shape A）、§5B（合成 Shape B）或 §5T（变换）。类别与形态独立：可有 Shape A×TRANSFORM（如 `grading` 若需新原语），也可有 Shape B×SYNTHESIS（如 title-card element）。

**一句话启发式**：**动作 → A；词汇/目录 → B。合成 → §5A/§5B；变换 → §5T。TIME 层域 → 必 A。** 无论哪种，**每库恰好一个 agent 面**。`(← P4/P8/P12)`

---

## §3 原则（宪法）

> 每条为 **MUST**，附 rationale、可测形式（testable form，标注测试是否已存在）、以及产品原则映射。产品原则（价值基线，高于"能跑 + 测试绿"）逐条内联。后续"接口 / 制作 / 安装 / 验收"标准都是这些原则的推论，并用 `(← P#)` 回溯。

### §3.0 原则适用性矩阵（先读此表——多数历史矛盾源于把 Shape-A 机制当作普适 MUST）

| 原则 | Shape A | Shape B | 备注（机制 vs 不变量） |
|---|---|---|---|
| P1 品味地板 | ✔ | ✔ | 不变量普适；**地板断言集按域自声明**（motion/grading/type 各异） |
| P2 无原始数字 | ✔（brief 全语义） | **部分**：content params ✔；**placement SHARED_PARAMS（x/y/w/h/start/duration）显式豁免** | 手艺型数字永不裸露；放置型裸量仅 Shape B overlay 可 |
| P3 封闭命名目录 | ✔ | ✔ | a11y 语义见 P3 改写（agent 面 = 机读 kind/label，非 aria-label） |
| P4 一 agent 面 + 主输入外全可选 | ✔（主输入 `subject`/`target asset_id`） | ✔（**全 param 有默认**，`fn()`/`expand_*(name,{})` 必成功） | |
| P5 确定性 | ✔ | ✔ | **不变量普适**；seed-threading/`reset_ids` **机制仅 SYNTHESIS/含 RNG 库**；确定性-by-construction 库无 seed 亦合规 |
| P6 只关一个域 | ✔ | ✔ | 域定义见 §0.6 |
| P7 编码手册 | ✔ | ✔ | **TIME 层仅对"输出有时间维"的库强制**；静态域库以 static 层满足 |
| P8 骑层 ADD-ONLY | ✔ | ✔ | |
| P9 逐字可寻址 + 完整可枚举 | ✔ | ✔ | **含库所骑的 host 显示面**（layer tree / `lumen_seek`） |
| P10 交付物+next+可恢复错误 | ✔（完整契约，§4.2） | **弱化契约**（§4.3B：apply op 回 layer-id 资产句柄 + describe_ops 验证提示） | 失败降级为诚实部分是 **host-ledger 契约**，库只"喂"不"独保" |
| P11 build==install | ✔ | ✔ | |
| P12 设计正确/原创 | ✔ | ✔ | |

### P1 · 结构性品味地板（Structural Taste Floor）—— 核心机制
**MUST**：点库必须把专业手艺作为其结构的**不变量**来强制执行，而非作为每次调用的选项来提供。不存在任何可达代码路径，让一个良构 brief 产出低于地板的输出——承载品味的决策由库的编舞/规则/preset 维护，**永不由模型设定**。
**地板断言集按域自声明**：P1 不规定一套固定断言，而要求**每库声明其域的地板断言集**（写入 §8 模板"地板断言 = ____"）。参考实例：
- **motion 域（`vector-motion`）**：organic 运动无 linear easing、存在最小 stagger、存在 focal order、存在留白 hold。
- **grading 域（已安装 `grade`，见 §11.5 状态更新）**：黑/白点不 clip、skin-tone 落在肤色线、全局饱和 ≤ cap、跨镜色温 ΔK ≤ tolerance、拒绝无脑 teal-orange。
- **kinetic-type 域（已安装 `kinetic_type`，见 §11.5 状态更新）**：每行 readability dwell ≥ N（依阅读速度）、measure/行长在带内、在 title-safe 边距内、无 orphan/widow，**外加** motion 地板（因其亦属 TIME 层）。

**可测形式**：对 brief 空间做**属性化（property-based）fuzz**（Hypothesis strategy 覆盖 brief schema，非固定 fixture 列表），每个输出都必须通过该库**声明的地板断言集**。任一 brief 击穿断言 → 地板还不是结构性的。
- 强制测试：`tests/test_<lib>_taste_floor_property.py`【待落地】（Hypothesis property）+ golden `test_<lib>_taste_floor_holds`【待落地】。
- **反平凡 meta-check**：`test_taste_floor_test_is_nontrivial`【待落地】断言每个已注册库都存在 floor 测试模块，且引用 ≥ N 条来自该库**声明的地板断言注册表**的断言——空/`assert True` 的 floor 测试 FAIL CI。
- 合并前必过**对抗评审门 G9**（人评审，`machine_checkable=false`），评审断言转成**永久 golden**。

**Rationale**：这是 Layer-2 输出高级、Layer-1 输出业余的**唯一原因**。品味一旦是每次重选的选项，输出必回归模型均值。把地板断言写成固定的 motion 词汇会漏掉 grading/type 的手艺；故断言集必须**按域声明**。
**产品原则映射**：原则 1（最好的技术不可感知）。

### P2 · 创作语言，永不夹带手艺的原始数字（Invisible Tech）
**MUST**：库的手艺型接口词汇是语义/创作的（`energy 0..1`、原型名、编舞动词）。agent 永远看不到、也不供给**承载手艺的**坐标、像素、keyframe 下标、`x+=20` 或任何 Layer-0/1 手艺量。
**Shape B 放置量显式豁免**：Shape B overlay 的 **placement SHARED_PARAMS**（`x,y,width,height` px-from-centre、`start,duration` s）**被显式豁免**，因为静态 overlay 的位置合法地是空间量、不是手艺决策。豁免**仅限放置**；任何**手艺型**数字（easing、stagger、节拍、饱和度…）不在豁免内，必须语义化（并因此通常须 Shape A，见 §2.4）。
**可测形式**：`tests/test_no_raw_numbers.py`【待落地】用**白名单**（非黑名单，以免漏掉 `offset_amount`/`nudge`/`pixels_wide`）：
- Shape A：断言每个 schema property 名落在语义词汇白名单内。
- Shape B：断言每个 **content param** 落在语义白名单内；**placement SHARED_PARAMS 在豁免集**（`{x,y,width,height,start,duration,prefix,animate,color,palette}`）内，其余裸几何/时间名 FAIL。
**Rationale**：暴露手艺型原始数字＝迫使模型充当底层动画师/合成师（做得差且不稳），并重开地板（一个手艺型数字就是一次伪装的品味决策）。放置型裸量不打开地板，故可在 Shape B 豁免。
**产品原则映射**：原则 1 + 原则 6。

### P3 · 命名目录优先于自由参数（图案优先 + 机读句柄）
**MUST**：库对外呈现一个**封闭的命名目录（CATALOG）**（behaviours / styles / archetypes / presets），而非开放参数空间；**一个原型词重塑整个输出**。**agent 面语义（改写自旧 a11y 条款）**：库交给模型的**每个 token 必须机器可读、且携带无歧义的 kind/label**（archetype 是哪类、handle 指向什么）——这与 P9 的逐字可寻址共同构成"图案可读性"。（旧条款把人机 UI 的 `aria-label`/shape+color 双通道误挂到 agent 文本面；agent 面是 LLM 消费的语义文本，无 icon-only 控件，故此处要求机读 kind/label，不是 aria-label。若指**渲染输出**的可及性，归 P7/输出侧，不在 agent 接口。）
**可测形式**：`describe_*()` 中每个 archetype 词全局唯一、可作参数解析，且每 token 附 kind 字段。强制测试：Shape B `test_documented_params_exist_on_the_function`【已存在】+ `test_every_element_has_a_catalog_entry_and_vice_versa`【已存在】；Shape A `describe_behaviors()` drift-pin【已存在】；kind/label 存在断言 `test_catalog_tokens_carry_kind`【待落地】。
**Rationale**：封闭命名目录可发现、确定、可评审，且无法像自由参数那样把品味夹带回来。原型让"重塑"成为原子操作。
**产品原则映射**：原则 6。

### P4 · 一个 agent 面 + 主输入外全可选（less is more / update_quantum）
**MUST**：一库只暴露**一个** agent 面——Shape A 为单一 agent 工具（`update_quantum` 模式，用 `op` 判别子命令），Shape B 为单一 `describe_ops()` 注入点 + 单一 `apply_<lib>` op；**绝不铺开一堆扁平工具**。
**"主输入外全可选"按类别/形态实例化**：
- Shape A × SYNTHESIS：唯 `subject` 必填；其余全可选、皆有承载品味默认值。
- Shape A × TRANSFORM：唯 `target asset_id`（要变换的 clip/layer）必填；其余全可选。
- Shape B：**全部 param 皆有默认**，`fn()` 与 `expand_*(name, {})` 必成功；CATALOG 中的 `'*'` 标记**仅信息性文档**（提示"内容语义上应给"），**绝非引擎强制的 required-arg**——凡文档标 `'*'` 的 content param 在 Python 签名里仍有默认值。
**可测形式**：`fn()` / `expand_*(name, {})` / 最小 brief 都必须成功并过地板断言；install-coverage meta-check 断言"one agent surface per library"（Shape B = 每库恰 1 段 bracketed 注入 + 1 个 `apply_<lib>` op，见 G4）。强制测试：`test_expands_to_valid_ops`【已存在】、install-coverage【待落地】。
**Rationale**：扁平工具泛滥＝逼模型编排库，把库本要废除的"堆积木"上移一层重新引入。单面 + 全默认让默认值（而非模型）在模型沉默时承载手艺。
**产品原则映射**：原则 2（less is more）。

### P5 · 确定性（不变量优先；机制按需）
**MUST（不变量，普适）**：同一 brief（同一输入）→ **逐字节相同**的输出；代码路径中**无墙钟、无 env 读取、无无序集合迭代**。这才能命中下游 content-hash 渲染缓存、让测试可复现、让 `adjust` 诚实。
**机制（按类别）**：
- **含内在随机性的库（SYNTHESIS/motion）**：一切随机来自穿线全程的唯一 `random.Random(scene.seed)`；id 来自**每线程可重置计数器**（`threading.local` + `reset_ids()`）。
- **确定性-by-construction 的库（如 grading LUT/curve、Shape B 纯宏）**：**无 RNG、无 seed 字段、无 `reset_ids` 亦满足 P5**；id 由 `prefix` 命名空间化保证唯一。§8 模板的"seed 来源"字段对这类库填 `N/A（deterministic-by-construction）`。
**可测形式**：
- Double-run golden，但**在全新子进程中以随机化 `PYTHONHASHSEED` 运行**（`subprocess` + env override），以真正暴露 set-order 与 env 依赖：`test_<lib>_determinism_subprocess`【待落地】。
- 静态门：AST/grep 禁止库模块内出现 `time.`、`datetime.now`、`os.environ`、以及对可变 `set`/`dict` 的顺序敏感迭代：`test_no_wallclock_no_env_static`【待落地】。
- 现有覆盖：`test_vector_creative.py::test_build_scene_is_deterministic_per_seed`【已存在，`:314`，断言 `svg1==svg2`】——**引用真实标识符**（旧文所称 `test_determinism_same_brief_same_svg` 在仓中不存在）。
**Rationale**：逐字节相同是缓存、回归、`adjust` 诚实的前提。把 seed-threading 当普适 MUST 会误伤无随机性的 grading 库；故不变量普适、机制按需。

### P6 · 恰好关闭一个创作域
**MUST**：每库**有且仅有**关闭一个创作域（按 §0.6 定义与边界判据）。域边界即点库的单位；**半闭合的域是漏洞（leak）**。
**可测形式**：`test_no_floor_disabling_default`【待落地】（§0.6 泄漏 proxy）+ PR 评审门 G10（`machine_checkable=false`）：若存在把某域内手艺决策泄回模型的路径（该域某手艺变成可选参数且默认无效），失败。
**Rationale**：品味只有在**封闭边界内**才能变得不可逃逸。这正是路线图是一**串**库而非一个巨型工具的原因。

### P7 · 结构性编码设计手册（含 TIME 层，仅对时间维输出强制）
**MUST**：`~/Code/lumeri-design-manuals/` 的 `DESIGN.md` 双层 token 系统（static 层：color/typography/frame/**grading**/depth ＋ **TIME 层**：motion/transition/pacing/audiosync）与定向手册（`01-grading`、`02-editing-rhythm`、`03-subtitle-typography`、`04-composition-camera`、`05-UI`、`06-AI-conduct`）是**原材料**。库的职责是把手册指引从散文转成**不可逃逸的结构**，且**编码其域触及的每一层**。
**TIME 层强制范围（收窄）**：**只有"交付物带时间维"的库**（其输出含 motion/transition/pacing/audiosync）**必须**编码 TIME 层。**静态域库**（如静帧 `grading`、静态 `composition` preset）以 **static 层**编码即满足 P7——grading 依 DESIGN.md 属 static 层（color/**grading**/…），一张静态调色合法地无任何 motion track。（修正旧条款：既把 grading 列为 static 层手艺、又要求"视频库必须有 TIME track"会自相矛盾并误伤 grading。）
**可测形式**：门 = **"库编码其域触及的每一层"**。对**时间维输出**库：`test_<lib>_emits_time_layer`【待落地】断言其 emitted ops 含动画/关键帧或 transition/pacing 结构，无则视为静态 UI 保真度不达标，FAIL。对**静态域**库：断言其编码 static 层 token（无 TIME 要求）。
**Rationale**：手册经 `SKILL.md`（`skill_router` 的 `glob('*/SKILL.md')` 自动注册）注入底层 Gemini 时只是**建议、可被忽略**；库是把同样手艺编进 choreography/defaults/preset、变成**绑定**的机器。时间维库遗漏 TIME 层会停留在静态保真度；但静态域库不应被 TIME 门误杀。

### P8 · 骑在既有层上，永不 fork 物理层、永不新增 render capability（ADD-ONLY）
**MUST**：库在 Layer-0 physics 与 Layer-1 primitives 之上**组合**（发出标准宿主产物、复用既有 render/cache/composite/timeline 路径），**不得**重实现渲染、编译或原始 op，**不得新增 render capability**。工具模块本身**永不渲染**（`vector_motion.py`：`this module never renders`）。
**可测形式**："no new render code in a library PR"——评审门 G11（`machine_checkable=partial`）+ **grep 门** `test_no_new_render_symbols`【待落地】（库 PR diff 不得新增 renderer/compile 符号）。
**Rationale**：架构尺度的 less-is-more：物理层单一真相源。发出宿主原生产物意味着每个既有面（frame preview、mp4 render、compositing、undo）零新工具、零回归面立即可用。这也约束 §2.5：Shape A 的正当理由不含"新 render capability"。

### P9 · 逐字可寻址 + 完整可枚举（工具诚实）—— 直接根治失败模式 2
**MUST（拆为两个不变量）**：
- **(a) 逐字（per-handle verbatim）**：任何**单个**可寻址 token（layer/shape id、handle、name、catalog key、archetype 名）在展示中**不得**被截断、加省略号、或以 display-only/美化别名出现——**单个 id 内部无 `[:12]`、无 `…`**。往返性质：展示里出现的每个 token 原样贴回作参数都能解析。
- **(b) 完整（completeness）**：模型可寻址的**每个实体**，要么**被完整展示**，要么**可经一个显式的、无损的枚举 op 触达**（如 `lumen_seek` 全量列出）。**列表级摘要（list-level summarization）允许**——当且仅当它**配有该无损枚举路径**。
**host 显示面规则**：per-identifier 截断**禁止**；list-level 摘要**允许且必须**伴随全量枚举 op。该规则不再依赖已移除的 pre-delivery completion gate。
**作用域扩展到 host 显示面**：原始 12 字符截断发生在 **host 的 layer-tree 显示**（`v3_routes.py`/`layer.py`），在"库展示给模型"范围之外。故 P9 **同时约束库所骑的 host 显示/树面**（layer tree、`lumen_seek`）——一个完美的库仍会经 host 显示路径复现该 bug。
**可测形式**：
- 库级 round-trip：`tests/test_<lib>_addressability_roundtrip.py`【待落地】——收割 `create`/`adjust`/`catalog` 输出里 emit 的每个 handle，原样喂回作参数，断言全部解析、无 `E_NOT_FOUND`；断言无单个 id 含 `…`/被截断。
- host 级 round-trip：`test_layer_tree_handles_round_trip`【待落地】——对 host layer-tree/`lumen_seek` 展示的每个 id 原样喂回，断言解析成功（截断的 12 字符 id 无法解析 → RED）。
- 列表完整性：`test_truncated_list_has_full_enumeration_path`【待落地】——凡出现"等共 N 个"摘要处，断言存在可调用的全量枚举 op。
- 现有近似：`test_vector_motion_tool.py:294`【已存在】只断言坏 id 返回 `E_NOT_FOUND`，**不足以**证明 (a)/(b)，故上述 round-trip 为必需新增。
**Rationale**：agent 会把工具输出里的句柄复制进下一次调用；任何有损单-id 显示都保证派生的每次 delete/edit 都 `E_NOT_FOUND`；而"只展示部分实体、无枚举路径"则让其余实体不可达。工具的输出**就是**其输入契约的一部分。
**产品原则映射**：正确性性质，非外观。

### P10 · 可验证交付物 + 中性活动记录 + 可恢复错误 —— 直接根治失败模式 3
**MUST（按形态分档）**：
- **Shape A（完整契约）**：库工具必须 (a) 注册一个**可验证的最终交付物/资产句柄（asset handle）**；(b) 把模型指向一条**验证路径**（`lumen_seek` / `lumen_render_range` / `render` / 视觉核对），每次成功回复以 `next` 字段收尾；(c) 只发**结构化、可恢复**错误（typed code ＋ recoverable flag）。
- **Shape B（弱化契约）**：Shape B 经通用 `apply_<lib>` op 上线，无 per-library 响应对象。其交付契约为：**apply op 的 op-level 结果回传所创建 layer(s) 的 asset handle（layer id）**，且**在共享 `describe_ops()` 的该库 bracketed 段内写明一行验证提示**（如"apply 后用 `lumen_seek`/`render` 核对该 overlay"）。Shape B **不要求** per-call `next` 字段（它没有那条通道）。G7/G12 对 Shape B 以此弱化契约评判。
**失败降级是模型解释契约（重要澄清）**：库负责提供最终资产、验证指针和 typed/recoverable 错误；host 只记录这些事实，不再以 completion ledger 覆盖模型的收尾判断。未解决的失败由模型基于工具结果诚实说明；host 不追加 `incomplete_goal` 或固定文案。
**可测形式**：
- 库侧：`tests/test_<verb>_tool.py`【部分待落地】断言终态成功含 asset handle **且**（Shape A）`next` 验证指针（现网 `vector_motion.py:135,213` 已 emit `next`，但**无测试**断言，须补 `test_success_carries_asset_and_next`【待落地】）；`test_errors_are_typed_and_recoverable`【待落地】断言所有错误带 typed code + recoverable flag。
- host 侧：`test_recoverable_library_failure_degrades_to_partial`【已存在】——证明 recoverable 库失败会交给模型诚实说明，模型无工具回复后自然 `turn_complete`，host 不发 `incomplete_goal`/`turn_wrapup`。
**Rationale**：不产出可验证最终资产、或其失败硬卡 turn 账本的创作库，会摧毁模型诚实收尾的能力。验证也闭合了地板的环：模型能**看见**资产越过了地板。
**产品原则映射**：诚实优先。

### P11 · 制作即安装、机器校验（Build == Install）—— 直接根治失败模式 1
**MUST**："制作"与"安装到线上面"是**同一个动作**，不可分割、须在**同一个 PR**。每个安装/注册点由一个 coverage/drift 测试枚举，**任一点缺失即 CI 红**。"在 `TOOL_SCHEMAS` 里但不在任何 router pack 里"必须**测试失败**，绝不许靠 full-fallback 静默存活。一个库在分支上建成、测试全绿、过对抗评审，但注册点未合入——是**未完成的工作**。
**可测形式**：见 §6.3 的两层 meta-check（Layer A 全安装 drift 测试【待落地】 + Layer B **自动发现式** manifest 测试【待落地】）；`catalog_coverage()==(∅,∅)`【已存在】。
**Rationale**：unit-green 不等于 system-reachability——一个工具能通过自己的测试，却对 live 模型不可见，因为接线是手工、可分离、缺失时静默的。唯一持久修复是让安装机器强制。**（自省：本原则的强制检查本身当前 【待落地】——见 §13 生效条件；在其转绿前，本原则以指导效力存在。）**

### P12 · 设计正确优先于方便；借鉴不抄袭；尊重疯狂 brief（与封闭目录的和解）
**MUST**：当"正确手艺"的设计与"方便发货"冲突时，库编码**正确手艺**。archetype/catalog 系统编码**独特、原创**的手艺，而非对参照外观的方便克隆。
**"尊重疯狂 brief" 的精确含义（与 P3 封闭目录的和解）**：本架构**刻意**用封闭目录约束模型以强制地板——它本质上是一台"夹逼"机器，这正是其价值。故"尊重疯狂提案"**不**等于"照单执行任意越界输入"，而是：**目录须为原则性可扩展（principled-extensible），使不寻常但合法的 in-vocabulary 组合映射到真实、可区分的 behaviour，而非被静默塌回默认**；而**真正 out-of-vocabulary 的输入被识别并报告（收进 `notes`），不被伪造**（对齐 §4.2/§5A.3/AP13 的"未知输入报告而非致命"）。
**可测形式**：`test_novel_inband_brief_yields_distinct_behaviour`【待落地】（新颖但合法的 in-vocabulary 组合产出与默认**不同**的真实 behaviour）+ 对抗评审门 G9（`machine_checkable=false`）。
**Rationale**：库让品味**永久**（发往每一次未来调用），库里的便利捷径比一次性代码代价高得多。封闭目录与"尊重疯狂"的张力靠"原则性可扩展 + 诚实报告"化解，而非靠放开目录。
**产品原则映射**：原则 3 / 4 / 5。

---

## §4 接口标准（作为 §3 的推论）

### §4.1 通用接口规则（两形态都适用，除非注明）
1. **手艺型词汇只暴露语义/创作量**（`energy 0..1`、archetype 名、编舞动词）；绝不暴露或接受**手艺型**坐标、像素、keyframe 下标或任何 Layer-0/1 手艺量。（Shape B 的 **placement** 裸量按 P2 豁免。）`(← P2)`
2. **一库一 agent 面**：Shape A 单个 `update_quantum` 式 verb（`op` 判别符），Shape B 单个 `describe_ops` 注入 + 单个 `apply_<lib>` op；绝无扁平工具泛滥。`(← P4)`
3. **主输入外全可选**：SYNTHESIS 唯 `subject` 必填、TRANSFORM 唯 `target asset_id` 必填、Shape B 全部有默认；每字段有承载品味的默认值，最小输入也产出地板级（或对内容型 overlay：合法非崩溃）输出。`(← P4/P1)`
4. **封闭命名目录**：`catalog`/`describe` 暴露**整套**可组合词汇；一个原型词重塑整体。`(← P3)`
5. **逐字可寻址 + 完整可枚举**：库（及其所骑 host 显示面）展示的每个 id/handle/name/catalog token 原样可作参数——单个 id 无截断、无 display-only；实体或完整展示或经无损枚举 op 可达；列表摘要须配枚举路径。`(← P9)`
6. **紧凑 digest + 验证指针**：Shape A 成功返回精简 digest（不返回渲染字节）并以 `next` 字段点名验证动词；Shape B 经 apply op 回 layer-id 资产句柄，验证提示写在 `describe_ops` 该库段。`(← P10)`
7. **每个 model-facing token 机器可读、携带 kind/label**（archetype 是哪类、handle 指向什么）。`(← P3)`
8. **结构化可恢复错误即数据**：`{applied:false, error_code, error_message}`（可含 recovery hint），永不泄漏堆栈或半应用文档；诚实优先：反馈被识别但没挪动任何东西（轴到极限）→ 在 `notes` 说明，不把 no-op 报成 change。失败向诚实部分答案的降级是 host-ledger 契约，库负责喂 typed/recoverable 错误。`(← P10)`
9. **跨工具边界的资产引用一律是 `asset_id` 字符串**（`v_001`、`img_003`），绝不用文件路径（`_schema.py:20-22`）。`(← P9)`
10. **库不得让 brief 直接寻址/变更 raw primitive 层**；由库拥有"意图 → primitive ops"的翻译。`(← P2/P6)`

### §4.2 Shape A 接口 schema 约定（以 `vector_motion` 为准）
- **单一工具 + op 判别符**：`op: create | adjust | catalog`；永不 `create_vector`/`adjust_vector`/`list_vector_catalog` 一族扁平工具。
- **schema 形状**固定：`_tool(name, description, properties, required)` → `{type:'function', function:{name, description, parameters:{type:'object', properties, required}}}`（`_schema.py:29`）。
- **dispatcher 签名**固定 `async def dispatch(args: dict, ctx: ToolContext) -> dict`；**不得吞错**（agent loop 包裹每次调用并 emit `tool_exec_error`，`tools/__init__.py:22-24`）。
- **主输入按类别**：SYNTHESIS 除 `subject` 全可选；TRANSFORM 除 `target asset_id` 全可选。默认 `intent=reveal`、`duration=5s`、`seed=7`（仅含 RNG 库）、house style/palette；`brief.canvas` 默认 = 宿主 doc canvas（artifact 填满整帧）。
- **`create` 返回紧凑 digest**——按域取形态：
  - **SYNTHESIS**：layer id/name、start、duration、`svg_bytes`（**或该域等价的紧凑预览量**）、压缩 **plan digest**（`_plan_digest()`：resolved params、phase windows、每 phase 选中 behaviour、focal id、node 结构）、notes——**不是**完整 SVG；完整 plan 存于 layer（`props.vector_scene.plan`）。agent 读 plan、**永不读 SVG/像素**。
  - **TRANSFORM**：**measurement / scope readback digest**——scopes/waveform/histogram/统计量 + 缩略 asset_id，**不是**整帧像素转储。（digest 字段**泛化**为"该域合适的紧凑可解释预览/测量句柄"，`svg_bytes` 只是 vector 域的实例。）`(← P2/P10)`
- **每个成功回复以 `next` 字段收尾**，点名验证动词与迭代方式。`(← P10)`
- **`adjust`（迭代）** 收非空 feedback phrases 列表（双语，`'more playful'` / `'更高级'`），针对存储的 brief（真相源）；同 seed 确定性重建；保持同 layer id；单 patch，可 undo；**保留用户在目标 artifact 上定制的一切**——重编舞只改 **CONTENT**，绝不改 placement/timing/compositing。
  - **保留键 = 不变量 + artifact-type 附录**：不变量＝"`adjust` 从存储 brief 重推、保留目标 artifact 上一切用户定制态"。具体键集是 **artifact 类型附录**：html-layer artifact（vector）＝`transform, opacity, blend_mode, visible, locked, mask, effects, lane`；clip-attached grade（grading）＝clip trim、clip 上其它 effects、node-stack 顺序。`(← P5/P9)`
- **`catalog`** 返回整套可组合词汇（subjects/mark presets、intents、styles+aliases、palettes、semantic axes、feelings、behaviour catalog、per-phase overrides + 示例、feedback vocabulary），让模型无需猜测即可撰写 brief。`(← P3)`
- **任何已注册动词都能从 brief 触达**：`brief.behaviors` 可把任一可覆盖 phase 钉到任一 catalog 动词；未知 phase/动词被丢弃并报告（收进 `notes`），永不 fatal。`(← P3/P8/P12)`

### §4.3 Shape B 接口 schema 约定——分两层：**(A) 通用 FORM 契约** + **(B) 绘图 profile**

> 旧 §4.3 把"通用 Shape-B 机制"与"templates/elements 的 2D 矢量绘图约定"混为一谈，导致非绘图 Shape-B 库（grading、audio-cue）无从下手。现拆开。

#### §4.3-FORM：通用 Shape-B FORM 契约（每个 Shape-B 库都适用）
- **catalog 函数签名**：`def fn(*, ...content params..., <SHARED_PARAMS>) -> list[dict[str, Any]]`——keyword-only，**每 param 有默认值**（使 `fn()` 与 `expand_*(name, {})` 可用），返回**每次全新**的 op dict 列表，每个 dict 含 `'op'` 键。
- **纯函数**：只读 theme/查表 + 算术 + emit ops；**不变更文档、不 I/O、不直接调用其它 op verb**——expansion 是唯一副作用，且发生在通用 apply op 的 `_dispatch`（`ops.py:2072-2073`）。`(← P8)`
- **两处注册（且仅两处）**：`REGISTRY[name] = fn` 与一个 `CATALOG` dict `{name, category, summary, params(仅 content，'*' 标必填、**仅信息性**), example}`；`example` 的键必须等于该库 apply 参数名、值等于条目名（测试强制 `example['element']==name`）。加入 `__all__`。
- **通过恰好一个 apply op 触达**（`apply_<lib>`）：`_require_arg name` → `REGISTRY.get` → 未知则 `E_ARG`（列合法名） → 校验 params 是 dict → `fn(**params)`（`TypeError` 捕获为 `LayerPatchError('E_ARG')`） → `for sub in sub_ops: _dispatch(doc, sub)`。
- **通过一个注入缝暴露**：`describe_<lib>()` 顶部渲染一次 `SHARED_PARAMS` + 说明，每 CATALOG 条目一行紧凑；在 `describe_ops()` 内以 bracketed 标题（`[Scene template library]` / `[Element library]`）经 lazy-import `try/except` 追加。任何客户端（web v3 / CLI）都不直接读 registry。`(← P4/P8)`
- **每库自声明 SHARED_PARAMS**：**新库必须显式声明自己的 SHARED_PARAMS**，不假定必是绘图库那套。非绘图 Shape-B 库（如 grading recipe、music-cue）声明自己的（如 `asset_id, strength, start, duration, prefix`）。`(← P2)`
- **每库自声明 op-emission 面**：绘图库 emit `add_shape`/`add_layer`/`set_keyframe`；非绘图库 emit 其域相应的既有 ops（如 grading emit 既有的 color-op；transition emit 既有 timeline op）。
- **Shape-B 交付契约（P10 弱化档）**：apply op 的 op-level 结果回所创建 layer(s) 的 asset handle；`describe_ops` 该库段写一行验证提示。**Shape B 不带 per-call `next`**。
- **Shape-B 迭代（refinement）**：Shape B 无 `adjust` op。精化 = **删除既有 stamp + 以调整后的 params 重新 apply**；`prefix` 保持稳定以维持 layer-id 语义连续（同 `prefix` 重盖两次 id 唯一，见 P9/AP25）。§8 模板须记明该库的 refinement 语义与 prefix 稳定策略。`(← P10/P9)`
- **结构化错误**：未知名→`E_ARG`（带合法名列表）；params 非 dict→`E_ARG`；`TypeError→LayerPatchError('E_ARG')`。

#### §4.3-DRAW：templates/elements 绘图 profile（**仅**当 Shape-B 库画 2D 矢量图形时）
- **SHARED_PARAMS（绘图实例）**：templates = `palette,width,height,start,duration,prefix,animate`（`__init__.py:80-82`）；elements = `x,y,color,start,duration,prefix,animate,width,height`（`__init__.py:97-99`）。**这些是绘图 profile 的实例，非普适 Shape-B 法**。
- **坐标 px-from-centre**（原点正中，x 右+，y 下+，匹配 `model.DEFAULT_TRANSFORM`）；仅在 emit 时经 `theme.nx`/`ny` 转到归一 shape 空间。尺寸/时序：px 与秒（秒为准）。（这些 placement 裸量按 P2 豁免。）
- **颜色**：caller 传 `None` 时默认品牌 accent（`theme.PALETTES['lumeri']['accent']`）；`color`/palette 参数必须逐字 round-trip 进 emitted ops。`(← P3/P9)`
- **从共享 theme 构建**：不嵌 hex/绝对 px；一 palette 名重塑，`nx/ny` + type scale 追 canvas（9:16 不崩）。`(← P3/P8)`
- **layer id 由 `prefix` 参数命名空间化**（默认 = 条目名），同条目盖两次绝不撞 id。`(← P9)`
- **ELEMENT-ONLY 不变量**：expansion 必须是 overlay——无满帧背景 layer/gradient——单独渲染时四角 alpha 保持 ~0。Templates **可**占满帧，这是二者的定义性区别。**（此 overlay/corner-alpha 门仅属绘图 profile；非绘图 Shape-B 库不适用。）** `(← P6)`

### §4.4 Shape B × TRANSFORM 的接口注记
若一个 Shape-B 库是 TRANSFORM 类（如 grading recipe 组合既有 color-op）：其 FORM 契约同 §4.3-FORM，但 (i) 主输入是 `target asset_id`；(ii) digest/验证走 §4.2 的 measurement readback 精神（apply 后 `next`→scope/render）；(iii) **不适用**绘图 profile 的坐标/overlay/corner-alpha 门。

---

## §5 制作标准（如何构建：从输入到交付物）

> **§5 分三支**：**§5A** = SYNTHESIS × Shape A 的分层生成管线（`vector-motion` 参考）；**§5B** = Shape B 的宏制作标准（含**地板机制**）；**§5T** = TRANSFORM 的变换制作标准。下方 **§5.0 适用性矩阵**给出每条与形态/类别的对应；**GATE 1 的每个 checkbox 都标注其适用类别**，未涉及的类别该项记 N/A（正如旧文只给 IR 项加了 `（有 IR 的库）` 守卫，现对每项都加守卫）。

### §5.0 制作条款适用性矩阵

| 条款 | SYNTHESIS×A（§5A） | SYNTHESIS×B / 内容型（§5B） | TRANSFORM（§5T） |
|---|---|---|---|
| 严格向下分层 + 唯一 director | ✔ 必须 | 简化：纯宏无跨层 import | ✔（param/node 栈分层） |
| renderer-agnostic IR + AdapterReport | ✔（有 IR 时） | N/A | 仅多后端时（见 §5T） |
| 语义→数字唯一表 ResolvedParams | ✔ | 若有语义轴则 ✔ | ✔（语义→LUT/curve 参数唯一表） |
| registry + 生成 catalog + drift-pin | ✔ | ✔ | ✔ |
| **地板机制** | §5A.5 choreography | **§5B.地板**（宏内烘焙 + archetype 驱动） | §5T.地板（色度守恒断言） |
| phase arc + 留白 hold | ✔ | N/A（除非时间维） | N/A |
| behaviour 契约（纯函数） | ✔ | 宏即纯函数 | node 即纯变换 |
| 编译 + render-safety | ✔ | 复用既有 | 复用既有 color pipeline |
| validate-before-mutate + 单原子 patch | ✔ | ✔（apply op 单 dispatch） | ✔（非破坏、可 undo） |
| adjust=从反馈重推 | ✔ | 删除+重 apply（§4.3-FORM） | ✔（改存储参数重推） |
| ADD-ONLY 自身不 render | ✔ | ✔ | ✔ |
| 结构化故障、未知报告非致命 | ✔ | ✔ | ✔ |
| 确定性 | RNG 穿线 | by-construction | by-construction |

---

### §5A —— SYNTHESIS × Shape A 生成管线（`vector-motion` 参考模板）

#### §5A.1 严格向下分层管线 + 唯一全知导演 `(← P8)`
引擎是一叠层（IR/geometry → toolkit → behaviours → styles → params → choreography → api/tool），依赖**严格向下**：任何层都不 import 其上之层；`behaviors` 从不 import `styles`；`svg.py` 只读 IR；**恰好一个顶层 `api.py`（"creative director"）被允许看到一切**。
证据：`docs/vector-motion-plan.md §1`。**强制**：`importlinter`（`.importlinter` 配置 + `test_layering_import_contract`【待落地】）机器校验层向；旧文承认"当前无 import-linter"，本次修订将其**落地为门**（AP9 不再是 machine-checked=false）。

#### §5A.2 renderer-agnostic IR：比最弱后端更富，诚实降级 `(← P8)`
`VectorScene` IR 是纯 JSON-可序列化 dict（`scene → nodes → tracks`），所有消费者都说这门语言。它**刻意携带超出最弱后端的词汇**（draw-on、morph、per-instance particles、bezier easing）；每个 adapter 只兑现它能兑现的，并**报告它丢弃了什么**（`render.py` 的 `AdapterReport(honored/dropped)`），**绝不**把 IR 削平到最弱目标、**绝不**静默近似一个 focal gesture。
IR 契约：`scene = {kind:'vector_scene', version, width, height, duration, background|None, seed, nodes[], meta}`；坐标画布中心制，编译器转左上。`node.kind ∈ {path, text, group, particles}`；每 node 携带 `style + transform + tracks + meta`；消费者**必须保留未知键**。`track = {prop: [{t seconds, value, ease}]}`，`prop ∈ TRACK_PROPS` 白名单，`ease` 命名**离开**该 keyframe 的曲线（CSS 语义）。`validate_scene` 强制：prop 在词汇内、`t ∈ [0,duration]`、按 t 排序、id 唯一。
（**适用性**：本条仅对**有多后端能力梯度的 IR** 库；单后端 TRANSFORM 库见 §5T 的诚实降级替代。）

#### §5A.3 唯一"语义→数字"映射表 `(← P2)`
创作意图 = 七条 `0..1` 语义轴（`energy, smoothness, playfulness, elegance, complexity, density, organicness`）；它们在**恰好一处**——`ResolvedParams`——变成低层数字，并作为一张表被单测。任何 behaviour / choreography **不得自造映射**。
解析顺序（后者胜）：`style baseline → feeling adjectives（±nudge，clamp 0..1）→ explicit param overrides`。`params.resolve` 对未知 override 轴 **raise**（机器供给的错误要响），对未知 feeling **收进 notes 继续**（近对的 brief 仍出东西，对齐 P12）。

#### §5A.4 一切皆可插拔 registry + 生成式 catalog + drift-pin 测试 `(← P3)`
behaviours、styles/archetypes、intents/phase-arcs、renderers 全是数据驱动 registry，扩展不 fork core。每个 registry 附机读 catalog 与由 registry **生成**的 `describe_*()` prose；一个测试把 catalog 条目 **pin 到真实注册签名**。证据：`behaviors/__init__.py`、`styles.py`、`choreography.py`、`catalog.py`；Shape B 等价 `test_documented_params_exist_on_the_function`【已存在】。

#### §5A.5 编舞：被强制的 phase arc + 预算化留白（**motion 域的地板机制**）`(← P1)`
输出被规划成 intent 驱动的 phase arc（reveal/intro：`anticipation→entrance→emphasis→hold`；loop：`entrance→cycle`；transition/outro：`…→exit`），不是一袋动画。收尾 hold（**留白**）从 `params.hold_fraction` **先**切出，剩余权重再归一。behaviour 必须待在其分配 window 内。api 从**一张可审计的 geometry-and-parameter 打分表**为每 phase 选 behaviour（rng 仅破平局），经 `apply_behavior` 应用。stagger 模式、focal-order/entrance-lands-last、留白 hold 都是被强制而非被期望的。**这是 motion 域的地板断言集来源**；其它域的地板机制见 §5B.地板 / §5T.地板。

#### §5A.6 behaviour 契约 `(← P5/P9)`
behaviour = `@behavior` 注册的**纯函数** `fn(scene, nodes, window, level, rng)`，只往其 target nodes、只在其 window 内写 tracks，组合 `motion.py` 的 track builders（**永不手写 keyframe dict**）。module-level randomness 是 bug。

#### §5A.7 编译与 render-safety `(← P8)`
`scene → compile_scene →` 自包含动画 SVG → 包成 html layer → `apply_layer_patch add_layer type:'html'`。**硬 render-safety 约束**（编进 compiler）：无 external URLs、无 `data:` URIs、CSS 无 `url()`、无 network JS、system-font stack、`duration ≤ 60s`。由 `gemia/hyperframes_adapter._validate_local_only_html/_css`（经 `render.validate_html_layer`）强制。

#### §5A.8 validate-before-mutate + 单原子 patch `(← P10)`
生成 scene 的 render-safety 在写入文档**之前**校验；写入是**单个原子 layer patch**（`adjust` 在一个 patch 内 delete+add-at-index，保留 index/parent）。`vector_motion._create` 先 `validate_html_layer` 再 `apply_layer_patch`。

#### §5A.9 adjust = 从反馈重新推导，永不对输出动刀 `(← P5)`
反馈短语（双语）映射到语义轴 delta，编辑存储的 brief，然后**同 seed 整场重建**。反馈**永不 patch SVG 文本**。证据：`api.adjust_scene()` 经 `apply_feedback` 重建；`vector_motion._adjust` 把 brief 保在 `props.vector_brief`。

#### §5A.10 ADD-ONLY 集成：复用宿主管线，自己永不 render `(← P8)`
模块 emit 标准宿主 artifact（html layer），骑既有 render/cache/composite/timeline 路径；只加一个 tool 和一个库，**不改任何既有 renderer，工具模块自己永不 render**（`vector_motion.py` docstring `this module never renders`）。

#### §5A.11 结构化故障，未知输入报告而非致命 `(← P10)`
每次失败返回结构化错误码（`E_ARG, E_NOT_FOUND, E_RENDER, E_NOT_AVAILABLE, …`）+ 消息 + 有时 recovery hint。未识别的 feeling / behaviour override / feedback 收进 `notes` 忽略；真正的程序员错误（override 瞄准不存在的轴）**要 raise**。

---

### §5B —— Shape B 制作标准（宏库：从内容参数到交付物，含地板机制）

> 回答"Shape B 如何被制作、其地板如何变结构性"——旧文只有 §5A 的 Shape-A 机器，Shape B 的地板无处落地。本节补齐。

#### §5B.1 宏体即制作单元
一个 Shape-B 条目是一个纯 `(**params)->list[op_dict]` 宏（§4.3-FORM）。它的"制作"= 在宏体内把该域的构图/读序/间距/色彩手艺**烘焙进发出的 ops**，由 archetype/preset 输入驱动，**绝不**把这些手艺决策以"默认失效"的可选参数暴露给模型。

#### §5B.2 Shape-B 地板机制（P1 对 Shape B 的落地）`(← P1)`
在没有 choreography 层时，Shape B 的地板由以下**结构**强制，逐条可测：
1. **手艺烘焙进宏体**：承载品味的量（间距节律、读序/焦点、留白比例、层级对比、色彩关系）在宏体内**由 theme + archetype 计算得出**，不作为模型可选参数（§0.6 泄漏判据 `test_no_floor_disabling_default`【待落地】把关）。
2. **archetype 驱动的重塑**：一个 preset/archetype 词重塑整条 sub-op 序列（P3）。
3. **从 theme 构建，无硬编码**：颜色/坐标/字阶从共享 theme 派生（AP24 门），使一处 restyle 全局生效、9:16 不崩。
4. **静态/内容型 overlay 的地板断言集**（写入 §8"地板断言"）：例——从 theme 取色（无硬编码 hex）、`nx/ny` + type scale 随 canvas、（element）corner-alpha ≤ 4 的 overlay 不变量、prefix 唯一。**若 Shape-B 库属 TIME 层域**（§2.3 已要求这类库通常升 A；若坚持 B）则必须宏内内嵌等价 choreography 并过 §5A.5 的 motion 断言。
5. **G1 对 Shape-B 可运行**：property fuzz（`expand_*(name, {})` 与随机 in-band content params）→ 断言该库声明的地板断言集全过。`test_<lib>_taste_floor_property.py`【待落地】参数化 over `sorted(REGISTRY)`。

> **诚实边界**：若一个 Shape-B 条目只是"把东西放某处"的**放置宏**（badge/arrow/progress_bar），其"地板"合法地是"theme 派生 + overlay 不变量 + prefix 唯一"这一**较弱但真实**的集合；它是**便利宏**而非完整手艺闭包，§8 须如实声明其地板层级，不得假装拥有 motion 级 choreography 地板。

#### §5B.3 纯度、注册、注入、参数化测试
- 宏纯函数（§4.3-FORM），expansion 只在 apply op 的 `_dispatch`。
- 两处注册（`REGISTRY` + `CATALOG`）、`__all__`、单 `apply_<lib>`、单 `describe_<lib>` 注入。
- 测试参数化 over `sorted(REGISTRY)`：新条目自动被全契约覆盖（`test_expands_to_valid_ops`/`test_applies_and_renders`【已存在】）。

#### §5B.4 确定性 by construction `(← P5)`
纯宏 + `prefix` 命名空间化 id ⇒ 无 RNG、无 seed 亦逐字节确定；§8"seed 来源"填 `N/A（by-construction）`；子进程 hash-seed 门（§5.0/P5）同样适用以防偶发 set-order 依赖。

---

### §5T —— TRANSFORM 制作标准（变换既有 asset，如 grading/stabilize/retime）

> grading 无 IR、无 behaviours、无 choreography、无 phase arc、无合成——它把 LUT/curve 施加到既有像素。§5A 完全不适用。此支为其而设。

#### §5T.1 输入与非破坏栈
- **主输入 = `target asset_id`**（要变换的 clip/layer）。
- 变换以**非破坏的 param / node 栈**表达（如 grading：`primary → secondary → qualifier`），可关可调、可 undo；绝不烧进源像素。

#### §5T.2 语义→变换参数唯一表 `(← P2)`
创作意图（语义 grading 轴：温度、对比、film-look 强度…）在**恰好一处**映射到低层变换参数（LUT/lift-gamma-gain/tone-curve 系数），单测该表。模型面**永不**给裸曲线点/裸 LUT 索引。

#### §5T.3 地板机制（TRANSFORM 域的断言集）`(← P1)`
grading 域的地板由**色度守恒断言**强制（写入 §8"地板断言"）：黑/白点不 clip、skin-tone 落肤色线、全局饱和 ≤ cap、跨镜 ΔK ≤ tolerance、拒绝无脑 teal-orange。property fuzz over brief → 断言全过（`test_grading_taste_floor_property`【待落地】示例）。

#### §5T.4 measurement readback digest + 像素级验证 `(← P10)`
- create/apply 回**测量 digest**（scopes/waveform/histogram/统计 + 缩略 asset_id），是 agent 读的中间产物——**不是**整帧像素转储（对齐 AP11 的收窄：禁的是"把无结构原始 render 当推理面"，允许结构化 readback）。
- **像素级验证合法且必要**：grading 手艺**本就在像素上判定**（肤色对不对、高光有没有 clip）。P10 的 `next` 指针指向 `render`/scope 抽帧，模型经此**看**像素做验证——这与 AP11 不冲突（AP11 禁的是把像素当**推理**面，不禁把像素当**验证**面）。

#### §5T.5 单后端诚实降级（§5A.2 的 TRANSFORM 替代）`(← P8)`
TRANSFORM 库常单后端（ffmpeg/PIL），无多后端能力梯度、无 honored/dropped 可报。其诚实义务改为：**报告 clamp / 越 gamut / 不可表示的值**，而非静默 clip。`test_grading_reports_out_of_gamut`【待落地】。

#### §5T.6 validate-before-mutate + 复用既有 render `(← P8/P10)`
变换 node 校验后再挂上目标 asset；施加是可 undo 的单一操作；**复用既有 color pipeline，永不新增 renderer**（P8/§2.5）。

---

### GATE 1 —— 制作门（必须全绿方可进入安装；每项标注适用类别）
- [ ] **[A]** 分层依赖严格向下，唯一 director 看到一切；`importlinter` 门绿。`(← P8)`
- [ ] **[A，有多后端 IR]** IR 纯 dict、比最弱后端更富、adapter 诚实 `AdapterReport(honored/dropped)`。`(← P8)`
- [ ] **[A/T，有语义轴]** 语义→数字**只在一处**（`ResolvedParams` / grading 变换表）且有 mapping-table 单测。`(← P2)`
- [ ] **[全]** registry + 生成 catalog + drift-pin 测试到位。`(← P3)`
- [ ] **[全]** 品味地板 property-fuzz + **本域声明的地板断言集**可测（motion/grading/type/overlay 各异），且已过一轮对抗评审硬化（断言变永久 golden）；反平凡 meta-check 绿。`(← P1/P12)`
- [ ] **[全]** 确定性：含 RNG 库同 brief+seed ⇒ 字节相同；by-construction 库无 seed 亦字节相同；子进程 hash-seed 门 + 静态 no-wallclock 门绿。`(← P5)`
- [ ] **[全]** 接口满足 §4 全部相关条款（Shape A：verbatim round-trip、`next`、主输入外全可选；Shape B：FORM 契约 + 适用 profile）。`(← P4/P9/P10)`
- [ ] **[全]** validate-before-mutate + 单原子 patch；ADD-ONLY，自身不渲染、不新增 render capability。`(← P8/P10)`
- [ ] **[T]** measurement readback digest + 像素/示波验证路径 + 越 gamut 报告。`(← P10)`

---

## §6 安装标准（build == install，机器校验的逐点清单）

> **最高原则**：make 与 install 是**同一次行为**，须在同一 PR。库"未 install 到 live"= 未完成工作。每个 install 点被 coverage/drift 测试枚举，缺任一点即**红 build**（＝本地 acceptance run 失败）。`(← P11)`
> **生效说明**：本节的两层 meta-check 当前 【待落地】；在其提交转绿前，安装债只由现存的 plan-mode / catalog_coverage 门部分兜住（且二者对"未进 schema"盲，见 §6.1）。见 §13。

### §6.1 为什么必须机器校验（失败模式 1 的根因）
- 一致性门（`plan_mode` coverage、`tool_router` `catalog_coverage`、DISPATCHER 推导）全部对着 `TOOL_NAMES` 计算——一个**从未进 schema** 的库对它们全部隐形。
- 只有两个门**响亮失败**：plan-mode 分类、tool-router pack 归属；**且二者只在 A2（schema 注册）已存在后才 fire**（它们迭代 `TOOL_NAMES`）。其余（DISPATCHER 接线、budget cost、prompt prose、routing keywords）**静默降级到安全但错误的默认**。
- fail-closed 只保护被门的点：缺 dispatcher → stub 仅在模型调用时 raise；缺 budget row → 静默 `$0`。
- 这正是 `vector_motion` 如何"built but not installed"：引擎存在，但 schema/dispatcher/budget/prose 接线被跳过，两个响亮门在 schema 名存在**之前**根本不触发。**Layer B（§6.3）是唯一能在这个阶段抓住它的门，且必须自动发现、不靠人维护清单。**

### §6.2 Shape A 安装清单（9 点，按顺序全部触碰）

| # | 文件 | 必须做什么（真实字段名） | 现状把关 | 机器校验? |
|---|---|---|---|---|
| A1 | `gemia/tools/<verb>.py` | 实现 `async def dispatch(...)`；不吞错；op 白名单 `_OPS`、`_err()` 码、validate-before-mutate、单原子 patch、compact digest + `next`、brief-canvas 默认、adjust 保留用户键 | **SILENT**：仅库自写测试覆盖 | ✗（由 meta-check 间接） |
| A2 | `gemia/tools/_schema.py` | **根注册**：向 `TOOL_SCHEMAS` append `_tool(...)`；`TOOL_NAMES` 由其派生（`:1914`） | **SILENT（最深缺口）**：无门能强制"未添加"；下游 exact-coverage 只在 name 出现**后**才触发 | ✗（须 Layer B manifest，且须自动发现） |
| A3 | `gemia/tools/__init__.py` | `from … import <verb>` + `_REAL["<verb>"]=…dispatch`；`DISPATCHER = {name: _REAL.get(name) or _make_stub(name) …}`（`:222`） | **SILENT**：漏行 → `_make_stub`，仅运行期 `NotImplementedError`（`:103-109`） | ✗→由 Layer A 转 ✓ |
| A4 | `gemia/budget_guard.py` | `_TOOL_COSTS` 加 `"<verb>": {"usd":.., "eta_sec":..}`；**并改 `estimate()` 对 miss 抛错/返回 sentinel，不再默认 `(0.0,5.0)`** | **SILENT→拟改 LOUD**：现 `estimate()` miss 返回默认（`:177`）→ verb 以 $0 溜过 | ✗→由 Layer A(b) + estimate-raise 转 ✓ |
| A5 | `gemia/plan_mode.py` | 恰好归入 `PLAN_ALLOWED_TOOLS` 或 `PLAN_BLOCKED_TOOLS` | **LOUD GATE（条件于 A2）**：`test_plan_mode.py:40`（disjoint 且并集==`TOOL_NAMES`）；`is_plan_safe` fail-closed | ✓（**仅 A2 存在后**） |
| A6 | `gemia/tool_router.py` | 加入 ≥1 个 `TOOL_PACKS` pack；理想再加 `WORKFLOW_KEYWORDS`/`ADJACENT_PACKS` | **LOUD GATE（pack 归属，条件于 A2）**：`catalog_coverage()==(∅,∅)`；`tests/test_tool_router.py:30`（keywords 半边非 exact） | ✓（pack 部分，**仅 A2 存在后**） |
| A7 | `tests/test_tool_router.py` | **【已被 §6.3 取代】** 旧 `test_catalog_exactly_covers_current_105_tool_schemas`（`:26-28`）的魔数计数由 Layer A 的 `len==len(set)` + 覆盖断言替换；**不再维护 `==105` 魔数** | — | ✓（并入 Layer A） |
| A8 | `gemia/prompts/system_v3.md` | 加 prose 块教模型何时/如何用（如 `### Vector motion design (vector_motion)`，`:431`） | **拟改 HARD**：omit → 模型可能永不发现、退回 primitives（正是"手推 keyframe"故障）——发现性属"已安装"，非可选 | ✗→**HARD ✓（带 `PROSE_EXEMPT` 白名单）** |
| A9 | `tests/test_<verb>_tool.py` | 专用测试：DISPATCHER 注册 + 非 stub、plan-mode block、显式 budget row、`next`+asset、typed/recoverable 错误、行为 | **SILENT**：无门强制该文件存在 | ✗→由 Layer A/B 取代其兜底职责 |

**Shape A schema 约定（钉死）**：dispatcher 签名固定、不得吞错；跨工具边界一律 `asset_id` 字符串非路径；plan-mode 分类对 `TOOL_NAMES` 二元穷尽；每 verb 必属 ≥1 pack。**§6.2 表的 A5/A6"机器校验✓"均须读作"✓（条件于 A2 已注册）"——它们不是"未进 schema"这一安装债的兜底，那由 Layer B 承担。**

### §6.3 Shape A 强制上线的两层 meta-check（**已落地并对抗验证 · 【已存在】**；两项精化仍【待落地】）—— 让"跳过任一点即失败"的单一门 `(← P11)`

> **落地状态（2026-07-15）**：Layer A（`tests/test_tool_catalog_contract.py`）与 Layer B（`tests/test_library_verb_manifest.py`）已实现、当前 105 工具全绿、且已**对抗验证**（孤儿 `dispatch` 模块 → Layer B RED；漏 pack/plan/budget 的 verb → Layer A 各条 RED）。**这是三失败模式中第一个真正落地的强制门。** 两项文档化精化**有意暂缓**、降级为 §8 人工门，理由见下。

**Layer A —— 一个参数化"full-install" drift 测试（【已存在】）**
`tests/test_tool_catalog_contract.py`（拆成多条断言函数），迭代 `gemia.tools._schema.TOOL_NAMES`，对每个 `name` 断言：
- (a) **【已存在 · HARD】** `not DISPATCHER[name].__name__.startswith('stub_')`，除非在显式 `INTENTIONAL_STUBS` 白名单（今为空）；
- (b) **【已存在 · HARD】** `name in budget_guard._TOOL_COSTS`，除非在有界白名单 `BUDGET_DEFAULT_TOOLS`（今 12 个走默认成本的近零读类/host verb）；
- (c) **【已存在 · HARD】** `catalog_coverage()==(∅,∅)`（吸收 `TOOL_PACKS` 覆盖）；
- (d) **【已存在 · HARD】** `name in (PLAN_ALLOWED_TOOLS ^ PLAN_BLOCKED_TOOLS)`（恰属一类，吸收 `plan_mode`）；
- (e) **【待落地 · 有意暂缓】** prose HARD（`name` 须现于 `system_v3.md`）**未采纳**：实证当前 44 个工具未单独入 prose，HARD 化需一张大而噪的 `PROSE_EXEMPT` 豁免表，信噪比低——降级为 §8 每库人工门（"prose 指引已写"打勾）。
并已把脆弱的 `len(...) == 105` 换成 `len == len(set)`（`test_tool_names_are_unique_no_magic_count`），门无需魔数即可扩展。**跳过 A3/A5/A6 任一点即 fail；A4（budget）漏行且未白名单即 fail。**
- **白名单纪律【已存在】**：`test_install_whitelists_are_bounded` 断言 `BUDGET_DEFAULT_TOOLS`/`INTENTIONAL_STUBS` 无游离条目（必是真 verb），防止"塞进白名单静默过门"。（`estimate()` raise-on-miss 的双保险仍【待落地】——当前靠 (b) 的显式行断言兜住。）

**Layer B —— 自动发现式 manifest 测试（【已存在】；唯一能在"built but not installed"阶段抓住 A2 的门）**
`tests/test_library_verb_manifest.py`：**机械派生**期望作为 verb 上线的点库，**不靠人维护清单**——扫描 `gemia/tools/*.py`，凡定义了**模块级 `async def dispatch`**（单-verb 工具约定，`vector_motion.py` 即此形）却**未接进 `DISPATCHER`** 者即判"孤儿=造好没装"→ RED（跳过 `.`/`_` 开头文件与外置盘 AppleDouble `._*.py` 资源叉）。**无 `LIBRARY_VERB_MANIFEST` 手维护清单逃生舱**——期望集从文件系统派生。刻意豁免用有界 `NON_VERB_DISPATCH_MODULES`（今为空），`test_non_verb_whitelist_is_bounded` 守其无游离项。
- 已验证：造 `gemia/tools/zzprobe_orphan.py`（带裸 `dispatch`）→ 测试 RED（`assert not ['zzprobe_orphan']`），删除后复绿。**"这应该是个 verb"的触发从代码可观察，绝不靠人脑清单。**

### §6.4 Shape B 安装清单（"加条目"只碰 B1/B2；建"新库"才碰 B4/B5/B6）

| # | 文件 | 加**条目**时 | 建**全新库**时（额外） | 强制测试 |
|---|---|---|---|---|
| B1 | `lumenframe/<lib>/<name>.py` | 纯宏 `def <name>(*, ...content, <本库 SHARED_PARAMS...>) -> list[op_dict]`；从 theme/表构建；emit 本库 op-emission 面；id 带 prefix；（绘图 profile）color 默认 accent、element 不 emit 满帧背景 | — | `test_expands_to_valid_ops`、`test_applies_and_renders`（参数化 over registry）、地板 property、（绘图）overlay 门、color round-trip |
| B2 | `lumenframe/<lib>/__init__.py`（`REGISTRY`+`CATALOG`；镜像 `templates/__init__.py`） | import 模块；`REGISTRY["<name>"]=<name>` **且** `CATALOG` 加 `{name,category,summary,params(content-only,'*'=仅信息性),example{<applyarg>:<name>,params:{…}}}`；加 `__all__`。**这就是全部注册** | — | `test_every_<lib>_has_a_catalog_entry_and_vice_versa`、`test_every_entry_has_the_required_fields`、`test_catalog_is_ordered_by_category`、`test_documented_params_exist_on_the_function`（均**已存在**于 element/component 套件） |
| B3 | `lumenframe/<lib>/theme.py`（或共享 `templates/theme.py`） | 多数条目**零改动**，只消费 | 仅当引入真正新原子（palette role / type role / 坐标 helper / 变换原子）才扩 `theme` 一次 | `test_norm_bridge_round_trips_centre` / `test_type_scale_tracks_height` / palette fallback（**已存在**） |
| B4 | `lumenframe/ops.py`（`apply_<lib>` op） | **不动** | 仅建新库：加**一个** `@register_op('apply_<lib>', source='core')`（镜像 `_op_apply_template :2046-2073` / `_op_apply_element :2076-2105`） | 未知名→E_ARG；params 非 dict→E_ARG；`catalog.py` `apply_<lib>` 条目 |
| B5 | `lumenframe/catalog.py`（`describe_ops` `:280-326`） | **不动** | 仅建新库：`describe_ops()` 内某 bracketed 标题下经 lazy-import `try/except` append `describe_<lib>()`，并加 `apply_<lib>` op catalog 条目 + **一行验证提示（P10 弱化契约）** | `test_injected_into_describe_ops`（`'[…library]' in describe_ops()`，`:186-192`，**已存在**） |
| B6 | `tests/test_lumenframe_<lib>_library.py` | **每条目不加**——参数化 over `sorted(REGISTRY)` | 仅建新库：克隆同胞测试模块（含地板 property） | 参数化套件本身（自安装覆盖） |

**Shape B 关键机器不变量**：`registered == documented`（无孤儿）、CATALOG 是 fresh copy（`:65-68`）、每个文档 param 是真签名 param、每条目 expand 成合法 op dict、经 apply op dispatch、渲染 frames `[0,1]` 到 canvas 尺寸 RGBA、两次 prefixed 盖章 id 唯一（`:145-153`）、`color` round-trip（`TestColour` `:159-170`）、`describe_*` 注入 `describe_ops`。**（绘图 profile）element overlay 门**：`test_element_is_an_overlay_not_a_background` 断言四角 alpha `<= 4`（`:119-135`）。`(← P8/P3/P9/P6)`

### §6.5 单一注入点 = 客户端天然对等 `(← P4/P8)`
`describe_ops` 是每个前端都注入的**单一函数**；web v3 与 CLI 渲染**同一** `describe_ops()` 字符串，功能对等是**结构性**的（满足 memory 规则「web v3 与 CLI 功能对等」by construction）。**澄清（修 G4 歧义）**：`describe_ops()` 是**单一共享注入函数**；**每个库**追加**恰好一段** bracketed 区块并拥有**恰好一个** `apply_<lib>` op。`templates` 与 `elements` 是**两个库共享机制**（各一段、各一 op），不是一个库两个面。禁止 per-client registration。

### §6.6 全局 token 唯一性（**现已必须**，非">1 库后再说"）`(← P3/P6)`
现网已有三个 model-facing namespace（`templates`、`elements`、`vector_motion`），触发条件**已满足**。`tests/test_global_token_uniqueness.py`【待落地】断言跨已注册库无碰撞（`ELEMENT_CATALOG` keys、`TEMPLATE_CATALOG` keys、vector catalog tokens、所有 registered op 名两两 disjoint），且**迭代 registries** 以自动覆盖未来库。所有 model-facing token 库-命名空间化。

### GATE 2 —— 安装门（必须全绿方可进入验收）
- [ ] Shape A：A1–A6、A8、A9 全部触碰；`test_tool_catalog_contract.py` Layer A + `test_library_verb_manifest.py` Layer B（自动发现）全绿；`test_plan_mode.py` / `catalog_coverage()==(∅,∅)` 全绿；`estimate()` 对 miss 抛错。
- [ ] Shape B：条目在 `REGISTRY` **且** `CATALOG`；catalog-symmetry 绿；（新库）`apply_<lib>` op + `describe_ops` 注入（含验证提示）绿。
- [ ] "schema-present but pack-absent" 会令 `catalog_coverage` 测试失败——**已验证不是靠 full-fallback 静默存活**。
- [ ] 全局 token uniqueness 绿（现已强制）。
- [ ] `INTENTIONAL_STUBS`/`PROSE_EXEMPT` 白名单有界（空或带 tracking ref）。
- [ ] build 与 install 在**同一个 PR**；无任何 install point 留空。`(← P11)`

---

## §7 验收门（pass/fail，GATE 3 的机器镜像）

一个库"达标"当且仅当以下门全部为 **PASS**。任一 FAIL = 红 build / 不合并。**`machine_checkable` 列诚实标注**：`true`=有自动测试；`partial`=测试+人评审混合；`false`=纯人评审/抽帧（不得当作自动门）。**`存在?` 列**标注强制测试当前是否已在仓中。

| 门 | PASS 判据 | 强制测试 | machine_checkable | 存在? | 守护原则 |
|---|---|---|---|---|---|
| G1 地板不可击穿 | brief 空间 property-fuzz，全部输出过**本域声明的地板断言集**（motion/grading/type/overlay 各异）；反平凡 meta-check | `test_<lib>_taste_floor_property` + `test_taste_floor_test_is_nontrivial` | true | 【待落地】 | P1 |
| G2 确定性 | 固定 brief 子进程 double-run 字节相同（生成 CONTENT 层面）；静态 no-wallclock 门 | `test_<lib>_determinism_subprocess`；现存 `test_build_scene_is_deterministic_per_seed`（`test_vector_creative.py:314`） | true | 部分【已存在】/新增【待落地】 | P5 |
| G3 创作语言纯净 | 手艺型模型面无原始几何/时间参数（Shape B placement 豁免） | `test_no_raw_numbers`（白名单式） | true | 【待落地】 | P2 |
| G4 单一 agent 面 | Shape A 恰 1 verb；Shape B 每库恰 1 段 describe 注入 + 1 个 apply op | install-coverage "one agent surface per library" | true | 【待落地】 | P4 |
| G5 目录不漂移 | registry keys == CATALOG keys == `describe_*()` 面；文档 param 都是真签名 param | Shape B `test_every_element_has_a_catalog_entry_and_vice_versa` / `test_documented_params_exist_on_the_function`；Shape A `describe_behaviors()` drift-pin | true | 【已存在】 | P3 |
| G6 逐字可寻址 + 完整可枚举 | 单 id 无截断、往返解析；每实体完整展示或经无损枚举 op 可达（含 host 显示面） | `test_<lib>_addressability_roundtrip` + `test_layer_tree_handles_round_trip` + `test_truncated_list_has_full_enumeration_path` | true | 【待落地】 | P9 |
| G7 可验证交付 + 可恢复错误 | Shape A：终态含 asset handle + `next`；Shape B：apply 回 layer 资产句柄 + describe 验证提示；错误 typed+recoverable | `test_success_carries_asset_and_next` + `test_errors_are_typed_and_recoverable` + host `test_recoverable_library_failure_degrades_to_partial` | partial（降级为 host-ledger 契约） | 【待落地】 | P10 |
| G8 build==install | 每个安装点被机器校验存在；缺则红 build | Layer A + Layer B（自动发现）+ `catalog_coverage()==(∅,∅)` | true | Layer A/B【待落地】；coverage【已存在】 | P11 |
| G9 对抗评审 | 地板已被对抗评审加固，断言已落为永久测试 + golden | 人评审门 + G1 golden | **false**（人评审） | 人工 | P1/P12 |
| G10 只关一个域 | 无手艺泄回模型（默认失效参数 proxy + 评审） | `test_no_floor_disabling_default` + PR 评审 | partial | proxy【待落地】 | P6 |
| G11 骑层不 fork | library PR 无新 render 代码，无新 render capability | grep 门 `test_no_new_render_symbols` + 评审 | partial | 【待落地】 | P8 |
| G12 真机验证 | 模型经 `next`/验证提示指向真实验证路径（`lumen_seek`/`render`/scope）看到资产真的过地板 | 抽帧 / 渲染可观察证据（不信任自检） | **false**（抽帧） | 人工 | P10 |
| G13 分层契约（新增） | 严格向下 import；仅 director 看全部 | `importlinter` `test_layering_import_contract` | true | 【待落地】 | P8/§5A.1 |
| G14 全局 token 唯一（新增） | 跨库 archetype/op/catalog key 无碰撞 | `test_global_token_uniqueness` | true | 【待落地】 | P3/P6 |
| G15 引用完整性（新增，meta） | §7/§12 引用的每个测试路径都解析到真实 test node | `tests/test_charter_integrity.py::test_charter_referenced_tests_exist` | true | 【已存在】 | §13 |

---

## §8 每库宪章模板 / 验收门（新库照填，逐项填满即可机械通过）

> 新建任一点库时，**先**在 PR 描述里（建议路径 `docs/<lib>-charter.md`）逐格填满本模板；填不满 = 未完成。CI 的 install-coverage / drift / golden 门是这张表的机器镜像。

```md
# 点库宪章卡：<library_name>  ·  namespace: <lib>_

## 1. 定义（GATE 0）
- 一句话：本库关闭的**唯一创作域** = ____（按 §0.6 定义；编舞 / easing / grading /
  kinetic-type / music-rhythm-edit / transition-edit-grammar / composition-framing / camera-movement …）
- 域**已完全闭合**（§0.6 泄漏判据）：潜在泄漏点及堵死方式 = ____
- 形态（§2 决策）：[ ] Shape A（顶层 verb，需独立 budget+plan+router 身份）
                  [ ] Shape B（纯 params->ops 目录，经既有 apply op；默认取此）
  主判据答案："需要独立工具身份（budget/plan/校验/create-adjust-catalog）吗？" = ____；破平局依据 = ____
- **类别（§0.7）**：[ ] SYNTHESIS（从 brief 合成）  [ ] TRANSFORM（变换既有 asset）
  → 走制作支线：[ ] §5A  [ ] §5B  [ ] §5T
- TIME 层域？= ____（是→按 §2.3 必 Shape A 或内嵌 choreography）
- 编码的手册：DESIGN.md 触及层 [ ] static [ ] TIME；定向手册 = ____（01-06）
      （时间维输出库必须勾 TIME 层；静态域库以 static 层满足 P7）
- **本域地板断言集**（§0.6/P1）= ____（motion: 无 linear/最小 stagger/focal/hold；
      grading: 黑白点不 clip/肤色线/饱和 cap/ΔK；type: dwell/measure/title-safe/orphan …）

## 2. 原则符合性（§3，逐条打勾 + 指向证据；参见 §3.0 适用性矩阵）
- [ ] P1 结构性品味地板：地板断言集 = ____ → golden `test_<lib>_taste_floor_holds` + property
- [ ] P2 创作语言无手艺原始数字：语义词汇 = ____；（Shape B）placement 豁免项 = ____；lint 通过
- [ ] P3 命名目录封闭 CATALOG：archetype 词（库命名空间、全局唯一、带 kind/label）= ____
- [ ] P4 一 agent 面 + 主输入外全可选：必填 = subject / target asset_id / (Shape B) 无 = ____
- [ ] P5 确定性：seed 来源 = ____（by-construction 库填 N/A）；子进程 double-run 测试名 = ____
- [ ] P6 只关一个域：无默认失效参数（`test_no_floor_disabling_default`）+ 评审确认
- [ ] P7 编码手册：引用手册 = ____；时间维库 TIME 层轨道 = ____（静态域填 static-only）
- [ ] P8 骑层不 fork、无新 render capability：emit 的 ops = ____；本 PR 无新 render 代码
- [ ] P9 逐字可寻址 + 完整可枚举：round-trip 测试名 = ____；列表摘要枚举路径 = ____
- [ ] P10 交付 + 可恢复错误：最终 asset handle = ____；Shape A `next` = ____ / Shape B 验证提示 = ____；
      错误码集 = ____（E_ARG/E_NOT_FOUND/E_RENDER/…）；typed+recoverable
- [ ] P11 build==install：Layer A + Layer B（自动发现）覆盖本库
- [ ] P12 设计正确/原创 + 尊重疯狂（原则性可扩展）：对抗评审门通过；novel-inband 测试名 = ____

## 3. 接口 schema（§4）
- Shape A：op 判别符 = create|adjust|catalog|____；主输入 = subject / target asset_id；
  create digest 字段 = layer id/name,start,duration,{svg_bytes|scope readback},plan/measurement digest,notes；
  `next` = ____；adjust 保留用户键（artifact-type 附录）= ____
- Shape B（FORM）：`REGISTRY` = ____；`*_CATALOG` = ____；本库 SHARED_PARAMS = ____；
  op-emission 面 = ____；apply op = apply_<lib>；describe 注入标题 = [____ library]；
  refinement 语义 = 删除+重 apply，prefix 稳定策略 = ____
  Shape B（绘图 profile，仅画 2D 矢量时）：px-from-centre / overlay corner-alpha≤4 / color round-trip

## 4. 制作（§5，按类别选支线）
- §5A（SYNTHESIS×A）：层图 IR→toolkit→behaviours→styles→params→choreography→api；
  语义轴（0..1）= ____ → ResolvedParams 单表；IR node.kind ____；TRACK_PROPS ____；
  AdapterReport；phase arc = ____；留白 hold 先切；validate-before-mutate + 单原子 patch
- §5B（Shape B）：手艺烘焙进宏体（无默认失效参数）；从 theme 构建；地板 property 参数化
- §5T（TRANSFORM）：target asset_id；非破坏 param/node 栈 = ____；语义→变换参数唯一表；
  measurement readback digest = ____；越 gamut 报告；像素/示波验证

## 5. 安装点（§6，逐点勾到"已注册 + 已被测试覆盖"）
- Shape A：[ ] A1 <verb>.py  [ ] A2 _schema.TOOL_SCHEMAS  [ ] A3 __init__._REAL
           [ ] A4 budget_guard._TOOL_COSTS(+estimate raise)  [ ] A5 plan_mode(ALLOWED/BLOCKED)
           [ ] A6 tool_router.TOOL_PACKS(+keywords)  [ ] A8 system_v3.md prose(HARD)
           [ ] A9 tests/test_<verb>_tool.py
           [ ] meta A: test_every_registered_verb_is_fully_installed 绿
           [ ] meta B: test_library_verb_manifest（自动发现）绿
- Shape B：[ ] B1 <name>.py 纯宏  [ ] B2 __init__ REGISTRY+CATALOG+__all__
           [ ] B3 theme.py（如需）  [ ] B4 ops.py apply_<lib>（仅全新库）
           [ ] B5 catalog.py describe_ops+验证提示（仅全新库）  [ ] B6 参数化测试克隆（仅全新库）
           [ ] registered==documented / params-exist / (绘图)overlay角α≤4 /
               prefix 唯一 / color round-trip / injected into describe_ops 全绿
- [ ] "schema 有但无 pack" / "dict 有但无 catalog" 会 CI 红（说明由哪个测试）= ____
- [ ] 全局 token uniqueness 绿

## 6. 验收门（§7，全 PASS 方可合并）
G1 __ G2 __ G3 __ G4 __ G5 __ G6 __ G7 __ G8 __ G9 __ G10 __ G11 __ G12 __ G13 __ G14 __ G15 __

## 7. 三失败模式自证（§10）
- [ ] 失败模式 1（installation debt）被 meta A+B 防住：填被覆盖的安装点 = ____
- [ ] 失败模式 2（addressability）被 round-trip（库 + host）防住：填测试名 = ____
- [ ] 失败模式 3（ledger）被 completion contract + error-taxonomy + host 降级测试防住：填测试名 = ____

## 8. 反模式自审（§9）
- [ ] 逐条对照 §9 反模式，声明本库均不触犯：____
```

---

## §9 反模式清单（每条 → 症状 → 预防规则 → 抓住它的检查；标注检查是否存在）

| # | 反模式 | 症状 | 预防规则（← 原则） | 抓住它的检查 |
|---|---|---|---|---|
| AP1 | Flat-tool proliferation | `create_vector`/`adjust_vector`/… 挤爆工具面 | 一 agent 面 + op 判别符 `(← P4)` | install-coverage "one agent surface per library"【待落地】 |
| AP2 | Taste-as-a-parameter | 库把 stagger/easing/留白/色度暴露成默认失效 param | 品味是不变量非 param；全可选默认自身承载手艺 `(← P1/P4/P6)` | `test_no_floor_disabling_default` + G1 property【待落地】 |
| AP3 | Raw-number briefs | agent/某层直接算手艺型 `x+=20`、`duration=0.4s` | 语义轴 + 唯一映射表 `(← P2)` | `test_no_raw_numbers`（白名单，Shape B placement 豁免）【待落地】 |
| AP4 | IR capped to weakest renderer | scene 只支持 opacity/位移，draw-on/morph 不可能 | IR 比最弱后端富；adapter 降级并报告（**仅多后端 IR 库**）`(← P8)` | IR 词汇测试 + `AdapterReport`【部分】 |
| AP5 | Silent degradation | 后端做不了 focal morph 悄悄换 fade | `AdapterReport.dropped`（多后端）/ 越 gamut 报告（单后端 TRANSFORM）`(← P8)` | AdapterReport / `test_grading_reports_out_of_gamut`【待落地】 |
| AP6 | Nondeterministic generation | module-level random/wall-clock/env，同 brief 出不同结果 | 不变量普适；机制（seeded RNG + reset_ids）仅含随机性库 `(← P5)` | 子进程 double-run + 静态 no-wallclock 门【待落地】 |
| AP7 | Output-string surgery on feedback | regex 编辑生成 SVG 处理反馈，artifact 脱同步 | adjust=feedback→语义 delta→同 seed 重推 `(← P5)` | `adjust` 保 layer id + `scene_signature` 相等【部分】 |
| AP8 | Catalog / doc drift | agent 面词汇与实现不匹配 | 从 registry 生成 catalog/`describe_*()` 并 drift-pin `(← P3)` | G5 覆盖测试【已存在】 |
| AP9 | Upward / cross-layer imports | `behaviours` import `styles` 等 | 严格向下；仅顶层 `api` 看全部 `(← P8)` | **`importlinter` 契约门**（本次落地，不再是 machine-checked=false）【待落地】 |
| AP10 | Mutate-then-validate | 畸形 scene 先写入毒化后续 render | validate-before-mutate + 单原子 patch `(← P10)` | `_create` 先 `validate_html_layer` 再 patch【部分】 |
| AP11 | Agent reasons over raw render | 工具回整帧 SVG/mp4 当推理面 | 回 explainable digest（plan / **measurement readback**）+ `next`；**像素仅作验证面不作推理面** `(← P2/P10)` | `create` 返回 digest 非整帧断言【部分】 |
| AP12 | Re-implementing render/composite | 新模块自带 renderer/cache/mp4 | ADD-ONLY，emit 宿主 artifact，永不 render，不新增 render capability `(← P8)` | grep 门 `test_no_new_render_symbols`【待落地】 |
| AP13 | Fatal on near-right brief | 一个未识别输入就 raise 丢整个合法 brief | 未知创作输入收进 `notes` 继续；硬错只留机器供给错误 `(← P10/P12)` | `params.resolve` 未知轴 raise、未知 feeling 收集测试【部分】 |
| **AP14** | **Built but not installed（安装债）** | 全建好躺分支，schema 缺 pack，live 手推 keyframe 造假 | build==install 原子 + meta Layer A/B（自动发现）`(← P11)` | `test_tool_catalog_contract.py` + `test_library_verb_manifest.py`**【已存在，已对抗验证】**【失败模式 1】 |
| AP15 | Silent stub dispatch | schema 加了忘 `_REAL`，DISPATCHER 落 stub | drift 断言 `DISPATCHER[name].__name__` 非 `stub_`（除有界 `INTENTIONAL_STUBS`）`(← P11)` | meta Layer A (a)【待落地】 |
| AP16 | Default-priced verb | 无 `_TOOL_COSTS` 行，贵 verb 估 $0 溜过 | drift 断言 `name in _TOOL_COSTS` **+ `estimate()` raise-on-miss** `(← P11)` | meta Layer A (b) + estimate 抛错【待落地】 |
| AP17 | Silent router omission | 工具在 schema 不在任何 pack，只 full-fallback 现身 | `catalog_coverage` FAIL `(← P11)` | `test_tool_router.py:30` `catalog_coverage()==(∅,∅)`【已存在】 |
| AP18 | Prompt-prose drift | verb 接线了但缺席 `system_v3.md`，模型永不偏好 | **HARD 覆盖**：每 verb（或有界 `PROSE_EXEMPT`）出现在 `system_v3.md` `(← P11)` | meta Layer A (e) **HARD**【待落地】 |
| **AP19** | **Display/Argument mismatch** | 模型复制被截断的 id，`E_NOT_FOUND` 后 flail/造假 | 逐字可寻址 (a)；人类缩写仅在完整 handle 同时在场时允许 `(← P9)` | 库+host round-trip【待落地】【失败模式 2】 |
| **AP19b** | **Incomplete enumeration** | 只展示部分实体、无枚举路径，其余不可寻址 | 完整可枚举 (b)：列表摘要须配无损枚举 op `(← P9)` | `test_truncated_list_has_full_enumeration_path`【待落地】 |
| **AP20** | **Ledger-hostile failure** | 未解决失败硬报 ledger incomplete，丢诚实部分答案 | 可验证交付物 + 可恢复错误；降级是 host-ledger 契约库只喂 `(← P10)` | completion contract + error-taxonomy + host 降级测试【待落地】【失败模式 3】 |
| AP21 | Entry mutates doc / calls ops directly | catalog 函数戳 doc 或自调别 op verb | 条目只返回 ops 且纯；expansion 经 apply op `_dispatch` `(← P8)` | 纯度测试 + 无直接 dispatch【部分】 |
| AP22 | Element paints background | "element" 填满 canvas 抹掉底下镜头 | 必为 overlay，corner-alpha 门（**仅绘图 profile**）`(← P6)` | `test_element_is_an_overlay_not_a_background`（角 α≤4）【已存在】 |
| AP23 | Per-client registration | 库分别接进 web 与 CLI，二者漂移 | 单一 `describe_ops()` 注入，客户端渲染同一字符串 `(← P4/P8)` | `test_injected_into_describe_ops` + 对等 by construction【已存在】 |
| AP24 | Hardcoded colours/coords per entry | 条目内嵌 hex/绝对 px，restyle 崩、9:16 崩 | 从共享 theme 构建，一 palette 名重塑，`nx/ny`+type scale `(← P3/P8)` | theme round-trip / type-scale 测试【已存在】 |
| AP25 | Id clash on repeat stamp | 同条目盖两次重复 layer id | 每条目用 `prefix` 命名空间化 id `(← P9)` | prefix-uniqueness-across-two-stamps（`:145-153`）【已存在】 |
| AP26 | One op verb per entry | 每 template/element 各得 `@register_op`，op 爆炸 | 每库一个通用 `apply_<lib>`；条目是数据 `(← P4)` | describe_ops symmetry + 单 apply op【已存在】 |
| AP27 | Token/Namespace collision | 两库同 archetype 词/op 名/catalog key 相互 shadow | 所有模型面 token 库命名空间化 + 全局唯一 `(← P3/P6)` | `test_global_token_uniqueness`（**现已必须**）【待落地】 |
| AP28 | Copy-craft convenience | 硬编码方便克隆参照外观，发往每次未来调用 | 设计正确 > 方便；借鉴不抄袭 `(← P12)` | 对抗评审门 G9（人评审）【人工】 |
| AP29 | Prose-only craft | 手艺只活在 system-prompt/`SKILL.md`，模型可忽略 | 结构性编码手册：advisory prose 变绑定库结构 `(← P7)` | 时间维库 TIME 层输出断言 / 静态域 static 层断言 + G10【待落地】 |
| AP30 | Wrong-shape proliferation | recipe catalog 建成一堆扁平 Shape-A；或新 verb 硬塞 Shape B 无法 budget/plan-gate | 形态决策（主判据=工具身份；TIME 域必 A）+ 一库一 agent 面 `(← P4/P8)` | §2 决策 + install-coverage【待落地】 |
| **AP31** | **Overfit pipeline on transform lib**（新增） | 硬把 IR/behaviours/choreography/phase-arc 套到 grading，GATE 1 误挡合法变换库 | 类别轴（SYNTHESIS/TRANSFORM）；GATE 1 checkbox 按类别守卫；TRANSFORM 走 §5T `(← §0.7)` | GATE 1 类别标注 + §5T 门【待落地】 |
| **AP32** | **TIME-mandate false-negative**（新增） | 静态 grading 库因无 motion track 被 P7 误判失败 | TIME 层仅对时间维输出强制；静态域以 static 层满足 `(← P7)` | `test_<lib>_emits_time_layer` 仅对时间维库 fire【待落地】 |
| **AP33** | **Vaporware gate**（新增，元反模式） | 宪章引用不存在的测试并盖 ✅，把 aspiration 当 enforcement | 引用完整性 meta 门 + DRAFT 生效纪律 `(← §13)` | `tests/test_charter_integrity.py::test_charter_referenced_tests_exist`【已存在】 |
| **AP34** | **Craft smuggled via skill**（新增 v1.3） | 手艺数字经 `save_skill` 蒸馏成技能，绕过库地板长期存活（`skills_v2/电影调色.json` 式失败） | 技能层边界 S1/S3：已关闭域的 craft 配方在保存入口 typed 拒存 `(← §14)` | `tests/test_lus_format.py` craft 三测试 + `tests/test_skill_distill.py` save 端拒存【已存在】 |

---

## §10 三大历史失败模式 → 预防规则（可证明防住；**当前状态诚实标注**）

> 依据本会话三条真实失败模式。每条给出「根因 → 守护原则 → 使其成为红 build 的确切检查 → **当前状态**」。**注意**：多数确切检查 【待落地】；在其转绿前，本节结论为**目标态**而非**已达态**（见 §13）。

### 失败模式 1 · INSTALLATION DEBT（安装债）
**事实**：`vector_motion` 全建好（engine + 163 tests，已过对抗评审）却躺在孤立分支——注册点 + router pack 被留作 "merge debt"，从未装进 live；live 模型只能**手推 keyframe** 造假。根因：`make` 与 `install` 可分离，且安装未被机器强制。
**守护原则**：P11（build==install）+ P4（骑层/单面）。
**确切检查**：
- **Layer A** `tests/test_tool_catalog_contract.py`**【已存在】**：缺 dispatcher（非stub）/ budget（行或白名单）/ plan（恰一类）/ pack 任一即 RED（覆盖 A3/A4/A5/A6）。prose HARD（A8）有意暂缓为 §8 人工门（44 工具未入 prose，见 §6.3-(e)）。
- **Layer B（自动发现）** `tests/test_library_verb_manifest.py`**【已存在】**：扫 `gemia/tools/*.py` 的模块级 `dispatch`，未接进 `DISPATCHER` 即 RED——唯一能在"连 schema 都没加"阶段抓住它的门。
- `catalog_coverage()==(∅,∅)`【已存在】；`len==len(set)` 换掉魔数【已存在】；`estimate()` raise-on-miss 双保险仍【待落地】（当前靠 Layer A-(b) 显式行断言兜住）。
**当前状态**：**已落地并对抗验证——失败模式 1 现已被强制防住。** 负向证据：孤儿 `dispatch` 模块 → Layer B RED；漏 pack/plan/budget 的 verb → Layer A 各条 RED。**这是三失败模式中第一个从"目标态"转为"已达态"的门。**（失败模式 2、3 的门仍【待落地】，故 §13 整体生效尚未满足。）

### 失败模式 2 · TOOL-HONESTY / ADDRESSABILITY（工具不诚实 / 可寻址性）
**事实**：host 给模型看的 layer-tree 把 layer id 截到 12 字符（`shape_1571eb06a035 → shape_1571eb`），模型复制的每次 delete/edit 都 `E_NOT_FOUND`。根因：**展示**的不是可逐字用作参数的形式；且截断在 **host 显示层**（`v3_routes.py`/`layer.py`），在"库展示"范围之外——库完美仍会经 host 路径复现。
**守护原则**：P9（逐字可寻址 + 完整可枚举，作用域含 host 显示面）。
**确切检查**：
- 库级 round-trip `test_<lib>_addressability_roundtrip`【待落地】：每 emitted handle 喂回解析、无 `E_NOT_FOUND`、单 id 无 `…`。
- **host 级** round-trip `test_layer_tree_handles_round_trip`【待落地】：对 host layer-tree/`lumen_seek` 展示 id 喂回——截断的 12 字符 id 无法解析 → RED。
- 列表完整性 `test_truncated_list_has_full_enumeration_path`【待落地】：per-id 截断禁止，list 摘要须配枚举 op。
**当前状态**：**部分已达态。** `tests/test_addressability_roundtrip.py`【已存在】落地 host 级 round-trip——对含长 id（>12 字符）的文档渲染 `_compact_tree_summary`（同一函数喂 `get_lumenframe` 结果与提示注入）并经 `dispatch_get`→`dispatch_delete_layer` 全链，断言展示 id 逐字出现且可解析可删除；对抗验证：重新引入 `[:12]` 截断 → 全 id 缺失 → RED。列表级全量枚举测试仍待落地。

### 失败模式 3 · LEDGER INTERACTION（账本交互）
**历史事实**：turn-ledger 曾在编辑 turn 硬报 `host acceptance ledger remains incomplete`，阻断模型的诚实部分解释。根因是 host 把活动记录当成了 completion 裁判器；该控制流已移除，ledger 现在只保留工具事实和资产投影。
**守护原则**：P10 + §5A.11/§5T.4（结构化错误）。
**确切检查 + 归属澄清**：
- 库侧 `test_success_carries_asset_and_next` + `test_errors_are_typed_and_recoverable`【待落地】：终态成功含 asset handle + `next`（现网 `vector_motion.py:135,213` 已 emit 但**无测试**断言）；错误 typed+recoverable。
- **host 侧** `test_recoverable_library_failure_degrades_to_partial`【已存在】：证明 typed/recoverable 错误完整进入工具结果，模型的诚实部分答案被交付，host 不改写或追加 completion 判决。
**当前状态**：**已达态。** 库侧 `tests/test_library_ledger_contract.py`【已存在】断言 `vector_motion` 终态成功携 `layer_id`（asset handle）+ `next`（指向 `lumen_seek`/`lumen_render` 验证），且六类坏输入均返回 typed 结构化错误。host 侧回归证明：模型诚实说明被交付并自然 `turn_complete`，无 `completion_check`、`incomplete_goal` 或 `turn_wrapup`。

---

## §11 演进阶段：catalog 稳定、版本化、风格学习、同胞路线图

### §11.1 catalog 稳定与漂移防护
registry / catalog / `describe_ops` 三方永远同步（Shape B 由 catalog-symmetry 测试守护；Shape A 由 drift-pin + `catalog_coverage` 守护）。agent-facing 词汇 prose 由 registry **生成**，人类不得手维护。`(← P3)`

### §11.2 版本化
IR 携带 `version` 字段。新增词汇是**加法**：加进 registry+catalog 即可，op 数保持扁平——N 个 Shape-B 条目加 0 个新 op verb；只有全新 library-of-macros 才加恰好一个 `apply_<lib>`。破坏性 IR 变更须 bump `version` 并保留旧 adapter 的诚实降级路径。`(← P8)`

### §11.3 命名空间唯一性（**现已强制**）
所有 model-facing token 库-命名空间化；`test_global_token_uniqueness`【待落地】防止两库共用 archetype 词/op 名/catalog key。现网三 namespace 已触发该条件。`(← P3/P6)`

### §11.4 风格学习 / 手册再编码
新手册指导通过**扩 registry / theme** 进入库，从 prose 变结构；绝不作为 `SKILL.md` prose 停留在建议层。时间维库演进持续维护 static + TIME 两层；静态域库维护其 static 层编码。`(← P7)`

### §11.5 同胞库路线图（**状态更新 2026-07-17**：六个动词已安装；per-library §7/§8 门状态未逐一考察）
每个新库照 §8 模板建 + 按 §6 装 + 过 §7 全门，并共同触发 §6.3 Layer A/B 门与 §6.6 全局 token 唯一性。**形态判定已依 §2.3（TIME 域必 A）与 §0.7（类别）修正**。
> **诚实标注（v1.3）**：下表原为"route only，尚未建"。截至 2026-07-17，对应工具动词已**全部安装**（`gemia/tools/_schema.py` creative point libraries 节：`grade`/`kinetic_type`/`rhythm_edit`/`edit_grammar`/`compose`/`camera` 均在 `TOOL_NAMES`，另有参考实现 `vector_motion`）。**"已安装"≠"已过 §7 全门"**——各库的 §8 宪章卡与 per-library 验收门尚未逐一补齐，据 §13.1 纪律不得据本表判任一库"完成"。craft 守卫（§14）对这些域的覆盖状态见 `CRAFT_GUARD_PENDING_VERBS` 有界清单。

| 点库（动词已安装） | 关闭的域 | 类别 | 编码的手册 | 形态判定（修订） |
|---|---|---|---|---|
| `grade` | 调色（static 层） | **TRANSFORM** | 01-grading（preset/LUT/depth） | 若需新 color 原语→A（×TRANSFORM，走 §5T）；若纯组合既有 color-op→B×TRANSFORM |
| `kinetic_type` | 动态排版（**TIME 层**） | SYNTHESIS | 03-subtitle-typography + TIME 层 | **必 Shape A**（TIME 域，§2.3）；走 §5A + type 地板断言 |
| `rhythm_edit` | 音乐节奏剪辑（TIME 层） | 混合（读音频+合成剪辑） | 02-editing-rhythm + audiosync | **A**（读音频、新 query 能力，TIME 域） |
| `edit_grammar` | 转场剪辑语法（TIME 层） | SYNTHESIS | 02-editing-rhythm | **A 或 B-内嵌-choreography**（TIME 域，§2.3；不得裸 B 硬编码 easing） |
| `compose` | 构图取景（static 层） | SYNTHESIS/TRANSFORM 视用途 | 04-composition-camera | 倾向 B（静态 preset 走 static 层，P7 不要求 TIME） |
| `camera` | 运镜（**TIME 层**） | SYNTHESIS | 04-composition-camera + TIME 层 | **必 Shape A**（TIME 域，§2.3） |

### GATE 4 —— 演进门（每次改动点库都必须过）
- [ ] catalog-symmetry / drift-pin / `catalog_coverage` 仍绿（词汇未漂移）。
- [ ] 破坏性 IR 变更已 bump `version` 且保留降级路径。
- [ ] 全局 token uniqueness 绿（无跨库碰撞）。
- [ ] 任何 floor-defining 改动已更新 golden + 再过对抗评审门（无静默降级）。
- [ ] 新手册指导作为结构进入库，而非 prose。

---

## §12 相关文件与测试索引（绝对路径；**明确区分【已存在】/【待落地】**，供 meta-check 直接实现）

**Shape A 安装点**：`/Volumes/Extreme SSD/lumeri/gemia/tools/_schema.py`（`_tool→TOOL_SCHEMAS`，`TOOL_NAMES :1914`）· `gemia/tools/__init__.py`（`_REAL`/`DISPATCHER :222`，`_make_stub :103-109`）· `gemia/budget_guard.py`（`_TOOL_COSTS`，默认 `(0.0,5.0) :177` — **拟改 raise-on-miss**）· `gemia/plan_mode.py`（`PLAN_ALLOWED_TOOLS`/`PLAN_BLOCKED_TOOLS`，`is_plan_safe`）· `gemia/tool_router.py`（`TOOL_PACKS`/`WORKFLOW_KEYWORDS`/`ADJACENT_PACKS`/`catalog_coverage()`）· `gemia/prompts/system_v3.md`（`:431`）
**Shape A 门测试 — 已存在**：`tests/test_plan_mode.py::test_every_registered_tool_is_classified`（`:40`）· `tests/test_tool_router.py`（`catalog_coverage()==(∅,∅) :30`）· `tests/test_vector_motion_tool.py`（`:95-124`，`test_vector_motion_has_explicit_budget_entry :113`；坏 id `E_NOT_FOUND :294`）· `tests/test_vector_creative.py::test_build_scene_is_deterministic_per_seed`（`:314`，`svg1==svg2`）· `tests/test_v3_predelivery_gate.py::test_visual_output_does_not_trigger_a_synthetic_review_round` · `tests/test_v3_completion_gate.py::test_open_activity_record_cannot_override_model_stop`
**Shape A 安装门 — 已落地（2026-07-15，已对抗验证）**：`tests/test_tool_catalog_contract.py`（Layer A：dispatcher非stub / router-pack / plan恰一类 / budget行或`BUDGET_DEFAULT_TOOLS`白名单 / `len==len(set)` / 白名单有界）· `tests/test_library_verb_manifest.py`（Layer B：扫 `gemia/tools/*.py` 模块级 `dispatch` 孤儿）
**FM2/FM3/meta 门 — 已落地（2026-07-15，已对抗验证）**：`tests/test_addressability_roundtrip.py`（FM2 可寻址 round-trip：`_compact_tree_summary` 长 id 逐字 + `dispatch_get`→`dispatch_delete_layer` 全链）· `tests/test_library_ledger_contract.py`（FM3 库侧：`vector_motion` 成功带 `layer_id`+`next`、六类 typed 错误、`recovery:'fix_args'`）· `tests/test_v3_ledger_partial_disclosure.py`（FM3 host 侧：`_FailsThenExplainsHonestly` 驱动真实 loop，诚实部分被交付，host 无 completion verdict）· `tests/test_charter_integrity.py`（§13.1(5)：宪章标【已存在】的测试文件必真存在 + 五项生效门文件存在）
**Shape A 门测试 — 待落地（TO BE CREATED）**：`tests/test_<verb>_tool.py::{test_success_carries_asset_and_next, test_errors_are_typed_and_recoverable}` · host `test_recoverable_library_failure_degrades_to_partial` · `test_<lib>_addressability_roundtrip` · `test_layer_tree_handles_round_trip` · `test_truncated_list_has_full_enumeration_path` · `test_no_raw_numbers` · `test_no_floor_disabling_default` · `test_<lib>_taste_floor_property` + `test_taste_floor_test_is_nontrivial` · `test_<lib>_determinism_subprocess` + `test_no_wallclock_no_env_static` · `test_no_new_render_symbols` · `test_layering_import_contract`（importlinter）· `test_global_token_uniqueness` · `test_novel_inband_brief_yields_distinct_behaviour` · `test_stub_and_prose_whitelists_are_bounded`
**Shape A 引擎参考实现（`lumenframe/vector/*`）**：`api.py`（唯一 director、`random.Random(seed)`+`reset_ids`、打分表、plan 组装、`adjust_scene`；`next` emit `:135,213`）· `params.py`（`ResolvedParams` 七轴表、`resolve()` 守卫）· `behaviors/__init__.py`（`@behavior`、`BEHAVIORS`/`BEHAVIOR_CATALOG`、`describe_behaviors()`、`apply_behavior`）· `scene.py`（IR、`TRACK_PROPS`、`validate_scene`、`threading.local` id 计数器、`scene_signature`）· `svg.py`（render-safety）· `render.py`（`scene_to_html_layer`、`validate_html_layer`、`AdapterReport(honored/dropped)`、`props.vector_brief`）· `choreography.py`（`phase_windows()`、`INTENT_ARCS`、`assign_roles`）· `motion.py`（track builders）· `docs/vector-motion-plan.md`（架构契约，人评审）
**Shape B（`lumenframe/*`）**：`<lib>/<name>.py`（纯 `(**params)->list[op_dict]`）· `elements/__init__.py`（`ELEMENTS`/`ELEMENT_CATALOG`/`SHARED_PARAMS :97-99`）· `templates/__init__.py`（`TEMPLATES`/`TEMPLATE_CATALOG`/`SHARED_PARAMS :80-82`）· `templates/theme.py`（`PALETTES['lumeri']['accent']`、`nx/ny`、type scale）· `ops.py`（`_op_apply_template :2046-2073`、`_op_apply_element :2076-2105`，`TypeError→LayerPatchError('E_ARG')`，`@register_op(..., source='core')`）· `catalog.py`（`describe_ops() :280-326`）
**Shape B 门测试 — 已存在（`tests/test_lumenframe_element_library.py` / `_component_library.py`）**：`test_every_element_has_a_catalog_entry_and_vice_versa`（`:57-63`）· `test_every_entry_has_the_required_fields` · `test_catalog_is_ordered_by_category`（`:85-90`）· `test_documented_params_exist_on_the_function`（`:70-83`）· `test_expands_to_valid_ops` · `test_applies_and_renders`（参数化 `sorted(ELEMENTS) :101-153`）· `test_element_is_an_overlay_not_a_background`（角 α`<=4 :119-135`）· prefix 唯一（`:145-153`）· `TestColour`（`:159-170`）· `test_injected_into_describe_ops`（`:186-192`）
**Shape B 门测试 — 待落地**：`test_<lib>_taste_floor_property`（Shape B 地板）· `test_catalog_tokens_carry_kind`
**设计手册（原材料）**：`~/Code/lumeri-design-manuals/DESIGN.md` 及 `01-grading`…`06-AI-conduct`
**技能层边界（§14）— 已存在**：`gemia/lus.py`（`CRAFT_CLOSED_DOMAINS` 注册表 + `E_LUS_CRAFT_NUMBERS` 检查 14b）· `tests/test_lus_format.py`（craft 拒/拒/放行三测试）· `tests/test_skill_distill.py`（`save_skill` 端 typed 拒存零写盘）· `tests/test_charter_integrity.py`（两文档交叉引用门 + 注册表 build==install 门）· `docs/lus-skill-format.md` §12 addendum

---

## §13 宪章治理、修订与生效条件（新增）—— "标准设定"作为一个过程

> §11 管**库**的演进；本节管**宪章本身**的演进，并给出其生效条件——回应"宪章自称 canonical 却引用不存在的门"的元问题。

### §13.1 生效条件（coming-into-force）
本宪章从 DRAFT 转为 **RATIFIED（生效）** 的充要条件：**§10 三失败模式的红-build 级检查全部落地并在本地 acceptance run 中转绿**，即以下最小集提交且绿：
1. `tests/test_tool_catalog_contract.py::test_every_registered_verb_is_fully_installed`（Layer A）
2. `tests/test_library_verb_manifest.py`（Layer B，自动发现式）
3. `test_<lib>_addressability_roundtrip` + `test_layer_tree_handles_round_trip`（P9）
4. `test_success_carries_asset_and_next` + `test_errors_are_typed_and_recoverable` + host `test_recoverable_library_failure_degrades_to_partial`（P10）
5. `test_charter_referenced_tests_exist`（meta：凡标【已存在】的引用路径解析到真实文件）→ 落地为 `tests/test_charter_integrity.py`

> **✅ 条件已满足（2026-07-15）**：上述五项的具体实现——`tests/test_tool_catalog_contract.py`、`tests/test_library_verb_manifest.py`、`tests/test_addressability_roundtrip.py`、`tests/test_library_ledger_contract.py` + `tests/test_v3_ledger_partial_disclosure.py`、`tests/test_charter_integrity.py`——已提交且在本地 acceptance run 全绿（23 tests），并逐一对抗验证。**本宪章据此转为 RATIFIED。** 具体门名与 §13.1 拟名略有出入（如 host 门精确名 `test_recoverable_library_failure_degrades_to_partial` 一致；库侧拆为 `test_success_carries_asset_handle_and_next` / `test_errors_are_typed_data_never_exceptions`，语义一致），以仓内真实 test node 为准。

RATIFIED 之后：仍标 **【待落地】** 的是**每库自带**验收门（§8 模板逐项），由各库自己的 PR 落实并转绿——**不影响宪章效力**；**不得**据此判定某具体库"完成"，除非该库的 §8 卡片逐项打勾且其自带门全绿。

### §13.2 引用完整性守护（meta 门）
`tests/test_charter_integrity.py::test_charter_referenced_tests_exist`【已存在，2026-07-15 落地】扫描本宪章标【已存在】的行中引用的每个测试文件路径，断言其解析到真实文件；任一为 phantom 即 FAIL。这防止宪章再次"声称拥有不存在的门"（AP33）。已知局限（诚实标注）：扫描是**行级、单向**的——只查"标已存在但文件不存在"，查不出"文件已存在但标注仍是待落地"的反向陈旧；后者靠修订时的 §13.4 一致性义务人工维护。

### §13.3 合并门与 CI 执行保证
项目约定"GitHub CI 非主信号；验收走本地 pytest + npm test"。因此**合并门＝本地 acceptance run**：执行合并的 agent 必须在合并前跑通 `pytest` + `npm test`，并把结果附于 PR。建议以 **pre-merge 脚本 / pre-commit hook** 保证 acceptance run 实际运行过——"machine-checkable 但从不运行"等于无门。禁止在未附 acceptance run 结果的情况下声称达标。

### §13.4 修订流程（amendment governance）
- **版本**：本宪章语义化版本（`vMAJOR.MINOR`）。MINOR = 澄清/新增门；MAJOR = 改变某原则的约束力或删门。
- **批准**：新增/退休一条**原则（P#）或 GATE** 属 MAJOR，需用户（Acrab）明确批准（如 v1.0 的四层模型 2026-07-14 批准）。新增**强制测试/收紧门**属 MINOR，架构师可提交但须在同 PR 落地测试。
- **退休标准**：删除一条标准须写明理由 + 指出其守护的失败模式已由何种更强机制覆盖，否则视为回归。
- **一致性义务**：任何修订不得回退既有覆盖（no-regression）；改动某原则须同步更新 §3.0 适用性矩阵、§7 门表、§9 反模式、§12 索引、附录清单，保持镜像一致。
- **草案纪律**：新增门默认以 【待落地】 入库，转绿后方升为生效门。

---

## §14 技能层（skill layer）与点库的边界（仲裁条款）—— 新增 v1.3

> **动机**：本宪章（2026-07-15 RATIFIED）与 `docs/lus-skill-format.md`（2026-07-06 locked design）对"文本技艺资产能否长期合法存在"给出过相反答案——AP29 把"手艺只活在 prose"钉为反模式，而 .lus 规范把文本技能当一等资产；两文档互不引用、矛盾无仲裁。2026-07-16 深读分析实锤了后果：调色域三重重叠（根 `skills/color_grade.json` / `skills_v2/电影调色.json` 写死 `shadows=[0.02,0,-0.02]` / `gemia/ai/skills/color-grade/SKILL.md` 指向 v3 不存在的 v2 primitives 却仍被 `recall_skills` 端给模型）。本节是唯一仲裁：**两文档世界观冲突时以本节为准**。经用户（Acrab）2026-07-17 批准。

### §14.0 定位：两层，不是两套平级体制

| 层 | 载体 | 固化什么 | 执行语义 | 抵达模型 |
|---|---|---|---|---|
| **点库（Layer 2）** | 确定性 Python + 一个工具动词 | **品味/手艺**（怎样做才专业） | 确定性，地板 ENFORCED | 静态注册推送（`TOOL_SCHEMAS`/`TOOL_PACKS` + system prompt 专节） |
| **技能（Layer 3 附属）** | `.lus` 文本 playbook（`~/.gemia/skills/`） | **流程/偏好/长尾**（这个场景通常做什么） | 模型参考着自由发挥，advisory | `recall_skills` 工具拉取（top-5 召回） |

**canonical 技能载体 = v3 沉淀库**（`.lus`，`gemia/skill_store.py` `DistilledSkillStore`）。其余三个 skill 形状子系统定位为 **legacy**：`gemia/ai/skills/*/SKILL.md`（v2 planner 注入链）与 `skills_v2/*.json`（v2 计划模板，git 止于 2026-04-05）不再新增条目，其中手艺类内容按 §11.4 结构化入库后退役（primitives 已失效者优先）；仓库根 `skills/` 为归档。技能体制的一切新投资只进 .lus 沉淀库。

### §14.1 三条边界规则（normative）

**S1 · 品味归库**：夹带手艺的原始数字（P2 意义上的 craft 数字：调色 RGB 配方、曲线控制点、`cubic-bezier(…)` 控制点、逐项 stagger 秒数）**MUST NOT** 存活于任何技能。已被**安装库**关闭的域，其 craft **配方形状**（中英文均覆盖：括号列表、逐通道 `各 ±0.02` 偏移、无单位控制点小数、同行 lift+gamma+gain 三连赋值）在 `save_skill` 入口即 typed 拒存、零写盘。
**可测形式（已存在）**：`gemia/lus.py` 检查 14b `E_LUS_CRAFT_NUMBERS`（第 16 个错误码）+ `CRAFT_CLOSED_DOMAINS` 注册表 + `find_craft_leak()`；`tests/test_lus_format.py`（grading 中英拒 / motion 拒 / 创意语言+日常剪辑 prose+放置量放行 / 围栏引用放行 / 检查顺序）、`tests/test_skill_distill.py`（`save_skill` 端 typed 拒存零写盘 + 存量隔离两测试）。
**扫描范围与读侧语义（对抗评审后精化，v1.3）**：守卫只扫**正文**（元数据承载召回信号非步骤，豁免）且**豁免围栏代码块**（Pitfalls 引用反面示例合法——引用疾病不等于患病）。读路径（`list_distilled`/召回选中）遇 `E_LUS_CRAFT_NUMBERS` **隔离该技能并记录 WARNING 日志**（文件留盘、不进召回、绝不静默）——`tests/test_skill_distill.py` 两个 quarantine 测试【已存在】；legacy JSON 双读通道同样过 `find_craft_leak` 隔离，craft 不得借旧格式绕道抵达模型。
**注册表诚实性（build==install 应用于守卫自身）**：`CRAFT_CLOSED_DOMAINS` 只可列**已安装**库关闭的域；未覆盖的已安装创作动词以**有界 pending 清单**显式列出（`CRAFT_GUARD_PENDING_VERBS`，当前：`kinetic_type`/`camera`/`compose`/`edit_grammar`/`rhythm_edit`）——`tests/test_charter_integrity.py::test_craft_guard_registry_matches_installed_tools` 断言 closed ∪ pending 恰好等于已安装创作动词参考集、两集不相交、全部活在 `TOOL_NAMES`【已存在】。新创作动词上线或 pending 转 closed 属 MINOR 修订，**同 PR** 落地对应 patterns + 拒/拒/放行三测试。
**守卫边界（宁窄勿宽）**：守卫 **MUST NOT** 误杀库自身的创作语言——语义轴（`warmth: 0.7`、`lift: 0.3`、`stagger_spread: 0.6`）、look/archetype 词、放置/时序量（`x=120`、"trim to 3s"、"fade easing: 0.3s"、"duck by -12dB"）、日常剪辑词汇（"highlights: 3 best moments"、时间戳 `0:32`、"高光时刻"、UI 投影 "shadows: 0.15 opacity"）都是合法技能内容（P2 的 Shape B 放置豁免同理适用于技能文本）。**射程诚实声明**：正则拦的是**明确配方形状**；自由改写的 prose 配方（"把阴影往蓝里推一点点再压 2%"）不在正则射程内——那是 recall 侧过滤与评审的兜底职责（见 S3 与 §14.3），不据此扩正则（误杀代价高于漏杀）。

**S2 · 流程归技能**：跨域 workflow、外部服务用法、本机/用户偏好、踩坑经验（Pitfalls）是技能的**合法长期居民**，**MUST NOT** 硬塞成库——P6 单域闭包 + §0.4 六点契约不满足者不得入库（强行入库即 AP1/AP30）。技能是长尾知识的唯一容器；现网 `pixabay-pexels`、`audio_ducking_setup`、`batch_rough_cut` 即为范式。

**S3 · 技能引用库的创作语言，不绕过库**：技能步骤涉及已关闭域时 **MUST** 说库动词 + 创意语言（"调 `grade` op create，look teal_orange，warmth 0.7"），**MUST NOT** 携带数字配方（由 S1 门拦截）或指向已失效的 legacy primitives。
**可测形式**：save 侧 = S1 门【已存在】；recall 侧 craft/坏文件隔离（含 legacy JSON 通道）【已存在，见 S1 读侧语义】；recall 侧 **.lus `tools_used` 失效引用**过滤（召回选中时按 `TOOL_NAMES` 校验，死引用者隔离出召回并留具名 WARNING 信号、工具面不可解析时 fail-open）**【已存在】**——`gemia/skill_store.py::_materialize_recall_view` + `tests/test_skill_distill.py`（`test_dead_tool_lus_is_excluded_from_recall_with_signal` 死引用隔离 / `test_live_tool_lus_is_recalled_not_over_excluded` 活工具放行，宁窄勿宽双测试）。**legacy SKILL.md 来源**（其 primitives 是 v2 点分路径，与 v3 `TOOL_NAMES` 天然不相交的 namespace）的**降权或排除**仍**【待落地】**：正确判定需先确定 v2 primitive 在 v3 是否仍被路由，盲用 `TOOL_NAMES` 校验会把全部内置库技能误判为死引用而抹除（违反本节守卫边界）；据 §14.3 纪律，这是"该 legacy namespace 需一次迁移或裁决"的信号，不是扩校验的理由。在该半边落地前，它以指导效力存在（§0.8 草案纪律）。

### §14.2 仲裁细则

1. **AP29 的适用范围 = 手艺（craft）**。"Prose-only craft"针对的是品味决策活在可忽略文本；**流程/偏好/长尾 prose 不在 AP29 射程内**——它们本来就不该是库（S2）。
2. **P7"原材料通道"指设计手册→库**（`~/Code/lumeri-design-manuals` 经 §11.4 结构化进 registry/theme），**不指 .lus 沉淀库**。.lus 不是手艺的孵化器：模型蒸馏出已关闭域的 craft 时 S1 门直接拒存；未关闭域见 §14.3。
3. **两规范分工**：本宪章管"什么知识允许长期活在哪一层"（本节）；`docs/lus-skill-format.md` 管".lus 文件怎么写"（格式），其 §12 addendum 记载 `E_LUS_CRAFT_NUMBERS` 扩展并回引本节。
4. **交叉引用为机器门**：`tests/test_charter_integrity.py::test_skill_boundary_docs_cross_reference` 钉住两文档互引、`E_LUS_CRAFT_NUMBERS` 两侧点名、以及本节 S1/S2/S3/§14.3 小节锚点存在【已存在】——删除或改名本节任一规则即红。（诚实标注：门钉的是锚点与互引存在性，不是语义一致性本身；语义漂移仍靠 §13.4 修订纪律。）

### §14.3 手艺信号（craft signal）→ 立项，而非扩白名单

守卫**有意宁窄勿宽**：只拦已关闭域的明确配方句式，未关闭域的 craft 不触发（避免误杀换不来的召回损失）。因此当评审或 builder 在技能里发现**未关闭域**的手艺数字（如稳定化平滑参数、变速曲线）——正确响应**不是**把该 pattern 加进守卫或放任技能承载，而是把它当作**"该域需要一个库"的立项信号**：按 §11.5 路线图走 GATE 0。技能层永远不是手艺的正式住所，只是暴露"哪里还缺库"的探测器。

---

## 附录 · ONE-PAGE 上线清单（builder 逐格打勾，两形态 × 两类别全覆盖）

```
库名 ____________  域 ____________
形态 [ ]A [ ]B     类别 [ ]SYNTHESIS [ ]TRANSFORM     TIME层域? [ ]是(必A/内嵌choreo)[ ]否
namespace <lib>_

■ 定义（§0/§1，GATE 0）
  [ ] 关闭恰好一个域（§0.6 定义 + 泄漏判据无默认失效参数）
  [ ] 六点契约可满足        [ ] §2 主判据(工具身份)命中哪条已写下
  [ ] 类别已判→走 §5A/§5B/§5T   [ ] 引用手册 + DESIGN.md 触及层(static/TIME)已指明
  [ ] 本域地板断言集已声明(非空非平凡)

■ 原则（§3；先看 §3.0 适用性矩阵）
  [ ] P1 地板 property-fuzz=本域断言集 golden   [ ] P2 无手艺原始数字(Shape B placement 豁免,白名单lint)
  [ ] P3 封闭 CATALOG + token 带 kind/label     [ ] P4 一 agent 面 + 主输入外全可选
  [ ] P5 确定性子进程 double-run 字节相同(by-construction 库无 seed 亦可)
  [ ] P6 单域无泄漏(no-floor-disabling-default)  [ ] P7 手册结构化(时间维库勾 TIME,静态域 static)
  [ ] P8 骑层不 fork、无新 render capability(PR 无 render 码)
  [ ] P9 逐字可寻址 + 完整可枚举(库+host round-trip)  [ ] P10 交付物+验证指针+可恢复错误(降级=host契约)
  [ ] P11 build==install                        [ ] P12 原创手艺过对抗评审 + novel-inband 真实behaviour

■ 接口（§4）
  [ ] Shape A: op=create|adjust|catalog；create 回 digest(plan/measurement)非整帧；next 指验证
      主输入=subject/target asset_id；adjust 保 layer id + 用户键(artifact-type 附录)，同 seed 重推
  [ ] Shape B(FORM): 纯 (**params)->[ops]；两处注册；单 apply_<lib>；单 describe 注入+验证提示；
      自声明 SHARED_PARAMS + op-emission 面；refinement=删除+重apply,prefix稳定
  [ ] Shape B(绘图 profile,仅画2D矢量): px-from-centre / (element)overlay角α≤4 / color round-trip
  [ ] 结构化错误即数据；跨边界 asset_id 非路径

■ 制作（§5，按类别）
  [ ] §5A(合成A): 严格向下分层+唯一 director(importlinter门) / IR 比最弱后端富+AdapterReport /
      语义→数字唯一表 ResolvedParams / registry+catalog+drift-pin / phase arc+留白先切 /
      validate-before-mutate+单原子 patch
  [ ] §5B(Shape B): 手艺烘焙进宏体(无默认失效参数) / 从 theme 构建 / 地板 property 参数化
  [ ] §5T(变换): target asset_id / 非破坏 param-node 栈 / 语义→变换参数唯一表 /
      measurement readback digest / 越 gamut 报告 / 像素·示波验证

■ 安装（§6）—— 与 build 同一 PR
  Shape A: [ ]A1 [ ]A2 [ ]A3 [ ]A4(+estimate raise) [ ]A5 [ ]A6 [ ]A8(HARD prose) [ ]A9
           [ ] Layer A 绿  [ ] Layer B(自动发现)绿  [ ] catalog_coverage==(∅,∅)
           [ ] INTENTIONAL_STUBS/PROSE_EXEMPT 白名单有界
  Shape B: [ ]B1 [ ]B2 (+[ ]B3 [ ]B4 [ ]B5 [ ]B6 仅新库)
           [ ] registered==documented  [ ] (绘图)overlay角α≤4  [ ] describe_ops 注入+验证提示
  [ ] 全局 token 唯一性绿(现已必须)

■ 验收门（§7，全 PASS 方合并）
  G1[ ] G2[ ] G3[ ] G4[ ] G5[ ] G6[ ] G7[ ] G8[ ] G9[ ] G10[ ] G11[ ] G12[ ] G13[ ] G14[ ] G15[ ]

■ 三失败模式兜底（§10，注意确切检查多为【待落地】,见 §13 生效条件）
  [ ] 1 安装债 → Layer A + Layer B(自动发现)
  [ ] 2 可寻址 → 库 round-trip + host round-trip + 列表枚举路径
  [ ] 3 账本 → completion contract + error-taxonomy + host 降级测试

验收信号：本地 acceptance run(pytest + npm test)全绿(合并前必跑,附结果)；真机抽帧见资产越过地板。
生效前提(§13)：§10 三失败模式的红-build 级检查已落地并转绿,否则本宪章为 DRAFT,不据以判"完成"。
```

**宪章生效判据（一句话）**：一个新点库"完成"，当且仅当它照 §8 模板填满、§7 的 G1–G15 全 PASS、§6 的安装点全部被机器校验存在、§10 三失败模式各有一个**已落地并转绿**的红 build 级检查兜底——此时品味已按其域结构性内嵌（Layer 2 成立，两形态、两类别皆然），且真正抵达 live 模型（不再是 merge debt）。**而本宪章自身"生效"当且仅当 §13.1 的最小检查集已落地转绿；在此之前它是 DRAFT，只作指导。**
