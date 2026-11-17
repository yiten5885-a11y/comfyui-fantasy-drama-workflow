#!/usr/bin/env python3
"""One command for the release checklist. Repository-only; starts no model.

    python tools/release_check.py            # verify; non-zero exit if anything is off
    python tools/release_check.py --write    # also regenerate BUILD_VERIFIED.json and SHA256SUMS
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from drama import workflow_build as build

SUMS = ROOT / 'SHA256SUMS'
TEXT = {'.py', '.json', '.md', '.html', '.js', '.csv', '.txt', '.yml'}
PRIVATE = re.compile(r'/Users/[^/\s"\']+|/home/[^/\s"\']+|[A-Za-z]:\\\\Users\\\\')


def tracked():
    """Files a release contains: tracked ones plus new ones not ignored, minus deletions."""
    out = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT, capture_output=True, check=True).stdout
    names = {name for name in out.decode().split('\0') if name and (ROOT / name).is_file()}
    return sorted(names, key=lambda name: name.encode())


def sums():
    return ''.join('%s  %s\n' % (hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), name)
                   for name in tracked() if name != SUMS.name)


def main():
    ap = argparse.ArgumentParser(description='发布前检查：测试、结构检查、构建证书、校验和、脱敏')
    ap.add_argument('--write', action='store_true', help='检查通过的同时重写构建证书与 SHA256SUMS')
    args = ap.parse_args()
    failures = []

    def report(name, ok, detail=''):
        print(('通过  ' if ok else '失败  ') + name + ('：' + detail if detail else ''))
        if not ok:failures.append(name)

    if args.write:
        passed, results = build.certify()
        report('离线结构检查并重写构建证书', passed, '；'.join(r['name'] + ' ' + r['detail'] for r in results if not r['ok']))
    else:
        results = build.static_checks()
        report('离线结构检查 %d 项' % len(results), all(r['ok'] for r in results), '；'.join(r['name'] + ' ' + r['detail'] for r in results if not r['ok']))
    valid, reason = build.certificate_status()
    report('构建证书与文件一致', valid, '' if valid else reason)
    report('推理门禁默认关闭', json.loads(build.GATE.read_text()).get('inference_enabled') is False)

    tests = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], cwd=ROOT, capture_output=True, text=True)
    summary = re.search(r'Ran (\d+) tests', tests.stderr)
    report('单元测试', tests.returncode == 0, (summary.group(1) + ' 项') if summary else tests.stderr[-400:])

    config = json.loads((ROOT / 'drama/production_config.json').read_text())
    report('配置中的本机模型名仍为占位符', config['quality']['vision']['model'] == 'YOUR_LOCAL_VISION_MODEL' == config['llm']['model'])
    leaks = []
    for name in tracked():
        if Path(name).suffix in TEXT and name != 'tools/release_check.py':
            leaks += ['%s: %s' % (name, hit) for hit in PRIVATE.findall((ROOT / name).read_text(encoding='utf-8', errors='replace'))]
    report('没有本机用户路径', not leaks, '；'.join(leaks[:5]))
    report('CHANGELOG 含当前版本', ('## [%s]' % build.__version__) in (ROOT / 'CHANGELOG.md').read_text(), 'v' + build.__version__)

    expected = sums()
    if args.write:SUMS.write_text(expected, encoding='utf-8')
    report('SHA256SUMS 与文件一致', SUMS.read_text(encoding='utf-8') == expected, '%d 个文件' % expected.count('\n'))

    print('\n%s（v%s）' % ('全部通过，可以发布' if not failures else '未通过：' + '、'.join(failures), build.__version__))
    return 1 if failures else 0


if __name__ == '__main__':raise SystemExit(main())
