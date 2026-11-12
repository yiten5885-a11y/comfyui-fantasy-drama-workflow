"""Compile the visible production graph into resumable stage executions."""
import copy
import hashlib
import json
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
DEFAULT_API = APP / 'drama/workflows/3d_full_api.json'
ANIMATE2_API = APP / 'drama/workflows/animate2_transfer_api.json'
STAGES = {
    'image': ('DramaSaveKeyframe', 0),
    'image_review': ('DramaKeyframeReview', 1),
    'video': ('DramaSaveClip', 0),
    'video_transfer': ('DramaSaveAnimate2Clip', 0),
    'video_review': ('DramaVideoReview', 1),
    'compose': ('DramaDeliverEpisode', 0),
}
REQUIRED = ['DramaStoryboardInput', 'DramaShotPlan', 'DramaModelGate',
            'DramaQualitySettings', 'DramaAudioSettings', 'DramaOutputSettings',
            'DramaSaveKeyframe', 'DramaKeyframeReview', 'DramaSaveClip', 'DramaEndpointFrames',
            'DramaVideoReview', 'DramaEpisodeAssets', 'DramaVoiceLines',
            'DramaTimelineSubtitles', 'DramaMixSound', 'DramaComposeEpisode',
            'DramaFinalReview', 'DramaDeliverEpisode']


def node_id(graph, class_type):
    found = [key for key, node in graph.items() if node['class_type'] == class_type]
    if len(found) != 1:raise ValueError('完整图需要一个 ' + class_type)
    return found[0]


def ancestors(graph, leaf):
    result = {}
    def visit(key):
        if key in result:return
        node = graph[key];result[key] = copy.deepcopy(node)
        for value in node['inputs'].values():
            if isinstance(value, list) and len(value) == 2:visit(str(value[0]))
    visit(str(leaf))
    return result


