# 点库宪章卡：vector_motion  ·  namespace: `vector_motion`（引擎注册表 `lumenframe/vector/`）

> 参考库（reference library）的宪章卡。宪章 §8 模板逐格填满，证据全部指向真实代码 file:line。
> **状态诚实声明**：vector_motion 是宪章指定的"回填其余库的模板"，但**尚未 §7 全门 PASS**——见 §6 门表：
> 7 门自动 PASS（G1/G2/G3/G5/G7/G8/G15）+ 2 门人工 PASS（G9/G12）+ 5 门部分（G4/G6/G10/G11 正式测试待落地；**G14 门已建且绿，但存 6 处已知跨库 token 碰撞待裁决**）+ 1 门待落地（G13）。
> 因此按 §13.1 纪律**不得判定本库"完成"**。本卡是该库验收门的 source-of-truth 台账。
> 生成：2026-07-17（item 3 参考库样板；本轮新落 G1 `tests/test_vector_taste_floor_property.py`、G2 `tests/test_vector_determinism_subprocess.py`、G3 `tests/test_vector_no_raw_numbers.py`、G14 `tests/test_global_token_uniqueness.py`（跨库，推进全部库））。

## 1. 定义（GATE 0）
- 一句话：本库关闭的**唯一创作域** = **矢量运动设计（vector motion design）** = 矢量几何随 TIME 的编舞式动画。agent 说风格/感受/语义轴，引擎编舞相位弧、staggering、焦点序、easing。证据：`gemia/tools/vector_motion.py:1-21`、`lumenframe/vector/api.py:1-26`、`gemia/lus.py:132-134`（域 `motion`→verb `vector_motion`）。
- 域**已完全闭合**（§0.6 泄漏判据）：潜在泄漏点及堵死方式 =
  - (a) craft-leak 守卫 `lus.py` `CRAFT_CLOSED_DOMAINS` `motion`→`vector_motion`（:132-147）拒绝原始手艺数字（`cubic-bezier(…)`、`stagger=0.08`、`easing:[0.42,0,0.58,1]`）泄漏进技能/模型文本；带单位时长（`0.3s`）放行。registry-pin 绿于 `tests/test_charter_integrity.py::test_craft_guard_registry_matches_installed_tools:77-108`（vector_motion 在 CLOSED 分区，非 pending）。
  - (b) `resolve_style_name` 遇未知 style 抛 `StyleError`（`styles.py:125-138`）——静默改运动角色的风格会误导。
  - (c) `params.resolve` 遇未知 OVERRIDE/BASELINE 轴抛错（`params.py:189-205`），未知 FEELINGS 收进 notes（近对 brief 不致命）。
- 形态（§2 决策）：**[x] Shape A**（顶层 verb，需独立 budget+plan+router 身份）　[ ] Shape B
  主判据答案："需要独立工具身份（budget/plan/校验/create-adjust-catalog）吗？" = **YES**——它写 lumenframe 文档（html 层），需 plan-mode 阻断 + 独立 budget 行；破平局依据 = 它是 **TIME 域合成库**，宪章 §2.3 强制 Shape A。证据：单 verb + op 判别符 `_OPS=("create","adjust","catalog")` `vector_motion.py:47`。
- **类别（§0.7）**：**[x] SYNTHESIS**（从 brief 合成场景）　[ ] TRANSFORM　→ 走制作支线：**[x] §5A**
- TIME 层域？= **是**（输出随 duration 的动画轨/关键帧；内建编舞相位弧 `choreography.INTENT_ARCS` `choreography.py:39-57`）→ §2.3 必 Shape A，已满足。
- 编码的手册：DESIGN.md 触及层 **[x] static [x] TIME**；定向手册 = 运动/动画手册（`motion.py:10` 对齐 DESIGN.md `motion.*`；`DURATION_BANDS` 遵手册时序带 `motion.py:146`）。
- **本域地板断言集**（§0.6/P1）= 无 linear 默认 ease / draw-on 单调 ease 无 overshoot 缝隙 / 每 behaviour 关键帧留在其相位窗内且 t-sorted（22 verb）/ EMPHASIS 必回 rest / 留白 hold 仅 reveal·intro 有 loop·exit 无 / 相位覆盖 0→duration 无缝且焦点最后落 / seed 字节确定性 / stagger 铺满单位区间 / oscillation 真相位偏移非齐步 / overshoot 随 elegance 抑制、particle_count≤420 / opacity 轨尊重设计基透明度 / SVG render-safe 本地无 `url()`/`<script>`。测试锚点见 `tests/test_vector_creative.py` + `tests/test_vector_engine.py`。

