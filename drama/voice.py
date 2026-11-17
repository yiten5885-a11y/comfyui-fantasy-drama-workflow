"""Voice casting and text-to-speech engines. Standard library only.

Casting order for a line's speaker:
  1. ``characters[speaker].voice`` / ``voice_rate`` in the storyboard
  2. ``voice.per_character[speaker]`` in production_config.json
  3. ``characters[speaker].gender`` -> ``_male`` / ``_female`` / ``_elder`` / ``_child``
  4. name hints (老/爷/婆 ... -> elder, 先生/哥/叔 ... -> male)
  5. ``_default``

Engines: ``say`` (macOS) or ``command`` (any program that writes an audio file,
configured as an argument list; no shell is involved).
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ENGINES = ('say', 'command')
GENDERS = ('male', 'female', 'elder', 'child')
ELDER_HINT = ('老', '爷', '婆', '奶', '翁', '姥')
MALE_HINT = ('先生', '哥', '叔', '伯', '父', '男', '公子', '少爷', '师兄', '师弟', '剑修', '将军', '王爷')
FEMALE_HINT = ('姐', '妹', '娘', '姑', '女', '妃', '夫人', '小姐', '师姐', '师妹', '仙子')
# macOS Sonoma+ Chinese voices only work under their full name; the short name
# silently produces a ~0.01 s empty file.
VOICE_FULL = {name: '%s (中文（中国大陆）)' % name for name in
              ('Tingting', 'Reed', 'Eddy', 'Rocko', 'Grandpa', 'Grandma', 'Flo', 'Shelley', 'Sandy')}
VOICE_FULL.update({'Meijia': 'Meijia (中文（台湾）)', 'Sinji': 'Sinji (中文（香港）)'})
SAFE_VOICE = VOICE_FULL['Tingting']
DEFAULT_MAP = {'_default': SAFE_VOICE, '_female': SAFE_VOICE, '_male': VOICE_FULL['Reed'],
               '_elder': VOICE_FULL['Grandpa'], '_child': VOICE_FULL['Flo']}
ELDER_RATE = 0.88


def norm_voice(name):
    name = (name or '').strip()
    return VOICE_FULL.get(name, name) or SAFE_VOICE


def voice_map(cfg):
    """Built-in defaults < cinema.voice_map < voice.per_character (later wins)."""
    merged = dict(DEFAULT_MAP)
    merged.update((cfg.get('cinema') or {}).get('voice_map') or {})
    merged.update((cfg.get('voice') or {}).get('per_character') or {})
    return merged


def guess_gender(name, character):
    gender = str((character or {}).get('gender') or '').strip().lower()
    if gender in GENDERS:return gender, 'gender'
    anchor = str((character or {}).get('anchor') or '')
    if any(k in name for k in ELDER_HINT) or re.search(r'[5-9]\d\s*岁|老者|白发老', anchor):return 'elder', 'hint'
    if any(k in name for k in FEMALE_HINT) or re.search(r'女性|少女|女子', anchor):return 'female', 'hint'
    if any(k in name for k in MALE_HINT) or re.search(r'男性|少年|男子', anchor):return 'male', 'hint'
    return '', ''


def cast(speaker, cfg, characters):
    """Resolve ``(voice, rate, source)`` for one speaker; source explains the choice."""
    vcfg = cfg.get('voice') or {}
    base_rate = int(vcfg.get('rate', 170))
    name = (speaker or '').strip()
    character = (characters or {}).get(name) or {}
    mapping = voice_map(cfg)
    gender, how = guess_gender(name, character)
    rate = base_rate
    if character.get('voice_rate') not in (None, ''):rate = int(character['voice_rate'])
    elif gender == 'elder':rate = int(base_rate * ELDER_RATE)
    if character.get('voice'):voice, source = character['voice'], '角色表指定'
    elif name and name in (vcfg.get('per_character') or {}):voice, source = vcfg['per_character'][name], '配置文件指定'
    elif gender:voice, source = mapping.get('_' + gender, mapping['_default']), ('按性别' if how == 'gender' else '按名字推断') + '：' + gender
    else:voice, source = mapping['_default'], '默认音色'
    engine = vcfg.get('engine', 'say')
    return (norm_voice(voice) if engine == 'say' else str(voice)), rate, source


def engine_name(cfg):
    engine = (cfg.get('voice') or {}).get('engine', 'say')
    return engine if engine in ENGINES else 'say'


def output_suffix(cfg):
    if engine_name(cfg) == 'say':return '.aiff'
    return '.' + str((cfg.get('voice') or {}).get('command_ext', 'wav')).lstrip('.')


def command(cfg, voice, rate, text, out, text_file):
    """argv for one line. ``text_file`` already holds ``text`` (UTF-8)."""
    if engine_name(cfg) == 'say':
        # -f keeps a line that starts with "-" from being read as an option.
        return ['say', '-v', voice, '-r', str(int(rate)), '-o', str(out), '-f', str(text_file)], None
    template = (cfg.get('voice') or {}).get('command') or []
    if not isinstance(template, list) or not template or not all(isinstance(part, str) for part in template):
        raise ValueError('voice.command 必须是非空的字符串数组，例如 ["piper", "--model", "{voice}", "--output_file", "{out}"]')
    values = {'voice': voice, 'rate': str(int(rate)), 'text': text, 'out': str(out), 'text_file': str(text_file)}
    argv = [part.format(**values) for part in template]
    stdin = None if any('{text}' in part or '{text_file}' in part for part in template) else text
    return argv, stdin


def synthesize(cfg, voice, rate, text, out, timeout=None):
    """Run the engine once and return the written path. Raises RuntimeError on failure."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    timeout = timeout or float((cfg.get('voice') or {}).get('timeout_sec', 60))
    with tempfile.NamedTemporaryFile('w', suffix='.txt', encoding='utf-8', delete=False) as handle:
        handle.write(text);text_file = handle.name
    try:
        argv, stdin = command(cfg, voice, rate, text, out, text_file)
        try:
            result = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:raise RuntimeError('找不到配音程序：' + argv[0]) from e
        except subprocess.TimeoutExpired as e:raise RuntimeError('配音超时（%ss）' % timeout) from e
        if result.returncode != 0:
            raise RuntimeError('配音程序返回 %d：%s' % (result.returncode, (result.stderr or result.stdout).strip()[:300]))
        if not out.is_file() or not out.stat().st_size:raise RuntimeError('配音程序没有写出音频文件：' + str(out))
        return out
    finally:
        os.unlink(text_file)


