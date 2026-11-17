#!/usr/bin/env python3
"""Copy this workflow into an existing ComfyUI folder. Standard library only.

    python install.py /path/to/ComfyUI            # dry run: only lists what would change
    python install.py /path/to/ComfyUI --apply    # copy; replaced files are backed up first

Your own production_config.json is kept on upgrade (pass --reset-config to
replace it). Whenever code or workflow files change, the inference gate is
written back to its shipped, closed state. No model is downloaded or started.
"""
import argparse
import filecmp
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from drama.workflow_build import BUILD_FILES, __version__

CONFIG = 'drama/production_config.json'
GATE = 'drama/build_gate.json'
EXTRA = ['drama/BUILD_VERIFIED.json', 'custom_nodes/drama_pipeline/requirements.txt']
EXAMPLE_FILES = ['storyboard.json', 'shots.csv', 'characters.json']


def install(root, apply=False, reset_config=False):
    root = Path(root).expanduser().resolve()
    if not (root / 'main.py').is_file() or not (root / 'custom_nodes').is_dir():
        raise SystemExit('这不像 ComfyUI 根目录（需要 main.py 和 custom_nodes/）：%s' % root)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    plan = {'copy': [], 'replace': [], 'kept': [], 'same': [], 'gate_closed': False, 'example': False}

    def place(relative, source=None):
        source = source or REPO / relative
        target = root / relative
        if target.is_file() and filecmp.cmp(source, target, shallow=False):plan['same'].append(relative);return False
        if target.is_file() and relative == CONFIG and not reset_config:plan['kept'].append(relative);return False
        plan['replace' if target.is_file() else 'copy'].append(relative)
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():shutil.copy2(target, target.with_name(target.name + '.bak-' + stamp))
            shutil.copy2(source, target)
        return True

    changed = [place(relative) for relative in BUILD_FILES + EXTRA]
    if any(changed) or not (root / GATE).is_file():
        shipped = json.loads((REPO / GATE).read_text())
        current = json.loads((root / GATE).read_text()) if (root / GATE).is_file() else None
        if current != shipped:
            plan['gate_closed'] = True
            if apply:
                (root / GATE).parent.mkdir(parents=True, exist_ok=True)
                (root / GATE).write_text(json.dumps(shipped, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    project = json.loads((REPO / CONFIG).read_text()).get('default_project') or 'example-fantasy-story'
    example = root / 'input/drama' / project
    if not example.exists():
        plan['example'] = True
        if apply:
            example.mkdir(parents=True)
            for name in EXAMPLE_FILES:shutil.copy2(REPO / 'examples' / name, example / name)
    return plan


def main():
    ap = argparse.ArgumentParser(description='把玄幻短剧工作流 v%s 安装到已有的 ComfyUI 目录' % __version__)
    ap.add_argument('comfyui', help='ComfyUI 根目录（含 main.py）')
    ap.add_argument('--apply', action='store_true', help='实际执行；不加时只预览')
    ap.add_argument('--reset-config', action='store_true', help='用仓库默认值覆盖已有的 production_config.json（会先备份）')
    args = ap.parse_args()
    plan = install(args.comfyui, apply=args.apply, reset_config=args.reset_config)
    root = Path(args.comfyui).expanduser().resolve()
    for key, label in [('copy', '新增'), ('replace', '覆盖（旧文件备份为 .bak-时间戳）'), ('kept', '保留你的版本'), ('same', '已是最新')]:
        if plan[key]:print('%s %d 个：' % (label, len(plan[key])) + ('\n  ' + '\n  '.join(plan[key]) if key != 'same' else ''))
    if plan['gate_closed']:print('推理门禁：写回默认的关闭状态')
    if plan['example']:print('示例项目：安装到 input/drama/')
    if not args.apply:
        print('\n以上为预览，未做任何修改。确认后加 --apply 执行。');return 0
    status = subprocess.run([sys.executable, str(root / 'drama/workflow_build.py'), '--status'], capture_output=True, text=True)
    print('\n已安装 v%s 到 %s\n构建状态：%s' % (__version__, root, status.stdout.strip() or status.stderr.strip()))
    print('下一步：\n  1. 在 ComfyUI 的 Python 环境执行 pip install -r custom_nodes/drama_pipeline/requirements.txt'
          '\n  2. 编辑 drama/production_config.json 后运行 python drama/workflow_build.py --certify'
          '\n  3. 重启 ComfyUI，打开 http://127.0.0.1:8188/drama/production 按“开工检查”逐项处理')
    return 0


if __name__ == '__main__':raise SystemExit(main())
