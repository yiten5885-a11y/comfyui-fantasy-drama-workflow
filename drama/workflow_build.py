"""Build certificate and inference gate; imports no generation runtime."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys

APP = Path(__file__).resolve().parent.parent
GATE = APP / 'drama/build_gate.json'
CERTIFICATE = APP / 'drama/BUILD_VERIFIED.json'
BUILD_FILES = [
    'drama/production.py', 'drama/batch.py', 'drama/render_cinema.py',
    'drama/workflow_graph.py', 'drama/workflows/3d_full_api.json',
    'drama/workflows/animate2_transfer_api.json', 'drama/workflows/animate2_transfer_api.meta.json',
    'custom_nodes/drama_pipeline/pipeline_nodes.py',
    'drama/workflow_contract.py', 'drama/workflow_build.py', 'drama/production_config.json',
    'custom_nodes/drama_pipeline/__init__.py',
    'custom_nodes/drama_pipeline/production_server.py',
    'custom_nodes/drama_pipeline/production_nodes.py',
    'custom_nodes/drama_pipeline/production.html',
    'custom_nodes/drama_pipeline/web/production.js',
    'user/default/workflows/玄幻-全动态短剧（自动纠错重试）.json',
    'user/default/workflows/玄幻-镜头生成链（Z-Image接Wan）.json',
    'user/default/workflows/玄幻3D-完整出片工作流（展开版）.json',
]


def file_hash(path):
    path = Path(path)
    data = path.read_bytes()
    if path.suffix == '.json':
        obj = json.loads(data)
        if 'nodes' in obj and 'links' in obj:
            nodes = {str(node['id']): node for node in obj['nodes']}
            edges = []
            for link in obj['links']:
                if isinstance(link, dict):
                    source, slot, target, target_slot = link['origin_id'], link['origin_slot'], link['target_id'], link['target_slot']
                else:
                    _, source, slot, target, target_slot, _ = link
                input_name = nodes[str(target)]['inputs'][target_slot]['name']
                edges.append([str(source), slot, str(target), input_name])
            obj = {'nodes': sorted([key, node['type'], node.get('mode', 0)] for key, node in nodes.items() if node['type'] != 'MarkdownNote'),
                   'edges': sorted(edges)}
        data = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(data).hexdigest()


def build_status():
    try:
        gate = json.loads(GATE.read_text())
        if not gate.get('build_complete'):
            return False, gate.get('reason', '工作流尚未搭建完成')
        certificate = json.loads(CERTIFICATE.read_text())
        for relative in BUILD_FILES:
            if file_hash(APP / relative) != certificate['files'].get(relative):
                return False, '工作流连线或代码已修改，需要重新构建检查：' + relative
        if not gate.get('inference_enabled'):
            return False, '工作流构建已通过，自动推理已停用：' + gate.get('reason', '等待验收')
        return True, '工作流构建证书有效'
    except (OSError, ValueError, KeyError) as e:
        return False, '缺少有效工作流构建证书：' + str(e)


def require_inference():
    ready, reason = build_status()
    if not ready:raise RuntimeError('模型启动已锁定：' + reason)


def main():
    ap = argparse.ArgumentParser(description='只检查工作流文件，不启动模型')
    ap.add_argument('--status', action='store_true');args = ap.parse_args()
    ready, reason = build_status(); print(json.dumps({'inference_allowed': ready, 'reason': reason}, ensure_ascii=False))
    return 0


if __name__ == '__main__':raise SystemExit(main())
