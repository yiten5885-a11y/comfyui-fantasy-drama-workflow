#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""精品短剧渲染器：分镜 CSV + 关键帧 -> 电影感成片 MP4

相比 render_drama.py（基础版）的升级：
  画面：1280x720、8 种运镜 + 缓动 + 手持微抖、S 曲线调色 / 暗角 / 动态胶片颗粒
        交叉溶解转场、片头标题卡淡入、片尾落幕
  字幕：底部渐变遮罩 + 描边白字、按配音时长精确出现、逐句淡入淡出
  声音：角色分声配音、12 类合成环境音效、三层次配乐（和弦垫/低音/琶音）、
        人声闪避、整体淡入淡出与软限幅
  交付：额外产出封面图 cover.png

只依赖 ComfyUI venv 里的 PyAV / Pillow / numpy 与 macOS 自带 say。

    ./venv/bin/python drama/render_cinema.py \
        --csv ./input/drama/example-fantasy-story/shots.csv \
        --images ./output/drama/example-fantasy-story \
        --out ./output/example-fantasy-story/cinema.mp4 \
        --title example-fantasy-story --logline "..."
"""

import argparse
import csv
import glob
import json
import math
import os
import subprocess
import sys
import time
from fractions import Fraction

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

ROOT = os.path.dirname(os.path.abspath(__file__))
if __package__:
    from . import voice as voicekit
else:
    sys.path.insert(0, ROOT)
    import voice as voicekit
DEFAULT_CONFIG = os.path.join(ROOT, "drama_config.json")
AAC_FRAME = 1024
_FONT_CACHE = {}

# ---------------------------------------------------------------- 默认参数
DEFAULTS = {
    "cinema": {
        "width": 1280, "height": 720, "fps": 24, "crf": 18, "preset": "medium",
        "intro_sec": 2.6, "outro_sec": 2.2,
        "outro_text": "完",
        "transition_frames": 14,
        "min_shot_sec": 2.0, "voice_tail_sec": 0.9,
        "zoom": 0.11, "pan": 0.075, "handheld_px": 2.4,
        "contrast": 1.07, "saturation": 0.95,
        "vignette": 0.30, "grain": 2.6, "letterbox": 0,
        "subtitle_fade_sec": 0.28,
        "sfx_gain": 0.26,
        "cover": True,
        "voice_map": {
            "_default": "Tingting",
            "_male": "Reed",
            "_elder": "Grandpa",
        },
    }
}

TINTS = {
    "cool": (-0.055, 0.004, 0.055),
    "warm": (0.055, 0.004, -0.045),
    "golden": (0.075, 0.022, -0.065),
    "noir": (0.0, 0.0, 0.0),
    "neutral": (0.0, 0.0, 0.0),
}

# 角色选声、语音全名补全、配音引擎都在 voice.py，渲染器只负责音频后处理。
VOICE_FULL = voicekit.VOICE_FULL
SAFE_VOICE = voicekit.SAFE_VOICE
norm_voice = voicekit.norm_voice


# ---------------------------------------------------------------- 配置 / IO
def load_config(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    c = dict(DEFAULTS["cinema"])
    c.update(raw.get("cinema") or {})
    raw["cinema"] = c
    raw.setdefault("video", {})
    raw.setdefault("subtitle", {})
    raw.setdefault("voice", {})
    raw.setdefault("music", {})
    raw.setdefault("audio", {})
    return raw


def read_shots(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("shot_id")]


def find_image(images_dir, shot_id):
    hits = sorted(glob.glob(os.path.join(images_dir, shot_id + "_*.png")))
    if not hits:
        hits = sorted(glob.glob(os.path.join(images_dir, shot_id + "*")))
    return hits[0] if hits else None


def srt_time(sec):
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def write_srt(entries, path):
    lines = []
    for i, (start, end, text) in enumerate(entries, 1):
        lines += [str(i), "%s --> %s" % (srt_time(start), srt_time(end)), text, ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


BREAKS = "，。！？；：…,.!?;:"
STOPS = "。！？；…!?;"
TRAILING = "，。、；：,.;:"


def split_phrases(text, limit):
    """把一句台词切成适合逐段显示的短句；不足 limit 字的整句不切。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in BREAKS:
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    merged = []
    for part in parts:
        visible = len(part.strip(BREAKS + " "))
        if merged and (visible < 4 or len(merged[-1].strip(BREAKS)) < 4) and len(merged[-1]) + len(part) <= limit * 2:
            merged[-1] += part
        else:
            merged.append(part)
    return [part.strip() for part in merged if part.strip(BREAKS + " ")]


def _weight(phrase):
    """字数 + 标点停顿，用来按语速分配每段字幕的时长。"""
    visible = sum(1 for ch in phrase if ch not in BREAKS and not ch.isspace())
    pause = sum(0.9 if ch in STOPS else 0.45 for ch in phrase if ch in BREAKS)
    return max(1.0, visible + pause)


def subtitle_cues(text, start, end, limit=15):
    """[(开始秒, 结束秒, 文字)]：长台词按标点切段，时长按字数比例分给配音窗口。

    中文字幕习惯：段尾的逗号、句号省略，问号和感叹号保留。
    """
    phrases = split_phrases(text, limit)
    if not phrases or end <= start:
        return []
    weights = [_weight(p) for p in phrases]
    total = sum(weights)
    cues, at = [], start
    for phrase, weight in zip(phrases, weights):
        span = (end - start) * weight / total
        shown = phrase.rstrip(TRAILING) or phrase
        cues.append((at, at + span, shown))
        at += span
    return cues


def get_font(path, index, size):
    key = (path, index, size)
    if key not in _FONT_CACHE:
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, size, index=index)
        except Exception as e:
            print("  [警告] 字体 %s 加载失败：%s" % (path, e), file=sys.stderr)
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


# ---------------------------------------------------------------- 画面：调色
class GradeKit:
    """预计算暗角与颗粒，避免逐帧重复生成大数组。"""

    def __init__(self, W, H, c):
        self.W, self.H = W, H
        self.contrast = float(c.get("contrast", 1.07))
        self.saturation = float(c.get("saturation", 0.95))
        self.vignette = float(c.get("vignette", 0.30))
        self.grain = float(c.get("grain", 2.6))
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        cx, cy = W / 2.0, H / 2.0
        r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
        self.vig = np.clip(1.0 - self.vignette * np.clip(r - 0.35, 0, 1.2) ** 1.5,
                           0.4, 1.0)[..., None].astype(np.float32)
        rng = np.random.default_rng(20261108)
        self.noise = rng.standard_normal((H, W, 1)).astype(np.float32)

    def apply(self, frame, grade, step=0, tint_scale=1.0):
        a = np.asarray(frame, dtype=np.float32) / 255.0
        a = np.clip((a - 0.5) * self.contrast + 0.5, 0, 1)
        lum = a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722
        a = np.clip(lum[..., None] + (a - lum[..., None]) * self.saturation, 0, 1)
        tint = np.array(TINTS.get(grade, TINTS["neutral"]), np.float32) * tint_scale
        a = np.clip(a * (1.0 + tint), 0, 1)
        a = a * self.vig
        if self.grain > 0:
            g = np.roll(self.noise, (step * 7) % 97, axis=0)
            g = np.roll(g, (step * 13) % 89, axis=1)
            a = np.clip(a + g * (self.grain / 255.0), 0, 1)
        return Image.fromarray((a * 255).astype(np.uint8), "RGB")


