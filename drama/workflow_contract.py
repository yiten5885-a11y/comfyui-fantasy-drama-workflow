"""Storyboard and settings contracts. Standard library only; never loads models."""
import csv
import hashlib
import json
import math
from pathlib import Path
import re

INPUT_FILES = ('storyboard.json', 'shots.csv', 'characters.json')
KNOWN_SFX = {
    'silence', 'rain', 'wind', 'waves', 'boat', 'steps', 'clock',
    'engine', 'city', 'birds', 'paper', 'door', 'energy', 'fire',
    'impact', 'sword', 'thunder'
}
KNOWN_GRADES = {'', 'neutral', 'cool', 'warm', 'golden', 'noir'}
KNOWN_TRANSITIONS = {'', 'dip', 'dissolve', 'cut', 'flash'}
KNOWN_MOTIONS = {
    '', 'lock', 'static', 'zoom_in', 'zoom_out', 'pan_left', 'pan_right',
    'pull_out', 'push_in', 'tilt_up', 'tilt_down'
}
ELDER_HINT = ('老', '爷', '婆', '祖', '长者')
MALE_HINT = ('哥', '弟', '公', '男', '剑修', '少年', '青年', '郎', '道士', '僧')
FEMALE_HINT = ('姐', '妹', '娘', '女', '仙子', '少女', '姑', '夫人', '姑娘', '姨')
_INSTALLED_VOICES_CACHE = None


def load_characters(path):
    path = Path(path)
    if not path.is_file(): return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        chars = data.get('characters', data) if isinstance(data, dict) else {}
        return chars if isinstance(chars, dict) else {}
    except Exception:
        return {}


def get_installed_say_voices():
    global _INSTALLED_VOICES_CACHE
    if _INSTALLED_VOICES_CACHE is not None:
        return _INSTALLED_VOICES_CACHE
    import subprocess, shutil
    voices = set()
    if shutil.which('say'):
        try:
            res = subprocess.run(['say', '-v', '?'], capture_output=True, text=True, timeout=5)
            for line in res.stdout.splitlines():
                if line.strip():
                    parts = line.split()
                    if parts:
                        voices.add(parts[0])
                        # Also add full name if formatted like Reed (zh_CN)
                        name_before_bracket = line.split('(')[0].strip()
                        if name_before_bracket:
                            voices.add(name_before_bracket)
        except Exception:
            pass
    _INSTALLED_VOICES_CACHE = voices
    return voices


def is_voice_installed(voice, engine='say'):
    if engine != 'say':
        return True
    installed = get_installed_say_voices()
    if not installed:
        # If say is not available or returned nothing, return False unless in test
        return False
    v = (voice or '').strip()
    short = v.split('(')[0].strip()
    return v in installed or short in installed


def resolve_speaker_voice(speaker, cfg=None, characters=None):
    if isinstance(cfg, dict) and ('voice' in cfg or 'cinema' in cfg or 'dynamic' in cfg):
        config = cfg
        chars = characters or {}
    elif isinstance(characters, dict) and ('voice' in characters or 'cinema' in characters or 'dynamic' in characters):
        config = characters
        chars = cfg or {}
    else:
        config = cfg or {}
        chars = characters or {}
    name = (speaker or '').strip()
    vcfg = config.get('voice', {})
    cmap = config.get('cinema', {}).get('voice_map', {})
    per_char = vcfg.get('per_character', {})
    base_rate = int(vcfg.get('rate', 170))
    info = chars.get(name, {}) if isinstance(chars, dict) else {}

    # 1. 角色表里该角色的 voice 和 voice_rate
    if info.get('voice'):
        voice = info['voice']
        rate = int(info.get('voice_rate') or base_rate)
        return voice, rate, '角色表指定'

    # 2. 配置文件 voice.per_character 里按角色名指定的声音
    if name in per_char:
        voice = per_char[name]
        rate = int(info.get('voice_rate') or base_rate)
        return voice, rate, '按角色名配置'

    # 3. 角色表里的 gender: male, female, elder, child
    gender = str(info.get('gender') or '').lower().strip()
    if gender in ('male', 'female', 'elder', 'child'):
        slot = f'_{gender}'
        if slot in cmap:
            voice = cmap[slot]
            rate = int(info.get('voice_rate') or (base_rate * 0.88 if gender == 'elder' else base_rate))
            return voice, rate, '按性别指定'

    # 4. 按名字和外观描述推断
    anchor = str(info.get('anchor') or '')
    if any(k in name for k in ELDER_HINT) or any(k in anchor for k in ('长者', '老年', '50岁', '60岁', '白发苍苍')):
        voice = cmap.get('_elder', 'Grandpa (中文（中国大陆）)')
        rate = int(info.get('voice_rate') or (base_rate * 0.88))
        reason = '按名字推断' if any(k in name for k in ELDER_HINT) else '按外观设定推断'
        return voice, rate, reason
    if any(k in name for k in FEMALE_HINT) or any(k in anchor for k in ('女性', '少女', '女子', '仙女', '师姐', '师妹')):
        voice = cmap.get('_female', 'Tingting (中文（中国大陆）)')
        rate = int(info.get('voice_rate') or base_rate)
        reason = '按名字推断' if any(k in name for k in FEMALE_HINT) else '按外观设定推断'
        return voice, rate, reason
    if any(k in name for k in MALE_HINT) or any(k in anchor for k in ('男性', '少年', '青年', '剑修', '男子')):
        voice = cmap.get('_male', 'Reed (中文（中国大陆）)')
        rate = int(info.get('voice_rate') or base_rate)
        reason = '按名字推断' if any(k in name for k in MALE_HINT) else '按外观设定推断'
        return voice, rate, reason

    # 5. _default
    voice = cmap.get('_default') or vcfg.get('name', 'Reed (中文（中国大陆）)')
    rate = int(info.get('voice_rate') or base_rate)
    return voice, rate, '默认音色'