## 2. 原则符合性（§3，逐条 + 证据）
- [x] **P1** 结构性品味地板：地板断言集见 §1；分项 parametrized 测试 `test_vector_creative.py::test_behavior_contract_keyframes_stay_inside_window:150`、`::test_every_intent_style_pair_builds_and_validates:331`、`::test_compiler_never_relies_on_css_default_ease:492`、`::test_draw_on_uses_monotonic_ease_no_overshoot:503`、`test_vector_engine.py::test_phase_windows_hold_presence_by_intent:405`。统一 property-fuzz `tests/test_vector_taste_floor_property.py::test_taste_floor_holds_across_brief_space`（60 brief + 25 intent×style 全格）+ 反平凡 `::test_taste_floor_test_is_nontrivial` = **已落（本轮）**。
- [x] **P2** 创作语言无手艺原始数字：`SEMANTIC_AXES`（7 轴，0..1）`params.py:26-29`；schema prose "Speak creative language… energy 0.8, never x+=20" `_schema.py:1336-1337`；craft-leak 守卫 `lus.py:132-147`。专用库面 `tests/test_vector_no_raw_numbers.py`（创作面 `find_craft_leak` 无泄漏 + 语义轴封闭集 drift-pin + schema 无原始几何旋钮）= **已落（本轮）**。
- [x] **P3** 命名目录封闭 CATALOG：behaviour 注册表 `BEHAVIORS` + `BEHAVIOR_CATALOG`（`behaviors/__init__.py:66-101`），drift-pin `test_behavior_catalog_matches_registrations_one_to_one:119`、`test_load_registers_all_22_verbs_across_5_families:109`。archetype 全局唯一性形式化 = 见 §5「全局 token uniqueness」（**待落地**）。
- [x] **P4** 一 agent 面 + 主输入外全可选：单 verb `vector_motion`，op 判别符 create|adjust|catalog（`vector_motion.py:47`）；brief 仅需 `subject`（`api.build_scene:56`）；schema required=["op"]（`_schema.py:1365`）。测试 `test_vector_motion_in_tool_names_and_dispatcher:95`。
- [x] **P5** 确定性：seed 来源 = `brief.seed`（默认 7，`api.py:42-47`）；`rng=random.Random(seed)`（`api.py:82`）；`vscene.reset_ids()` thread-local 计数器（`scene.py:114-126`，并发安全）。子进程 double-run 测试名 = **`tests/test_vector_determinism_subprocess.py::test_build_scene_svg_is_byte_identical_across_subprocess_hash_seeds`（本轮新落，PASS）** + 反平凡 `::test_subprocess_build_matches_in_process_build`。进程内/并发：`test_build_scene_is_deterministic_per_seed:314`、`test_concurrent_build_is_deterministic_and_unique_ids:461`、`test_create_then_adjust_is_deterministic_across_sessions:232`。
- [x] **P6** 只关一个域：craft 守卫 `motion`→`vector_motion` CLOSED（`lus.py:132`），registry-pin `test_charter_integrity.py:77-108`。无默认失效参数代理 `test_no_floor_disabling_default` = **待落地**。
- [x] **P7** 编码手册：引用手册 = DESIGN.md `motion.*`（`motion.py:10`、`DURATION_BANDS:146-160`）；时间维库 TIME 层轨道 = `TRACK_PROPS`（`scene.py:84`）+ 相位弧。static+TIME 均满足（结构编码非 prose）。
- [x] **P8** 骑层不 fork、无新 render capability：ADD-ONLY（`vector_motion.py:18-21`）；复用 `layer._lumendoc`/`_save_lumendoc` + `apply_layer_patch`（`vector_motion.py:88,119,124`）；`render.py` 骑 `resolve_html`、"never renders"（`render.py:1-21`）。grep 门 `test_no_new_render_symbols` = **待落地**。
- [x] **P9** 逐字可寻址 + 完整可枚举：create 返回完整 `layer_id` 逐字（`vector_motion.py:128`）；host round-trip `tests/test_addressability_roundtrip.py::test_tree_shows_full_ids_verbatim_and_they_resolve:54`、`::test_no_handle_is_ellipsis_truncated:64`、`::test_get_lumenframe_tool_selection_ids_match_shown_tree:71`。截断枚举 `test_truncated_list_has_full_enumeration_path` = **待落地（文件缺）**。
- [x] **P10** 交付 + 可恢复错误：最终 asset handle = `layer_id` + `next` 指针（`vector_motion.py:135-136`）；错误码 `E_ARG/E_NOT_FOUND/E_RENDER/E_NOT_AVAILABLE/E_UNKNOWN`，recovery=`fix_args`（:100,164）。测试 `tests/test_library_ledger_contract.py::test_success_carries_asset_handle_and_next:40`、`::test_errors_are_typed_data_never_exceptions:64`、`::test_bad_args_error_is_marked_recoverable:75`；host 降级 `tests/test_v3_ledger_partial_disclosure.py::test_recoverable_library_failure_degrades_to_partial:67`；错误路径不改文档 `test_vector_motion_tool.py:280`。
- [x] **P11** build==install：Layer A `tests/test_tool_catalog_contract.py`（自动覆盖 vector_motion：真 dispatcher / 在 pack / 恰一 plan class / 显式 budget）+ Layer B `tests/test_library_verb_manifest.py` + `catalog_coverage()==(∅,∅)` `tests/test_tool_router.py:33`。本库即"built-not-installed"起源案例，这两道 meta 门因它而加（`test_tool_catalog_contract.py:15`）。
- [x] **P12** 设计正确/原创 + 尊重疯狂：对抗评审已做（4 镜 + 抽帧，修复回归块 `test_vector_creative.py:458-601`）；近对 brief 不致命 `test_unknown_behavior_override_is_reported_not_fatal:582`、`test_build_scene_reports_unknown_feelings_in_notes:363`；22 verb 均可经 `brief.behaviors` override 触达 `test_every_behavior_reachable_via_brief_override:549`。具名 novel-inband 测试 = **待落地**；G9 人工门已过。