def capture(prompt):
    graph = copy.deepcopy(prompt)
    for key in list(graph):
        if graph[key]['class_type'] == 'DramaBatchRun':del graph[key]
    for name in REQUIRED:node_id(graph, name)
    # Animate2 lives in its own immutable stage template and is captured below
    # by capture_animate2(); the main Wan workflow must not contain its saver.
    for stage, (target, _) in STAGES.items():
        if stage != 'video_transfer':ancestors(graph, node_id(graph, target))
    # Retain only execution data in the fingerprint; titles remain in the saved graph.
    execution = {key: {'class_type': node['class_type'], 'inputs': node['inputs']} for key, node in graph.items()}
    fingerprint = hashlib.sha256(json.dumps(execution, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return graph, fingerprint


def capture_animate2(prompt=None):
    graph = copy.deepcopy(prompt if prompt is not None else json.loads(ANIMATE2_API.read_text()))
    for name in ['DramaStoryboardInput', 'DramaShotPlan', 'WanAnimate2ToVideo', 'DramaEndpointFrames', 'DramaSaveAnimate2Clip']:
        node_id(graph, name)
    image_nodes = ancestors(graph, node_id(graph, 'DramaSaveAnimate2Clip'))
    if not any(node['class_type'] == 'TrimVideoLatent' for node in image_nodes.values()):
        raise ValueError('Animate2 分支缺少参考潜帧裁剪')
    if not any(node['class_type'] == 'ImageFromBatch' and node['inputs'].get('batch_index') == [node_id(image_nodes, 'WanAnimate2ToVideo'), 4]
               for node in image_nodes.values()):
        raise ValueError('Animate2 分支缺少续接重叠帧裁剪')
    gate = node_id(graph, 'DramaModelGate')
    for class_type, field in [('UNETLoader', 'unet_name'), ('CLIPLoader', 'clip_name'),
                              ('CLIPVisionLoader', 'clip_name'), ('LoraLoaderModelOnly', 'lora_name'),
                              ('VAELoader', 'vae_name')]:
        loader = next(node for node in image_nodes.values() if node['class_type'] == class_type)
        if loader['inputs'].get(field, [None])[0] != gate:
            raise ValueError(class_type + ' 未连接到 Animate2 模型启动门禁')
    execution = {key: {'class_type': node['class_type'], 'inputs': node['inputs']} for key, node in graph.items()}
    fingerprint = hashlib.sha256(json.dumps(execution, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return graph, fingerprint


def graph_settings(graph, base):
    shot = graph[node_id(graph, 'DramaShotPlan')]['inputs']
    quality = dict(graph[node_id(graph, 'DramaQualitySettings')]['inputs'])
    audio = dict(graph[node_id(graph, 'DramaAudioSettings')]['inputs'])
    output = dict(graph[node_id(graph, 'DramaOutputSettings')]['inputs'])
    gate = graph[node_id(graph, 'DramaModelGate')]['inputs']
    animation_settings = [node['inputs'] for node in graph.values() if node['class_type'] == 'DramaAnimate2Settings']
    image_nodes = ancestors(graph, node_id(graph, 'DramaSaveKeyframe'))
    image_sampler = next(n['inputs'] for n in image_nodes.values() if n['class_type'] == 'KSampler')
    video_nodes = ancestors(graph, node_id(graph, 'DramaSaveClip'))
    video_sampler = next(n['inputs'] for key, n in video_nodes.items() if n['class_type'] == 'KSampler' and key not in image_nodes)
    latent = next(n['inputs'] for n in video_nodes.values() if n['class_type'] in ('Wan22ImageToVideoLatent', 'WanFirstLastFrameToVideo'))
    video = next(n['inputs'] for n in video_nodes.values() if n['class_type'] == 'CreateVideo')
    negative_node = graph[str(video_sampler['negative'][0])]
    if negative_node['class_type'] == 'WanFirstLastFrameToVideo':
        negative_node = graph[str(negative_node['inputs']['negative'][0])]
    negative = negative_node['inputs']['text']
    animation_preset = animation_settings[0]['preset'] if animation_settings else base['animation']['preset']
    animation = copy.deepcopy(base['animation']['presets'][animation_preset])
    animation.update({
        'enabled': True, 'preset': animation_preset,
        'model': gate.get('animate2_model', base['animation']['model']),
        'text_encoder': gate.get('animate2_text', base['animation']['text_encoder']),
        'clip_vision': gate.get('animate2_clip', base['animation']['clip_vision']),
        'lora': gate.get('animate2_lora', base['animation']['lora']),
        'vae': gate.get('animate2_vae', base['animation']['vae']),
    })
    return {
        'image': {'width': shot['default_width'], 'height': shot['default_height'],
                  'style': shot['style'], 'steps': image_sampler['steps'], 'cfg': image_sampler['cfg'],
                  'sampler': image_sampler['sampler_name'], 'scheduler': image_sampler['scheduler']},
        'dynamic': {'width': latent['width'], 'height': latent['height'], 'frames': latent['length'],
                    'steps': video_sampler['steps'], 'cfg': video_sampler['cfg'],
                    'sampler': video_sampler['sampler_name'], 'fps': video['fps'], 'negative': negative,
                    'model': gate.get('wan_model', base['dynamic']['model']),
                    'text_encoder': gate.get('wan_text', base['dynamic']['text_encoder']),
                    'vae': gate.get('wan_vae', base['dynamic']['vae'])},
        'quality': {'max_retries': quality['max_retries'], 'stage_timeout_sec': quality['timeout_minutes'] * 60,
                    'vision': {**base['quality']['vision'], 'enabled': quality['visual_review']}},
        'animation': animation,
        'audio_settings': audio,
        'cinema': {'width': output['width'], 'height': output['height'], 'fps': output['fps']},
        'delivery': {'dir': output['delivery_dir']},
    }


def compile_stage(graph, stage, job_id, index, attempt, asset=None, preset=None):
    graph = copy.deepcopy(graph)
    board_id = node_id(graph, 'DramaStoryboardInput')
    graph[board_id]['inputs']['job_id'] = job_id
    shot_id = node_id(graph, 'DramaShotPlan')
    graph[shot_id]['inputs'].update(index=index, attempt=attempt, stage=stage)
    if preset:
        if stage == 'video':
            for node in graph.values():
                if node['class_type'] in ('Wan22ImageToVideoLatent', 'WanFirstLastFrameToVideo'):
                    node['inputs'].update(width=preset['width'], height=preset['height'], length=preset['frames'])
                elif node['class_type'] == 'CreateVideo':node['inputs']['fps'] = preset['fps']
            image_nodes = ancestors(graph, node_id(graph, 'DramaSaveKeyframe'))
            for key, node in graph.items():
                if node['class_type'] == 'KSampler' and key not in image_nodes:node['inputs']['steps'] = preset['steps']
    if stage == 'image_review':
        target = node_id(graph, 'DramaKeyframeReview');port = 'asset'
    elif stage == 'video':
        target = node_id(graph, 'DramaKeyframeReview');port = None
    elif stage == 'video_review':
        target = node_id(graph, 'DramaVideoReview');port = 'clip'
    elif stage == 'compose':
        target = node_id(graph, 'DramaEpisodeAssets');port = 'approved_clip'
    else:target = port = None
    if target is not None:
        key = '__resume_asset'
        if stage == 'video':
            graph[target] = {'class_type': 'LoadImage', 'inputs': {'image': asset['input_image']}}
            if asset.get('end_input_image'):
                end_key = '__end_frame'
                graph[end_key] = {'class_type': 'LoadImage', 'inputs': {'image': asset['end_input_image']}}
                wan = next((node for node in graph.values() if node['class_type'] == 'WanFirstLastFrameToVideo'), None)
                if wan is None:raise ValueError('完整工作流缺少 WanFirstLastFrameToVideo 首尾帧条件节点')
                wan['inputs']['end_image'] = [end_key, 0]
                endpoint = next((node for node in graph.values() if node['class_type'] == 'DramaEndpointFrames'), None)
                if endpoint is None:raise ValueError('完整工作流缺少 DramaEndpointFrames 硬锁端点节点')
                endpoint['inputs']['end_image'] = [end_key, 0]
        else:
            graph[key] = {'class_type': 'DramaStoredAsset', 'inputs': {'job_id': job_id,
                          'shot_index': index, 'kind': 'image' if stage == 'image_review' else 'video'}}
            graph[target]['inputs'][port] = [key, 0]
    cls, slot = STAGES[stage];leaf = node_id(graph, cls)
    graph = ancestors(graph, leaf)
    graph['__stage_result'] = {'class_type': 'DramaStageOutput', 'inputs': {'value': [leaf, slot]}}
    return graph


def compile_animate2_stage(graph, job_id, index, attempt, asset, preset):
    graph = copy.deepcopy(graph)
    graph[node_id(graph, 'DramaStoryboardInput')]['inputs']['job_id'] = job_id
    graph[node_id(graph, 'DramaShotPlan')]['inputs'].update(index=index, attempt=attempt, stage='video')
    for key, node in graph.items():
        inputs = node['inputs']
        if node['class_type'] == 'DramaModelGate':
            inputs.update(animate2_model=preset['model'], animate2_text=preset['text_encoder'],
                          animate2_clip=preset['clip_vision'], animate2_lora=preset['lora'],
                          animate2_vae=preset['vae'])
        elif node['class_type'] == 'LoadImage' and inputs.get('image') == 'reference_pending.png':
            inputs['image'] = asset['input_image']
        elif node['class_type'] == 'LoadVideo':
            inputs['file'] = asset['driver_video']
        elif node['class_type'] == 'WanAnimate2ToVideo':
            inputs.update(width=preset['width'], height=preset['height'], length=preset['frames'])
        elif node['class_type'] == 'ModelSamplingSD3':
            inputs['shift'] = preset['shift']
        elif node['class_type'] == 'KSampler':
            inputs.update(steps=preset['steps'], cfg=preset['cfg'], sampler_name=preset['sampler'])
        elif node['class_type'] == 'CreateVideo':
            inputs['fps'] = preset['fps']
    endpoint = next(node for node in graph.values() if node['class_type'] == 'DramaEndpointFrames')
    if asset.get('end_input_image'):
        graph['__animate2_end'] = {'class_type': 'LoadImage', 'inputs': {'image': asset['end_input_image']}}
        endpoint['inputs']['end_image'] = ['__animate2_end', 0]
    else:
        endpoint['inputs'].pop('end_image', None)
    leaf = node_id(graph, 'DramaSaveAnimate2Clip')
    graph = ancestors(graph, leaf)
    graph['__stage_result'] = {'class_type': 'DramaStageOutput', 'inputs': {'value': [leaf, 0]}}
    return graph
