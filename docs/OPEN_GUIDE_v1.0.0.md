# v1.0.0 开放指南

这份指南面向两类读者：第一次拿到仓库、想把工作流跑起来的**使用者**（第 1～7 节），和负责打标签、发布 Release 的**维护者**（第 8 节）。

## 1. v1.0.0 开放的是什么

| | 内容 |
|---|---|
| **开放** | 三张 ComfyUI 画布、`drama_pipeline` 自定义节点、制作队列页面与后台服务、分镜输入约定、任务状态与恢复逻辑、配音字幕合成脚本、离线检查与测试 |
| **不包含** | 模型权重、ComfyUI 本体、Python 环境、语音包、任何样片 |
| **已验证** | 11 项离线结构检查、47 项不启动模型的单元测试（任务重试与恢复逻辑、分镜校验、开工检查、队列接口、安装脚本）；制作页面在演示数据上做过浏览器交互检查 |
| **未验证** | 完整动态出片。Wan / Animate2 片段此前出现过人物、服装、姿态或画风漂移，最后一次 Animate2 任务没有完成，没有走到配音、字幕、混音和 MP4 交付 |

所以 v1.0.0 的含义是：**工作流结构、输入格式和工具链稳定可用，可以公开给别人安装和试跑**；它不是成片质量承诺。详细边界见 [`VALIDATION.md`](VALIDATION.md)。

## 2. 环境要求

- **系统**：按 macOS 设计。配音调用系统自带的 `say`，默认字幕字体是 `/System/Library/Fonts/STHeiti Medium.ttc`。Linux 可以跑到动态镜头阶段，配音需要自行替换；Windows 原生环境不支持（后台进程依赖 `fcntl` 文件锁）。
- **ComfyUI**：一份可以正常启动的本地安装，监听 `127.0.0.1:8188`（后台进程写死了这个地址）。它需要已注册工作流用到的全部节点，包括 `WanFirstLastFrameToVideo`、`WanAnimate2ToVideo`、`WanAnimate2Cache` 等；缺哪个，制作页面的开工检查会列出来。
- **Python 依赖**：`aiohttp`、`psutil`、`numpy`、`Pillow`、`av`，装在 ComfyUI 使用的同一个环境里。
- **模型权重**（共 11 个文件，自行获取，文件名需与配置一致）：

  | `models/` 子目录 | 文件 | 用途 |
  |---|---|---|
  | `diffusion_models` | `z_image_turbo_bf16.safetensors` | 关键帧 |
  | `text_encoders` | `qwen_3_4b.safetensors` | 关键帧 |
  | `vae` | `ae.safetensors` | 关键帧 |
  | `diffusion_models` | `wan2.2_ti2v_5B_fp16.safetensors` | Wan 动作草稿 |
  | `text_encoders` | `umt5_xxl_fp16.safetensors` | Wan 动作草稿 |
  | `vae` | `wan2.2_vae.safetensors` | Wan 动作草稿 |
  | `diffusion_models` | `wan_animate_2_int8_convrot.safetensors` | Animate2 角色迁移 |
  | `text_encoders` | `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | Animate2 角色迁移 |
  | `clip_vision` | `clip_vision_h.safetensors` | Animate2 角色迁移 |
  | `loras` | `lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors` | Animate2 角色迁移 |
  | `vae` | `Wan2_1_VAE_bf16.safetensors` | Animate2 角色迁移 |

- **视觉质检服务**：一个本机的 OpenAI 兼容接口（`/v1/chat/completions`，能接收图片），默认地址 `http://127.0.0.1:18088`。没有这个服务时，可以从画布提交并在节点「质检与自动重试」里关闭 `visual_review`，此时只做空白帧、尺寸、帧数等技术检查。
- **内存**：Wan 与 Animate2 阶段占用较大，仓库没有给出经过验证的数字。流程在两个视频模型之间会先卸载再加载；请按你的量化版本和分辨率自行评估，先跑预览档。

