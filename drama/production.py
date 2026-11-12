#!/usr/bin/env python3
"""Local storyboard -> keyframes -> Wan video -> voiced, subtitled MP4.

Each job owns an immutable input snapshot and a resumable state.json. Only a
confirmed terminal failure is resubmitted; an uncertain submission is held.
"""
import argparse
import base64
import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import uuid

import av
import numpy as np
from PIL import Image

if __package__:
    from . import batch
    from .workflow_contract import load_storyboard
    from .workflow_build import require_inference
else:
    import batch
    from workflow_contract import load_storyboard
    from workflow_build import require_inference

ROOT = Path(__file__).resolve().parent
APP = ROOT.parent
JOBS = APP / 'output/drama_jobs'
HOST = 'http://127.0.0.1:8188'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load_board(folder, cfg):
    return load_storyboard(folder, cfg)


def image_check(path, size=None):
    with Image.open(path) as im:
        im.load()
        if size and im.size != tuple(size):
            raise ValueError('图片尺寸不符：%s' % (im.size,))
        a = np.asarray(im.convert('RGB').resize((128, 128)), dtype=np.float32)
    if not np.isfinite(a).all() or a.std() < 2 or a.mean() < 1 or a.mean() > 254:
        raise ValueError('空白、纯色或损坏图片')
    return {'mean': round(float(a.mean()), 2), 'std': round(float(a.std()), 2)}


def video_check(path, audio=False, size=None, expected_frames=None):
    frames, samples, movement, previous, flat_frames = 0, 0, [], None, 0
    with av.open(str(path)) as container:
        vs = container.streams.video[0]
        actual_size = (vs.width, vs.height)
        if size and actual_size != tuple(size):
            raise ValueError('视频尺寸不符：%s' % (actual_size,))
        duration = float(vs.duration * vs.time_base) if vs.duration else 0
        for f in container.decode(video=0):
            a = np.asarray(f.to_image().resize((96, 96)), dtype=np.float32)
            if not np.isfinite(a).all():
                raise ValueError('视频有无效帧')
            if a.std() < 2 or a.mean() < 1 or a.mean() > 254:
                flat_frames += 1
            if previous is not None:
                movement.append(float(np.abs(a - previous).mean()))
            previous = a
            frames += 1
        has_audio = bool(container.streams.audio)
    if frames < 2:
        raise ValueError('视频缺帧')
    if expected_frames and frames != expected_frames:
        raise ValueError('视频帧数不符：%d / %d' % (frames, expected_frames))
    if not audio and flat_frames > frames // 4:
        raise ValueError('动态视频包含过多空白帧')
    if audio:
        if not has_audio:
            raise ValueError('成片缺少音轨')
        with av.open(str(path)) as container:
            for f in container.decode(audio=0):
                samples += f.samples
        if samples < 1000:
            raise ValueError('成片音轨为空')
    elif np.mean(movement) < 0.02:
        raise ValueError('图生视频没有可检测的画面变化')
    return {'frames': frames, 'duration': duration, 'width': actual_size[0],
            'height': actual_size[1], 'audio': has_audio, 'audio_samples': samples,
            'mean_frame_delta': round(float(np.mean(movement)), 4)}


def wan_graph(prompt, negative, image, prefix, seed, cfg, end_image=None):
    v = cfg['dynamic']
    return {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': v['model'], 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': v['text_encoder'], 'type': 'wan', 'device': 'cpu'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': v['vae']}},
        '4': {'class_type': 'CLIPTextEncode', 'inputs': {'clip': ['2', 0], 'text': prompt}},
        '5': {'class_type': 'CLIPTextEncode', 'inputs': {'clip': ['2', 0], 'text': negative}},
        '6': {'class_type': 'LoadImage', 'inputs': {'image': image}},
        '7': {'class_type': 'WanFirstLastFrameToVideo', 'inputs': {'positive': ['4', 0], 'negative': ['5', 0], 'vae': ['3', 0], 'width': v['width'], 'height': v['height'], 'length': v['frames'], 'batch_size': 1, 'start_image': ['6', 0], **({'end_image': ['13', 0]} if end_image else {})}},
        '8': {'class_type': 'ModelSamplingSD3', 'inputs': {'model': ['1', 0], 'shift': 8.0}},
        '9': {'class_type': 'KSampler', 'inputs': {'model': ['8', 0], 'seed': seed, 'steps': v['steps'], 'cfg': v['cfg'], 'sampler_name': v.get('sampler', 'euler'), 'scheduler': 'simple', 'positive': ['7', 0], 'negative': ['7', 1], 'latent_image': ['7', 2], 'denoise': 1.0}},
        '10': {'class_type': 'VAEDecodeTiled', 'inputs': {'vae': ['3', 0], 'samples': ['9', 0], 'tile_size': 256, 'overlap': 64, 'temporal_size': 16, 'temporal_overlap': 4}},
        '11': {'class_type': 'CreateVideo', 'inputs': {'images': ['10', 0], 'fps': v['fps']}},
        '12': {'class_type': 'SaveVideo', 'inputs': {'video': ['11', 0], 'filename_prefix': prefix, 'format': 'mp4', 'format.codec': 'h264'}},
        **({'13': {'class_type': 'LoadImage', 'inputs': {'image': end_image}}} if end_image else {}),
    }


