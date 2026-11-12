"""Visible, model-free input and setting nodes for the production workflow."""
import json
from drama.workflow_contract import load_storyboard, input_fingerprint, VIDEO_PRESETS
from drama.workflow_build import require_inference
from .production_server import APP, INPUT, JOBS, contained, projects, submit

CATEGORY = '短剧/全动态流水线'


class DramaStoryboardInput:
    @classmethod
    def INPUT_TYPES(cls):
        names = projects()
        default = json.loads((APP / 'drama/production_config.json').read_text()).get('default_project')
        return {'required': {'project': (names, {'default': default} if default in names else {})},
                'optional': {'job_id': ('STRING', {'default': '', 'advanced': True})}}
    RETURN_TYPES = ('DRAMA_STORYBOARD',)
    RETURN_NAMES = ('storyboard',)
    FUNCTION = 'load'
    CATEGORY = CATEGORY

    def load(self, project, job_id=''):
        folder = contained(JOBS, job_id) if job_id else None
        source = folder / 'input' if folder else contained(INPUT, project)
        cfg = json.loads((folder / 'config.json' if folder else APP / 'drama/production_config.json').read_text())
        if folder:project = json.loads((folder / 'state.json').read_text())['project']
        board, shots, characters, style = load_storyboard(source, cfg)
        return ({'project': project, 'job_id': job_id, 'source_fingerprint': input_fingerprint(source),
                 'title': board.get('title', project), 'shot_count': len(shots)},)

    @classmethod
    def IS_CHANGED(cls, project, job_id=''):return input_fingerprint(contained(JOBS, job_id) / 'input' if job_id else contained(INPUT, project))


class DramaImageSettings:
    @classmethod
    def INPUT_TYPES(cls):
        cfg = json.loads((APP / 'drama/production_config.json').read_text())['image']
        return {'required': {
            'default_width': ('INT', {'default': cfg['width'], 'min': 16, 'max': 16384, 'step': 16}),
            'default_height': ('INT', {'default': cfg['height'], 'min': 16, 'max': 16384, 'step': 16}),
            'steps': ('INT', {'default': cfg['steps'], 'min': 1, 'max': 100}),
            'style': ('STRING', {'default': cfg['style'], 'multiline': True, 'tooltip': '分镜没有 global_style 时使用的缺省画风'}),
        }}
    RETURN_TYPES = ('DRAMA_IMAGE_SETTINGS',)
    FUNCTION = 'configure'
    CATEGORY = CATEGORY

    def configure(self, default_width, default_height, steps, style):
        return ({'width': default_width, 'height': default_height, 'steps': steps, 'style': style},)


class DramaVideoSettings:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'preset': (list(VIDEO_PRESETS), {'default': 'production'})}}
    RETURN_TYPES = ('DRAMA_VIDEO_SETTINGS',)
    FUNCTION = 'configure'
    CATEGORY = CATEGORY

    def configure(self, preset):return ({'preset': preset, **VIDEO_PRESETS[preset]},)


class DramaAnimate2Settings:
    @classmethod
    def INPUT_TYPES(cls):
        animation = json.loads((APP / 'drama/production_config.json').read_text())['animation']
        return {'required': {'preset': (list(animation['presets']), {'default': animation['preset']})}}
    RETURN_TYPES = ('DRAMA_ANIMATE2_SETTINGS',)
    RETURN_NAMES = ('Animate2 串行角色一致性设置',)
    FUNCTION = 'configure'
    CATEGORY = CATEGORY

    def configure(self, preset):
        animation = json.loads((APP / 'drama/production_config.json').read_text())['animation']
        return ({'preset': preset, **animation['presets'][preset]},)


class DramaQualitySettings:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {
        'max_retries': ('INT', {'default': 2, 'min': 0, 'max': 10}),
        'visual_review': ('BOOLEAN', {'default': True}),
        'timeout_minutes': ('INT', {'default': 180, 'min': 1, 'max': 1440}),
    }}
    RETURN_TYPES = ('DRAMA_QUALITY_SETTINGS',)
    FUNCTION = 'configure'
    CATEGORY = CATEGORY

    def configure(self, max_retries, visual_review, timeout_minutes):
        return ({'max_retries': max_retries, 'visual_review': visual_review,
                 'stage_timeout_sec': timeout_minutes * 60},)


class DramaAudioSettings:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {
        'voice_enabled': ('BOOLEAN', {'default': True}),
        'voice_name': ('STRING', {'default': 'Reed (中文（中国大陆）)'}),
        'voice_rate': ('INT', {'default': 170, 'min': 80, 'max': 350}),
        'subtitles': ('BOOLEAN', {'default': True}),
        'music': ('BOOLEAN', {'default': True}),
    }}
    RETURN_TYPES = ('DRAMA_AUDIO_SETTINGS',)
    FUNCTION = 'configure'
    CATEGORY = CATEGORY

    def configure(self, voice_enabled, voice_name, voice_rate, subtitles, music):
        return ({'voice_enabled': voice_enabled, 'voice_name': voice_name,
                 'voice_rate': voice_rate, 'subtitles': subtitles, 'music': music},)