## 3. 上手步骤

### 第一步　安装

```bash
git clone https://github.com/yiten5885-a11y/comfyui-fantasy-drama-workflow.git
```

```bash
cd comfyui-fantasy-drama-workflow
```

```bash
python install.py /path/to/ComfyUI
```

不带 `--apply` 时只列出将要新增、覆盖、保留的文件，不做任何修改。确认后执行：

```bash
python install.py /path/to/ComfyUI --apply
```

安装脚本会：复制 `drama/`、`custom_nodes/drama_pipeline/` 和三张画布；把示例分镜放到 `input/drama/example-fantasy-story/`；升级时保留你自己的 `production_config.json`，被覆盖的旧文件备份为 `.bak-时间戳`；只要代码或画布有变化，就把推理门禁写回关闭状态。

也可以手动复制，目录对应关系见 README。

### 第二步　安装依赖

在 ComfyUI 使用的 Python 环境里、ComfyUI 根目录下执行：

```bash
pip install -r custom_nodes/drama_pipeline/requirements.txt
```

### 第三步　填写本机配置并重签构建证书

编辑 `drama/production_config.json`，至少处理这几项：

| 字段 | 说明 |
|---|---|
| `quality.vision.model` | 把占位符 `YOUR_LOCAL_VISION_MODEL` 改成视觉服务实际提供的模型名 |
| `quality.vision.url` | 视觉服务地址 |
| `dynamic.*` / `animation.*` | 如果你的权重文件名不同，在这里改 |
| `subtitle.font` | 非默认字体时改成本机中文字体路径 |
| `delivery.dir` | 成片交付目录，相对路径以 ComfyUI 的启动目录为基准 |

配置文件属于受构建证书保护的文件。**改完必须重签**，否则制作保持锁定：

```bash
python drama/workflow_build.py --certify
```

这条命令先跑 11 项离线结构检查（包括用新配置校验两档预设的尺寸和帧数约束），全部通过才写入 `drama/BUILD_VERIFIED.json`。它不启动模型，也不会打开推理门禁。

### 第四步　重启 ComfyUI，过一遍开工检查

打开 <http://127.0.0.1:8188/drama/production>。页面顶部的「开工检查」逐项核对：

构建证书 · 运行系统 · Python 依赖 · ComfyUI 节点 · 模型文件 · 视觉质检服务 · 配音引擎 · 字幕字体 · 分镜项目 · 交付目录 · 推理门禁

每一项只读文件和配置、不加载模型；未就绪的项会直接给出修复办法。改完点「重新检查」。

### 第五步　打开推理门禁

前面各项都就绪后，把 `drama/build_gate.json` 里的 `inference_enabled` 改为 `true`。不需要重启，页面几秒内会显示「推理门禁已开启」。

门禁是给操作者的最后一道确认：在它关闭期间，页面、画布节点和后台进程都不会向 ComfyUI 提交任何生成请求。

### 第六步　先跑预览档

在制作页面：

1. 选择示例项目，或上传自己的 `storyboard.json`（也可以是 `shots.csv` 加 `characters.json`）。上传时即校验，不合格的分镜不会入库，页面会说明哪个镜头哪个字段有问题。
2. 核对分镜预览：镜头数、时长、角色、台词，以及提示（例如哪些镜头没写 `video_prompt`）。
3. 预设选「动态预览」（384×672 / 17 帧），点「开始制作」。

预览档的目的是用最小代价确认整条链路在你的机器上能走通。走通之后再用「正式出片」。

### 第七步　看片

任务完成后，页面提供成片播放、下载和交付目录路径。视觉模型质检和技术质检都不能替代人工看片，交付前请自己完整看一遍。

## 4. 制作流程

