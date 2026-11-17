# v1.0.0 工作流公开版

面向竖屏 3D 国风动画短剧的 ComfyUI 工作流：分镜输入 → 关键帧 → Wan 动作草稿 → Animate2 角色迁移 → 视觉质检与有限重试 → 中文配音、字幕、混音 → 1080×1920 合成。

> **尚未通过完整动态出片验收。** 此前的 Wan / Animate2 片段出现过人物、服装、姿态或画风漂移，最后一次 Animate2 任务没有完成，没有走到配音、字幕、混音和 MP4 交付。本版本代表一套结构稳定、可安装试跑的工作流，不代表已验收的端到端成片，也不含样片。模型推理默认关闭。

## 这一版有什么

- 三张画布、`drama_pipeline` 自定义节点、制作队列页面与后台服务、分镜输入约定、示例分镜。
- 一条命令安装：`python install.py /path/to/ComfyUI --apply`，默认只预览，升级时保留本机配置并备份被覆盖的文件。
- 制作页面的「开工检查」：不启动模型，逐项核对构建证书、节点、模型文件、视觉服务、配音、字体、交付目录和推理门禁，并给出修复办法。
- 分镜在导入和提交前校验，问题在启动模型之前暴露。
- 任务卡片显示五段进度、每个镜头的尝试次数和质检意见、出错原因与处理建议，可直接查看后台日志。
- `python drama/workflow_build.py --verify / --certify`：离线结构检查，以及修改配置后重签构建证书。

## 开始使用

按 [v1.0.0 开放指南](https://github.com/yiten5885-a11y/comfyui-fantasy-drama-workflow/blob/main/docs/OPEN_GUIDE_v1.0.0.md) 操作。需要自备 ComfyUI、11 个模型权重文件和一个本机视觉质检服务；按 macOS 设计。

## 验证范围

11 项离线结构检查和 47 项单元测试通过，均不启动模型。完整边界见 [VALIDATION.md](https://github.com/yiten5885-a11y/comfyui-fantasy-drama-workflow/blob/main/docs/VALIDATION.md)。

下载后可用 `shasum -a 256 -c SHA256SUMS` 核对文件。