def ease(t):
    """缓入缓出，避免匀速运镜的机械感。"""
    return t * t * (3 - 2 * t)


def camera_frame(base, k, n, motion, W, H, c, step):
    """8 种运镜：推、拉、左右摇、上下摇、手持、固定。"""
    t = ease(k / max(1, n - 1))
    zoom = float(c.get("zoom", 0.11))
    pan = float(c.get("pan", 0.075))
    jitter = float(c.get("handheld_px", 2.4))
    z, dx, dy = 1.0, 0.0, 0.0
    if motion == "push_in":
        z = 1.0 + zoom * t
    elif motion == "pull_out":
        z = 1.0 + zoom * (1 - t)
    elif motion == "pan_left":
        z, dx = 1.0 + zoom * 0.55, -pan * (t - 0.5)
    elif motion == "pan_right":
        z, dx = 1.0 + zoom * 0.55, pan * (t - 0.5)
    elif motion == "tilt_up":
        z, dy = 1.0 + zoom * 0.55, -pan * 0.7 * (t - 0.5)
    elif motion == "tilt_down":
        z, dy = 1.0 + zoom * 0.55, pan * 0.7 * (t - 0.5)
    elif motion == "handheld":
        z = 1.0 + zoom * 0.7 * t
        dx = 0.0035 * math.sin(step * 0.19) + 0.002 * math.sin(step * 0.07)
        dy = 0.0030 * math.cos(step * 0.23)
    elif motion == "lock":
        z = 1.0 + zoom * 0.25 * t

    src = base
    cw, ch = int(W / z), int(H / z)
    need = max(cw / src.width, ch / src.height, 1.0)
    if need > 1.0:
        src = src.resize((int(src.width * need) + 4, int(src.height * need) + 4),
                         Image.LANCZOS)
    x = (src.width - cw) / 2 + dx * src.width
    y = (src.height - ch) / 2 + dy * src.height
    x = max(0, min(src.width - cw, x))
    y = max(0, min(src.height - ch, y))
    if jitter > 0 and motion in ("handheld", "push_in", "pull_out"):
        x += jitter * math.sin(step * 0.31) * 0.35
        y += jitter * math.cos(step * 0.27) * 0.35
        x = max(0, min(src.width - cw, x))
        y = max(0, min(src.height - ch, y))
    box = (int(x), int(y), int(x) + cw, int(y) + ch)
    return src.crop(box).resize((W, H), Image.LANCZOS)


# ---------------------------------------------------------------- 画面：字幕 / 卡片
def wrap_text(text, limit):
    punct = "，。！？；、,.!?：:"
    lines, buf = [], ""
    for ch in text:
        buf += ch
        if len(buf) >= limit:
            cut = -1
            for i, ch2 in enumerate(buf[:-1]):
                if ch2 in punct:
                    cut = i + 1
            if cut > limit // 2:
                lines.append(buf[:cut])
                buf = buf[cut:]
            else:
                lines.append(buf)
                buf = ""
    if buf:
        lines.append(buf)
    while len(lines) > 1 and all(ch in punct + " 　" for ch in lines[-1]):
        lines[-2] += lines[-1]
        lines.pop()
    return lines or [""]