| 页面进度 | 阶段 | 做什么 | 不通过时 |
|---|---|---|---|
| 1 校验 | 模型与节点校验 | 向 ComfyUI 确认所需权重已登记 | 任务停下，提示缺少的文件 |
| 2 关键帧 | 生成关键帧 | 逐镜头生成首帧，检查尺寸与空白 | 换种子自动重试 |
| | 关键帧视觉复检 | 视觉模型对照角色设定和分镜检查 | 带修正提示词重新生成 |
| 3 动态镜头 | 生成动作草稿 | Wan 2.2 由关键帧生成动作 | 自动重试 |
| | Animate2 角色迁移 | 卸载 Wan，用关键帧和动作草稿做角色动作迁移 | 自动重试 |
| | 动态视频视觉复检 | 取首、中、尾三帧检查 | 重新生成草稿并再次迁移 |
| 4 配音字幕 | 逐句配音、时间轴与字幕、混音 | 按台词生成配音，排时间轴，混入配乐和音效 | 任务停下 |
| 5 合成交付 | 竖屏合成、成片质检、复制交付 | 合成 1080×1920 MP4，检查音画，复制到交付目录并写入哈希回执 | 任务停下 |

几条规则：

- **重试有上限**。每个阶段默认最多自动重试 2 次（共 3 次尝试）。用完后任务进入「需要处理」，保留全部现场；点「恢复制作」会再给一轮重试额度。
- **不盲目重复提交**。提交回执丢失、等待超时时，任务会停下等待核对，恢复时继续等原任务，而不是再生成一次。
- **可停止、可恢复**。停止会中断在途的模型任务，已完成的步骤保留。
- **相同输入得到同一个任务**。任务编号由配置和分镜内容的哈希决定；重复提交相同内容会打开已有任务。
- **任务文件**在 `output/drama_jobs/<任务编号>/`：`state.json`（状态）、`worker.log`（后台日志，页面上可直接查看）、`artifacts/`（各次尝试的产物）、`episode.mp4`。

也可以不用页面，直接在画布「玄幻3D-完整出片工作流（展开版）」里调整设置后运行，由「全分镜调度与自动重试」节点提交；画布上的设置只对从画布提交的任务生效，从制作页面提交的任务使用 `drama/workflows/3d_full_api.json` 里的默认值。

## 5. 构建证书与推理门禁

两个文件共同决定能否启动模型：

| 文件 | 作用 | 谁来改 |
|---|---|---|
| `drama/BUILD_VERIFIED.json` | 记录 20 个构建文件（代码、画布、API 图、配置）的哈希 | 只由 `--certify` 写入 |
| `drama/build_gate.json` | `inference_enabled` 开关 | 操作者手动改 |

任何一个构建文件与证书不一致，制作就会锁定，并指出是哪个文件。常用命令：

```bash
python drama/workflow_build.py --status
```

```bash
python drama/workflow_build.py --verify
```

```bash
python drama/workflow_build.py --certify
```

`--status` 显示当前状态，`--verify` 只跑结构检查，`--certify` 检查通过后重写证书。三者都不启动模型。

## 6. 排错速查

| 现象 | 处理 |
|---|---|
| 页面提示「构建证书失效」 | 提示里有文件名。有意修改过就运行 `--certify`；否则还原该文件 |
| 「开始制作」按钮是灰的 | 按钮旁有原因：未选项目、分镜未通过校验，或门禁关闭 |
| 任务停在「需要处理」 | 任务卡片会显示原因和建议；点「查看日志」看 `worker.log` 末尾 |
| 「缺少视频模型」 | 对照开工检查的「模型文件」，放好权重后点「恢复制作」 |
| 「视觉检查未返回有效结果」 | 确认视觉服务已启动、模型名正确、能处理图片输入 |
| 「配音失败」 | 在 macOS 系统设置里下载所选的中文语音包 |
| 片头或字幕显示成方块 | `subtitle.font` 指向的字体不存在，改路径后重签证书 |
| 某个镜头反复质检不通过 | 展开该镜头的「质检意见」，据此修改分镜的 `description` 或 `video_prompt` 后重新提交 |