VIDEO_PRESETS = {
    'preview': {'width': 384, 'height': 672, 'frames': 17, 'steps': 12, 'fps': 8},
    'production': {'width': 736, 'height': 1312, 'frames': 73, 'steps': 20, 'fps': 24},
}


def input_payloads(folder):
    folder = Path(folder).resolve()
    payloads = {}
    for name in INPUT_FILES:
        path = folder / name
        if path.is_file():
            path.resolve().relative_to(folder)
            payloads[name] = path.read_bytes()
    # Optional final-pose image references are plain files inside the project
    # folder. Include them in the immutable input fingerprint and job snapshot.
    if 'storyboard.json' in payloads:
        try:
            board = json.loads(payloads['storyboard.json'].decode('utf-8-sig'))
            shots = board.get('shots', []) if isinstance(board, dict) else []
        except (ValueError, UnicodeError):
            shots = []
    elif 'shots.csv' in payloads:
        try:shots = list(csv.DictReader(payloads['shots.csv'].decode('utf-8-sig').splitlines()))
        except UnicodeError:shots = []
    else:shots = []
    for shot in shots:
        if not isinstance(shot, dict):continue
        name = shot.get('end_frame')
        if not name:continue
        if not isinstance(name, str) or not re.fullmatch(r'[\w.-]{1,100}\.(?:png|jpe?g|webp)', name, re.I):
            raise ValueError('end_frame 必须是项目目录内的 PNG/JPG/WEBP 文件名')
        path = (folder / name).resolve();path.relative_to(folder)
        if path.is_file():payloads[name] = path.read_bytes()
    return payloads


def input_fingerprint(folder):
    h = hashlib.sha256()
    for name, content in sorted(input_payloads(folder).items()):
        h.update(name.encode()); h.update(content)
    return h.hexdigest()


