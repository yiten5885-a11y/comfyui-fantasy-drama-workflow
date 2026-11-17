"""Build certificate and inference gate; imports no generation runtime."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys

__version__ = '1.1.0'
APP = Path(__file__).resolve().parent.parent
GATE = APP / 'drama/build_gate.json'
CERTIFICATE = APP / 'drama/BUILD_VERIFIED.json'
BUILD_FILES = [
    'drama/production.py', 'drama/batch.py', 'drama/render_cinema.py',
    'drama/workflow_graph.py', 'drama/workflows/3d_full_api.json',
    'drama/workflows/animate2_transfer_api.json', 'drama/workflows/animate2_transfer_api.meta.json',
    'custom_nodes/drama_pipeline/pipeline_nodes.py',
    'drama/workflow_contract.py', 'drama/workflow_build.py', 'drama/readiness.py', 'drama/production_config.json',
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


def certificate_status():
    """Whether every build file still matches the certificate; ignores the gate switch."""
    try:
        if not json.loads(GATE.read_text()).get('build_complete'):
            return False, json.loads(GATE.read_text()).get('reason', '工作流尚未搭建完成')
        certificate = json.loads(CERTIFICATE.read_text())
        for relative in BUILD_FILES:
            if file_hash(APP / relative) != certificate['files'].get(relative):
                return False, '工作流连线或代码已修改，需要重新构建检查：' + relative + '（确认修改后运行 python drama/workflow_build.py --certify）'
        return True, '工作流构建证书有效'
    except (OSError, ValueError, KeyError) as e:
        return False, '缺少有效工作流构建证书：' + str(e)


def build_status():
    valid, reason = certificate_status()
    if not valid:return False, reason
    gate = json.loads(GATE.read_text())
    if not gate.get('inference_enabled'):
        return False, '工作流构建已通过，自动推理已停用：' + gate.get('reason', '等待验收')
    return True, reason


def require_inference():
    ready, reason = build_status()
    if not ready:raise RuntimeError('模型启动已锁定：' + reason)


def canvas_problems(obj):
    """Dangling links in a saved canvas: every link must join two existing slots."""
    nodes = {node['id']: node for node in obj['nodes']}
    problems = []
    for link in obj['links']:
        if isinstance(link, dict):link = [link['id'], link['origin_id'], link['origin_slot'], link['target_id'], link['target_slot'], link.get('type')]
        number, source, slot, target, target_slot = link[:5]
        if source not in nodes or target not in nodes:problems.append('连线 %s 指向不存在的节点' % number);continue
        if slot >= len(nodes[source].get('outputs', [])):problems.append('连线 %s 的输出口不存在' % number)
        inputs = nodes[target].get('inputs', [])
        if target_slot >= len(inputs) or inputs[target_slot].get('link') != number:problems.append('连线 %s 与目标输入口不一致' % number)
    return problems


def static_checks():
    """Offline structure checks. Imports no generation runtime and starts no model."""
    sys.path.insert(0, str(APP))
    try:
        from drama import workflow_contract as contract, workflow_graph as graphs
    finally:
        sys.path.pop(0)
    results = []

    def check(name, run):
        try:results.append({'name': name, 'ok': True, 'detail': run() or ''})
        except Exception as e:results.append({'name': name, 'ok': False, 'detail': '%s: %s' % (type(e).__name__, e)})

    def files_present():
        missing = [relative for relative in BUILD_FILES if not (APP / relative).is_file()]
        if missing:raise ValueError('缺少 ' + '、'.join(missing))
        return '%d 个构建文件' % len(BUILD_FILES)

    def python_syntax():
        sources = [relative for relative in BUILD_FILES if relative.endswith('.py')]
        for relative in sources:ast.parse((APP / relative).read_text(encoding='utf-8'), relative)
        return '%d 个 Python 文件' % len(sources)

    def json_syntax():
        documents = [relative for relative in BUILD_FILES if relative.endswith('.json')]
        for relative in documents:json.loads((APP / relative).read_text(encoding='utf-8-sig'))
        return '%d 个 JSON 文件' % len(documents)

    canvases = [relative for relative in BUILD_FILES if relative.startswith('user/default/workflows/')]

    def canvas_links():
        sizes = []
        for relative in canvases:
            obj = json.loads((APP / relative).read_text())
            problems = canvas_problems(obj)
            if problems:raise ValueError(relative + '：' + '；'.join(problems[:5]))
            sizes.append('%d 节点 / %d 连线' % (len(obj['nodes']), len(obj['links'])))
        return '，'.join(sizes)

    def canvas_complete():
        main = next(relative for relative in canvases if '完整出片' in relative)
        types = [node['type'] for node in json.loads((APP / main).read_text())['nodes']]
        wrong = [name for name in graphs.REQUIRED + ['DramaBatchRun', 'DramaAnimate2Settings'] if types.count(name) != 1]
        if wrong:raise ValueError('主画布应各有一个：' + '、'.join(wrong))
        return '主画布含全部 %d 类必需节点' % (len(graphs.REQUIRED) + 2)

    base = json.loads((APP / 'drama/production_config.json').read_text())
    asset = {'input_image': 'drama_jobs/check/S01.png', 'end_input_image': 'drama_jobs/check/S01_end.png',
             'driver_video': 'drama_jobs/check/S01_driver.mp4'}

    def main_graph():
        graph, _ = graphs.capture(json.loads(graphs.DEFAULT_API.read_text()))
        stages = [stage for stage in graphs.STAGES if stage != 'video_transfer']
        for stage in stages:
            compiled = graphs.compile_stage(graph, stage, 'check', 0, 1, asset=asset, preset=base['dynamic'])
            if '__stage_result' not in compiled:raise ValueError(stage + ' 阶段缺少回执节点')
        return '可编译 %d 个阶段：%s' % (len(stages), '、'.join(stages))

    def animate2_graph():
        graph, _ = graphs.capture_animate2()
        compiled = graphs.compile_animate2_stage(graph, 'check', 0, 1, asset, {**base['animation'], **base['animation']['presets']['production']})
        if '__stage_result' not in compiled:raise ValueError('Animate2 阶段缺少回执节点')
        return 'Animate2 迁移分支可编译，含参考潜帧与续接重叠帧裁剪'

    def gated_loaders():
        fields = {'UNETLoader': 'unet_name', 'CLIPLoader': 'clip_name', 'VAELoader': 'vae_name',
                  'CLIPVisionLoader': 'clip_name', 'LoraLoaderModelOnly': 'lora_name'}
        count = 0
        for path in (graphs.DEFAULT_API, graphs.ANIMATE2_API):
            graph = json.loads(path.read_text());gate = graphs.node_id(graph, 'DramaModelGate')
            for key, node in graph.items():
                if node['class_type'] in fields:
                    value = node['inputs'].get(fields[node['class_type']])
                    if not isinstance(value, list) or str(value[0]) != gate:
                        raise ValueError('%s 节点 %s 的模型文件名没有经过启动门禁' % (path.name, key))
                    count += 1
        return '%d 个模型加载节点全部接在启动门禁之后' % count

    def presets():
        graph, _ = graphs.capture(json.loads(graphs.DEFAULT_API.read_text()))
        for preset in contract.VIDEO_PRESETS:
            settings = graphs.graph_settings(graph, base)
            if preset == 'preview':settings['dynamic'].update(contract.VIDEO_PRESETS['preview'])
            contract.configure(base, preset, settings)
        return '预设 ' + '、'.join(contract.VIDEO_PRESETS) + ' 均通过尺寸与帧数约束'

    def example():
        folder = APP / 'examples'
        if not (folder / 'storyboard.json').is_file():return '未随安装复制示例，跳过'
        summary = contract.storyboard_summary(folder, base)
        return '示例分镜 %d 个镜头通过校验' % summary['shot_count']

    def gate_file():
        gate = json.loads(GATE.read_text())
        if not isinstance(gate.get('build_complete'), bool) or not isinstance(gate.get('inference_enabled'), bool):
            raise ValueError('build_gate.json 需要布尔值 build_complete 与 inference_enabled')
        return '推理门禁当前' + ('开启' if gate['inference_enabled'] else '关闭')

    for name, run in [('构建文件齐全', files_present), ('Python 语法', python_syntax), ('JSON 语法', json_syntax),
                      ('画布连线完整', canvas_links), ('主画布节点齐备', canvas_complete), ('主图分阶段编译', main_graph),
                      ('Animate2 分支编译', animate2_graph), ('模型加载受门禁约束', gated_loaders),
                      ('制作预设约束', presets), ('示例分镜校验', example), ('门禁文件格式', gate_file)]:
        check(name, run)
    return results


def certify():
    """Run the static checks and, only if all pass, rewrite the hash certificate.

    The inference gate is never touched: certifying a build does not enable it.
    """
    results = static_checks()
    if not all(result['ok'] for result in results):return False, results
    certificate = {
        'version': __version__,
        'build_status': 'passed_static_file_integrity',
        'hash_mode': 'Python/JS/HTML bytes; JSON normalized; workflow graph topology and API graph inputs hashed',
        'checks': [result['name'] for result in results],
        'files': {relative: file_hash(APP / relative) for relative in BUILD_FILES},
        'note': 'Static structure checks only; no model was started. Inference remains disabled by default.',
    }
    CERTIFICATE.write_text(json.dumps(certificate, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return True, results


def main():
    ap = argparse.ArgumentParser(description='只检查工作流文件，不启动模型')
    ap.add_argument('--status', action='store_true', help='显示构建证书与推理门禁状态（默认）')
    ap.add_argument('--verify', action='store_true', help='运行离线结构检查，任何一项失败则返回非零')
    ap.add_argument('--certify', action='store_true', help='结构检查全部通过后重写构建证书；不会开启推理门禁')
    args = ap.parse_args()
    if args.verify or args.certify:
        passed, results = certify() if args.certify else (None, static_checks())
        if passed is None:passed = all(result['ok'] for result in results)
        for result in results:print(('通过  ' if result['ok'] else '失败  ') + result['name'] + '：' + result['detail'])
        print('%d / %d 项通过%s' % (sum(result['ok'] for result in results), len(results),
                                 '，已写入 ' + CERTIFICATE.name if args.certify and passed else ''))
        if not passed:return 1
    ready, reason = build_status()
    print(json.dumps({'version': __version__, 'inference_allowed': ready, 'reason': reason}, ensure_ascii=False))
    return 0


if __name__ == '__main__':raise SystemExit(main())