## 7. 已知限制

- 完整动态出片未通过验收（见第 1 节）。
- 没有口型同步；成片按分镜时长和配音长度调整播放速度。
- 最终 1080×1920 由较低分辨率的镜头放大并居中裁切得到，放大不会增加原生细节。
- 配音只支持 macOS `say`；从制作页面提交的任务使用 API 图里的音频设置，不能在页面上单独关闭配音。
- ComfyUI 地址固定为 `127.0.0.1:8188`。
- 制作页面没有登录保护，只应在本机或可信网络内使用。

## 8. 维护者：发布 v1.0.0

### 发布前检查

一条命令完成全部检查，任何一项不通过都会返回非零：

```bash
python tools/release_check.py
```

它检查：11 项离线结构检查、构建证书与文件一致、推理门禁默认关闭、全部单元测试、配置里的本机模型名仍是占位符、仓库里没有本机用户路径、CHANGELOG 含当前版本、`SHA256SUMS` 与文件一致。

改过任何文件后，用下面的命令重写证书和校验和，再提交：

```bash
python tools/release_check.py --write
```

同样的检查会在每次推送和拉取请求时由 GitHub Actions（`.github/workflows/ci.yml`）自动运行。

### 发布清单

- [ ] `python tools/release_check.py` 全部通过，工作区干净
- [ ] `drama/build_gate.json` 的 `inference_enabled` 为 `false`
- [ ] `CHANGELOG.md` 里 1.0.0 条目填上发布日期
- [ ] README 与 `docs/VALIDATION.md` 的验收边界与实际一致，没有夸大
- [ ] 仓库设置：填写 About 描述和 Topics（如 `comfyui`、`comfyui-workflow`、`wan`、`ai-video`）；开启 Issues；`main` 分支要求 CI 通过后才能合并
- [ ] 打标签并发布

### 打标签与发布

```bash
git tag -a v1.0.0 -m "v1.0.0"
```

```bash
git push origin main v1.0.0
```

```bash
gh release create v1.0.0 --title "v1.0.0 工作流公开版" --notes-file docs/RELEASE_NOTES_v1.0.0.md
```

发布说明在 [`RELEASE_NOTES_v1.0.0.md`](RELEASE_NOTES_v1.0.0.md)。其中「尚未通过完整动态出片验收」一段请保留在显眼位置。

### 版本号约定

版本号写在 `drama/workflow_build.py` 的 `__version__`，制作页面和证书都读取它。

- **补丁版本**（1.0.x）：修复问题，不改变分镜格式、配置字段和任务状态结构。
- **次版本**（1.x.0）：新增字段或功能，旧分镜和旧配置仍可使用。
- **主版本**（2.0.0）：分镜格式、配置结构或任务状态不兼容。

## 9. 反馈与贡献

- 问题和建议请提到仓库的 [Issues](https://github.com/yiten5885-a11y/comfyui-fantasy-drama-workflow/issues)。报告问题时请附上：系统与 ComfyUI 版本、开工检查的结果、任务卡片上的错误信息和 `worker.log` 末尾。不要上传模型权重或含个人信息的路径。
- 提交代码前运行 `python tools/release_check.py --write`，把更新后的 `BUILD_VERIFIED.json` 和 `SHA256SUMS` 一起提交。
- 改制作页面不需要 ComfyUI 和模型：

  ```bash
  python tools/preview_page.py
  ```

  它用演示数据在 `127.0.0.1:8199` 提供页面；加 `--ready` 可以预览全部就绪、门禁开启时的样子。

## 10. 之后的方向

按对使用者的影响排序：

1. 在真实硬件上完成一次端到端动态出片验收，并把通过条件和实测内存写进 `VALIDATION.md`。
2. 配音引擎可替换，解除对 macOS `say` 的依赖。
3. ComfyUI 地址改为读取配置。
4. 制作页面支持按任务覆盖音频和质检设置。
