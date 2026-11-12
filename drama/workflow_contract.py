"""Storyboard and settings contracts. Standard library only; never loads models."""
import csv
import hashlib
import json
import math
from pathlib import Path
import re

INPUT_FILES = ('storyboard.json', 'shots.csv', 'characters.json')
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
        for key in ['character', 'dialogue', 'speaker', 'video_prompt', 'camera', 'motion', 'lighting', 'grade', 'sfx', 'end_frame']:
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
