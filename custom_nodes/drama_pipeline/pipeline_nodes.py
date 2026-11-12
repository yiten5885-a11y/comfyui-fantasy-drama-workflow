"""Executable checkpoints and post-production nodes for the expanded canvas."""
import copy
import json
from pathlib import Path
import shutil

import numpy as np
from PIL import Image

from .production_server import APP, JOBS, INPUT, contained, submit
from drama.workflow_contract import load_storyboard, input_fingerprint
from drama.workflow_build import require_inference

CATEGORY = '短剧/完整制作链'


def production():
    from drama import production as module
    return module


def job_for(shot):
    job = production().Job(contained(JOBS, shot['job_id']))
    job.check_stop()
    return job


def phase(episode, name):
    job = job_for({'job_id': episode['job_id']})
    job.save(phase=name)
    return job


def image_tensor(path):
    import torch
    with Image.open(path) as image:
        pixels = np.array(image.convert('RGB'), dtype=np.float32) / 255.0
    return torch.from_numpy(pixels).unsqueeze(0)


def audio_config(cfg, settings):
    cfg = copy.deepcopy(cfg)
    cfg['voice'].update(enabled=settings['voice_enabled'], name=settings['voice_name'], rate=settings['voice_rate'])
    cfg['voice']['per_character'] = {'_default': settings['voice_name']}
    cfg['cinema']['voice_map']['_default'] = settings['voice_name']
    cfg['subtitle']['burn_in'] = settings['subtitles'];cfg['music']['enabled'] = settings['music']
    return cfg


class DramaShotPlan:
    @classmethod
    def INPUT_TYPES(cls):
        cfg = json.loads((APP / 'drama/production_config.json').read_text())['image']
        return {'required': {'storyboard': ('DRAMA_STORYBOARD',),
            'index': ('INT', {'default': 0, 'min': 0, 'max': 999}),
            'default_width': ('INT', {'default': cfg['width'], 'min': 16, 'max': 16384, 'step': 16}),
            'default_height': ('INT', {'default': cfg['height'], 'min': 16, 'max': 16384, 'step': 16}),
            'style': ('STRING', {'default': cfg['style'], 'multiline': True})},
            'optional': {'stage': ('STRING', {'default': 'image', 'advanced': True}),
                         'attempt': ('INT', {'default': 1, 'min': 1, 'advanced': True})}}
    RETURN_TYPES = ('STRING', 'STRING', 'INT', 'INT', 'INT', 'DRAMA_SHOT')
    RETURN_NAMES = ('首帧提示词', '连续动作提示词', '宽', '高', 'seed', '镜头记录')
    FUNCTION = 'load';CATEGORY = CATEGORY

    def load(self, storyboard, index, default_width, default_height, style, stage='image', attempt=1):
        module = production()
        job_id = storyboard.get('job_id', '')
        if job_id:
            job = job_for({'job_id': job_id});cfg = job.cfg
            board, shots, characters, global_style = job.board, job.shots, job.characters, job.style
            states = job.state.get('shots', {})
        else:
            cfg = json.loads((APP / 'drama/production_config.json').read_text())
            cfg['image'].update(width=default_width, height=default_height, style=style)
            board, shots, characters, global_style = load_storyboard(contained(INPUT, storyboard['project']), cfg)
            states = {}
        shot = shots[int(index) % len(shots)]
        kind = 'video' if stage.startswith('video') else 'image'
        state = states.get(shot['shot_id'], {}).get(kind, {})
        prompt = module.batch.make_prompt(shot, characters, global_style)
        action = shot.get('video_prompt') or shot['description']
        if state.get('quality_issues'):
            correction = state.get('correction_prompt') or (shot['description'] if kind == 'image' else action)
            if kind == 'image':prompt += '，修正后的画面要求：' + correction
            else:action = correction
        action += '，' + global_style + '，人物动作连贯，保持首帧角色与服装'
        seed = module.batch.resolve_seed(shot, characters, shot['_index']) + (attempt - 1) * 100003
        context = {'job_id': job_id, 'shot_id': shot['shot_id'], 'index': int(index), 'attempt': attempt, 'stage': stage}
        return prompt, action, shot['width'], shot['height'], seed, context


