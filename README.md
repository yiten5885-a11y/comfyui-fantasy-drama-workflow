# ComfyUI 3D 国风玄幻短剧工作流

这是一套面向竖屏 3D 国风动画短剧的 ComfyUI 工作流，包含画布、配套自定义节点、分镜输入格式、任务队列、质检与重试、配音字幕和竖屏合成脚本。

主画布串联：分镜输入 → 关键帧 → Wan 动态草稿 → Animate2 角色与动作迁移 → 视觉质检和有限重试 → 中文配音、字幕、混音 → 1080×1920 合成。

## 仓库内容

- `user/default/workflows/玄幻3D-完整出片工作流（展开版）.json`：主工作流画布，40 个节点、71 条连线。
- `user/default/workflows/玄幻-全动态短剧（自动纠错重试）.json`：简化制作控制画布。
- `user/default/workflows/玄幻-镜头生成链（Z-Image接Wan）.json`：单镜头生成链。
- `custom_nodes/drama_pipeline/`：ComfyUI 节点、制作队列页面和后台服务。
- `drama/`：任务调度、分镜校验、API 图编译、渲染和默认配置。
- `examples/`：虚构的分镜、角色和 CSV 示例。

## 安装

1. 先备份 ComfyUI 根目录、现有自定义节点和工作流。
2. 将本仓库内容复制到 ComfyUI 根目录，使 `drama/`、`custom_nodes/drama_pipeline/` 和 `user/default/workflows/` 位于对应位置。若 ComfyUI 使用其他用户配置目录，请将工作流文件合并到该目录。
3. 在 ComfyUI 正在使用的 Python 环境中安装缺少的依赖。自定义节点使用 ComfyUI API，以及 `aiohttp`、`psutil`、`numpy`、`Pillow` 和 PyAV（`av`）。
4. 将分镜项目放到 `ComfyUI/input/drama/<项目名>/`。可提交 `storyboard.json`，也可使用兼容的 `shots.csv` 与 `characters.json`。字段示例见 `examples/`。
5. 编辑 `drama/production_config.json`，填写本机可用的模型名称、服务地址、语音和交付目录。`YOUR_LOCAL_VISION_MODEL` 是占位符，必须改成你本机视觉质检服务实际提供的模型名。
6. 重启 ComfyUI，打开“玄幻3D-完整出片工作流（展开版）”，确认自定义节点已加载。模板默认关闭模型推理；先核对工作流、模型文件和目标设备内存预算，再由操作者显式开启 `drama/build_gate.json` 中的推理门禁。

仓库不包含模型权重、ComfyUI 源码或 Python 虚拟环境。Wan 与 Animate2 阶段可能需要较多统一内存；开启前应按实际模型量化、分辨率和目标设备评估峰值内存。

## 验收边界

2026-11-12 的原始构建记录为：15 项离线结构检查和 27 项模拟制作逻辑测试通过，期间没有启动模型。随后为公开发布替换了本机路径和项目示例；测试套件随仓库提供，但没有在这些发布清理修改后重跑。主画布保持 40 个节点、71 条连线。

**完整动态出片尚未通过验收。** 之前的 Wan/Animate2 片段在视觉复检中出现人物、服装、姿态或画风漂移；最后一次 Animate2 任务没有完成，未进入最终配音、字幕、混音和 MP4 交付。本仓库因此代表一套已构建的工作流，不代表已验收的端到端成片，也不含任何样片。

详细边界见 [`docs/VALIDATION.md`](docs/VALIDATION.md)，依赖许可见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

---

# ComfyUI 3D Fantasy Drama Workflow

A local ComfyUI workflow for vertical 3D Chinese fantasy animation: storyboard intake, keyframes, Wan motion drafts, Animate2 transfer, bounded visual retry, Chinese voice/subtitles, audio mix, and 1080×1920 composition. Model weights and ComfyUI itself are not included. Dynamic end-to-end acceptance has not passed; read the validation boundary above before production use.