def load_storyboard(folder, cfg):
    folder = Path(folder).resolve()
    files = input_payloads(folder)
    board, characters = {}, {}
    style = cfg['image'].get('style', '')
    if 'storyboard.json' in files:
        board = json.loads(files['storyboard.json'].decode('utf-8-sig'))
        if not isinstance(board, dict):raise ValueError('storyboard.json 顶层必须是对象')
        shots = board.get('shots', [])
        characters = board.get('characters', {})
        style = board.get('global_style') or style
    elif 'shots.csv' in files:
        shots = list(csv.DictReader(files['shots.csv'].decode('utf-8-sig').splitlines()))
        board = {'title': folder.name, 'logline': ''}
    else:raise ValueError('需要 storyboard.json 或 shots.csv')
    if 'characters.json' in files:
        obj = json.loads(files['characters.json'].decode('utf-8-sig'))
        characters, style = obj.get('characters', characters), obj.get('global_style') or style
    if not isinstance(shots, list) or not shots:raise ValueError('分镜 shots 必须是非空数组')
    if not isinstance(characters, dict):raise ValueError('characters 必须是角色名到设定的对象')
    for name, character in characters.items():
        if not isinstance(character, dict) or not isinstance(character.get('anchor', ''), str):
            raise ValueError('角色设定无效：' + name)
    seen, rows = set(), []
    for index, raw in enumerate(shots, 1):
        if not isinstance(raw, dict):raise ValueError('每个分镜必须是对象')
        s = {key: value for key, value in raw.items() if value is not None}
        sid = str(s.get('shot_id') or 'S%02d' % index)
        if not re.fullmatch(r'[\w-]{1,64}', sid) or sid in seen:
            raise ValueError('shot_id 必须唯一，且只含文字、数字、横线或下划线：' + sid)
        seen.add(sid)
        s.update(shot_id=sid, _index=index)
        s['description'] = s.get('description') or s.get('prompt', '')
        if not isinstance(s['description'], str) or not s['description'].strip():
            raise ValueError(sid + ' 缺少有效 description / prompt')
        for key in ['character', 'dialogue', 'speaker', 'video_prompt', 'camera', 'motion', 'lighting', 'grade', 'transition', 'sfx', 'end_frame']:
            s[key] = s.get(key, '')
            if not isinstance(s[key], str):raise ValueError(sid + '.' + key + ' 必须是字符串')
        if s['end_frame']:
            if not re.fullmatch(r'[\w.-]{1,100}\.(?:png|jpe?g|webp)', s['end_frame'], re.I):
                raise ValueError(sid + '.end_frame 必须是项目目录内的 PNG/JPG/WEBP 文件名')
            end_path = (folder / s['end_frame']).resolve();end_path.relative_to(folder)
            if not end_path.is_file() or not end_path.stat().st_size:
                raise ValueError(sid + '.end_frame 文件不存在或为空：' + s['end_frame'])
        for name in s['character'].split('|'):
            if name.strip() and name.strip() not in characters:raise ValueError(sid + ' 的角色不在角色表中：' + name.strip())
        for key in ['width', 'height']:
            s[key] = int(s.get(key) or cfg['image'][key])
            if s[key] < 16 or s[key] > 16384 or s[key] % 16:raise ValueError(sid + ' 的宽高必须在 16～16384 之间且为 16 的倍数')
        s['duration_sec'] = float(s.get('duration_sec') or 3)
        if not math.isfinite(s['duration_sec']) or s['duration_sec'] <= 0:raise ValueError(sid + ' duration_sec 必须是正数')
        if s.get('seed') not in ('', None):
            s['seed'] = int(s['seed'])
            if not 0 <= s['seed'] <= 2**64 - 1:raise ValueError(sid + ' seed 超出范围')
        rows.append(s)
    return board, rows, characters, style


def configure(base, preset='preview', overrides=None):
    cfg = json.loads(json.dumps(base))
    if preset not in VIDEO_PRESETS:raise ValueError('未知视频预设')
    cfg['dynamic'].update(VIDEO_PRESETS[preset])
    overrides = overrides or {}
    unknown = set(overrides) - {'image', 'dynamic', 'animation', 'quality', 'audio_settings', 'cinema', 'delivery'}
    if unknown:raise ValueError('未知制作设置：' + ','.join(sorted(unknown)))
    for section in ['image', 'dynamic', 'animation', 'quality', 'cinema', 'delivery']:
        cfg[section].update(overrides.get(section, {}))
    # A selected preview profile also replaces geometry inherited from the
    # canvas default, so both sequential video stages use the same frame budget.
    if preset == 'preview':
        cfg['animation']['preset'] = 'preview'
    for key in ['width', 'height']:
        if cfg['image'][key] % 16:raise ValueError('关键帧宽高必须为 16 的倍数')
        if cfg['dynamic'][key] % 32:raise ValueError('Wan 宽高必须为 32 的倍数')
    if cfg['dynamic']['frames'] < 5 or (cfg['dynamic']['frames'] - 1) % 4:raise ValueError('Wan 帧数必须为 4n+1 且至少 5 帧')
    if cfg['animation']['enabled']:
        if cfg['animation']['preset'] not in cfg['animation']['presets']:raise ValueError('未知 Animate2 预设')
        video = cfg['animation']['presets'][cfg['animation']['preset']]
        for key, value in video.items():
            if preset == 'preview':cfg['animation'][key] = value
            else:cfg['animation'].setdefault(key, value)
        if video['width'] % 32 or video['height'] % 32:raise ValueError('Animate2 宽高必须为 32 的倍数')
        if video['frames'] < 5 or (video['frames'] - 1) % 4:raise ValueError('Animate2 帧数必须为 4n+1 且至少 5 帧')
    if cfg['quality']['max_retries'] < 0:raise ValueError('重试次数不能为负数')
    a = overrides.get('audio_settings', {})
    if a:
        cfg['voice'].update(enabled=a['voice_enabled'], name=a['voice_name'], rate=a['voice_rate'])
        cfg['voice']['per_character'] = {'_default': a['voice_name']}
        cfg['cinema']['voice_map']['_default'] = a['voice_name']
        cfg['subtitle']['burn_in'] = a['subtitles']
        cfg['music']['enabled'] = a['music']
    return cfg


