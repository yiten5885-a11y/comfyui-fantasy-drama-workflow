# v1.1.0 配音与画面

> **完整动态出片仍未通过验收。** 本版本中影响生成结果的两项改动——动作提示词带角色外观、视频复检对照关键帧——尚未在真实模型上验证。模型推理默认关闭。详见 [VALIDATION.md](https://github.com/yiten5885-a11y/comfyui-fantasy-drama-workflow/blob/main/docs/VALIDATION.md)。

## 配音

- **每个角色有自己的声音。** 在角色表写 `voice`、`voice_rate` 或 `gender` 即可；修复了 v1.0.0 从页面或画布提交时所有角色被换成同一个声音的问题。
- **先听再出画面。** 分镜预览里有配音表，可以逐角色、逐句试听；「整集声音样片」在生成任何画面之前混出配音、音效和配乐，并显示按配音调整后的镜头时长和字幕分段。不启动模型。
- **配音与字幕对齐。** 每句配音去首尾静音、统一响度；长台词按标点逐段显示字幕。
- **混音更干净。** 配乐提前、平滑地为台词让路，台词时音效也会压低。
- **玄幻音效与国风配乐。** 新增冲击、剑鸣、能量、雷、火；`sfx` 可组合并指定时间点。内置五声音阶拨弦配乐。
- **配音引擎可替换。** 除 macOS `say` 外，可以接入任意写出音频文件的本地程序。

## 画面

- **动态镜头不再卡顿。** 片段短于镜头时平滑放慢并在帧间混合，超出上限的部分用缓推补足。
- **放大锐化、逐镜色调、四种转场**（暗场溶解、溶解、硬切、白闪）。
- **角色一致性。** 动作提示词带上角色外观；视频复检以已通过的关键帧为基准，逐帧比对 5 帧。
- **逐镜对照。** 任务卡片显示关键帧缩略图，点开并排查看关键帧、动作草稿和进入成片的镜头。

## 修复

上一镜字幕残影带进下一镜转场；示例里的 `impact` 音效静默无声；整集只用第一个镜头的色调；手机宽度横向溢出。

## 升级

```bash
python install.py /path/to/ComfyUI --apply
```

```bash
python drama/workflow_build.py --certify
```

旧的 `production_config.json` 会保留并可以继续使用。说明见 [v1.1.0 开放指南](https://github.com/yiten5885-a11y/comfyui-fantasy-drama-workflow/blob/main/docs/OPEN_GUIDE_v1.1.0.md)，完整变更见 [CHANGELOG](https://github.com/yiten5885-a11y/comfyui-fantasy-drama-workflow/blob/main/CHANGELOG.md)。

## 验证范围

11 项离线结构检查和 74 项单元测试通过，均不启动模型；测试中的配音使用替身程序。制作页面在演示数据上做过浏览器检查。

下载后可用 `shasum -a 256 -c SHA256SUMS` 核对文件。