class DramaSaveKeyframe:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'images': ('IMAGE',), 'shot': ('DRAMA_SHOT',)}, 'hidden': {'prompt': 'PROMPT', 'extra_pnginfo': 'EXTRA_PNGINFO'}}
    RETURN_TYPES = ('DRAMA_IMAGE_ASSET',);RETURN_NAMES = ('已保存关键帧',)
    FUNCTION = 'save';CATEGORY = CATEGORY
    def save(self, images, shot, prompt=None, extra_pnginfo=None):
        from nodes import SaveImage
        job = job_for(shot)
        prefix = 'drama_jobs/%s/artifacts/%s_image_a%d' % (job.folder.name, shot['shot_id'], shot['attempt'])
        saved = SaveImage().save_images(images, prefix, prompt=prompt, extra_pnginfo=extra_pnginfo)
        item = saved['ui']['images'][0];path = APP / 'output' / item['subfolder'] / item['filename']
        asset = {'path': str(path), 'sha256': production().sha(path), 'shot_id': shot['shot_id']}
        return {'ui': saved['ui'], 'result': (asset,)}


class DramaKeyframeReview:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'asset': ('DRAMA_IMAGE_ASSET',), 'shot': ('DRAMA_SHOT',), 'quality': ('DRAMA_QUALITY_SETTINGS',)}}
    RETURN_TYPES = ('IMAGE', 'DRAMA_REVIEW');RETURN_NAMES = ('送入Wan的关键帧', '关键帧质检报告')
    FUNCTION = 'review';CATEGORY = CATEGORY
    def review(self, asset, shot, quality):
        module = production();job = job_for(shot);row = job.shots[shot['index']]
        module.image_check(asset['path'], (row['width'], row['height']))
        anchors = {}
        names = set(row.get('character', '').split('|'))
        for previous in job.shots[:shot['index']]:
            if names.intersection(previous.get('character', '').split('|')):
                state = job.state['shots'].get(previous['shot_id'], {}).get('image', {})
                if state.get('vision', {}).get('pass') is True:
                    for name in names.intersection(previous.get('character', '').split('|')):anchors.setdefault(name, state['path'])
        job.cfg['quality']['vision']['enabled'] = quality['visual_review']
        review = job.vision(list(dict.fromkeys(anchors.values())) + [asset['path']], module.batch.make_prompt(row, job.characters, job.style))
        return image_tensor(asset['path']), review


class DramaSaveClip:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'video': ('VIDEO',), 'shot': ('DRAMA_SHOT',)}}
    RETURN_TYPES = ('DRAMA_CLIP',);RETURN_NAMES = ('动态镜头文件',)
    FUNCTION = 'save';CATEGORY = CATEGORY
    def save(self, video, shot):
        from comfy_api.latest import Types
        module = production();job = job_for(shot)
        path = job.folder / 'artifacts' / ('%s_video_a%d.mp4' % (shot['shot_id'], shot['attempt']))
        path.parent.mkdir(exist_ok=True)
        video.save_to(str(path), format=Types.VideoContainer('mp4'), codec=Types.VideoCodec('h264'), crf=18)
        qa = module.video_check(path, size=(job.cfg['dynamic']['width'], job.cfg['dynamic']['height']), expected_frames=job.cfg['dynamic']['frames'])
        asset = {'path': str(path), 'sha256': module.sha(path), 'shot_id': shot['shot_id'], 'qa': qa}
        item = {'filename': path.name, 'subfolder': path.parent.relative_to(APP / 'output').as_posix(), 'type': 'output'}
        return {'ui': {'videos': [item]}, 'result': (asset,)}