def draw_subtitle(frame, text, subcfg, W, H, alpha=1.0):
    """底部渐变遮罩 + 描边白字；alpha 控制逐句淡入淡出。"""
    if not text or alpha <= 0.01:
        return frame
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")
    band = int(H * 0.26)
    for row in range(band):
        a = int(150 * (row / band) ** 1.6 * alpha)
        d.line((0, H - band + row, W, H - band + row), fill=(0, 0, 0, a))

    fpath = subcfg.get("font", "/System/Library/Fonts/STHeiti Medium.ttc")
    fidx = int(subcfg.get("font_index", 0))
    size = int(subcfg.get("size", 46))
    font = get_font(fpath, fidx, size)
    sw = int(subcfg.get("stroke_width", 3))
    lines = wrap_text(text, int(subcfg.get("max_chars_per_line", 22)))
    boxes = [d.textbbox((0, 0), ln, font=font, stroke_width=sw) for ln in lines]
    line_h = (boxes[0][3] - boxes[0][1]) + int(subcfg.get("line_gap", 12))
    text_h = line_h * len(lines)
    base_y = H - int(subcfg.get("bottom_margin", 66)) - text_h
    A = int(255 * alpha)
    for i, ln in enumerate(lines):
        b = boxes[i]
        lw = b[2] - b[0]
        d.text(((W - lw) // 2, base_y + i * line_h - b[1]), ln, font=font,
               fill=(255, 255, 255, A), stroke_width=sw,
               stroke_fill=(0, 0, 0, A))
    return Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")


def draw_centered_lines(draw, text, font, width, y, fill):
    lines, line = [], ""
    for char in text:
        if line and draw.textlength(line + char, font=font) > width - 64:
            lines.append(line)
            line = ""
        line += char
    if line:
        lines.append(line)
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font)
        draw.text(((width - (box[2] - box[0])) // 2, y), line, font=font, fill=fill)
        y += font.size + 10
    return y


def make_title_card(title, logline, W, H, subcfg, outro=False, text=""):
    """片头 / 片尾卡：暗色渐变 + 细金线 + 排版。"""
    img = Image.new("RGB", (W, H), (11, 14, 20))
    d = ImageDraw.Draw(img, "RGBA")
    for y in range(H):
        f = y / H
        d.line((0, y, W, y), fill=(int(11 + 14 * f), int(14 + 16 * f),
                                   int(20 + 20 * f), 255))
    fpath = subcfg.get("font", "/System/Library/Fonts/STHeiti Medium.ttc")
    fidx = int(subcfg.get("font_index", 0))
    if outro:
        f = get_font(fpath, fidx, 56)
        b = d.textbbox((0, 0), text, font=f)
        d.text(((W - (b[2] - b[0])) // 2, H // 2 - 46), text, font=f,
               fill=(232, 226, 213, 255))
        for off in (-62, 62):
            d.line((W // 2 + off - 22, H // 2 + 46, W // 2 + off + 22, H // 2 + 46),
                   fill=(174, 145, 98, 150), width=1)
    else:
        big = get_font(fpath, fidx, 78)
        small = get_font(fpath, fidx, 26)
        title_end = draw_centered_lines(d, title, big, W, H // 2 - 108, (246, 243, 236, 255))
        line_y = max(H // 2 - 4, title_end + 12)
        d.line((W // 2 - 150, line_y, W // 2 + 150, line_y),
               fill=(186, 154, 100, 220), width=2)
        if logline:
            draw_centered_lines(d, logline[:44], small, W, line_y + 38, (172, 176, 188, 255))
        tag = "AI 短片 · 本地生成"
        tb = d.textbbox((0, 0), tag, font=small)
        d.text(((W - (tb[2] - tb[0])) // 2, H - 92), tag, font=small,
               fill=(120, 126, 138, 180))
    return img


def make_cover(base, title, logline, W, H, c, subcfg, grade, out_path):
    """封面：关键帧调色 + 底部压暗 + 剧名排版。"""
    kit = GradeKit(W, H, c)
    img = base.convert("RGB").resize((W, H), Image.LANCZOS)
    img = kit.apply(img, grade, step=0)
    # 用 numpy 一次性生成底部压暗渐变，避免逐行绘制带来的透明度歧义
    yy = np.arange(H, dtype=np.float32)[:, None]
    alpha = np.clip(210 * (np.clip((yy - H / 2) / (H / 2), 0, 1) ** 1.3),
                    0, 210).astype(np.uint8)
    arr = np.zeros((H, W, 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = 6, 9, 15
    arr[..., 3] = np.broadcast_to(alpha, (H, W))
    over = Image.fromarray(arr, "RGBA")
    img = Image.alpha_composite(img.convert("RGBA"), over)
    # 必须重新绑定绘制对象：文字要画在合成后的 img 上，而不是 over 图层上
    d = ImageDraw.Draw(img, "RGBA")
    fpath = subcfg.get("font", "/System/Library/Fonts/STHeiti Medium.ttc")
    fidx = int(subcfg.get("font_index", 0))
    big = get_font(fpath, fidx, 84)
    small = get_font(fpath, fidx, 27)
    title_end = draw_centered_lines(d, title, big, W, H // 2 + 40, (248, 245, 238, 255))
    line_y = max(H // 2 + 152, title_end + 12)
    d.line((W // 2 - 120, line_y, W // 2 + 120, line_y),
           fill=(190, 158, 104, 230), width=2)
    if logline:
        draw_centered_lines(d, logline[:36], small, W, line_y + 26, (206, 208, 214, 235))
    img.convert("RGB").save(out_path)
    return out_path


# ---------------------------------------------------------------- 音频：解码 / 配音
def decode_audio(path, sr):
    container = av.open(path)
    rs = av.AudioResampler(format="fltp", layout="stereo", rate=sr)
    chunks = []
    for frame in container.decode(audio=0):
        out = rs.resample(frame)
        for f in (out if isinstance(out, list) else [out]):
            if f is not None:
                chunks.append(f.to_ndarray())
    container.close()
    data = np.concatenate(chunks, axis=1) if chunks else np.zeros((2, 0), np.float32)
    return np.ascontiguousarray(data.astype(np.float32))


def write_wav(path, audio, sr):
    """(2, n) 或 (n,) 浮点音频写成 16 位 WAV，供页面试听。"""
    import wave
    data = np.atleast_2d(np.asarray(audio, dtype=np.float32))
    if data.shape[0] == 1:
        data = np.vstack([data, data])
    pcm = (np.clip(data[:2].T, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(int(sr))
        handle.writeframes(pcm.tobytes())
    return path


def pick_voice(speaker, cfg, characters):
    return voicekit.cast(speaker, cfg, characters)[0]


def _db(value):
    return 10 ** (value / 20.0)


def polish_voice(audio, sr, vcfg):
    """去掉首尾静音、统一响度、两端做短淡入淡出，避免爆音和字幕对不上口。"""
    if audio.size == 0:
        return audio
    mono = np.abs(audio).max(axis=0)
    hop = max(1, int(0.01 * sr))
    frames = mono[:mono.size // hop * hop].reshape(-1, hop).max(axis=1) if mono.size >= hop else mono
    floor = float(np.max(frames)) * _db(float(vcfg.get("trim_db", -42)))
    loud = np.nonzero(frames > floor)[0]
    if vcfg.get("trim_silence", True) and loud.size:
        pad = int(float(vcfg.get("trim_pad_sec", 0.04)) * sr)
        a = max(0, loud[0] * hop - pad)
        b = min(audio.shape[1], (loud[-1] + 1) * hop + pad)
        audio = audio[:, a:b]
    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
    if vcfg.get("normalize", True) and rms > 1e-5:
        gain = _db(float(vcfg.get("target_rms_db", -20))) / rms
        gain = min(gain, _db(float(vcfg.get("peak_db", -1.5))) / max(float(np.abs(audio).max()), 1e-6))
        audio = audio * float(np.clip(gain, 0.2, 8.0))
    edge = min(int(0.008 * sr), audio.shape[1] // 2)
    if edge:
        ramp = np.linspace(0, 1, edge, dtype=np.float32)
        audio = audio.copy()
        audio[:, :edge] *= ramp
        audio[:, -edge:] *= ramp[::-1]
    return np.ascontiguousarray(audio.astype(np.float32))


def make_voice(shots, out_dir, cfg, sr, characters):
    """逐句配音：[(序号, 文件, 音频, 时长, 音色)]；没有台词或关闭配音时音频为 None。"""
    vcfg = cfg["voice"]
    vdir = os.path.join(out_dir, "voice")
    os.makedirs(vdir, exist_ok=True)
    engine = voicekit.engine_name(cfg)
    suffix = voicekit.output_suffix(cfg)
    made = []
    for i, s in enumerate(shots, 1):
        text = (s.get("dialogue") or "").strip()
        if not text or not vcfg.get("enabled", True):
            made.append((i, None, None, 0.0, ""))
            continue
        speaker = (s.get("speaker") or "").strip()
        voice, rate, _source = voicekit.cast(speaker, cfg, characters)
        slug = "".join(ch for ch in voice.split("(")[0].strip() if ch.isalnum() or ch in "-_") or "voice"
        dst = os.path.join(vdir, "line%02d_%s%s" % (i, slug, suffix))
        try:
            def synth(v):
                voicekit.synthesize(cfg, v, rate, text, dst)
                return polish_voice(decode_audio(dst, sr), sr, vcfg)

            audio = synth(voice)
            dur = audio.shape[1] / sr
            # 某些语音包未下载/短名失效时会返回 0.01 秒静音，必须兜底重合成
            if engine == "say" and (dur < 0.35 or float(np.abs(audio).max()) < 1e-4) and voice != SAFE_VOICE:
                print("  [提示] %s 返回空音频（%.2fs），自动回退 %s" % (voice, dur, SAFE_VOICE), flush=True)
                voice = SAFE_VOICE
                audio = synth(voice)
                dur = audio.shape[1] / sr
            if dur < 0.2 or float(np.abs(audio).max()) < 1e-4:
                raise RuntimeError("配音为空，请确认语音「%s」已安装" % voice)
            made.append((i, dst, audio, dur, voice))
            print("  [配音] %02d  %-9s %.1fs  %s" % (i, slug, dur, text[:24]), flush=True)
        except Exception as e:
            raise RuntimeError("配音失败（%s）：%s" % (text[:16], e)) from e
    return made


def plan_timeline(shots, voices, cfg, image_for):
    """分镜时长与配音对齐后的时间轴。

    返回 (timeline, total)。timeline 每项为
    (分镜, 起点秒, 时长, 关键帧, 运镜, 台词, 配音起点, 配音终点)，后两者相对镜头起点。
    镜头至少容纳整句配音；字幕窗口与修剪过静音的配音严格对齐。
    """
    c = cfg["cinema"]
    lead = float(cfg["voice"].get("lead_in_sec", 0.2))
    tail = float(c.get("voice_tail_sec", 0.5))
    cursor = float(c.get("intro_sec", 0.5))
    timeline = []
    for shot, line in zip(shots, voices):
        voice_dur = float(line[3] or 0.0)
        try:
            planned = float(shot.get("duration_sec") or 0)
        except (TypeError, ValueError):
            planned = 0.0
        dur = max(planned, float(c.get("min_shot_sec", 2.0)), voice_dur + lead + tail if voice_dur else 0.0)
        dialogue = (shot.get("dialogue") or "").strip()
        vstart, vend = (lead, min(dur, lead + voice_dur + 0.25)) if voice_dur else (0.0, dur)
        timeline.append((shot, cursor, dur, image_for(shot), (shot.get("motion") or "lock").strip(),
                         dialogue, vstart, vend))
        cursor += dur
    return timeline, cursor + float(c.get("outro_sec", 0.35))


def timeline_cues(timeline, subcfg):
    limit = int(subcfg.get("max_chars_per_line", 15))
    cues = []
    for _shot, at, _dur, _img, _motion, text, vstart, vend in timeline:
        cues += [(at + a, at + b, t) for a, b, t in subtitle_cues(text, vstart, vend, limit)]
    return cues


# ---------------------------------------------------------------- 音频：环境音效
def _env(n, sr, speed=0.004, seed=1):
    rng = np.random.default_rng(seed)
    raw = rng.standard_normal(n + 64).astype(np.float32)
    k = max(2, int(speed * sr))
    pad = np.pad(raw, (k, 0))
    csum = np.cumsum(pad)
    return (csum[k:k + n] - csum[:n]) / k


def synth_sfx(kind, n, sr, seed=7):
    """12 类程序合成环境音，无版权依赖、无外部素材。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.float32) / sr
    white = rng.standard_normal(n).astype(np.float32)
    if kind == "rain":
        low = _env(n, sr, 0.0015, seed)
        drops = (rng.random(n) > 0.9992).astype(np.float32)
        d = np.convolve(drops, np.ones(180, np.float32) / 180)[:n]
        x = 0.26 * low + 0.05 * white + 0.42 * d
    elif kind == "wind":
        x = 0.30 * _env(n, sr, 0.0006, seed) * (
            0.6 + 0.4 * np.sin(2 * np.pi * 0.13 * t).astype(np.float32))
    elif kind == "waves":
        env = (0.5 + 0.5 * np.sin(2 * np.pi * 0.11 * t)).astype(np.float32) ** 2
        x = (0.22 * _env(n, sr, 0.002, seed) + 0.03 * white) * env
    elif kind == "boat":
        rumble = (0.55 * np.sin(2 * np.pi * 54 * t)
                  + 0.25 * np.sin(2 * np.pi * 108 * t)).astype(np.float32)
        horn_t = max(0.0, float(n / sr) - 1.4)
        horn = np.zeros(n, np.float32)
        m = t < horn_t
        if m.any():
            horn[m] = 0.16 * np.sin(2 * np.pi * 165 * t[m]) * np.exp(-2.2 * t[m])
        x = 0.16 * rumble * (0.8 + 0.2 * np.sin(2 * np.pi * 0.7 * t)) + horn \
            + 0.06 * _env(n, sr, 0.003, seed)
    elif kind == "steps":
        x = np.zeros(n, np.float32)
        period = int(0.62 * sr)
        for st in range(0, n, period):
            L = min(int(0.10 * sr), n - st)
            x[st:st + L] += (rng.standard_normal(L) * np.linspace(1, 0.05, L)).astype(np.float32)
        x *= 0.32
    elif kind == "clock":
        x = np.zeros(n, np.float32)
        period = int(1.0 * sr)
        for st in range(0, n, period):
            L = min(int(0.045 * sr), n - st)
            x[st:st + L] += (np.sin(2 * np.pi * 1900 * t[st:st + L])
                             * np.exp(-90 * t[:L])).astype(np.float32)
        x *= 0.24
    elif kind == "engine":
        x = 0.18 * (np.sin(2 * np.pi * 62 * t) + 0.4 * np.sin(2 * np.pi * 124 * t)) \
            + 0.05 * _env(n, sr, 0.002, seed)
    elif kind == "city":
        x = 0.12 * _env(n, sr, 0.004, seed) * (
            0.7 + 0.3 * np.sin(2 * np.pi * 0.21 * t)) + 0.015 * white
    elif kind == "birds":
        x = np.zeros(n, np.float32)
        for st in range(int(0.3 * sr), n, int(1.35 * sr)):
            L = min(int(0.16 * sr), n - st)
            tt = t[:L]
            x[st:st + L] += (0.14 * np.sin(2 * np.pi * (2100 + 700 * tt) * tt)
                             * np.sin(np.pi * tt / max(tt[-1], 1e-6))).astype(np.float32)
    elif kind == "door":
        x = 0.28 * np.sin(2 * np.pi * 90 * t) * np.exp(-4.5 * t) \
            + 0.10 * _env(n, sr, 0.006, seed) * np.exp(-3.0 * t)
    elif kind == "paper":
        x = 0.20 * _env(n, sr, 0.012, seed) * (
            0.4 + 0.6 * (rng.random(n) > 0.5).astype(np.float32))
        x *= (0.5 + 0.5 * np.sin(2 * np.pi * 1.7 * t))
    elif kind == "energy":
        hum = (0.16 * np.sin(2 * np.pi * 110 * t) + 0.08 * np.sin(2 * np.pi * 165 * t)).astype(np.float32)
        hum *= (0.75 + 0.25 * np.sin(2 * np.pi * 0.9 * t)).astype(np.float32)
        x = hum + 0.05 * _env(n, sr, 0.0008, seed)
        for st in range(int(0.1 * sr), n, int(0.23 * sr)):
            L = min(int(0.12 * sr), n - st)
            fq = 3200 + 2600 * rng.random()
            x[st:st + L] += (0.05 * np.sin(2 * np.pi * fq * t[:L]) * np.exp(-28 * t[:L])).astype(np.float32)
    elif kind == "fire":
        x = 0.16 * _env(n, sr, 0.003, seed) * (0.8 + 0.2 * np.sin(2 * np.pi * 0.4 * t)).astype(np.float32)
        pops = np.nonzero(rng.random(n) > 0.99965)[0]
        for st in pops:
            L = min(int(0.012 * sr), n - st)
            x[st:st + L] += (rng.standard_normal(L) * np.exp(-400 * t[:L]) * 0.25).astype(np.float32)
    elif kind == "impact":
        sweep = 2 * np.pi * np.cumsum(45 + 75 * np.exp(-9 * t)) / sr
        boom = np.sin(sweep).astype(np.float32) * np.exp(-4.2 * t).astype(np.float32)
        crack = white * np.exp(-60 * t).astype(np.float32)
        x = 0.85 * boom + 0.35 * crack + 0.12 * _env(n, sr, 0.004, seed) * np.exp(-6 * t)
        return np.clip(x, -1, 1).astype(np.float32)
    elif kind == "sword":
        swoosh_len = min(n, int(0.32 * sr))
        bell = np.sin(np.pi * np.arange(swoosh_len, dtype=np.float32) / max(swoosh_len, 1)) ** 2
        hiss = (white[:swoosh_len] - _env(swoosh_len, sr, 0.0004, seed) * 4).astype(np.float32)
        x = np.zeros(n, np.float32)
        x[:swoosh_len] = 0.22 * hiss * bell
        ring_t = np.clip(t - 0.26, 0, None)
        ring = sum(a * np.sin(2 * np.pi * fq * ring_t) for a, fq in ((0.22, 2210), (0.14, 3170), (0.08, 4730)))
        x += (ring * np.exp(-3.2 * ring_t) * (t >= 0.26)).astype(np.float32)
        return np.clip(x, -1, 1).astype(np.float32)
    elif kind == "thunder":
        crack = white * np.exp(-22 * t).astype(np.float32)
        rumble = _env(n, sr, 0.006, seed) * 6 * np.exp(-1.1 * t).astype(np.float32)
        x = 0.4 * crack + 0.5 * np.clip(rumble, -1, 1)
        return np.clip(x, -1, 1).astype(np.float32)
    else:
        return np.zeros(n, np.float32)
    f = min(int(0.22 * sr), n // 2)
    if f:
        x[:f] *= np.linspace(0, 1, f, dtype=np.float32)
        x[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
    return np.clip(x, -1, 1).astype(np.float32)


AMBIENT_SFX = ("rain", "wind", "waves", "boat", "steps", "clock", "engine", "city",
               "birds", "paper", "energy", "fire")
ONESHOT_SFX = {"impact": 1.6, "sword": 1.4, "thunder": 3.2, "door": 1.2}
SFX_NAMES = AMBIENT_SFX + tuple(ONESHOT_SFX) + ("silence",)


def parse_sfx(value):
    """"wind|impact@1.2" -> [("wind", None), ("impact", 1.2)]；未知名称原样返回，由调用方提示。"""
    events = []
    for token in str(value or "").replace("，", "|").replace(",", "|").split("|"):
        token = token.strip().lower()
        if not token or token == "silence":
            continue
        name, _, at = token.partition("@")
        try:
            events.append((name.strip(), float(at) if at else None))
        except ValueError:
            events.append((name.strip(), None))
    return events


def shot_sfx(value, dur, sr, seed):
    """一个镜头的音效轨：环境声铺满整镜；一次性音效默认落在镜头 35% 处，可用 @秒 指定。"""
    n = int(dur * sr)
    track = np.zeros(n, np.float32)
    for k, (name, at) in enumerate(parse_sfx(value)):
        if name in ONESHOT_SFX:
            start = int(min(max(0.0, at if at is not None else dur * 0.35), max(0.0, dur - 0.05)) * sr)
            seg = synth_sfx(name, int(ONESHOT_SFX[name] * sr), sr, seed=seed + k)
            end = min(n, start + seg.size)
            track[start:end] += seg[:end - start]
        elif name in AMBIENT_SFX:
            track += synth_sfx(name, n, sr, seed=seed + k)
    return track


# ---------------------------------------------------------------- 音频：配乐
CHORD_SETS = {
    "cool": [[220.0, 261.63, 329.63], [174.61, 220.0, 261.63],
             [196.0, 246.94, 293.66], [164.81, 196.0, 246.94]],
    "warm": [[261.63, 329.63, 392.0], [220.0, 261.63, 329.63],
             [174.61, 220.0, 261.63], [196.0, 246.94, 392.0]],
    "golden": [[293.66, 369.99, 440.0], [261.63, 329.63, 392.0],
               [220.0, 277.18, 329.63], [246.94, 311.13, 369.99]],
    "noir": [[174.61, 207.65, 261.63], [155.56, 185.0, 233.08],
             [146.83, 174.61, 220.0], [164.81, 196.0, 246.94]],
    "neutral": [[220.0, 261.63, 329.63], [174.61, 220.0, 261.63],
                [196.0, 246.94, 293.66], [174.61, 220.0, 261.63]],
}


# 五声音阶（宫商角徵羽）。各色调用不同调式：冷色偏羽调，暖色偏宫调。
PENTATONIC = {
    "neutral": (146.83, [0, 2, 4, 7, 9]),   # D 宫
    "warm": (146.83, [0, 2, 4, 7, 9]),
    "golden": (164.81, [0, 2, 4, 7, 9]),    # E 宫
    "cool": (123.47, [0, 3, 5, 7, 10]),     # B 羽
    "noir": (110.0, [0, 3, 5, 7, 10]),      # A 羽
}


def _pluck(fq, L, sr, decay=3.2):
    """拨弦音色：带轻微非谐泛音和起音滑音，近似古筝。"""
    tt = np.arange(L, dtype=np.float32) / sr
    bend = 1.0 + 0.012 * np.exp(-30 * tt)
    phase = 2 * np.pi * fq * np.cumsum(bend) / sr
    tone = np.zeros(L, np.float32)
    for k, (amp, ratio) in enumerate(((1.0, 1.0), (0.45, 2.0), (0.22, 3.01), (0.1, 4.03))):
        tone += (amp * np.sin(phase * ratio) * np.exp(-decay * (1 + 0.6 * k) * tt)).astype(np.float32)
    attack = min(L, int(0.004 * sr))
    if attack:
        tone[:attack] *= np.linspace(0, 1, attack, dtype=np.float32)
    return tone


def synth_guofeng(total_sec, sr, mcfg, grade):
    """国风配乐：五度持续低音 + 五声音阶拨弦旋律 + 空间延迟。"""
    b = mcfg.get("builtin") or {}
    root, steps = PENTATONIC.get(grade, PENTATONIC["neutral"])
    n = int(total_sec * sr) + sr
    t = np.arange(n, dtype=np.float32) / sr
    drone = (0.20 * np.sin(2 * np.pi * root / 2 * t) + 0.12 * np.sin(2 * np.pi * root * 0.75 * t)).astype(np.float32)
    drone *= (0.8 + 0.2 * np.sin(2 * np.pi * 0.07 * t)).astype(np.float32)
    melody = np.zeros(n, np.float32)
    rng = np.random.default_rng(int(b.get("seed", 8)))
    beat = float(b.get("beat_sec", 0.55))
    degree, at = 2, 0.6
    scale = [root * 2 ** (s / 12.0) for s in steps] + [root * 2 * 2 ** (s / 12.0) for s in steps]
    while at < total_sec - 0.6:
        degree = int(np.clip(degree + rng.choice([-2, -1, -1, 1, 1, 2]), 0, len(scale) - 1))
        start = int(at * sr)
        L = min(int(2.2 * sr), n - start)
        melody[start:start + L] += 0.22 * _pluck(scale[degree], L, sr)
        at += beat * rng.choice([1, 1, 1, 2, 2, 3])
    mono = drone + melody
    d1, d2 = int(0.17 * sr), int(0.29 * sr)
    wet = np.zeros(n, np.float32)
    wet[d1:] += 0.28 * mono[:-d1]
    wet[d2:] += 0.16 * mono[:-d2]
    left = mono + 0.6 * wet
    right = np.concatenate([np.zeros(int(0.011 * sr), np.float32), mono])[:n] + 0.8 * wet
    f = min(int(float(mcfg.get("fade_sec", 1.5)) * sr), n // 2)
    if f:
        ramp = np.linspace(0, 1, f, dtype=np.float32)
        for ch in (left, right):
            ch[:f] *= ramp
            ch[-f:] *= ramp[::-1]
    return np.vstack([left, right])


def synth_music(total_sec, sr, mcfg, grade):
    """内置配乐。builtin.style 为 guofeng 时用五声音阶拨弦，否则为氛围和弦垫。"""
    b = mcfg.get("builtin") or {}
    if b.get("style") == "guofeng":
        return synth_guofeng(total_sec, sr, mcfg, grade)
    chords = CHORD_SETS.get(grade, CHORD_SETS["neutral"])
    n = int(total_sec * sr) + sr
    pad = np.zeros(n, np.float32)
    bass = np.zeros(n, np.float32)
    arp = np.zeros(n, np.float32)
    step = int(float(b.get("bar_sec", 4.0)) * sr)
    fade = int(0.22 * sr)
    for k, start in enumerate(range(0, n, step)):
        L = min(step, n - start)
        t = np.arange(L, dtype=np.float32) / sr
        chord = chords[k % len(chords)]
        seg = np.zeros(L, np.float32)
        for j, fq in enumerate(chord):
            seg += (0.50 / (j + 1)) * np.sin(
                2 * np.pi * fq * t + j * 0.7).astype(np.float32)
            seg += (0.10 / (j + 1)) * np.sin(
                2 * np.pi * fq * 2 * t + j * 1.1).astype(np.float32)
        seg *= (0.85 + 0.15 * np.sin(2 * np.pi * 0.19 * t)).astype(np.float32)
        f = min(fade, L // 2)
        if f:
            seg[:f] *= np.linspace(0, 1, f, dtype=np.float32)
            seg[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
        pad[start:start + L] += seg
        low = chord[0] / 2
        bseg = 0.30 * np.sin(2 * np.pi * low * t).astype(np.float32)
        bseg *= np.exp(-1.3 * t / max(L / sr, 1e-6)).astype(np.float32)
        bass[start:start + L] += bseg
        # 稀疏琶音：每小节 6 个音，带衰减
        note_len = int(0.42 * sr)
        for q in range(6):
            st = start + int((0.45 + q * 0.62) * sr)
            if st + note_len > n:
                break
            fq = chord[q % len(chord)] * 2
            tt = np.arange(note_len, dtype=np.float32) / sr
            env = np.exp(-5.0 * tt).astype(np.float32)
            arp[st:st + note_len] += (0.16 * env
                                      * np.sin(2 * np.pi * fq * tt)).astype(np.float32)
    mono = pad + bass + arp
    # 简易空间感：两个短延迟 + 轻微立体声展开
    d1, d2 = int(0.11 * sr), int(0.19 * sr)
    wet = np.zeros(n, np.float32)
    wet[d1:] += 0.22 * mono[:-d1]
    wet[d2:] += 0.12 * mono[:-d2]
    left = mono + wet * 0.7
    right = np.concatenate([np.zeros(int(0.013 * sr), np.float32), mono])[:n] + wet * 0.9
    f = min(int(float(mcfg.get("fade_sec", 1.5)) * sr), n // 2)
    if f:
        left[:f] *= np.linspace(0, 1, f, dtype=np.float32)
        right[:f] *= np.linspace(0, 1, f, dtype=np.float32)
        left[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
        right[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
    return np.vstack([left, right])


def load_music(total_sec, sr, mcfg, grade):
    path = (mcfg.get("file") or "").strip()
    if mcfg.get("source") == "file" and path and os.path.exists(path):
        raw = decode_audio(path, sr)
        n = int(total_sec * sr)
        if raw.shape[1] >= n:
            return raw[:, :n] * float(mcfg.get("gain", 0.16))
        return np.tile(raw, (1, int(np.ceil(n / max(1, raw.shape[1])))))[:, :n] \
            * float(mcfg.get("gain", 0.16))
    if path and not os.path.exists(path):
        print("  [提示] 插曲文件不存在，改用内置配乐：%s" % path, file=sys.stderr)
    return synth_music(total_sec, sr, mcfg, grade) * float(mcfg.get("gain", 0.16))


def duck_envelope(mask, sr, depth, attack=0.06, release=0.45, lookahead=0.18, rate=200):
    """闪避增益：人声开始前 lookahead 秒提前压低，平滑起落，避免抽吸感。"""
    n = mask.size
    hop = max(1, sr // rate)
    blocks = int(np.ceil(n / hop))
    padded = np.zeros(blocks * hop, np.float32)
    padded[:n] = mask
    ctrl = padded.reshape(blocks, hop).max(axis=1)
    ahead = int(lookahead * rate)
    if ahead:
        ctrl = np.maximum(ctrl, np.concatenate([ctrl[ahead:], np.zeros(ahead, np.float32)]))
    a = 1.0 - np.exp(-1.0 / max(1.0, attack * rate))
    r = 1.0 - np.exp(-1.0 / max(1.0, release * rate))
    env = np.empty(blocks, np.float32)
    level = 0.0
    for i, target in enumerate(ctrl):
        level += (target - level) * (a if target > level else r)
        env[i] = level
    smooth = np.interp(np.arange(n, dtype=np.float32) / hop, np.arange(blocks, dtype=np.float32), env).astype(np.float32)
    return 1.0 - (1.0 - depth) * np.clip(smooth, 0, 1)


def build_audio(voices, timeline, total_sec, sr, cfg, grade):
    n = int(total_sec * sr)
    voice_track = np.zeros((2, n + sr), np.float32)
    sfx_track = np.zeros(n + sr, np.float32)
    mask = np.zeros(n + sr, np.float32)
    vcfg, mcfg, c = cfg["voice"], cfg["music"], cfg["cinema"]

    for (_idx, _p, audio, _dur, _voice), tl in zip(voices, timeline):
        _shot, start, _dur, _img, _motion, _text, vstart, _vend = tl
        if audio is None:
            continue
        at = int((start + vstart) * sr)
        L = min(audio.shape[1], max(0, voice_track.shape[1] - at))
        if L > 0:
            voice_track[:, at:at + L] += audio[:, :L] * float(vcfg.get("gain", 1.0))
            mask[at:at + L] = 1.0

    if c.get("sfx_enabled", True):
        for i, (shot, start, dur, *_rest) in enumerate(timeline, 1):
            seg = shot_sfx(shot.get("sfx"), dur, sr, seed=100 + i * 10)
            at = int(start * sr)
            end = min(sfx_track.size, at + seg.size)
            sfx_track[at:end] += seg[:end - at] * float(c.get("sfx_gain", 0.26))
        # 台词出现时音效也让开一些，人声更清楚
        sfx_track[:n] *= duck_envelope(mask[:n], sr, float(c.get("sfx_duck_under_voice", 0.7)))

    voice_track = voice_track[:, :n]
    sfx2 = np.vstack([sfx_track[:n] * 0.94,
                      np.concatenate([np.zeros(int(0.009 * sr), np.float32),
                                      sfx_track])[:n] * 0.94])
    mix = voice_track + sfx2

    if mcfg.get("enabled", True):
        bgm = load_music(total_sec, sr, mcfg, grade)
        bgm = bgm[:, :n] if bgm.shape[1] >= n else np.pad(
            bgm, ((0, 0), (0, n - bgm.shape[1])))
        bgm = bgm * duck_envelope(mask[:n], sr, float(mcfg.get("duck_under_voice", 0.42)),
                                  attack=float(mcfg.get("duck_attack_sec", 0.06)),
                                  release=float(mcfg.get("duck_release_sec", 0.45)))
        mix = mix + bgm

    # 软限幅（tanh）比硬归一化更不容易把人声压扁
    ceiling = float(cfg["audio"].get("normalize_ceiling", 0.92))
    peak = float(np.abs(mix).max())
    if peak > 1e-6:
        mix = np.tanh(mix * (1.0 / max(peak, 1.0)) * 1.15) / np.tanh(1.15)
        mix *= ceiling / max(float(np.abs(mix).max()), 1e-6)
    f = min(int(0.5 * sr), n // 2)
    if f:
        mix[:, :f] *= np.linspace(0, 1, f, dtype=np.float32)
        mix[:, -f:] *= np.linspace(1, 0, f, dtype=np.float32)
    return np.ascontiguousarray(mix.astype(np.float32))


# ---------------------------------------------------------------- 画面：动态镜头与转场
TRANSITIONS = ("dip", "dissolve", "cut", "flash")


def shot_grade_of(shot, default):
    grade = (shot.get("grade") or "").strip().lower()
    return grade if grade in TINTS else default


def shot_transition_of(shot, c):
    kind = (shot.get("transition") or c.get("transition") or "dip").strip().lower()
    return kind if kind in TRANSITIONS else "dip"


def load_clip(path):
    with av.open(path) as clip:
        stream = clip.streams.video[0]
        rate = float(stream.average_rate or stream.guessed_rate or 24)
        frames = [vf.to_image() for vf in clip.decode(video=0)]
    return frames, rate


def clip_positions(count, clip_fps, n, fps, max_slowdown=1.5):
    """每个输出帧对应的源帧位置（可为小数）。

    片源长于镜头：均匀加速，完整保留动作。片源短于镜头：按需放慢，但最多放慢
    max_slowdown 倍，剩余时间由调用方用最后一帧缓推补足；不再把 17 帧硬拉成整镜。
    """
    if count < 2 or n < 1:
        return [0.0] * n
    clip_sec, shot_sec = count / clip_fps, n / fps
    if clip_sec >= shot_sec:
        return [k * (count - 1) / max(1, n - 1) for k in range(n)]
    speed = max(clip_sec / shot_sec, 1.0 / max(1.0, max_slowdown))
    step = speed * clip_fps / fps
    played = min(n, int(math.floor((count - 1) / step)) + 1)
    return [min(count - 1.0, k * step) for k in range(played)]


def clip_frame_at(frames, position):
    """小数位置在相邻两帧之间做线性混合，慢放时画面连续，不再一帧重复多次。"""
    i = int(position)
    frac = position - i
    if frac < 0.02 or i + 1 >= len(frames):
        return frames[min(i, len(frames) - 1)]
    if frac > 0.98:
        return frames[i + 1]
    return Image.blend(frames[i], frames[i + 1], frac)


def fit_frame(frame, W, H, sharpen=0.6):
    """居中裁切放大到交付尺寸；明显放大时加轻度 USM 锐化，补回放大损失的边缘。"""
    scale = max(W / frame.width, H / frame.height)
    out = ImageOps.fit(frame, (W, H), method=Image.LANCZOS)
    if sharpen > 0 and scale > 1.15:
        out = out.filter(ImageFilter.UnsharpMask(radius=1.6, percent=int(100 * sharpen), threshold=2))
    return out


def apply_transition(kind, previous, frame, a, black, white, first=False):
    """a 从 0 到 1。dip 暗场溶解；dissolve 直接溶解；cut 硬切；flash 白闪，适合打击镜头。"""
    if previous is None:
        return Image.blend(black, frame, a) if first else frame
    if kind == "cut":
        return frame
    if kind == "flash":
        return Image.blend(white, frame, a ** 0.7)
    mixed = Image.blend(previous, frame, a)
    if kind == "dissolve":
        return mixed
    # 暗场溶解：在过渡中点把画面压暗，避免构图差异大的两帧叠成「鬼影」
    return Image.blend(black, mixed, max(0.05, 1.0 - 0.85 * math.sin(math.pi * a)))


# ---------------------------------------------------------------- 编码
def encode(out_path, timeline, intro_frames, outro_frames, intro_img, outro_img,
           audio, cfg, grade):
    c = cfg["cinema"]
    sr = int(cfg["audio"].get("sample_rate", 44100))
    fps, W, H = int(c["fps"]), int(c["width"]), int(c["height"])
    subcfg = cfg["subtitle"]
    sub_on = bool(subcfg.get("burn_in", True))
    tb = Fraction(1, fps)
    kit = GradeKit(W, H, c)
    fade_sub = float(c.get("subtitle_fade_sec", 0.28))
    trans_n = int(c.get("transition_frames", 14))
    letterbox = int(c.get("letterbox", 0))

    container = av.open(out_path, mode="w")
    vstream = container.add_stream("libx264", rate=fps)
    vstream.width, vstream.height = W, H
    vstream.pix_fmt = "yuv420p"
    vstream.options = {"crf": str(c.get("crf", 18)),
                       "preset": c.get("preset", "medium")}
    astream = container.add_stream(cfg["audio"].get("codec", "aac"), rate=sr)
    astream.layout = "stereo"
    try:
        astream.bit_rate = int(cfg["audio"].get("bit_rate", 160000))
    except Exception:
        pass

    audio_cursor, sample_pts, frame_idx, step = 0, 0, 0, 0
    n_samples = audio.shape[1]
    black = Image.new("RGB", (W, H), (0, 0, 0))
    last_frame = None

    def push_audio(target):
        nonlocal audio_cursor, sample_pts
        while audio_cursor + AAC_FRAME <= target:
            chunk = np.ascontiguousarray(audio[:, audio_cursor:audio_cursor + AAC_FRAME])
            af = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="stereo")
            af.sample_rate = sr
            af.pts = sample_pts
            af.time_base = Fraction(1, sr)
            for pkt in astream.encode(af):
                container.mux(pkt)
            audio_cursor += AAC_FRAME
            sample_pts += AAC_FRAME

    def push_video(frame, alpha_from_black=1.0):
        nonlocal frame_idx, step
        if alpha_from_black < 1.0:
            frame = Image.blend(black, frame, max(0.0, min(1.0, alpha_from_black)))
        vf = av.VideoFrame.from_ndarray(np.asarray(frame), format="rgb24")
        vf.pts = frame_idx
        vf.time_base = tb
        for pkt in vstream.encode(vf):
            container.mux(pkt)
        frame_idx += 1
        step += 1
        push_audio(int(frame_idx * sr / fps))

    # 片头：淡入 -> 保持 -> 淡出到第一镜（由第一镜的转场接手）
    if intro_frames:
        for k in range(intro_frames):
            a = min(1.0, k / max(1, fps * 0.7),
                    (intro_frames - k) / max(1, fps * 0.55))
            push_video(intro_img, a)
        # 片头自己淡出到黑，第一镜从黑淡入；不能用标题卡做溶解，
        # 否则剧名/logline 会以半透明残影压在正片上，像水印。
        last_frame = None

    white = Image.new("RGB", (W, H), (255, 252, 240))
    limit = int(subcfg.get("max_chars_per_line", 15))
    for si, (shot, start, dur, img_path, motion, dialogue, vstart, vend) in enumerate(timeline):
        n = max(1, int(round(dur * fps)))
        base = Image.open(img_path).convert("RGB") if img_path else None
        shot_grade = shot_grade_of(shot, grade)
        transition = shot_transition_of(shot, c)
        clip_frames, positions, hold_base = [], [], None
        if cfg.get("_videos_dir"):
            clip_path = os.path.join(cfg["_videos_dir"], shot["shot_id"] + ".mp4")
            clip_frames, clip_fps = load_clip(clip_path)
            if len(clip_frames) < 2:
                raise RuntimeError("动态镜头缺帧：" + clip_path)
            positions = clip_positions(len(clip_frames), clip_fps, n, fps, float(c.get("max_clip_slowdown", 1.5)))
        cues = subtitle_cues(dialogue, vstart, vend, limit) if sub_on else []
        hold_cfg = dict(c, zoom=float(c.get("hold_push_in", 0.045)), handheld_px=0)
        clean = None
        for k in range(n):
            t_sec = k / fps
            if base is not None:
                if clip_frames and k < len(positions):
                    f = fit_frame(clip_frame_at(clip_frames, positions[k]), W, H, float(c.get("sharpen", 0.6)))
                    hold_base = f
                elif clip_frames:
                    # 片源比镜头短：停在最后一帧并缓慢推近，避免冻结画面
                    f = camera_frame(hold_base, k - len(positions), n - len(positions), "push_in", W, H, hold_cfg, step)
                else:
                    f = camera_frame(base, k, n, motion, W, H, c, step)
                f = kit.apply(f, shot_grade, step=step)
            else:
                f = Image.new("RGB", (W, H), (12, 15, 21))
            if letterbox > 0:
                dl = ImageDraw.Draw(f)
                dl.rectangle((0, 0, W, letterbox), fill=(0, 0, 0))
                dl.rectangle((0, H - letterbox, W, H), fill=(0, 0, 0))
            if k < trans_n:
                f = apply_transition(transition, last_frame, f, (k + 1) / trans_n, black, white, first=si == 0)
            clean = f
            # 字幕：长台词逐段显示；只在整句首尾淡入淡出，段与段之间直接切换
            for j, (a, b, text) in enumerate(cues):
                if a <= t_sec < b:
                    fade_in = 1.0 if j else max(0.0, (t_sec - a) / fade_sub)
                    fade_out = 1.0 if j < len(cues) - 1 else max(0.0, (b - t_sec) / fade_sub)
                    f = draw_subtitle(f, text, subcfg, W, H, min(1.0, fade_in, fade_out))
                    break
            push_video(f)
        # 转场只用不带字幕的画面，避免上一镜字幕以残影带进下一镜
        last_frame = clean if base is not None else None
        print("  [镜头] %s  %.1fs  %3d 帧  %-10s %s %s%s" %
              (shot["shot_id"], dur, n, motion, transition,
               "音效:" + (shot.get("sfx") or "—"),
               "  字幕 %d 段" % len(cues) if cues else ""), flush=True)

    # 片尾：从最后一帧溶解到落幕卡
    if outro_frames:
        for k in range(outro_frames):
            a = (k + 1) / outro_frames
            f = Image.blend(last_frame or black, outro_img, a ** 1.4)
            push_video(f)

    while audio_cursor < n_samples:
        chunk = np.zeros((2, AAC_FRAME), np.float32)
        tail = min(AAC_FRAME, n_samples - audio_cursor)
        if tail:
            chunk[:, :tail] = audio[:, audio_cursor:audio_cursor + tail]
        af = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="stereo")
        af.sample_rate = sr
        af.pts = sample_pts
        af.time_base = Fraction(1, sr)
        for pkt in astream.encode(af):
            container.mux(pkt)
        audio_cursor += AAC_FRAME
        sample_pts += AAC_FRAME
    for pkt in astream.encode():
        container.mux(pkt)
    for pkt in vstream.encode():
        container.mux(pkt)
    container.close()
    return frame_idx


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="精品短剧渲染：运镜/调色/转场/分声/音效/配乐")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--images", required=True)
    ap.add_argument("--videos", help="逐镜头动态 MP4 目录；指定后缺片会停止")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--title", default="")
    ap.add_argument("--logline", default="")
    ap.add_argument("--grade", default="")
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--no-music", action="store_true")
    ap.add_argument("--no-subtitle", action="store_true")
    ap.add_argument("--no-intro", action="store_true")
    ap.add_argument("--no-sfx", action="store_true")
    ap.add_argument("--no-cover", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.videos:
        cfg["_videos_dir"] = os.path.abspath(args.videos)
    c = cfg["cinema"]
    if args.no_voice:
        cfg["voice"]["enabled"] = False
    if args.no_music:
        cfg["music"]["enabled"] = False
    if args.no_subtitle:
        cfg["subtitle"]["burn_in"] = False
    if args.no_intro:
        c["intro_sec"] = 0
        c["outro_sec"] = 0
    if args.no_sfx:
        c["sfx_enabled"] = False
    fps, W, H = int(c["fps"]), int(c["width"]), int(c["height"])
    sr = int(cfg["audio"].get("sample_rate", 44100))
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)

    shots = read_shots(args.csv)
    if not shots:
        raise RuntimeError("分镜 CSV 为空：" + args.csv)
    grade = (args.grade or shots[0].get("grade") or "neutral").strip().lower()
    char_path = os.path.join(os.path.dirname(os.path.abspath(args.csv)),
                             "characters.json")
    characters = {}
    if os.path.exists(char_path):
        with open(char_path, encoding="utf-8-sig") as f:
            characters = (json.load(f).get("characters") or {})
    print("[载入] %d 镜头 ｜ %dx%d @%dfps ｜ 色调 %s" %
          (len(shots), W, H, fps, grade), flush=True)

    print("[音频] 角色分声配音 ...", flush=True)
    voices = make_voice(shots, out_dir, cfg, sr, characters)

    intro = float(c.get("intro_sec", 2.6))
    outro = float(c.get("outro_sec", 2.2))
    intro_frames, outro_frames = int(round(intro * fps)), int(round(outro * fps))
    timeline, total = plan_timeline(shots, voices, cfg, lambda s: find_image(args.images, s["shot_id"]))
    missing = [tl[0]["shot_id"] for tl in timeline if not tl[3]]
    print("[时间轴] %.1fs（片头 %.1fs / 片尾 %.1fs），缺图 %d" %
          (total, intro, outro, len(missing)), flush=True)
    if missing:
        raise RuntimeError("缺少关键帧：" + ", ".join(missing))

    subs = timeline_cues(timeline, cfg["subtitle"])
    srt = os.path.join(out_dir, "subtitles.srt")
    write_srt(subs, srt)
    print("[字幕] %d 条 -> %s%s" %
          (len(subs), srt, "（已烧进画面）" if cfg["subtitle"]["burn_in"] else ""),
          flush=True)

    audio = build_audio(voices, timeline, total, sr, cfg, grade)
    print("[混音] 配音 %s ｜ 环境音效 %s ｜ 配乐 %s" %
          ("开" if cfg["voice"]["enabled"] else "关",
           "开" if c.get("sfx_enabled", True) else "关",
           "开" if cfg["music"]["enabled"] else "关"), flush=True)

    subcfg = cfg["subtitle"]
    intro_img = make_title_card(args.title or "短剧", args.logline, W, H, subcfg) \
        if intro_frames else None
    outro_img = make_title_card("", "", W, H, subcfg, outro=True,
                                text=c.get("outro_text", "— 完 —")) \
        if outro_frames else None

    cover_path = ""
    if c.get("cover", True) and not args.no_cover:
        src = next((tl[3] for tl in timeline if tl[3]), None)
        if src:
            cover_path = os.path.join(out_dir, "cover.png")
            make_cover(Image.open(src), args.title or "短剧", args.logline,
                       W, H, c, subcfg, grade, cover_path)
            print("[封面] %s" % cover_path, flush=True)

    print("[编码] %s ..." % args.out, flush=True)
    t0 = time.time()
    nframes = encode(args.out, timeline, intro_frames, outro_frames,
                     intro_img, outro_img, audio, cfg, grade)
    check = av.open(args.out)
    kinds = [s.type for s in check.streams]
    check.close()
    size = os.path.getsize(args.out)
    print("\n[成片] %s" % args.out)
    print("  %d 帧 / %.1fs / %.2f MB / 流 %s / 耗时 %.0fs" %
          (nframes, nframes / fps, size / 1e6, "+".join(kinds), time.time() - t0))
    return 0 if "video" in kinds and "audio" in kinds else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("[失败] %s: %s" % (type(e).__name__, e), file=sys.stderr)
        raise SystemExit(1)
