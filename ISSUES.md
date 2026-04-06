# Known Issues / Blocking Problems

## Blocking — Primitives 缺失

### [ISSUE-001] 溶解转场 primitive 缺失
- **问题**：没有跨视频的 dissolve/crossfade 转场 primitive
- **影响**：无法实现两段视频之间的渐变过渡，只能做硬切
- **需要新增**：`gemia.video.timeline.crossfade(video_a: str, video_b: str, output_path: str, *, duration_sec: float) -> str`
- **实现思路**：ffmpeg `xfade` filter，支持 fade/dissolve/wipeleft 等模式

### [ISSUE-002] 竖屏转横屏（letterbox）primitive 缺失
- **问题**：`rotate_video` 只做 90° 旋转，无法做竖屏(9:16)→横屏(16:9)的信箱格式转换
- **影响**：竖拍视频无法自动适配横屏导出
- **需要新增**：`gemia.video.timeline.letterbox(input_path: str, output_path: str, *, target_w: int, target_h: int, bg_color: str = "black") -> str`
- **实现思路**：ffmpeg `scale` + `pad` filter

### [ISSUE-003] 真正的黑白转换 primitive 缺失
- **问题**：`color_space_convert` 不支持 grayscale 模式，现有黑白 Skill 用 cool 色调近似，不是真正灰度
- **需要新增**：`gemia.picture.pixel.to_grayscale(img: Image) -> Image`（保留3通道但RGB相等）

### [ISSUE-004] auto_duck Skill 无法通过单输入 plan 调用
- **问题**：`gemia.audio.mixer.auto_duck` 需要 `music` 和 `voice` 两个独立 numpy array 输入，当前 plan engine 只支持单 `$input` 变量
- **影响**：无法将 auto_duck 包装成用户可直接执行的 Skill
- **解决方向**：引入 `$input_2` 多输入支持，或在 server 层预混音后传入

### [ISSUE-005] 片头字幕叠加无法参数化
- **问题**：`generate_image` 可以生成带文字的图片，但文字内容（学校名、节目名）是 plan args 里的固定字符串，Skill 执行时无法动态替换
- **影响**：艺术节片头 Skill 无法让用户自定义标题文字
- **解决方向**：引入 Skill parameters 覆盖机制（`SkillStore.apply_parameters` 已存在），在 UI 执行时提示用户填写参数

## 非 Blocking — 已知限制

### [ISSUE-006] Tauri DMG 中 skills_v2 未自动打包
- **问题**：skills_v2/ 目录需要手动在 tauri.conf.json 的 resources 里配置，当前可能只打包了 skills/
- **需要验证**：重新构建 DMG 时确认 skills_v2 在 bundle 中

### [ISSUE-007] 音频 Skill 的 engine routing 未完全验证
- **问题**：音频 skill（人声降噪、音量均衡等）的 plan 步骤调用 `gemia.audio.*` 函数，但 engine 的 `_call_audio_func` 路由逻辑对 highpass/lowpass/compress 等函数的参数传递未在集成测试中覆盖
- **状态**：单元测试覆盖各函数，但 plan → engine → audio chain 的端到端待验证

### [ISSUE-008] `/run-skill` 的 skills_v2 路径仅支持文件名匹配
- **问题**：当 skill_id 含中文（如 `多段合并`）时，文件系统路径匹配在某些情况可能有编码问题
- **缓解**：SkillStore._slugify 已处理，但 server.py 直接用 `skill_id` 作文件名查找，可能失配
