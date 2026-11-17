# 更新记录

## [1.1.0] - 未发布

重点优化配音与画面。发布日期在打标签时填写。完整动态出片仍未通过验收，见 `docs/VALIDATION.md`；本版本中影响生成结果的改动（动作提示词带角色外观、视频复检对照关键帧）尚未在真实模型上验证。使用说明见 `docs/OPEN_GUIDE_v1.1.0.md`。

### 配音

- 按角色选声：角色表 `voice` / `voice_rate` → 配置 `voice.per_character` → 角色表 `gender`（`male` `female` `elder` `child`）→ 名字与外观推断 → `_default`。选声逻辑集中到新模块 `drama/voice.py`。
- 可替换的配音引擎：`voice.engine` 为 `say`（macOS）或 `command`（任意写出音频文件的程序，参数数组形式，不经过 shell）。`say` 的台词改由文件传入，以 `-` 开头的台词不再被当成参数。
- 每句配音去除首尾静音、统一响度（`voice.target_rms_db`）、两端短淡入淡出。
- 长台词按标点切段逐段显示字幕，时长按字数和停顿分配；烧录字幕与 SRT 使用同一份分段；段尾逗号句号省略。
- 配乐闪避改为提前 0.18 秒、平滑起落的包络；台词时音效也自动压低。
- 新音效 `impact` `sword` `thunder`（一次性）和 `energy` `fire`（环境）；`sfx` 支持 `|` 组合和 `@秒` 定位。
- 内置国风配乐 `music.builtin.style: "guofeng"`：五声音阶拨弦加五度持续低音，冷暖色调对应羽调式与宫调式。
- 开工检查核对分镜实际用到的语音是否已安装。

### 画面

- 动态镜头变速：片段短于镜头时放慢并在相邻帧间混合，最多放慢 `cinema.max_clip_slowdown` 倍，余下时间停在最后一帧缓推；不再逐帧重复造成卡顿。
- 明显放大时加轻度 USM 锐化（`cinema.sharpen`）。
- 每个镜头的 `grade` 逐镜生效；新增分镜字段 `transition`：`dip` `dissolve` `cut` `flash`。
- Wan / Animate2 动作提示词带上角色外观锚点，并要求全程保持首帧的五官、发型、服装、配色与道具。
- 视频复检把已通过的关键帧作为角色外观基准放在最前，抽帧从 3 帧增加到 5 帧（`quality.vision.video_frames`、`video_reference`）。

### 页面

- 分镜预览：配音表（音色、语速、选声依据、是否已安装、逐角色试听）、逐句试听、镜头的运镜 / 色调 / 转场 / 音效。
- 整集声音样片：在生成画面前混出配音、音效和配乐，显示按配音调整后的镜头时长与字幕分段，可下载 SRT。不启动模型。
- 任务卡片显示每个镜头的关键帧缩略图，点开可并排查看关键帧、动作草稿和进入成片的动态镜头。
- 新接口：`POST /api/drama/projects/<项目>/voice-preview`、`POST /api/drama/projects/<项目>/soundtrack`、`GET /api/drama/preview/...`、`GET /api/drama/jobs/<任务>/media`。
- 分镜预览提示未识别的运镜、色调、转场和音效名称。

### 修复

- 从页面或画布提交时，所有角色被换成同一个声音（`per_character` 被画布音频节点覆盖）。
- 示例分镜使用的 `impact` 音效不存在，静默变成无声。
- 转场使用已烧录字幕的上一帧，上一镜的字幕以残影出现在下一镜开头。
- 整集只使用第一个镜头的色调。
- 修复字幕时间码的毫秒截断。
- 手机宽度下分镜区域被宽表格撑出横向滚动。

### 其他

- 示例分镜加入第二个角色（女声）、长台词、组合音效和白闪转场。
- `production_config.json` 升到 `version: 3`；新增键均有代码内默认值，旧配置可继续使用。
- `tests/fake_tts.py` 替身配音程序；新增 27 项测试（共 74 项）。`tools/preview_page.py` 演示任务带真实的关键帧和片段文件。

## [1.0.0] - 2026-11-17

首个公开版本。完整动态出片尚未通过验收，见 `docs/VALIDATION.md`。

### 新增

- `install.py`：一条命令安装到已有的 ComfyUI 目录。默认只预览；升级时保留本机配置、备份被覆盖的文件，并把推理门禁写回关闭状态。
- 制作页面「开工检查」和 `/api/drama/readiness` 接口：不启动模型，核对构建证书、运行系统、Python 依赖、ComfyUI 节点、模型文件、视觉质检服务、配音引擎、字幕字体、分镜项目、交付目录和推理门禁。
- 分镜预检：`/api/drama/projects/<项目>` 返回镜头数、时长、角色和提示；制作页面在提交前显示。
- `/api/drama/jobs/<任务>/log`：在页面上查看 `worker.log` 和 `compose.log` 末尾。
- `/api/drama/presets`：两档预设的实际尺寸、帧数和步数，页面不再写死。
- `/api/drama/examples`：没有任何项目时一键安装示例分镜。
- `python drama/workflow_build.py --verify`：11 项离线结构检查。`--certify`：检查通过后重写构建证书。
- `tools/release_check.py` 与 GitHub Actions：发布前检查。`tools/preview_page.py`：不依赖 ComfyUI 预览制作页面。
- `custom_nodes/drama_pipeline/requirements.txt`、`docs/OPEN_GUIDE_v1.0.0.md`。
- 20 项新测试，覆盖构建检查、开工检查、分镜摘要、安装脚本和队列接口。

### 变更

- 制作页面重做：三步提交、预设卡片、五段进度、每个镜头的尝试次数与质检意见、出错原因与处理建议、成片技术指标；支持浅色模式和窄屏。
- 上传分镜时立即校验，不合格的分镜不再入库。此前要到任务启动后才报错。
- 页面默认选中「动态预览」预设，此前默认是正式档。
- 停止任务前需要确认。
- 构建证书失效的提示里给出重签命令。

### 修复

- 全新安装时 `input/drama/` 不存在导致节点加载和页面接口报错。
- 页面每 6 秒整体重绘，会打断正在播放的成片并收起已展开的内容；现在只更新有变化的任务卡片，页面在后台时暂停轮询。
- 按 README 修改 `production_config.json` 后构建证书失效，且仓库没有提供重签办法。
- 页面把交付目录固定称作「桌面」，把角色迁移后的镜头尺寸写成动作草稿的尺寸。
- 画布节点的进度显示缺少「Animate2 角色迁移」阶段名称。