def storyboard_summary(folder, cfg):
    """Validated, display-ready digest of a project; raises ValueError like load_storyboard."""
    files = input_payloads(folder)
    board, shots, characters, style = load_storyboard(folder, cfg)
    warnings = []
    plain = [s['shot_id'] for s in shots if not s['video_prompt'].strip()]
    if plain:warnings.append('未填 video_prompt，动态镜头将沿用首帧描述：' + '、'.join(plain))
    unvoiced = [s['shot_id'] for s in shots if s['dialogue'].strip() and not s['speaker'].strip()]
    if unvoiced:warnings.append('有台词但未填 speaker，将使用默认配音：' + '、'.join(unvoiced))
    loose = [s['shot_id'] for s in shots if not s['character'].strip()]
    if loose:warnings.append('未指定 character，角色外观锚点不会写入提示词：' + '、'.join(loose))
    if not characters:warnings.append('角色表为空，跨镜头一致性只能依赖画面描述')

    # 检查未识别的音效、运镜、色调和转场名称
    unrec_sfx = []
    unrec_grade = []
    unrec_trans = []
    for s in shots:
        sid = s['shot_id']
        sfx_text = s.get('sfx', '').strip()
        if sfx_text:
            for part in sfx_text.split('|'):
                base = part.split('@')[0].strip().lower()
                if base and base not in KNOWN_SFX:
                    unrec_sfx.append('%s(%s)' % (sid, base))
        grade_text = s.get('grade', '').strip().lower()
        if grade_text and grade_text not in KNOWN_GRADES:
            unrec_grade.append('%s(%s)' % (sid, grade_text))
        trans_text = s.get('transition', '').strip().lower()
        if trans_text and trans_text not in KNOWN_TRANSITIONS:
            unrec_trans.append('%s(%s)' % (sid, trans_text))
    if unrec_sfx:
        warnings.append('未识别的音效名称（将静音）：' + '、'.join(unrec_sfx))
    if unrec_grade:
        warnings.append('未识别的色调名称：' + '、'.join(unrec_grade))
    if unrec_trans:
        warnings.append('未识别的转场名称：' + '、'.join(unrec_trans))

    # 配音表汇总
    speaker_shots = {}
    for s in shots:
        text = (s.get('dialogue') or '').strip()
        if text:
            sp = (s.get('speaker') or '').strip()
            speaker_shots.setdefault(sp, []).append(s)

    engine = cfg.get('voice', {}).get('engine', 'say')
    speakers = []
    for sp, sp_shots in speaker_shots.items():
        v, r, reason = resolve_speaker_voice(sp, cfg, characters)
        installed = is_voice_installed(v, engine)
        speakers.append({
            'speaker': sp or '（默认）',
            'voice': v,
            'rate': r,
            'reason': reason,
            'dialogue_count': len(sp_shots),
            'installed': installed,
            'sample_text': (sp_shots[0].get('dialogue') or '').strip()
        })

    return {
        'title': board.get('title') or Path(folder).name, 'logline': board.get('logline', ''), 'style': style,
        'source': 'storyboard.json' if 'storyboard.json' in files else 'shots.csv',
        'characters': list(characters), 'shot_count': len(shots),
        'duration_sec': round(sum(s['duration_sec'] for s in shots), 2),
        'dialogue_lines': sum(1 for s in shots if s['dialogue'].strip()),
        'speakers': speakers,
        'warnings': warnings,
        'shots': [{'shot_id': s['shot_id'], 'character': s['character'], 'camera': s['camera'],
                   'duration_sec': s['duration_sec'], 'dialogue': s['dialogue'], 'speaker': s['speaker'],
                   'description': s['description'], 'has_video_prompt': bool(s['video_prompt'].strip()),
                   'grade': s.get('grade', ''), 'transition': s.get('transition', ''), 'sfx': s.get('sfx', ''),
                   'end_frame': s['end_frame'], 'width': s['width'], 'height': s['height']} for s in shots],
    }