## 3. 接口 schema（§4）
- Shape A：op 判别符 = **create | adjust | catalog**（`_schema.py:1344`）；主输入 = `brief`（唯一必含 `subject`）/ `place` / `layer_id` / `feedback`（`_schema.py:1347-1363`）；
  create digest 字段 = layer id、start、duration、svg_bytes 摘要、plan digest、notes（`_plan_digest` `vector_motion.py:54`）；
  `next` = 指向 `lumen_seek` / `lumen_render_range` 的验证路径（`vector_motion.py:135`）；adjust 保留用户键 = `_PRESERVED_LAYER_KEYS`（`vector_motion.py:42`）。

## 4. 制作（§5A —— SYNTHESIS × Shape A）
- 层图 IR→toolkit→behaviours→styles→params→choreography→api（`lumenframe/vector/*`）；
  语义轴（0..1）= `SEMANTIC_AXES` 7 轴（`params.py:26-29`）→ ResolvedParams 单表（`params.resolve`）；
  IR node.kind + `TRACK_PROPS`（`scene.py:84`）；phase arc = `choreography.INTENT_ARCS`（`choreography.py:39-57`）；留白 hold 先切（`test_phase_windows_hold_presence_by_intent`）；
  validate-before-mutate + 单原子 patch（`api.build_scene` → `apply_layer_patch`，错误路径不改文档 `test_vector_motion_tool.py:280`）。

## 5. 安装点（§6，逐点已注册 + 已被测试覆盖）
- Shape A：
  - [x] A1 `gemia/tools/vector_motion.py`（`dispatch:73` / `_create:87` / `_adjust:140` / `_err:50`）
  - [x] A2 `_schema.TOOL_SCHEMAS`（`_schema.py:1327` `_tool("vector_motion",…)`，op enum :1344，required :1365）
  - [x] A3 `__init__._REAL`（`gemia/tools/__init__.py:78,191` `"vector_motion": _vector_motion.dispatch`，非 stub）
  - [x] A4 `budget_guard._TOOL_COSTS`（`budget_guard.py:128` `{"usd":0.00,"eta_sec":1.0}`，非 unknown-default）
  - [x] A5 `plan_mode`（`plan_mode.py:79` ∈ `PLAN_BLOCKED_TOOLS`，写文档→BLOCKED）
  - [x] A6 `tool_router.TOOL_PACKS`（`tool_router.py:102` `lumen_core`、:118 `motion_graphics`；keywords :176-179）
  - [x] A8 `system_v3.md` prose(HARD)（`prompts/system_v3.md:482-496`，do-NOT-hand-animate 指令）
  - [x] A9 `tests/test_vector_motion_tool.py`（+ `test_vector_creative.py` / `test_vector_engine.py` / `test_library_ledger_contract.py` / `test_addressability_roundtrip.py`）
  - [x] meta A：`test_tool_catalog_contract.py` 绿　[x] meta B：`test_library_verb_manifest.py` 绿