class DramaProductionSubmit:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {
        'storyboard': ('DRAMA_STORYBOARD',), 'image': ('DRAMA_IMAGE_SETTINGS',),
        'video': ('DRAMA_VIDEO_SETTINGS',), 'quality': ('DRAMA_QUALITY_SETTINGS',),
        'audio': ('DRAMA_AUDIO_SETTINGS',),
    }}
    RETURN_TYPES = ('STRING',)
    RETURN_NAMES = ('job_url',)
    FUNCTION = 'start'
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def start(self, storyboard, image, video, quality, audio):
        source = contained(INPUT, storyboard['project'])
        if input_fingerprint(source) != storyboard['source_fingerprint']:
            raise ValueError('分镜文件已修改，请重新执行分镜输入节点')
        quality = dict(quality)
        visual = quality.pop('visual_review')
        cfg = json.loads((APP / 'drama/production_config.json').read_text())
        quality['vision'] = {**cfg['quality']['vision'], 'enabled': visual}
        video = dict(video);preset = video.pop('preset')
        result = submit(storyboard['project'], preset, {'image': image, 'dynamic': video,
                        'quality': quality, 'audio_settings': audio})
        url = 'http://127.0.0.1:8188' + result['url']
        return {'ui': {'text': ['任务进度：' + url], 'drama_job': [result]}, 'result': (url,)}

    @classmethod
    def IS_CHANGED(cls, **kwargs):return float('nan')


NODE_CLASS_MAPPINGS = {c.__name__: c for c in [DramaStoryboardInput, DramaImageSettings,
    DramaVideoSettings, DramaAnimate2Settings, DramaQualitySettings, DramaAudioSettings, DramaProductionSubmit]}
NODE_DISPLAY_NAME_MAPPINGS = dict(zip(NODE_CLASS_MAPPINGS, [
    '① 分镜输入与校验', '② 3D国风关键帧设置', '③ Wan 动作草稿设置', '④ Wan Animate2 角色动作迁移',
    '⑤ 质检与自动重试', '⑥ 配音字幕与配乐', '⑦ 提交批量出片任务']))


class DramaModelGate:
    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths
        cfg = json.loads((APP / 'drama/production_config.json').read_text())['dynamic']
        choices = [('z_image_model', 'diffusion_models', 'z_image_turbo_bf16.safetensors'),
                   ('z_image_text', 'text_encoders', 'qwen_3_4b.safetensors'),
                   ('z_image_vae', 'vae', 'ae.safetensors'),
                   ('wan_model', 'diffusion_models', cfg['model']),
                   ('wan_text', 'text_encoders', cfg['text_encoder']), ('wan_vae', 'vae', cfg['vae']),
                   ('animate2_model', 'diffusion_models', 'wan_animate_2_int8_convrot.safetensors'),
                   ('animate2_text', 'text_encoders', 'umt5_xxl_fp8_e4m3fn_scaled.safetensors'),
                   ('animate2_clip', 'clip_vision', 'clip_vision_h.safetensors'),
                   ('animate2_lora', 'loras', 'lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors'),
                   ('animate2_vae', 'vae', 'Wan2_1_VAE_bf16.safetensors')]
        return {'required': {}, 'optional': {name: (folder_paths.get_filename_list(kind), {'default': default}) for name, kind, default in choices}}
    RETURN_TYPES = ('*',) * 11
    RETURN_NAMES = ('z_image_model', 'z_image_text', 'z_image_vae', 'wan_model', 'wan_text', 'wan_vae',
                    'animate2_model', 'animate2_text', 'animate2_clip', 'animate2_lora', 'animate2_vae')
    FUNCTION = 'filenames'
    CATEGORY = CATEGORY

    def filenames(self, z_image_model=None, z_image_text=None, z_image_vae=None, wan_model=None, wan_text=None, wan_vae=None,
                  animate2_model=None, animate2_text=None, animate2_clip=None, animate2_lora=None, animate2_vae=None):
        require_inference()
        cfg = json.loads((APP / 'drama/production_config.json').read_text())['dynamic']
        animate = json.loads((APP / 'drama/production_config.json').read_text())['animation']
        return (z_image_model or 'z_image_turbo_bf16.safetensors', z_image_text or 'qwen_3_4b.safetensors', z_image_vae or 'ae.safetensors',
                wan_model or cfg['model'], wan_text or cfg['text_encoder'], wan_vae or cfg['vae'],
                animate2_model or animate['model'], animate2_text or animate['text_encoder'],
                animate2_clip or animate['clip_vision'], animate2_lora or animate['lora'], animate2_vae or animate['vae'])


NODE_CLASS_MAPPINGS['DramaModelGate'] = DramaModelGate
NODE_DISPLAY_NAME_MAPPINGS['DramaModelGate'] = '模型启动门禁（构建通过后才放行）'