class DramaSaveAnimate2Clip:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'video': ('VIDEO',), 'shot': ('DRAMA_SHOT',)}}
    RETURN_TYPES = ('DRAMA_CLIP',);RETURN_NAMES = ('角色一致性动态镜头',)
    FUNCTION = 'save';CATEGORY = CATEGORY

    def save(self, video, shot):
        from comfy_api.latest import Types
        module = production();job = job_for(shot);settings = job.cfg['animation']
        path = job.folder / 'artifacts' / ('%s_video_transfer_a%d.mp4' % (shot['shot_id'], shot['attempt']))
        path.parent.mkdir(exist_ok=True)
        video.save_to(str(path), format=Types.VideoContainer('mp4'), codec=Types.VideoCodec('h264'), crf=18)
        qa = module.video_check(path, size=(settings['width'], settings['height']), expected_frames=settings['frames'])
        asset = {'path': str(path), 'sha256': module.sha(path), 'shot_id': shot['shot_id'], 'qa': qa}
        item = {'filename': path.name, 'subfolder': path.parent.relative_to(APP / 'output').as_posix(), 'type': 'output'}
        return {'ui': {'videos': [item]}, 'result': (asset,)}


class DramaEndpointFrames:
    """Make approved storyboard endpoints explicit in the encoded dynamic clip.

    Wan conditions the first and last images in latent space, but it does not
    guarantee byte-identical decoded endpoint frames.  Keeping these two frame
    positions exact gives downstream visual QA a real, inspectable contract;
    all interior frames still come from Wan and remain subject to motion QA.
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'frames': ('IMAGE',), 'start_image': ('IMAGE',)},
                'optional': {'end_image': ('IMAGE',)}}
    RETURN_TYPES = ('IMAGE',); RETURN_NAMES = ('硬锁首尾帧的动态帧',)
    FUNCTION = 'enforce'; CATEGORY = CATEGORY

    def enforce(self, frames, start_image, end_image=None):
        import torch.nn.functional as F
        if frames.ndim != 4 or frames.shape[0] < 2:
            raise ValueError('动态帧不足，无法执行首尾帧约束')

        def fit(image):
            image = image[:1].to(device=frames.device, dtype=frames.dtype)
            if tuple(image.shape[1:3]) == tuple(frames.shape[1:3]):
                return image[0]
            return F.interpolate(image.movedim(-1, 1), size=tuple(frames.shape[1:3]),
                                 mode='bilinear', align_corners=False).movedim(1, -1)[0]

        result = frames.clone()
        result[0] = fit(start_image)
        if end_image is not None:
            result[-1] = fit(end_image)
        return (result,)


class DramaVideoReview:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'clip': ('DRAMA_CLIP',), 'shot': ('DRAMA_SHOT',), 'quality': ('DRAMA_QUALITY_SETTINGS',)}}
    RETURN_TYPES = ('DRAMA_CLIP', 'DRAMA_REVIEW');RETURN_NAMES = ('已复检动态镜头', '视频质检报告')
    FUNCTION = 'review';CATEGORY = CATEGORY
    def review(self, clip, shot, quality):
        import av
        module = production();job = job_for(shot);row = job.shots[shot['index']]
        video = job.cfg['animation'] if job.animate2_template is not None else job.cfg['dynamic']
        qa = module.video_check(clip['path'], size=(video['width'], video['height']), expected_frames=video['frames'])
        with av.open(clip['path']) as video:frames = [frame.to_image() for frame in video.decode(video=0)]
        paths = []
        for index in sorted({0, len(frames)//2, len(frames)-1}):
            path = job.folder / ('%s_review_%d.jpg' % (shot['shot_id'], index));frames[index].save(path);paths.append(path)
        job.cfg['quality']['vision']['enabled'] = quality['visual_review']
        review = job.vision(paths, row.get('video_prompt') or row['description'], sequence=True)
        return {**clip, 'qa': qa, 'review': review}, review


class DramaStoredAsset:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'job_id': ('STRING',), 'shot_index': ('INT',), 'kind': (['image', 'video'],)}}
    RETURN_TYPES = ('*',);FUNCTION = 'load';CATEGORY = CATEGORY
    def load(self, job_id, shot_index, kind):
        job = job_for({'job_id': job_id});row = job.shots[shot_index];state = job.state['shots'][row['shot_id']][kind]
        if production().sha(state['path']) != state['sha256']:raise ValueError('恢复素材哈希不符')
        return ({'path': state['path'], 'sha256': state['sha256'], 'shot_id': row['shot_id'], 'review': state.get('vision'), 'qa': state.get('qa')},)


class DramaEpisodeAssets:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'approved_clip': ('DRAMA_CLIP',), 'shot': ('DRAMA_SHOT',)}}
    RETURN_TYPES = ('DRAMA_EPISODE',);RETURN_NAMES = ('全分镜素材与剧集',)
    FUNCTION = 'collect';CATEGORY = CATEGORY
    def collect(self, approved_clip, shot):
        module = production();job = job_for(shot)
        if approved_clip['review'].get('pass') is not True and approved_clip['review'].get('status') != 'not_checked':raise ValueError('动态镜头尚未通过质检')
        if module.sha(approved_clip['path']) != approved_clip['sha256']:raise ValueError('当前镜头哈希不符')
        for row in job.shots:
            for kind, folder, suffix in [('image', 'keyframes', '.png'), ('video', 'clips', '.mp4')]:
                state = job.state['shots'][row['shot_id']][kind]
                if state['status'] != 'complete' or module.sha(state['path']) != state['sha256']:raise ValueError('整集素材尚未齐全')
                if job.cfg['quality']['vision']['enabled'] and state.get('vision', {}).get('pass') is not True:raise ValueError('整集素材尚未通过视觉质检')
                target = job.folder / folder / (row['shot_id'] + suffix);target.parent.mkdir(exist_ok=True);shutil.copy2(state['path'], target)
        cfg = copy.deepcopy(job.cfg);cfg['_videos_dir'] = str(job.folder / 'clips')
        return ({'job_id': job.folder.name, 'folder': str(job.folder), 'shots': job.shots,
                 'characters': job.characters, 'board': job.board, 'cfg': cfg},)


class DramaOutputSettings:
    @classmethod
    def INPUT_TYPES(cls):
        cfg = json.loads((APP / 'drama/production_config.json').read_text())
        return {'required': {'width': ('INT', {'default': cfg['cinema']['width'], 'min': 2, 'max': 16384, 'step': 2}),
            'height': ('INT', {'default': cfg['cinema']['height'], 'min': 2, 'max': 16384, 'step': 2}),
            'fps': ('INT', {'default': cfg['cinema']['fps'], 'min': 1, 'max': 120}),
            'delivery_dir': ('STRING', {'default': cfg['delivery']['dir']})}}
    RETURN_TYPES = ('DRAMA_OUTPUT_SETTINGS',);FUNCTION = 'configure';CATEGORY = CATEGORY
    def configure(self, width, height, fps, delivery_dir):return ({'width': width, 'height': height, 'fps': fps, 'delivery_dir': delivery_dir},)


class DramaVoiceLines:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'episode': ('DRAMA_EPISODE',), 'audio': ('DRAMA_AUDIO_SETTINGS',)}}
    RETURN_TYPES = ('DRAMA_VOICES',);RETURN_NAMES = ('逐句配音与时长',)
    FUNCTION = 'synthesize';CATEGORY = CATEGORY
    def synthesize(self, episode, audio):
        from drama import render_cinema as renderer
        phase(episode, 'voice');cfg = audio_config(episode['cfg'], audio);sr = cfg['audio']['sample_rate']
        lines = renderer.make_voice(episode['shots'], episode['folder'], cfg, sr, episode['characters'])
        return ({'lines': lines, 'sample_rate': sr},)


class DramaTimelineSubtitles:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'episode': ('DRAMA_EPISODE',), 'voices': ('DRAMA_VOICES',), 'audio': ('DRAMA_AUDIO_SETTINGS',), 'output': ('DRAMA_OUTPUT_SETTINGS',)}}
    RETURN_TYPES = ('DRAMA_TIMELINE',);RETURN_NAMES = ('时间轴与字幕SRT',)
    FUNCTION = 'build';CATEGORY = CATEGORY
    def build(self, episode, voices, audio, output):
        from drama import render_cinema as renderer
        phase(episode, 'subtitles');cfg = audio_config(episode['cfg'], audio);cfg['cinema'].update({k: output[k] for k in ['width', 'height', 'fps']});c = cfg['cinema'];fps = c['fps']
        intro = float(c['intro_sec']);outro = float(c['outro_sec']);cursor = intro;timeline = []
        for row, voice in zip(episode['shots'], voices['lines']):
            lead = float(cfg['voice'].get('lead_in_sec', .2));voice_duration = voice[3]
            duration = max(float(row['duration_sec']), float(c['min_shot_sec']), voice_duration + lead + float(c['voice_tail_sec']) if voice_duration else 0)
            image = str(Path(episode['folder']) / 'keyframes' / (row['shot_id'] + '.png'))
            start = lead + .12;end = min(voice_duration + lead + .45 if voice_duration else duration, duration)
            timeline.append((row, cursor, duration, image, row.get('motion') or 'lock', row.get('dialogue', ''), start, end));cursor += duration
        path = Path(episode['folder']) / 'subtitles.srt'
        renderer.write_srt([(at + start, at + end, text) for row, at, duration, image, motion, text, start, end in timeline if text], str(path))
        return ({'timeline': timeline, 'total': cursor + outro, 'intro_frames': round(intro * fps), 'outro_frames': round(outro * fps),
                 'grade': episode['shots'][0].get('grade') or 'neutral', 'cfg': cfg, 'subtitles': str(path)},)


class DramaMixSound:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'episode': ('DRAMA_EPISODE',), 'voices': ('DRAMA_VOICES',), 'timeline': ('DRAMA_TIMELINE',), 'audio': ('DRAMA_AUDIO_SETTINGS',)}}
    RETURN_TYPES = ('AUDIO',);RETURN_NAMES = ('配音音乐音效混音',)
    FUNCTION = 'mix';CATEGORY = CATEGORY
    def mix(self, episode, voices, timeline, audio):
        import torch
        from drama import render_cinema as renderer
        phase(episode, 'audio');cfg = audio_config(timeline['cfg'], audio)
        waveform = renderer.build_audio(voices['lines'], timeline['timeline'], timeline['total'], voices['sample_rate'], cfg, timeline['grade'])
        return ({'waveform': torch.from_numpy(waveform).unsqueeze(0), 'sample_rate': voices['sample_rate']},)


class DramaComposeEpisode:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'episode': ('DRAMA_EPISODE',), 'timeline': ('DRAMA_TIMELINE',), 'mix': ('AUDIO',), 'output': ('DRAMA_OUTPUT_SETTINGS',)}}
    RETURN_TYPES = ('DRAMA_MOVIE',);RETURN_NAMES = ('1080竖屏成片',)
    FUNCTION = 'compose';CATEGORY = CATEGORY
    def compose(self, episode, timeline, mix, output):
        from drama import render_cinema as renderer
        phase(episode, 'compose');cfg = copy.deepcopy(timeline['cfg']);cfg['cinema'].update({k: output[k] for k in ['width', 'height', 'fps']});c = cfg['cinema'];w,h = c['width'],c['height'];board = episode['board'];sub = cfg['subtitle']
        intro = renderer.make_title_card(board.get('title') or '短剧', board.get('logline', ''), w, h, sub) if timeline['intro_frames'] else None
        outro = renderer.make_title_card('', '', w, h, sub, outro=True, text=c.get('outro_text', '完')) if timeline['outro_frames'] else None
        if c.get('cover'):
            with Image.open(timeline['timeline'][0][3]) as image:renderer.make_cover(image, board.get('title') or '短剧', board.get('logline', ''), w, h, c, sub, timeline['grade'], str(Path(episode['folder'])/'cover.png'))
        path = Path(episode['folder']) / 'episode.partial.mp4'
        frames = renderer.encode(str(path), timeline['timeline'], timeline['intro_frames'], timeline['outro_frames'], intro, outro,
                                 mix['waveform'][0].cpu().numpy(), cfg, timeline['grade'])
        return ({'job_id': episode['job_id'], 'path': str(path), 'frames': frames},)


class DramaFinalReview:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'movie': ('DRAMA_MOVIE',), 'output': ('DRAMA_OUTPUT_SETTINGS',)}}
    RETURN_TYPES = ('DRAMA_FINAL',);RETURN_NAMES = ('已验收MP4',)
    FUNCTION = 'review';CATEGORY = CATEGORY
    def review(self, movie, output):
        module = production();job = job_for({'job_id': movie['job_id']});job.save(phase='final_review')
        qa = module.video_check(movie['path'], audio=True, size=(output['width'], output['height']))
        source = Path(movie['path']);final = source.with_name('episode.mp4');source.replace(final)
        return {'job_id': job.folder.name, 'path': str(final), 'sha256': module.sha(final), 'qa': qa},


class DramaDeliverEpisode:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'final': ('DRAMA_FINAL',), 'output': ('DRAMA_OUTPUT_SETTINGS',)}}
    RETURN_TYPES = ('DRAMA_DELIVERY',);RETURN_NAMES = ('成片与交付目录',)
    FUNCTION = 'deliver';CATEGORY = CATEGORY
    def deliver(self, final, output):
        module = production();job = job_for({'job_id': final['job_id']})
        if module.sha(final['path']) != final['sha256']:raise ValueError('成片哈希不符')
        job.cfg['delivery']['dir'] = output['delivery_dir'];job.finish_delivery(Path(final['path']), final['qa'])
        return ({'job_id': job.folder.name, 'path': final['path'], 'sha256': final['sha256'], 'delivery_dir': job.state.get('delivery_dir'), 'qa': final['qa']},)


class DramaStageOutput:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'value': ('*',)}}
    RETURN_TYPES = ();FUNCTION = 'record';OUTPUT_NODE = True;CATEGORY = CATEGORY
    def record(self, value):return {'ui': {'drama_stage': [value]}, 'result': ()}


class DramaBatchRun:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'storyboard': ('DRAMA_STORYBOARD',), 'animation': ('DRAMA_ANIMATE2_SETTINGS',), 'chain': ('DRAMA_DELIVERY', {'lazy': True})}, 'hidden': {'prompt': 'PROMPT', 'unique_id': 'UNIQUE_ID'}}
    RETURN_TYPES = ('STRING',);RETURN_NAMES = ('制作进度',);FUNCTION = 'run';OUTPUT_NODE = True;CATEGORY = CATEGORY
    def check_lazy_status(self, **kwargs):return []
    def run(self, storyboard, animation, chain=None, prompt=None, unique_id=None):
        require_inference()
        source = contained(INPUT, storyboard['project'])
        if input_fingerprint(source) != storyboard['source_fingerprint']:raise ValueError('分镜已变化，请重新提交')
        linked = prompt[str(unique_id)]['inputs']['chain']
        if prompt[linked[0]]['class_type'] != 'DramaDeliverEpisode':raise ValueError('批量调度必须连接完整链路的最终交付节点')
        result = submit(storyboard['project'], 'production', workflow=prompt)
        return {'ui': {'drama_job': [result], 'text': ['完整画布任务：' + result['job_id']]}, 'result': ('http://127.0.0.1:8188'+result['url'],)}
    @classmethod
    def IS_CHANGED(cls, **kwargs):return float('nan')


NODE_CLASS_MAPPINGS = {cls.__name__: cls for cls in [DramaShotPlan, DramaSaveKeyframe, DramaKeyframeReview, DramaSaveClip, DramaEndpointFrames, DramaVideoReview,
    DramaSaveAnimate2Clip, DramaStoredAsset, DramaEpisodeAssets, DramaOutputSettings, DramaVoiceLines, DramaTimelineSubtitles, DramaMixSound,
    DramaComposeEpisode, DramaFinalReview, DramaDeliverEpisode, DramaStageOutput, DramaBatchRun]}
NODE_DISPLAY_NAME_MAPPINGS = dict(zip(NODE_CLASS_MAPPINGS, ['读取当前镜头与提示词','保存关键帧检查点','关键帧视觉质检','保存动态镜头检查点','硬锁首尾帧','动态视频视觉质检',
    '恢复已保存素材','汇集全剧已通过镜头','高清输出与交付位置','逐句生成配音','时间轴与字幕','配音音乐音效混音','1080竖屏合成','成片媒体质检','复制成片与素材到桌面','批量阶段回执','全分镜调度与自动重试']))
