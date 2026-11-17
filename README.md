# ComfyUI 3D 国风玄幻短剧工作流

[![checks](https://github.com/yiten5885-a11y/comfyui-fantasy-drama-workflow/actions/workflows/ci.yml/badge.svg)](https://github.com/yiten5885-a11y/comfyui-fantasy-drama-workflow/actions/workflows/ci.yml)

一套面向竖屏 3D 国风动画短剧的 ComfyUI 工作流，包含画布、配套自定义节点、分镜输入格式、任务队列、质检与重试、配音字幕和竖屏合成脚本。

主画布串联：分镜输入 → 关键帧 → Wan 动态草稿 → Animate2 角色与动作迁移 → 视觉质检和有限重试 → 中文配音、字幕、混音 → 1080×1920 合成。

> **完整动态出片尚未通过验收。** 之前的 Wan/Animate2 片段在视觉复检中出现人物、服装、姿态或画风漂移；最后一次 Animate2 任务没有完成，未进入最终配音、字幕、混音和 MP4 交付。本仓库代表一套已构建、可安装试跑的工作流，不代表已验收的端到端成片，也不含任何样片。详见 [`docs/VALIDATION.md`](docs/VALIDATION.md)。

**使用说明**：[v1.1.0 开放指南](docs/OPEN_GUIDE_v1.1.0.md)（配音与画面：选声、试听、整集声音样片、字幕、音效、配乐、镜头变速、转场、角色一致性）；环境准备和首次安装见 [v1.0.0 开放指南](docs/OPEN_GUIDE_v1.0.0.md) 第 2～3 节。

## 快速开始

需要自备：一份可运行的 ComfyUI、11 个模型权重文件、一个本机视觉质检服务。按 macOS 设计。

```bash
git clone https://github.com/yiten5885-a11y/comfyui-fantasy-drama-workflow.git
```

```bash
cd comfyui-fantasy-drama-workflow
```

```bash
python install.py /path/to/ComfyUI
```

上面这条只预览将要复制的文件。确认后加 `--apply` 执行，然后在 ComfyUI 根目录：

1. `pip install -r custom_nodes/drama_pipeline/requirements.txt`
2. 编辑 `drama/production_config.json`（把 `YOUR_LOCAL_VISION_MODEL` 改成本机视觉服务的模型名，核对权重文件名），再运行 `python drama/workflow_build.py --certify` 重签构建证书。
3. 重启 ComfyUI，打开 <http://127.0.0.1:8188/drama/production>，按页面顶部的「开工检查」逐项处理。
4. 全部就绪后，把 `drama/build_gate.json` 的 `inference_enabled` 改为 `true`。
5. 选择示例分镜，先用「动态预览」预设跑通，再用「正式出片」。

模板默认关闭模型推理：在你显式打开门禁之前，页面、节点和后台进程都不会提交生成请求。Wan 与 Animate2 阶段可能需要较多统一内存；开启前请按实际模型量化、分辨率和设备评估峰值内存。

## 仓库内容

- `user/default/workflows/玄幻3D-完整出片工作流（展开版）.json`：主工作流画布，40 个节点、71 条连线。
- `user/default/workflows/玄幻-全动态短剧（自动纠错重试）.json`：简化制作控制画布。
- `user/default/workflows/玄幻-镜头生成链（Z-Image接Wan）.json`：单镜头生成链。
- `custom_nodes/drama_pipeline/`：ComfyUI 节点、制作队列页面和后台服务。
- `drama/`：任务调度、分镜校验、API 图编译、渲染、开工检查、构建证书和默认配置。
- `examples/`：虚构的分镜、角色和 CSV 示例。
- `install.py`：安装脚本。`tools/`：发布前检查和页面预览。`tests/`：不启动模型的单元测试。

手动安装时，把 `drama/`、`custom_nodes/drama_pipeline/` 和 `user/default/workflows/` 复制到 ComfyUI 根目录的对应位置，把分镜项目放到 `ComfyUI/input/drama/<项目名>/`。仓库不包含模型权重、ComfyUI 源码或 Python 虚拟环境。

## 分镜格式

每个项目一个文件夹，放 `storyboard.json`，或 `shots.csv` 加 `characters.json`。字段示例见 [`examples/`](examples/)。`description` 描述首帧画面，`video_prompt` 描述连续动作；复杂动作可以填 `end_frame` 并提供同名尾帧图。分镜在导入和提交时校验，页面会指出具体是哪个镜头的哪个字段有问题。

v1.1.0 起，角色表可以写 `voice`、`voice_rate`、`gender` 指定配音；每个镜头可以写 `grade`（色调）、`transition`（`dip` `dissolve` `cut` `flash`）和 `sfx`（如 `wind|sword@1.4`）。可用名称见开放指南第 3.6 节和第 4.3 节。

## 验收边界

v1.1.0 在没有 ComfyUI、模型和 GPU 的干净环境中通过了 11 项离线结构检查和 74 项单元测试（配音使用替身程序），制作页面在演示数据上做过浏览器检查。这些检查都不启动模型，也不能说明成片质量。完整动态出片尚未通过验收；本版本中动作提示词带角色外观、视频复检对照关键帧两项改动还没有在真实模型上验证。

详细边界见 [`docs/VALIDATION.md`](docs/VALIDATION.md)，更新记录见 [`CHANGELOG.md`](CHANGELOG.md)，依赖许可见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

---

# ComfyUI 3D Fantasy Drama Workflow

A local ComfyUI workflow for vertical 3D Chinese fantasy animation: storyboard intake, keyframes, Wan motion drafts, Animate2 transfer, bounded visual retry, Chinese voice/subtitles, audio mix, and 1080×1920 composition. Model weights and ComfyUI itself are not included. Dynamic end-to-end acceptance has not passed; read the validation boundary above before production use.

Install with `python install.py /path/to/ComfyUI --apply`, then follow the readiness checklist at `/drama/production`. Inference stays disabled until you open `drama/build_gate.json` yourself. Guides are in Chinese: [`docs/OPEN_GUIDE_v1.1.0.md`](docs/OPEN_GUIDE_v1.1.0.md) (voice and picture) and [`docs/OPEN_GUIDE_v1.0.0.md`](docs/OPEN_GUIDE_v1.0.0.md) (setup).