class Hold(RuntimeError):
    pass


class Job:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.cfg = read_json(self.folder / 'config.json')
        self.state_path = self.folder / 'state.json'
        self.state = read_json(self.state_path)
        self.board, self.shots, self.characters, self.style = load_board(self.folder / 'input', self.cfg)
        self.template = read_json(self.folder / 'workflow_api.json') if (self.folder / 'workflow_api.json').is_file() else None
        animate2_path = self.folder / 'animate2_workflow_api.json'
        self.animate2_template = read_json(animate2_path) if self.cfg.get('animation', {}).get('enabled') and animate2_path.is_file() else None

    def save(self, **values):
        self.state.update(values)
        self.state['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        atomic_json(self.state_path, self.state)

    def request(self, path, data=None):
        for n in range(4):
            try:
                return batch.api_get(HOST, path, timeout=90) if data is None else batch.api_post(HOST, path, data, timeout=90)
            except (urllib.error.URLError, TimeoutError):
                if data is not None or n == 3:
                    raise
                time.sleep(3)

    def check_stop(self):
        if (self.folder / 'stop_requested.json').exists():
            self.save(status='stopped', error='制作已停止，可恢复后继续')
            raise Hold(self.state['error'])

    def graph_stage(self, stage, shot, attempt, asset=None):
        if __package__:
            from .workflow_graph import compile_stage, compile_animate2_stage
        else:
            from workflow_graph import compile_stage, compile_animate2_stage
        self.check_stop()
        key = '%s_%s_a%d' % (shot['shot_id'], stage, attempt)
        steps = self.state.setdefault('graph_steps', {})
        step = steps.setdefault(key, {})
        if step.get('status') == 'complete':return step['result']
        if step.get('status') not in ('running', 'submitting', 'unknown'):
            count = step.get('executions', 0)
            if count >= self.cfg['quality']['max_retries'] + 1:raise Hold('阶段执行已达到上限：' + key)
            prompt_id = str(uuid.uuid4())
            if stage == 'video_transfer':
                if self.animate2_template is None:raise Hold('当前任务未保存 Animate2 工作流分支')
                graph = compile_animate2_stage(self.animate2_template, self.folder.name, shot['_index'] - 1,
                                               attempt, asset, self.cfg['animation'])
            else:
                graph = compile_stage(self.template, stage, self.folder.name, shot['_index'] - 1, attempt,
                                      asset=asset, preset=self.cfg['dynamic'])
            backend = self.request('/api/drama/runtime')['backend_id']
            step.update(status='submitting', prompt_id=prompt_id, backend_id=backend, executions=count + 1)
            self.save(phase=stage, current_shot=shot['shot_id'])
            request = {'prompt_id': prompt_id, 'prompt': graph, 'client_id': self.folder.name,
                       'extra_data': {'drama_job': self.folder.name, 'shot': shot['shot_id'], 'stage': stage, 'attempt': attempt}}
            atomic_json(self.folder / (key + '_graph.json'), request)
            try:
                result = self.request('/prompt', request)
                if result.get('node_errors') or result.get('prompt_id') != prompt_id:
                    step.update(status='failed', error=str(result));self.save();raise Hold('节点校验失败：' + str(result))
                step['status'] = 'running'
            except urllib.error.HTTPError as error:
                step.update(status='failed', error=error.read().decode()[:2000]);self.save();raise Hold('阶段节点校验失败：' + step['error'])
            except (urllib.error.URLError, TimeoutError):step['status'] = 'unknown'
            self.save()
        prompt_id = step['prompt_id'];start = time.monotonic()
        while time.monotonic() - start < self.cfg['quality']['stage_timeout_sec']:
            self.check_stop()
            history = self.request('/history/' + prompt_id)
            if prompt_id in history:
                entry = history[prompt_id];atomic_json(self.folder / (key + '_history.json'), entry)
                if entry['status']['status_str'] == 'error':
                    interrupted = any(message[0] == 'execution_interrupted' for message in entry['status'].get('messages', []))
                    step.update(status='failed', error=json.dumps(entry['status'], ensure_ascii=False));self.save()
                    if interrupted:
                        if step.get('executions', 0) < self.cfg['quality']['max_retries'] + 1:
                            print('RETRY', key, 'execution_interrupted', flush=True)
                            return self.graph_stage(stage, shot, attempt, asset=asset)
                        self.save(status='stopped');raise Hold('阶段执行已中断且已达到重试上限：' + key)
                    raise Hold('完整图阶段执行失败：' + key + '，见阶段历史')
                if entry['status'].get('completed'):
                    result = entry['outputs']['__stage_result']['drama_stage'][0]
                    if stage == 'compose':
                        self.state = read_json(self.state_path)
                        step = self.state.setdefault('graph_steps', {}).setdefault(key, step)
                    step.update(status='complete', result=result);self.save()
                    return result
            elif self.request('/api/drama/runtime')['backend_id'] != step['backend_id']:
                step.update(status='failed', error='ComfyUI 已重启，原阶段进程已结束');self.save()
                if step.get('executions', 0) < self.cfg['quality']['max_retries'] + 1:
                    print('RETRY', key, 'backend_restarted', flush=True)
                    return self.graph_stage(stage, shot, attempt, asset=asset)
                raise Hold(step['error'] + '；该阶段已达到重试上限')
            time.sleep(3)
        raise Hold('阶段等待超时，保留原 prompt_id，未重复提交：' + prompt_id)

    def recover_saved_stage(self, shot, stage, state):
        """Adopt a completed, locally recorded stage before considering a retry.

        ComfyUI can finish writing an output and its history snapshot just before
        the worker process exits.  In that narrow window ``state.json`` still
        says ``running``.  The snapshot is only accepted when it reports a
        successful completion, references an output-local file, and that file
        passes the same technical QA used for a live history response.
        """
        if state.get('status') not in ('running', 'submitting', 'unknown'):
            return False
        attempt = state.get('attempts', 0)
        if attempt < 1:
            return False
        snapshot = self.folder / ('%s_%s_a%d_history.json' % (shot['shot_id'], stage, attempt))
        if not snapshot.is_file():
            return False
        try:
            entry = read_json(snapshot)
            status = entry.get('status', {})
            if not status.get('completed') or status.get('status_str') != 'success':
                return False
            if self.template is not None:
                artifacts = entry['outputs']['__stage_result']['drama_stage']
                artifact = artifacts[0]
                if artifact.get('shot_id') not in (None, shot['shot_id']):
                    return False
                path = Path(artifact['path']).resolve()
                expected_sha = artifact.get('sha256')
            else:
                output = entry['outputs'].get('10' if stage == 'image' else '12', {})
                media = output.get('images', []) + output.get('gifs', []) + output.get('videos', [])
                if not media:
                    return False
                file = media[0]
                path = (APP / 'output' / file.get('subfolder', '') / file['filename']).resolve()
                expected_sha = None
            path.relative_to((APP / 'output').resolve())
            digest = sha(path)
            if expected_sha and digest != expected_sha:
                return False
            if stage == 'image':
                qa = image_check(path, (shot['width'], shot['height']))
            else:
                settings = self.cfg['animation'] if stage == 'video_transfer' else self.cfg['dynamic']
                qa = video_check(path, size=(settings['width'], settings['height']),
                                 expected_frames=settings['frames'])
        except (ValueError, OSError, KeyError, IndexError, json.JSONDecodeError):
            return False
        state.update(status='complete', path=str(path), sha256=digest, qa=qa, error='',
                     recovered_from_history=snapshot.name)
        self.save()
        return True

    def preflight(self, video=False):
        info = self.request('/object_info')
        d = self.cfg['dynamic']
        wanted = [('UNETLoader', 'unet_name', d['model']), ('CLIPLoader', 'clip_name', d['text_encoder']), ('VAELoader', 'vae_name', d['vae'])] if video else []
        if video and self.animate2_template is not None:
            animation = self.cfg['animation']
            wanted.extend([('UNETLoader', 'unet_name', animation['model']),
                           ('CLIPLoader', 'clip_name', animation['text_encoder']),
                           ('CLIPVisionLoader', 'clip_name', animation['clip_vision']),
                           ('LoraLoaderModelOnly', 'lora_name', animation['lora']),
                           ('VAELoader', 'vae_name', animation['vae'])])
        for node, key, value in wanted:
            if value not in info[node]['input']['required'][key][0]:
                raise Hold('缺少视频模型：' + value)
        batch.preflight(HOST)

    def release_models(self, timeout=600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            queue = self.request('/queue')
            if not queue['queue_running'] and not queue['queue_pending']:
                self.request('/free', {'unload_models': True, 'free_memory': True})
                for _ in range(60):
                    if self.request('/api/drama/runtime').get('loaded_model_count') == 0:return
                    self.check_stop();time.sleep(2)
                raise Hold('ComfyUI 在卸载后仍报告已加载模型，已停止切换到下一视频阶段')
            self.check_stop();time.sleep(2)
        raise Hold('ComfyUI 队列未清空，未卸载其他任务的模型，也未启动下一阶段')

    def run_animate2_stage(self, shot):
        if self.animate2_template is None:return
        sid = shot['shot_id'];state = self.state['shots'][sid]['video']
        attempt = state['attempts']
        driver = Path(state.get('driver_path') or state['path']).resolve()
        driver_sha = state.get('driver_sha256') or sha(driver)
        if sha(driver) != driver_sha:raise Hold('Animate2 动作草稿哈希不符：' + sid)
        input_dir = APP / 'input/drama_jobs' / self.folder.name
        input_dir.mkdir(parents=True, exist_ok=True)
        keyframe = Path(self.state['shots'][sid]['image']['path']).resolve()
        reference_target = input_dir / (sid + '_animate2_reference.png')
        if not reference_target.is_file() or sha(reference_target) != sha(keyframe):shutil.copy2(keyframe, reference_target)
        driver_target = input_dir / ('%s_animate2_driver_a%d.mp4' % (sid, attempt))
        if not driver_target.is_file() or sha(driver_target) != driver_sha:shutil.copy2(driver, driver_target)
        asset = {
            'input_image': reference_target.relative_to(APP / 'input').as_posix(),
            'driver_video': driver_target.relative_to(APP / 'input').as_posix(),
        }
        if shot.get('end_frame'):
            source = self.folder / 'input' / shot['end_frame']
            end_target = input_dir / (sid + '_animate2_end' + source.suffix.lower())
            if not end_target.is_file() or sha(end_target) != sha(source):shutil.copy2(source, end_target)
            asset['end_input_image'] = end_target.relative_to(APP / 'input').as_posix()
        state.update(driver_path=str(driver), driver_sha256=driver_sha,
                     motion_draft={'path': str(driver), 'sha256': driver_sha})
        self.save(phase='video_transfer', current_shot=sid)
        transfer = self.graph_stage('video_transfer', shot, attempt, asset=asset)
        path = Path(transfer['path']).resolve();path.relative_to((APP / 'output').resolve())
        digest = sha(path)
        if digest != transfer.get('sha256'):raise Hold('Animate2 成片哈希不符：' + sid)
        settings = self.cfg['animation']
        qa = video_check(path, size=(settings['width'], settings['height']), expected_frames=settings['frames'])
        transfer_state = self.state.setdefault('shots', {}).setdefault(sid, {}).setdefault('video_transfer', {})
        transfer_state.update(status='complete', attempts=attempt, path=str(path), sha256=digest, qa=qa, error='')
        state.update(status='complete', path=str(path), sha256=digest, qa=qa, error='',
                     transfer_attempt=attempt)
        self.save()

    def vision(self, paths, expected, sequence=False):
        self.check_stop()
        cfg = self.cfg['quality']['vision']
        if not cfg['enabled']:
            return {'status': 'not_checked'}
        order = '每个 FRAME 标签紧接其对应视频帧，按首帧、中间帧、尾帧排列；允许动作带来的正常姿态变化。动态修正提示词用英文完整描述连续动作，作为下一次生成的完整替换提示词；必须保留原始分镜要求的动作、方向、终态和道具关系，不得为了通过质检而降低或改换动作目标。' if sequence else '每个图像标签紧接其对应图片。REFERENCE 仅作角色外观参考；CANDIDATE 是本次唯一待检分镜。动作、道具和场景只检查 CANDIDATE，不得将参考图的姿态或背景当作待检图的问题。'
        content = [{'type': 'text', 'text': '你是国风动漫分镜质检员。只依据图片检查主体、服装、画风、明显肢体崩坏与文字水印。' + order + '原始角色设定与预期分镜：' + expected + '\n只返回 JSON：{"pass":true或false,"issues":[具体问题],"correction_prompt":"不通过时给出完整的正向修正画面或动作要求；通过时留空"}。只对明确的主体遗漏、画风类别错误、动作语义冲突、角色变装、严重形变、主体明显模糊或文字水印判为不通过。原始设定为3D时必须保留立体建模与材质，特效不能代替分镜要求的人物动作；景深造成的背景模糊不算主体失焦。服装只检查主要颜色类别和款式；青绿的明暗、黑与深蓝、腰带宽窄、衣褶、发根扎结轮廓的差异不属于换装。参考图辅助识别，约束以文字中的原始角色设定为准，不得从参考图自行增加新的服装要求。左右手以人物自身方向为准，不能按画面左右判断；背面遮挡或视角导致无法确认的细节不作为失败依据。correction_prompt 只写应当出现的正确主体、服装和动作，不要复述错误画面，不写“当前图片是……”等问题描述。'}]
        instruction = content.pop()
        receipt = []
        for index, path in enumerate(paths):
            with Image.open(path) as im:
                im.thumbnail((512, 512))
                b = io.BytesIO(); im.convert('RGB').save(b, 'JPEG', quality=85)
            role = 'sequence_frame' if sequence else 'candidate' if index == len(paths) - 1 else 'reference'
            label = 'FRAME %d/%d（按时间顺序的待检视频帧）' % (index + 1, len(paths)) if sequence else 'CANDIDATE（本次唯一待检分镜）' if role == 'candidate' else 'REFERENCE %d（只作角色外观参考）' % (index + 1)
            content.append({'type': 'text', 'text': label})
            content.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(b.getvalue()).decode()}})
            receipt.append({'role': role, 'label': label, 'path': str(Path(path).resolve()), 'sha256': sha(path), 'submitted_jpeg_sha256': hashlib.sha256(b.getvalue()).hexdigest()})
        content.append(instruction)
        res = batch.api_post(cfg['url'].rsplit('/v1/', 1)[0], '/v1/chat/completions', {'model': cfg['model'], 'messages': [{'role': 'user', 'content': content}], 'temperature': 0, 'max_tokens': 800, 'chat_template_kwargs': {'enable_thinking': False}}, timeout=cfg['timeout_sec'])
        text = res['choices'][0]['message']['content']
        a, b = text.find('{'), text.rfind('}')
        result = json.loads(text[a:b + 1])
        if not isinstance(result.get('pass'), bool) or not isinstance(result.get('issues'), list) or not all(isinstance(issue, str) for issue in result['issues']):
            raise Hold('视觉检查未返回有效结果')
        correction = result.get('correction_prompt', '')
        if not isinstance(correction, str):
            raise Hold('视觉检查未返回有效修正提示词')
        result['correction_prompt'] = (correction.strip() or expected) if result['pass'] is False else ''
        result['input_receipt'] = {'images': receipt, 'expected_sha256': hashlib.sha256(expected.encode()).hexdigest()}
        return result

    def run_stage(self, shot, stage):
        sid = shot['shot_id']; state = self.state.setdefault('shots', {}).setdefault(sid, {}).setdefault(stage, {'attempts': 0})
        if state.get('status') == 'complete' and Path(state.get('path', '')).is_file() and sha(state['path']) == state.get('sha256'):
            return
        if self.recover_saved_stage(shot, stage, state):
            return
        budget = state.get('allowed_attempts', self.cfg['quality']['max_retries'] + 1)
        while state['attempts'] < budget or state.get('status') in ('running', 'unknown', 'submitting'):
            self.check_stop()
            if state.get('status') in ('submitting', 'unknown'):
                try:
                    queue = self.request('/queue')
                    history = self.request('/history?max_items=200')
                except (urllib.error.URLError, TimeoutError) as e:
                    raise Hold('暂时无法核对原提交，未重复生成：' + str(e)) from e
                candidates = queue['queue_running'] + queue['queue_pending'] + [entry['prompt'] for entry in history.values()]
                matches = {item[1]: item for item in candidates if item[3].get('drama_job') == self.folder.name and item[3].get('shot') == sid and item[3].get('stage') == stage and item[3].get('attempt') == state['attempts']}
                if len(matches) != 1:
                    raise Hold('提交结果不确定，未重复提交：%s / %s' % (sid, stage))
                state.update(status='running', prompt_id=next(iter(matches))); self.save()
            attempt = state['attempts'] if state.get('prompt_id') and state.get('status') == 'running' else state['attempts'] + 1
            if stage == 'video' and state.get('status') != 'running':
                state.pop('driver_path', None);state.pop('driver_sha256', None);state.pop('motion_draft', None)
            seed = batch.resolve_seed(shot, self.characters, shot['_index']) + (attempt - 1) * 100003
            prompt = batch.make_prompt(shot, self.characters, self.style)
            if state.get('quality_issues'):
                prompt += '，修正后的画面要求：' + (state.get('correction_prompt') or shot['description'])
            prefix = 'drama_jobs/%s/artifacts/%s_%s_a%d' % (self.folder.name, sid, stage, attempt)
            if stage == 'image':
                graph = batch.build_workflow(prompt, seed, shot['width'], shot['height'], prefix, 'res_multistep', 'ae.safetensors')
                graph['7']['inputs'].update({k: self.cfg['image'][k] for k in ['steps', 'cfg']})
            else:
                keyframe = self.state['shots'][sid]['image']['path']
                target = APP / 'input/drama_jobs' / self.folder.name / (sid + '.png')
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(keyframe, target)
                end_image = ''
                if shot.get('end_frame'):
                    source_end = self.folder / 'input' / shot['end_frame']
                    target_end = APP / 'input/drama_jobs' / self.folder.name / (sid + '_end' + source_end.suffix.lower())
                    target_end.parent.mkdir(parents=True, exist_ok=True);shutil.copy2(source_end, target_end)
                    end_image = target_end.relative_to(APP / 'input').as_posix()
                action = shot.get('video_prompt') or shot['description']
                if state.get('quality_issues'):
                    action = state.get('correction_prompt') or action
                graph = wan_graph(action + '，' + self.style + '，人物动作连贯，衣袖和头发自然运动，保持首帧角色外观与服装，不要字幕水印', self.cfg['dynamic']['negative'], target.relative_to(APP / 'input').as_posix(), prefix, seed, self.cfg, end_image=end_image)
            if self.template is not None:
                if __package__:
                    from .workflow_graph import compile_stage
                else:
                    from workflow_graph import compile_stage
                asset = {'input_image': target.relative_to(APP / 'input').as_posix(), 'end_input_image': end_image} if stage == 'video' else None
                graph = compile_stage(self.template, stage, self.folder.name, shot['_index'] - 1, attempt, asset=asset, preset=self.cfg['dynamic'])
            if state.get('status') != 'running':
                backend = self.request('/api/drama/runtime')['backend_id']
                state.update(status='submitting', attempts=attempt, seed=seed)
                self.save(status='running', phase=stage, current_shot=sid)
                try:
                    res = self.request('/prompt', {'prompt': graph, 'client_id': self.folder.name, 'extra_data': {'drama_job': self.folder.name, 'shot': sid, 'stage': stage, 'attempt': attempt}})
                except urllib.error.HTTPError as e:
                    state.update(status='failed', error=e.read().decode()[:2000]); self.save()
                    raise Hold('工作流校验失败：' + state['error'])
                except (urllib.error.URLError, TimeoutError) as e:
                    state.update(status='unknown', error=str(e)); self.save()
                    raise Hold('提交回执丢失，需要核对队列；不会盲目重试')
                if res.get('node_errors') or not res.get('prompt_id'):
                    state.update(status='failed', error=str(res)); self.save(); raise Hold('节点校验失败：' + str(res))
                state.update(status='running', prompt_id=res['prompt_id'], submitted_at=time.time(), backend_id=backend); self.save()
            self.save(status='running', phase=stage, current_shot=sid)
            pid = state['prompt_id']; start = time.monotonic()
            while time.monotonic() - start < self.cfg['quality']['stage_timeout_sec']:
                hist = self.request('/history/' + pid)
                if pid not in hist and state.get('backend_id'):
                    backend = self.request('/api/drama/runtime')['backend_id']
                    if backend != state['backend_id']:
                        state.update(status='failed', error='ComfyUI 已重启，原执行进程结束；自动重试该镜头'); self.save(); break
                if pid in hist:
                    entry = hist[pid]
                    if entry['status']['status_str'] == 'error':
                        interrupted = any(message[0] == 'execution_interrupted' for message in entry['status'].get('messages', []))
                        if interrupted and not state.pop('resume_after_interrupt', False):
                            atomic_json(self.folder / ('%s_%s_a%d_history.json' % (sid, stage, attempt)), entry)
                            state.update(status='interrupted', error='模型执行已中断，等待明确恢复')
                            self.save(status='stopped', error=state['error'])
                            raise Hold(state['error'])
                        state.update(status='failed', error=json.dumps(entry['status'], ensure_ascii=False)); self.save(); break
                    if entry['status'].get('completed'):
                        atomic_json(self.folder / ('%s_%s_a%d_history.json' % (sid, stage, attempt)), entry)
                        try:
                            if self.template is not None:
                                artifact = entry['outputs']['__stage_result']['drama_stage'][0]
                                path = Path(artifact['path']).resolve()
                            else:
                                output = entry['outputs'].get('10' if stage == 'image' else '12', {})
                                artifacts = output.get('images', []) + output.get('gifs', []) + output.get('videos', [])
                                if not artifacts:raise ValueError('执行完成但没有媒体输出')
                                f = artifacts[0]; path = (APP / 'output' / f.get('subfolder', '') / f['filename']).resolve()
                            path.relative_to((APP / 'output').resolve())
                            if stage == 'image':
                                qa = image_check(path, (shot['width'], shot['height']))
                            else:
                                settings = self.cfg['animation'] if stage == 'video_transfer' else self.cfg['dynamic']
                                qa = video_check(path, size=(settings['width'], settings['height']),
                                                 expected_frames=settings['frames'])
                            state.pop('vision', None)
                            state.update(status='complete', path=str(path), sha256=sha(path), qa=qa, error=''); self.save()
                        except (ValueError, OSError, KeyError, IndexError) as e:
                            state.update(status='failed', error=str(e)); self.save()
                        break
                time.sleep(3)
            else:
                self.save(status='needs_attention', error='等待超时，保留 prompt_id；恢复时继续等待原任务')
                raise Hold('等待超时，原任务未确认结束，未自动重复提交：' + pid)
            if state.get('status') == 'complete':return
            print('RETRY', sid, stage, state.get('error', ''), flush=True)
            self.release_models()
            state.pop('prompt_id', None); self.save()
        raise Hold('%s / %s 已达到重试上限，保留全部失败证据' % (sid, stage))

    def run(self):
        self.save(status='queued', pid=os.getpid())
        with (JOBS / 'gpu.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.save(status='running', phase='preflight', error='')
            self.preflight()
            for shot in self.shots:self.run_stage(shot, 'image')
            self.release_models()
            self.save(phase='image_review')
            anchors = {}
            for shot in self.shots:
                st = self.state['shots'][shot['shot_id']]['image']
                while st.get('vision', {}).get('pass') is not True:
                    self.save(phase='image_review', current_shot=shot['shot_id'])
                    refs = list(dict.fromkeys(anchors[name] for name in shot.get('character', '').split('|') if name in anchors))
                    expected = batch.make_prompt(shot, self.characters, self.style)
                    try:review = self.graph_stage('image_review', shot, st['attempts']) if self.template is not None else self.vision(refs + [st['path']], expected)
                    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:raise Hold('本地视觉检查失败：' + str(e))
                    atomic_json(self.folder / ('%s_image_a%d_review.json' % (shot['shot_id'], st['attempts'])), review)
                    st['vision'] = review; self.save()
                    if review.get('status') == 'not_checked':break
                    if review.get('pass') is False:
                        st.update(status='failed', quality_issues=review['issues'], correction_prompt=review['correction_prompt']); self.save()
                        self.run_stage(shot, 'image')
                        self.release_models()
                        st.pop('vision', None)
                for name in shot.get('character', '').split('|'):
                    if name:anchors.setdefault(name, st['path'])
            self.preflight(video=True)
            for shot in self.shots:
                self.run_stage(shot, 'video')
                if self.animate2_template is not None:
                    self.release_models()
                    self.run_animate2_stage(shot)
                    self.release_models()
            self.release_models()
            self.save(phase='video_review')
            for shot in self.shots:
                st = self.state['shots'][shot['shot_id']]['video']
                while st.get('vision', {}).get('pass') is not True:
                    self.save(phase='video_review', current_shot=shot['shot_id'])
                    with av.open(st['path']) as clip:
                        frames = [f.to_image() for f in clip.decode(video=0)]
                    paths = []
                    for index in sorted({0, len(frames) // 2, len(frames) - 1}):
                        path = self.folder / ('%s_review_%d.jpg' % (shot['shot_id'], index))
                        frames[index].save(path); paths.append(path)
                    try:review = self.graph_stage('video_review', shot, st['attempts']) if self.template is not None else self.vision(paths, shot.get('video_prompt') or shot['description'], sequence=True)
                    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:raise Hold('动态镜头视觉检查失败：' + str(e))
                    atomic_json(self.folder / ('%s_video_a%d_review.json' % (shot['shot_id'], st['attempts'])), review)
                    st['vision'] = review; self.save()
                    if review.get('status') == 'not_checked':break
                    if review.get('pass') is False:
                        st.update(status='failed', quality_issues=review['issues'], correction_prompt=review['correction_prompt']); self.save()
                        self.run_stage(shot, 'video')
                        if self.animate2_template is not None:
                            self.release_models()
                            self.run_animate2_stage(shot)
                            self.release_models()
                        st.pop('vision', None)
            self.compose()

    def compose(self):
        self.check_stop()
        if self.template is not None:
            self.graph_stage('compose', self.shots[-1], 1)
            return
        previous = self.folder / 'episode.mp4'
        if previous.is_file() and self.state.get('output_sha256') == sha(previous):
            qa = video_check(previous, audio=True, size=(self.cfg['cinema']['width'], self.cfg['cinema']['height']))
            self.finish_delivery(previous, qa)
            return
        self.save(phase='compose')
        images, videos = self.folder / 'keyframes', self.folder / 'clips'
        images.mkdir(exist_ok=True); videos.mkdir(exist_ok=True)
        for shot in self.shots:
            sid = shot['shot_id']
            for stage, folder, suffix in [('image', images, '.png'), ('video', videos, '.mp4')]:
                shutil.copy2(self.state['shots'][sid][stage]['path'], folder / (sid + suffix))
        fields = list(dict.fromkeys(k for s in self.shots for k in s if not k.startswith('_')))
        with (self.folder / 'shots.csv').open('w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows({k: s.get(k, '') for k in fields} for s in self.shots)
        atomic_json(self.folder / 'characters.json', {'characters': self.characters, 'global_style': self.style})
        out = self.folder / 'episode.partial.mp4'
        cmd = [sys.executable, str(ROOT / 'render_cinema.py'), '--csv', str(self.folder / 'shots.csv'), '--images', str(images), '--videos', str(videos), '--config', str(self.folder / 'config.json'), '--out', str(out), '--title', self.board.get('title', '玄幻测试片'), '--logline', self.board.get('logline', '')]
        with (self.folder / 'compose.log').open('a') as f:
            for attempt in range(self.cfg['quality']['max_retries'] + 1):
                result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
                if result.returncode == 0:break
            else:raise Hold('合成失败，详见 compose.log')
        qa = video_check(out, audio=True, size=(self.cfg['cinema']['width'], self.cfg['cinema']['height']))
        final = self.folder / 'episode.mp4'; out.replace(final)
        self.finish_delivery(final, qa)

    def finish_delivery(self, final, qa):
        final_sha = sha(final)
        self.save(status='running', phase='publish', output=str(final), output_sha256=final_sha, qa=qa)
        settings = self.cfg.get('delivery', {})
        destination = settings.get('dir')
        if destination:
            title = re.sub(r'[^\w.-]+', '_', self.board.get('title') or self.folder.name)[:60] or '短剧'
            target = Path(destination).expanduser().resolve() / (title + '_' + self.folder.name + '_' + final_sha[:8])
            target.mkdir(parents=True, exist_ok=True)
            files = [(final, Path('episode.mp4'))]
            for name in ['cover.png', 'subtitles.srt']:
                if (self.folder / name).is_file():files.append((self.folder / name, Path(name)))
            for enabled, name in [('copy_keyframes', 'keyframes'), ('copy_voice', 'voice'), ('copy_portraits', 'portraits')]:
                source = self.folder / name
                if settings.get(enabled) and source.is_dir():
                    files.extend((p, p.relative_to(self.folder)) for p in sorted(source.rglob('*')) if p.is_file())
            receipt = {'job_id': self.folder.name, 'output_sha256': final_sha, 'files': {}}
            for source, relative in files:
                digest = sha(source); output = target / relative; output.parent.mkdir(parents=True, exist_ok=True)
                if output.exists() and sha(output) != digest:
                    raise Hold('交付目录中同名文件已被修改，已保留原文件：' + str(output))
                if not output.exists():
                    partial = output.with_name('.' + output.name + '.copying-' + str(os.getpid()))
                    shutil.copy2(source, partial)
                    if sha(partial) != digest:raise Hold('交付复制哈希不一致：' + str(output))
                    partial.replace(output)
                receipt['files'][relative.as_posix()] = digest
            atomic_json(target / 'delivery.json', receipt)
            self.save(delivery_dir=str(target), delivery_output=str(target / 'episode.mp4'))
        self.save(status='complete', phase='complete', error='')


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--job', required=True); args = ap.parse_args()
    require_inference()
    folder = Path(args.job).resolve(); folder.relative_to(JOBS)
    with (folder / 'job.lock').open('w') as lock:
        try:fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:return 0
        try:job = Job(folder)
        except Exception as e:
            state = read_json(folder / 'state.json')
            state.update(status='needs_attention', error='分镜校验失败：' + str(e), updated_at=time.strftime('%Y-%m-%d %H:%M:%S'))
            atomic_json(folder / 'state.json', state)
            print(state['error'], flush=True); return 1
        if job.state.get('status') == 'complete':return 0
        try:job.run()
        except Exception as e:
            job.save(status='stopped' if job.state.get('status') == 'stopped' else 'needs_attention', error=str(e)); print(type(e).__name__, str(e), flush=True); return 1
    return 0


if __name__ == '__main__':raise SystemExit(main())