- [x] "schema 有但无 pack" / "dict 有但无 catalog" 会 CI 红 = `tests/test_tool_router.py::test_catalog_exactly_covers_current_111_tool_schemas:33`（`catalog_coverage()==(∅,∅)`）
- [x] 全局 token uniqueness 绿 = `tests/test_global_token_uniqueness.py`（本轮新落，10 测试绿）——9 个模型面库的 archetype∪catalog 命名空间从活注册表实时算碰撞并钉在有界已知集；共享控制词（op 判别符 / 语义轴 / TIME 控制词）显式排除且带反误报断言，探测器带反平凡断言。**残余**：6 处已知碰撞待裁决（见 §6 G14）。

## 6. 验收门（§7，全 PASS 方可合并 —— 本库尚未全绿，见状态声明）
```
G1 PASS   G2 PASS   G3 PASS   G4 部分   G5 PASS
G6 部分   G7 PASS   G8 PASS   G9 人工PASS  G10 部分
G11 部分  G12 人工PASS  G13 待落地  G14 部分  G15 PASS
```
- **PASS（自动，7）**：G1（`test_vector_taste_floor_property.py` brief-空间 property-fuzz + 反平凡 meta，本轮新落）、G2（`test_vector_determinism_subprocess.py` 子进程双 hash-seed，本轮新落）、G3（`test_vector_no_raw_numbers.py` 创作面无手艺配方 + 语义轴封闭集，本轮新落）、G5、G7、G8、G15。
- **人工 PASS（2）**：G9 对抗评审、G12 抽帧真机验证（均已做，机器不可判）。
- **部分（5）**：G4（one-agent-surface 覆盖断言）、G6（`test_truncated_list_has_full_enumeration_path`）、G10（`test_no_floor_disabling_default`）、G11（`test_no_new_render_symbols`）以上四门正式测试待落地；**G14**（`tests/test_global_token_uniqueness.py` 本轮新落且绿）——门已建、跨库碰撞集已从活注册表实时计算并钉死在有界已知集（新增/解决碰撞均转红），但宪章 G14 判据是"无碰撞"，现存 **6 处待裁决碰撞**故计部分：
  - 风格名 `documentary`/`energetic`（edit↔camera）、`minimal`（vector↔kinetic）——跨域共用美学形容词，受 verb 作用域天然消歧，**大概率有意共享**。
  - catalog 键 `title_card`/`lower_third`/`caption`（**kinetic↔templates**）——**更尖锐**：两个库为同一产物命名，"做个下三分之一"有两个库答案，需设计裁决（改名，或把一面折进另一面）。
- **待落地（1）**：G13（importlinter `test_layering_import_contract`）。

## 7. 三失败模式自证（§10）
- [x] 失败模式 1（installation debt）被 meta A+B 防住：覆盖安装点 = A1–A9（Layer A `test_tool_catalog_contract.py` + Layer B `test_library_verb_manifest.py`）。
- [x] 失败模式 2（addressability）被 round-trip（库 + host）防住：`test_addressability_roundtrip.py::test_tree_shows_full_ids_verbatim_and_they_resolve` / `::test_no_handle_is_ellipsis_truncated`。截断枚举半边 `test_truncated_list_has_full_enumeration_path` 待落地。
- [x] 失败模式 3（ledger）被 completion contract + error-taxonomy + host 降级测试防住：`test_library_ledger_contract.py::test_success_carries_asset_handle_and_next` / `::test_errors_are_typed_data_never_exceptions` / `test_v3_ledger_partial_disclosure.py::test_recoverable_library_failure_degrades_to_partial`。

## 8. 反模式自审（§9）
- [x] 逐条对照 §9 反模式，声明本库均不触犯：AP1/AP30（单域闭包，非杂凑）、AP29/AP34（手艺不活 prose/技能，craft 守卫 CLOSED）、AP5（越 gamut——运动域 N/A）均清；**残余诚实标注**：G1/G3/G4/G6/G10/G11 的正式门测试待落地、G13/G14 待落地——按 §13.1 本库据此**不判"完成"**，待补齐。
