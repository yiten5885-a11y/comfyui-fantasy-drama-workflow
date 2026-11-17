# 更新记录

## [1.0.0] - 未发布

首个公开版本。发布日期在打标签时填写。完整动态出片尚未通过验收，见 `docs/VALIDATION.md`。

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
