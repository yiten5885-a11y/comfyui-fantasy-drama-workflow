"""Model-free readiness checklist shown on the production page.

Standard library only. Nothing here loads a model, submits a prompt or changes
the inference gate; every item is advisory and names the fix an operator can
apply before opening the gate.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import sys

PLACEHOLDER = 'YOUR_LOCAL_VISION_MODEL'
Z_IMAGE = [('diffusion_models', 'z_image_turbo_bf16.safetensors'),
           ('text_encoders', 'qwen_3_4b.safetensors'), ('vae', 'ae.safetensors')]


def required_models(cfg):
    """(models sub-folder, filename) pairs the default production path loads."""
    dynamic, animation = cfg['dynamic'], cfg.get('animation', {})
    wanted = Z_IMAGE + [('diffusion_models', dynamic['model']), ('text_encoders', dynamic['text_encoder']),
                        ('vae', dynamic['vae'])]
    if animation.get('enabled'):
        wanted += [('diffusion_models', animation['model']), ('text_encoders', animation['text_encoder']),
                   ('clip_vision', animation['clip_vision']), ('loras', animation['lora']), ('vae', animation['vae'])]
    return list(dict.fromkeys(wanted))


def item(key, label, state, detail, fix=''):
    return {'key': key, 'label': label, 'state': state, 'detail': detail, 'fix': fix}


def writable_parent(path):
    path = Path(path).expanduser().resolve()
    while not path.exists() and path != path.parent:path = path.parent
    return os.access(path, os.W_OK)


def checks(cfg, certificate, gate_open, projects, model_lists=None, platform=None, which=shutil.which,
           find_spec=importlib.util.find_spec, reachable=None, node_types=None):
    """Return the checklist. ``certificate`` is ``(valid, reason)``.

    States: ``ok``; ``warn`` (works, with a caveat); ``fail`` (a production run
    is expected to stop at this point); ``off`` (the inference gate, closed by
    design until the operator opens it).
    """
    platform = platform or sys.platform
    items = []
    valid, reason = certificate
    items.append(item('certificate', '构建证书', 'ok' if valid else 'fail', reason,
                      '' if valid else '如果不是有意修改，请还原该文件'))

    if platform == 'darwin':items.append(item('platform', '运行系统', 'ok', 'macOS：配音、字幕字体和后台进程均为本系统设计'))
    elif platform.startswith('win'):
        items.append(item('platform', '运行系统', 'fail', 'Windows 原生环境缺少后台制作进程依赖的 fcntl 文件锁',
                          '请在 macOS 上运行；Linux/WSL 可跑到动态镜头，配音需自行替换'))
    else:items.append(item('platform', '运行系统', 'warn', '非 macOS：生成链路可用，配音默认依赖 macOS 的 say 命令'))

    missing = [name for name, module in [('PyAV', 'av'), ('numpy', 'numpy'), ('Pillow', 'PIL'), ('psutil', 'psutil'),
                                         ('aiohttp', 'aiohttp')] if find_spec(module) is None]
    items.append(item('python', 'Python 依赖', 'fail' if missing else 'ok',
                      '缺少：' + '、'.join(missing) if missing else 'PyAV、numpy、Pillow、psutil、aiohttp 均可导入',
                      '在 ComfyUI 的 Python 环境执行 pip install -r custom_nodes/drama_pipeline/requirements.txt' if missing else ''))

    if node_types is not None:
        wanted, available = node_types
        absent = sorted(set(wanted) - set(available))
        items.append(item('nodes', 'ComfyUI 节点', 'fail' if absent else 'ok',
                          '当前 ComfyUI 没有注册：' + '、'.join(absent) if absent else '工作流用到的 %d 种节点均已注册' % len(set(wanted)),
                          '升级 ComfyUI，或安装提供这些节点的扩展后重启' if absent else ''))

    if model_lists is not None:
        absent = ['%s/%s' % (kind, name) for kind, name in required_models(cfg) if name not in model_lists.get(kind, [])]
        items.append(item('models', '模型文件', 'fail' if absent else 'ok',
                          '缺少 %d 个：\n%s' % (len(absent), '\n'.join(absent)) if absent
                          else '%d 个权重文件均已在 models 目录登记（只核对文件名，不加载）' % len(required_models(cfg)),
                          '把权重放入 ComfyUI/models 对应子目录，或在 drama/production_config.json 改成本机文件名' if absent else ''))

    vision = cfg['quality']['vision']
    if not vision.get('enabled'):
        items.append(item('vision', '视觉质检服务', 'warn', '视觉复检已关闭：只做空白帧、尺寸、帧数等技术检查'))
    elif vision.get('model') in ('', None, PLACEHOLDER):
        items.append(item('vision', '视觉质检服务', 'fail', '模型名仍是占位符 ' + PLACEHOLDER,
                          '把 drama/production_config.json 中 quality.vision.model 改成本机视觉服务提供的模型名'))
    elif reachable is not None and not reachable(vision['url']):
        items.append(item('vision', '视觉质检服务', 'fail', '连接不上 ' + vision['url'],
                          '启动本机 OpenAI 兼容的视觉服务，或修正 quality.vision.url'))
    else:items.append(item('vision', '视觉质检服务', 'ok', '%s @ %s' % (vision['model'], vision['url'])))

    voice = cfg.get('voice', {})
    if not voice.get('enabled', True):items.append(item('voice', '配音引擎', 'warn', '配音已关闭，成片只有配乐和音效'))
    elif which('say'):items.append(item('voice', '配音引擎', 'ok', 'macOS say 可用；所选中文语音包需已在系统设置中下载'))
    else:items.append(item('voice', '配音引擎', 'fail', '找不到 say 命令，逐句配音阶段会失败',
                           '在 macOS 上运行，或在画布节点「配音字幕与配乐」关闭 voice_enabled 后从画布提交'))

    font = cfg.get('subtitle', {}).get('font', '')
    present = bool(font) and Path(font).expanduser().is_file()
    items.append(item('font', '字幕字体', 'ok' if present else 'fail',
                      font if present else '字体文件不存在：' + (font or '（未设置）'),
                      '' if present else '把 subtitle.font 改成本机中文字体路径，否则片头和字幕会显示成方块'))

    count = len(projects)
    items.append(item('project', '分镜项目', 'ok' if count else 'warn',
                      '已有 %d 个项目' % count if count else 'input/drama/ 下还没有分镜项目',
                      '' if count else '在本页上传 storyboard.json，或先安装示例项目'))

    target = cfg.get('delivery', {}).get('dir', '')
    if not target:items.append(item('delivery', '交付目录', 'warn', '未设置交付目录，成片只保留在 output/drama_jobs/'))
    else:
        resolved = str(Path(target).expanduser().resolve())
        ok = writable_parent(target)
        items.append(item('delivery', '交付目录', 'ok' if ok else 'fail', resolved if ok else '无法写入：' + resolved,
                          '' if ok else '修改 delivery.dir 或画布「高清输出与交付位置」节点'))

    items.append(item('gate', '推理门禁', 'ok' if gate_open else 'off',
                      '已开启：允许提交生成任务' if gate_open else '默认关闭：不会提交任何生成任务',
                      '' if gate_open else '以上各项就绪后，把 drama/build_gate.json 的 inference_enabled 改为 true（无需重启）'))
    blocking = [entry['key'] for entry in items if entry['state'] == 'fail']
    return {'items': items, 'blocking': blocking, 'ready_for_gate': not blocking,
            'can_start': bool(valid and gate_open)}