_SAY_LINE = re.compile(r'^(?P<name>.+?)\s+(?P<locale>[a-z]{2,3}[_-][A-Za-z0-9]{2,4})\s+#')


def parse_say_voices(listing):
    return [match.group('name').strip() for match in map(_SAY_LINE.match, listing.splitlines()) if match]


def installed_voices(which=shutil.which, run=subprocess.run):
    """Voice names ``say -v ?`` reports, or None when ``say`` is unavailable."""
    if not which('say'):return None
    try:
        listing = run(['say', '-v', '?'], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):return None
    return parse_say_voices(listing)


def engine_status(cfg, which=shutil.which, run=subprocess.run):
    """``{'engine', 'available', 'detail', 'voices'}``; voices is None when the engine cannot list them."""
    vcfg = cfg.get('voice') or {}
    engine = engine_name(cfg)
    if not vcfg.get('enabled', True):return {'engine': engine, 'available': False, 'detail': '配音已关闭', 'voices': None}
    if engine == 'say':
        voices = installed_voices(which, run)
        if voices is None:return {'engine': 'say', 'available': False, 'detail': '找不到 say 命令（仅 macOS 自带）', 'voices': None}
        return {'engine': 'say', 'available': True, 'detail': 'macOS say，已安装 %d 个语音' % len(voices), 'voices': voices}
    template = vcfg.get('command') or []
    program = template[0] if isinstance(template, list) and template and isinstance(template[0], str) else ''
    if not program:return {'engine': 'command', 'available': False, 'detail': 'voice.command 未设置', 'voices': None}
    found = which(program) or (Path(program).is_file() and program)
    return {'engine': 'command', 'available': bool(found), 'detail': ('自定义命令：' + program) if found else '找不到配音程序：' + program, 'voices': None}


def _base(name):
    return name.split(' (')[0].strip().lower().replace('-', '').replace(' ', '')


def voice_installed(name, installed):
    """Lenient match: macOS lists "Tingting", "Ting-Ting" or "Tingting (中文（中国大陆）)" depending on version."""
    if installed is None:return None
    return name in installed or _base(name) in {_base(item) for item in installed}


def voices_for(cfg, characters, speakers):
    """Voices one storyboard will ask for: every speaker's cast voice plus the default slot."""
    wanted = {cast('', cfg, characters)[0]}
    for speaker in speakers:wanted.add(cast(speaker, cfg, characters)[0])
    return sorted(wanted)
