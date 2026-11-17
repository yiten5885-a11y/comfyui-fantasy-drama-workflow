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
import re
import subprocess
import sys
import time
from fractions import Fraction

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

ROOT = os.path.dirname(os.path.abspath(__file__))
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
        "transition": "dip",
        "sharpen": 0.6,
        "max_clip_slowdown": 1.5,
        "hold_push_in": 0.045,
        "min_shot_sec": 2.0, "voice_tail_sec": 0.5,
        "zoom": 0.11, "pan": 0.075, "handheld_px": 2.4,
        "contrast": 1.07, "saturation": 0.95,
        "vignette": 0.30, "grain": 2.6, "letterbox": 0,
        "subtitle_fade_sec": 0.28,
        "sfx_gain": 0.26,
        "sfx_duck_under_voice": 0.7,
        "cover": True,
        "voice_map": {
            "_default": "Reed (中文（中国大陆）)",
            "_male": "Reed (中文（中国大陆）)",
            "_female": "Tingting (中文（中国大陆）)",
            "_elder": "Grandpa (中文（中国大陆）)",
            "_child": "Flo",
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

MALE_HINT = ("先生", "哥", "叔", "伯", "父", "爷", "陆屿", "男", "老板", "船老大")
ELDER_HINT = ("老", "爷", "婆", "奶", "船老大", "父", "母")

# macOS Sonoma+ 的新版中文语音必须用「完整名称（带语言后缀）」调用 say，
# 只写短名会生成 0.01 秒的空音频且不报错。这里统一做一次补全。
VOICE_FULL = {
    "Tingting": "Tingting (中文（中国大陆）)",
    "Reed": "Reed (中文（中国大陆）)",
    "Eddy": "Eddy (中文（中国大陆）)",
    "Rocko": "Rocko (中文（中国大陆）)",
    "Grandpa": "Grandpa (中文（中国大陆）)",
    "Grandma": "Grandma (中文（中国大陆）)",
    "Flo": "Flo (中文（中国大陆）)",
    "Shelley": "Shelley (中文（中国大陆）)",
    "Sandy": "Sandy (中文（中国大陆）)",
    "Meijia": "Meijia (中文（台湾）)",
    "Sinji": "Sinji (中文（香港）)",
}
SAFE_VOICE = "Tingting (中文（中国大陆）)"


def norm_voice(name):
    name = (name or "").strip()
    return VOICE_FULL.get(name, name) or SAFE_VOICE


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
    h, m = int(sec // 3600), int((sec % 3600) // 60)
    s = int(sec % 60)
    return "%02d:%02d:%02d,%03d" % (h, m, s, int((sec - int(sec)) * 1000))


def split_dialogue_subtitles(text, vstart, vend, subcfg=None, max_chars=None):
    """超过 max_chars_per_line 的台词，在标点处切成几段依次显示；
    每段的时长按字数和标点分配到 [vstart, vend]；
    段尾逗号、句号省略，问号和感叹号保留。
    返回: [(seg_start, seg_end, seg_text), ...]
    """
    text = (text or "").strip()
    if not text:
        return []
    max_chars = int(max_chars if max_chars is not None else (subcfg or {}).get("max_chars_per_line", 15))
    total_dur = max(0.1, vend - vstart)

    if len(text) <= max_chars:
        clean = re.sub(r"[，。,.]$", "", text)
        return [(vstart, vend, clean)]

    punct_pattern = r"([，。！？；：、…,.!?;:\n])"
    tokens = re.split(punct_pattern, text)
    clauses = []
    curr = ""
    for tok in tokens:
        if not tok:
            continue
        curr += tok
        if re.match(punct_pattern, tok):
            clauses.append(curr)
            curr = ""
    if curr:
        clauses.append(curr)

    segments = []
    accum = ""
    for clause in clauses:
        if len(accum) + len(clause) <= max_chars:
            accum += clause
        else:
            if accum:
                segments.append(accum)
            accum = clause
    if accum:
        segments.append(accum)

    clean_segs = []
    for seg in segments:
        s = seg.strip()
        s = re.sub(r"[，。,.]$", "", s)
        if s:
            clean_segs.append(s)
    if not clean_segs:
        clean_segs = [re.sub(r"[，。,.]$", "", text)]

    weights = [max(1, len(s)) for s in clean_segs]
    total_w = sum(weights)
    result = []
    cur_t = vstart
    for i, (seg, w) in enumerate(zip(clean_segs, weights)):
        dur_i = total_dur * (w / total_w)
        next_t = vend if i == len(clean_segs) - 1 else cur_t + dur_i
        result.append((cur_t, next_t, seg))
        cur_t = next_t
    return result


def write_srt(entries, path):
    lines = []
    for i, (start, end, text) in enumerate(entries, 1):
        lines += [str(i), "%s --> %s" % (srt_time(start), srt_time(end)), text, ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


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


def apply_sharpen(img, factor=0.6, scale=None, strength=None):
    """放大锐化：轻度 USM 补回放大损失的边缘细节。"""
    if strength is not None:
        factor = strength
    if scale is not None and scale <= 1.15:
        return img
    if factor <= 0.001:
        return img
    percent = int(max(0.0, min(2.0, factor)) * 100)
    return img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=percent, threshold=3))


def adapt_clip_frames(clip_frames, n, c=None, max_slowdown=None, hold_push_in=None):
    """动态镜头的变速：
    - 片段比镜头长：均匀加速，保留完整动作
    - 片段比镜头短：放慢播放，相邻两帧做混合；最多放慢 max_clip_slowdown 倍（默认 1.5）
    - 放慢到上限仍不够长：停在最后一帧并缓慢推近 hold_push_in（默认 0.045 / 4.5%）
    """
    m = len(clip_frames)
    if m == 0:
        return []
    if m == 1:
        return [clip_frames[0]] * n
    if m >= n:
        return [clip_frames[min(m - 1, int(round(k * (m - 1) / max(1, n - 1))))] for k in range(n)]

    c = c or {}
    max_slow = float(max_slowdown if max_slowdown is not None else c.get("max_clip_slowdown", 1.5))
    hold_push = float(hold_push_in if hold_push_in is not None else c.get("hold_push_in", 0.045))
    n_slow = min(n, int(round(m * max_slow)))

    result = []
    for k in range(n_slow):
        pos = k * (m - 1) / max(1, n_slow - 1)
        i0 = int(pos)
        i1 = min(m - 1, i0 + 1)
        alpha = float(pos - i0)
        if alpha < 1e-4:
            frame = clip_frames[i0]
        else:
            frame = Image.blend(clip_frames[i0], clip_frames[i1], alpha)
        result.append(frame)

    if n > n_slow:
        last = clip_frames[-1]
        w, h = last.width, last.height
        for k in range(n_slow, n):
            p = ((k - n_slow + 1) / max(1, n - n_slow)) * hold_push
            zoom = 1.0 + p
            cw = int(w / zoom)
            ch = int(h / zoom)
            x0 = (w - cw) // 2
            y0 = (h - ch) // 2
            cropped = last.crop((x0, y0, x0 + cw, y0 + ch))
            frame = cropped.resize((w, h), Image.LANCZOS)
            result.append(frame)

    return result


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


def pick_voice(speaker, cfg, characters):
    """按角色表设定 -> voice.per_character -> gender -> 名字/外观推断 -> _default 顺序选声。"""
    try:
        from drama.workflow_contract import resolve_speaker_voice
    except ImportError:
        try:
            from workflow_contract import resolve_speaker_voice
        except ImportError:
            resolve_speaker_voice = None

    if resolve_speaker_voice is not None:
        voice, rate, reason = resolve_speaker_voice(speaker, cfg, characters)
    else:
        name = (speaker or "").strip()
        vcfg = cfg.get("voice", {})
        cmap = cfg.get("cinema", {}).get("voice_map", {})
        voice = cmap.get("_default", vcfg.get("name", "Reed"))
        rate = int(vcfg.get("rate", 170))
        reason = "默认音色"

    engine = cfg.get("voice", {}).get("engine", "say")
    if engine == "say":
        voice = norm_voice(voice)
    return voice, rate, reason


def process_voice_audio(audio, sr, cfg=None, trim=None, normalize=None, target_rms_db=None, peak_db=None):
    """配音后处理：去首尾静音（保留 40ms）、统一响度、两端 8ms 淡入淡出防爆音。"""
    vcfg = (cfg or {}).get("voice", cfg or {})
    if audio.ndim == 1:
        audio = np.vstack([audio, audio])
    if audio.shape[1] == 0:
        return audio

    do_trim = trim if trim is not None else vcfg.get("trim_silence", True)
    do_norm = normalize if normalize is not None else vcfg.get("normalize", True)

    # 1. 去首尾静音（保留 40ms）
    if do_trim:
        mono = np.abs(audio).mean(axis=0)
        peak = float(mono.max())
        if peak > 1e-4:
            thresh = max(peak * 0.015, 10.0 ** (-45.0 / 20.0))
            active = np.where(mono > thresh)[0]
            if len(active) > 0:
                pad = int(0.040 * sr)
                st = max(0, active[0] - pad)
                ed = min(audio.shape[1], active[-1] + 1 + pad)
                audio = audio[:, st:ed]

    # 2. 统一响度 (RMS) 与峰值上限
    if do_norm and audio.shape[1] > 0:
        t_rms_db = float(target_rms_db if target_rms_db is not None else vcfg.get("target_rms_db", -20.0))
        p_lim_db = float(peak_db if peak_db is not None else vcfg.get("peak_db", -1.5))
        target_rms = 10.0 ** (t_rms_db / 20.0)
        peak_limit = 10.0 ** (p_lim_db / 20.0)

        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms > 1e-6:
            audio = audio * (target_rms / rms)
            curr_peak = float(np.abs(audio).max())
            if curr_peak > peak_limit:
                audio = audio * (peak_limit / curr_peak)

    # 3. 两端各 8ms 淡入淡出
    fade_len = int(0.008 * sr)
    if fade_len > 0 and audio.shape[1] >= 2 * fade_len:
        f_in = np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
        f_out = np.linspace(1.0, 0.0, fade_len, dtype=np.float32)
        audio[:, :fade_len] *= f_in
        audio[:, -fade_len:] *= f_out

    return np.ascontiguousarray(audio.astype(np.float32))


def synth_voice_line(text, voice, rate, dst, cfg, sr=24000):
    """支持 say 与 command 引擎的单句语音合成。"""
    vcfg = cfg.get("voice", {})
    engine = vcfg.get("engine", "say")
    timeout = vcfg.get("timeout_sec", 60)

    if engine == "command":
        cmd_tmpl = vcfg.get("command") or []
        if not cmd_tmpl:
            raise RuntimeError("command 配音引擎未设置 voice.command 参数数组")
        ext = vcfg.get("command_ext", "wav").lstrip(".")
        if not dst.endswith("." + ext):
            dst = os.path.splitext(dst)[0] + "." + ext

        cmd = []
        has_text_placeholder = False
        text_file_path = None
        for arg in cmd_tmpl:
            arg_str = str(arg)
            if "{out}" in arg_str:
                arg_str = arg_str.replace("{out}", dst)
            if "{voice}" in arg_str:
                arg_str = arg_str.replace("{voice}", str(voice))
            if "{rate}" in arg_str:
                arg_str = arg_str.replace("{rate}", str(rate))
            if "{text}" in arg_str:
                arg_str = arg_str.replace("{text}", text)
                has_text_placeholder = True
            if "{text_file}" in arg_str:
                text_file_path = dst + ".text.tmp"
                with open(text_file_path, "w", encoding="utf-8") as tf:
                    tf.write(text)
                arg_str = arg_str.replace("{text_file}", text_file_path)
                has_text_placeholder = True
            cmd.append(arg_str)

        stdin_data = None if has_text_placeholder else text.encode("utf-8")
        try:
            subprocess.run(cmd, input=stdin_data, check=True, capture_output=True, timeout=timeout)
        finally:
            if text_file_path and os.path.exists(text_file_path):
                try: os.remove(text_file_path)
                except Exception: pass

        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            raise RuntimeError("没有写出音频文件：%s" % dst)
        raw = decode_audio(dst, sr)
        audio = process_voice_audio(raw, sr, cfg)
        return audio, dst
    else:
        v = norm_voice(voice)
        subprocess.run(["say", "-v", v, "-r", str(rate), "-o", dst, text],
                       check=True, capture_output=True, timeout=timeout)
        audio = decode_audio(dst, sr)
        dur = audio.shape[1] / sr
        if dur < 0.35 or float(np.abs(audio).max()) < 1e-4:
            print("  [提示] %s 返回空音频（%.2fs），自动回退 %s" % (v, dur, SAFE_VOICE), flush=True)
            v = SAFE_VOICE
            subprocess.run(["say", "-v", v, "-r", str(rate), "-o", dst, text],
                           check=True, capture_output=True, timeout=timeout)
            audio = decode_audio(dst, sr)
            dur = audio.shape[1] / sr
            if dur < 0.35 or float(np.abs(audio).max()) < 1e-4:
                raise RuntimeError("配音重试后仍为空")
        audio = process_voice_audio(audio, sr, cfg)
        return audio, dst


def make_voice(shots, out_dir, cfg, sr, characters):
    vcfg = cfg["voice"]
    vdir = os.path.join(out_dir, "voice")
    os.makedirs(vdir, exist_ok=True)
    ext = vcfg.get("command_ext", "wav").lstrip(".") if vcfg.get("engine") == "command" else "aiff"
    made = []
    for i, s in enumerate(shots, 1):
        text = (s.get("dialogue") or "").strip()
        if not text or not vcfg.get("enabled", True):
            made.append((i, None, None, 0.0, ""))
            continue
        speaker = (s.get("speaker") or "").strip()
        voice, rate, reason = pick_voice(speaker, cfg, characters)
        slug = "".join(ch for ch in str(voice).split("(")[0].strip()
                       if ch.isalnum() or ch in "-_") or "voice"
        dst = os.path.join(vdir, "line%02d_%s.%s" % (i, slug, ext))
        try:
            audio, actual_dst = synth_voice_line(text, voice, rate, dst, cfg, sr)
            dur = audio.shape[1] / sr
            made.append((i, actual_dst, audio, dur, voice))
            print("  [配音] %02d  %-9s %.1fs  %s" % (i, slug, dur, text[:24]), flush=True)
        except Exception as e:
            raise RuntimeError("配音失败（%s）：%s" % (text[:16], e)) from e
    return made


# ---------------------------------------------------------------- 音频：环境音效
def _env(n, sr, speed=0.004, seed=1):
    rng = np.random.default_rng(seed)
    raw = rng.standard_normal(n + 64).astype(np.float32)
    k = max(2, int(speed * sr))
    pad = np.pad(raw, (k, 0))
    csum = np.cumsum(pad)
    return (csum[k:k + n] - csum[:n]) / k


def synth_sfx(kind, n, sr=24000, seed=7):
    """12 类程序合成环境音，无版权依赖、无外部素材。"""
    if isinstance(n, (float, np.floating)):
        n = int(round(n * sr)) if n < 1000 else int(n)
    else:
        n = int(n)
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
        hum = (0.32 * np.sin(2 * np.pi * 82 * t) + 0.16 * np.sin(2 * np.pi * 164 * t)).astype(np.float32)
        shimmer = (0.7 + 0.3 * np.sin(2 * np.pi * 3.4 * t)).astype(np.float32)
        sparks = (rng.random(n) > 0.997).astype(np.float32) * white * 0.22
        x = hum * shimmer + 0.08 * _env(n, sr, 0.003, seed) + sparks
    elif kind == "fire":
        rumble = 0.22 * _env(n, sr, 0.005, seed) * (0.8 + 0.2 * np.sin(2 * np.pi * 0.4 * t)).astype(np.float32)
        crackle = np.zeros(n, np.float32)
        pops = np.where(rng.random(n) > 0.9982)[0]
        pop_len = int(0.006 * sr)
        for p in pops:
            L = min(pop_len, n - p)
            crackle[p:p + L] += (rng.standard_normal(L) * np.linspace(0.8, 0.0, L)).astype(np.float32)
        x = rumble + crackle * 0.45
    elif kind == "impact":
        boom = np.sin(2 * np.pi * (70 - 40 * t / max(t[-1], 1e-6)) * t).astype(np.float32)
        env = np.exp(-4.2 * t).astype(np.float32)
        transient = white * np.exp(-18.0 * t).astype(np.float32)
        x = 0.55 * boom * env + 0.35 * transient + 0.12 * _env(n, sr, 0.008, seed) * env
    elif kind == "sword":
        woosh = _env(n, sr, 0.02, seed) * np.sin(np.pi * np.clip(t / 0.25, 0, 1)).astype(np.float32) * np.exp(-4.0 * t).astype(np.float32)
        ring_t = np.maximum(0.0, t - 0.08)
        ring = (0.28 * np.sin(2 * np.pi * 2450 * ring_t) + 0.18 * np.sin(2 * np.pi * 3680 * ring_t)).astype(np.float32)
        ring *= np.exp(-3.2 * ring_t).astype(np.float32) * (t >= 0.08)
        x = 0.32 * woosh + ring
    elif kind == "thunder":
        crack = white * np.exp(-14.0 * t).astype(np.float32)
        rumble = (0.35 * np.sin(2 * np.pi * 48 * t) + 0.20 * np.sin(2 * np.pi * 72 * t)).astype(np.float32)
        roll = _env(n, sr, 0.003, seed) * (0.6 + 0.4 * np.sin(2 * np.pi * 1.5 * t)).astype(np.float32)
        env = (0.4 + 0.6 * np.exp(-1.2 * t)).astype(np.float32)
        x = 0.35 * crack + (0.35 * rumble + 0.30 * roll) * env
    else:
        return np.zeros(n, np.float32)
    f = min(int(0.22 * sr), n // 2)
    if f:
        x[:f] *= np.linspace(0, 1, f, dtype=np.float32)
        x[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
    return np.clip(x, -1, 1).astype(np.float32)


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


def synth_guofeng_music(total_sec, sr=24000, mcfg=None, grade="cool", style="guofeng"):
    """国风五声音阶配乐：五度持续低音加五声音阶拨弦旋律。
    冷色调（cool、noir）用羽调式，暖色调用宫调式；色调取第一个镜头的 grade。
    beat_sec 控制旋律疏密，seed 控制旋律走向。
    """
    if isinstance(mcfg, str):
        if mcfg in ("cool", "warm", "golden", "noir", "neutral"):
            grade = mcfg
            mcfg = {}
        else:
            style = mcfg
            mcfg = {}
    mcfg = mcfg or {}
    b = mcfg.get("builtin") or {}
    cold = grade in ("cool", "noir")
    if cold:
        # 羽调式 (A羽调式)
        notes = [220.0, 261.63, 293.66, 329.63, 392.0, 440.0, 523.25, 587.33]
        drone = [55.0, 82.41]
    else:
        # 宫调式 (C宫调式)
        notes = [261.63, 293.66, 329.63, 392.0, 440.0, 523.25, 587.33, 659.25]
        drone = [65.41, 98.0]

    n = int(total_sec * sr) + sr
    t = np.arange(n, dtype=np.float32) / sr

    # 五度持续低音 (Drone bass)
    drone_wave = (0.35 * np.sin(2 * np.pi * drone[0] * t) +
                  0.20 * np.sin(2 * np.pi * drone[1] * t)).astype(np.float32)
    drone_wave *= (0.85 + 0.15 * np.sin(2 * np.pi * 0.15 * t)).astype(np.float32)

    # 五声音阶拨弦旋律 (Plucked melody)
    pluck = np.zeros(n, np.float32)
    beat_sec = float(b.get("beat_sec", 0.75))
    seed = int(b.get("seed", 42))
    rng = np.random.default_rng(seed)

    st = 0
    while st < n:
        fq = notes[int(rng.integers(0, len(notes)))]
        L = min(int(1.2 * sr), n - st)
        if L > 0:
            tt = np.arange(L, dtype=np.float32) / sr
            env = np.exp(-5.5 * tt).astype(np.float32)
            seg = (0.38 * np.sin(2 * np.pi * fq * tt) +
                   0.16 * np.sin(2 * np.pi * fq * 2 * tt) +
                   0.08 * np.sin(2 * np.pi * fq * 3 * tt)).astype(np.float32) * env
            pluck[st:st + L] += seg
        st += int(max(0.2, beat_sec * (0.8 + 0.4 * rng.random())) * sr)

    mono = drone_wave * 0.45 + pluck

    # 空间感：短延迟与立体声展开
    d1, d2 = int(0.12 * sr), int(0.21 * sr)
    wet = np.zeros(n, np.float32)
    if d1 < n: wet[d1:] += 0.22 * mono[:-d1]
    if d2 < n: wet[d2:] += 0.12 * mono[:-d2]
    left = mono + wet * 0.7
    right = np.concatenate([np.zeros(int(0.012 * sr), np.float32), mono])[:n] + wet * 0.85

    fade = min(int(float(mcfg.get("fade_sec", 1.8)) * sr), n // 2)
    if fade:
        f_in = np.linspace(0, 1, fade, dtype=np.float32)
        f_out = np.linspace(1, 0, fade, dtype=np.float32)
        left[:fade] *= f_in; right[:fade] *= f_in
        left[-fade:] *= f_out; right[-fade:] *= f_out
    return np.vstack([left, right])


def synth_music(total_sec, sr, mcfg, grade):
    """内置配乐：支持国风五声音阶拨弦 (guofeng) 或原有的氛围和弦垫。"""
    b = mcfg.get("builtin") or {}
    style = b.get("style", "guofeng")
    if style == "guofeng":
        return synth_guofeng_music(total_sec, sr, mcfg, grade)

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


ONE_SHOT_SFX = {"impact", "sword", "thunder", "door"}


def build_audio(voices, timeline, total_sec, sr, cfg, grade):
    n = int(total_sec * sr)
    voice_track = np.zeros((2, n + sr), np.float32)
    sfx_track = np.zeros(n + sr, np.float32)
    mask = np.zeros(n + sr, np.float32)
    vcfg, mcfg, c = cfg["voice"], cfg["music"], cfg["cinema"]
    lead = float(vcfg.get("lead_in_sec", 0.2))

    for (idx, _p, audio, _dur, _voice), tl in zip(voices, timeline):
        _shot, start, dur, _img, _motion, _text, _vstart, _vend = tl
        if audio is None:
            continue
        at = int((start + lead) * sr)
        L = min(audio.shape[1], max(0, voice_track.shape[1] - at))
        if L > 0:
            voice_track[:, at:at + L] += audio[:, :L] * float(vcfg.get("gain", 1.0))
            mask[at:at + L] = 1.0

    if c.get("sfx_enabled", True):
        for i, tl in enumerate(timeline, 1):
            shot, start, dur, _img, _motion, _text, _vstart, _vend = tl
            sfx_spec = (shot.get("sfx") or "silence").strip()
            parts = [p.strip() for p in sfx_spec.split("|") if p.strip()]
            for p_idx, part in enumerate(parts):
                if "@" in part:
                    kind_str, at_str = part.split("@", 1)
                    kind = kind_str.strip().lower()
                    try:
                        at_offset = float(at_str.strip())
                    except ValueError:
                        at_offset = 0.35 * dur
                else:
                    kind = part.strip().lower()
                    at_offset = (0.35 * dur) if kind in ONE_SHOT_SFX else 0.0

                if kind == "silence":
                    continue

                if kind in ONE_SHOT_SFX:
                    seg_dur = max(0.2, min(dur - at_offset, 2.5))
                else:
                    seg_dur = dur
                    at_offset = 0.0

                L = int(seg_dur * sr)
                at = int((start + at_offset) * sr)
                if L > 0 and at < sfx_track.size:
                    seg = synth_sfx(kind, L, sr, seed=100 + i * 10 + p_idx)
                    end = min(sfx_track.size, at + L)
                    sfx_track[at:end] += seg[:end - at] * float(c.get("sfx_gain", 0.26))

    # 台词出现时，音效自动压低 (默认 70%)
    sfx_duck = float(c.get("sfx_duck_under_voice", 0.7))
    sfx_track[:n] *= (1.0 - (1.0 - sfx_duck) * np.clip(mask[:n], 0, 1))

    voice_track = voice_track[:, :n]
    sfx2 = np.vstack([sfx_track[:n] * 0.94,
                      np.concatenate([np.zeros(int(0.009 * sr), np.float32),
                                      sfx_track])[:n] * 0.94])
    mix = voice_track + sfx2

    if mcfg.get("enabled", True):
        bgm = load_music(total_sec, sr, mcfg, grade)
        bgm = bgm[:, :n] if bgm.shape[1] >= n else np.pad(
            bgm, ((0, 0), (0, n - bgm.shape[1])))

        # 平滑闪避：台词开始前 0.18 秒提前压低，按 attack/release 曲线变化
        lead_duck = int(0.18 * sr)
        early_mask = np.zeros(n, np.float32)
        if lead_duck < n:
            early_mask[:n - lead_duck] = mask[lead_duck:n]
        else:
            early_mask[:] = mask[:n]

        duck = float(mcfg.get("duck_under_voice", 0.42))
        att_sec = float(mcfg.get("duck_attack_sec", 0.06))
        rel_sec = float(mcfg.get("duck_release_sec", 0.45))
        att_coeff = float(np.exp(-1.0 / max(1, int(att_sec * sr))))
        rel_coeff = float(np.exp(-1.0 / max(1, int(rel_sec * sr))))

        duck_env = np.zeros(n, np.float32)
        curr = 0.0
        for i in range(n):
            tgt = early_mask[i]
            if tgt > curr:
                curr = tgt + (curr - tgt) * att_coeff
            else:
                curr = tgt + (curr - tgt) * rel_coeff
            duck_env[i] = curr
        bgm *= (1.0 - (1.0 - duck) * np.clip(duck_env, 0, 1))
        mix = mix + bgm

    # 软限幅（tanh）
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
    white = Image.new("RGB", (W, H), (255, 255, 255))
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
        last_frame = None

    for si, (shot, start, dur, img_path, motion, dialogue, vstart, vend) in enumerate(timeline):
        n = max(1, int(round(dur * fps)))
        base = Image.open(img_path).convert("RGB") if img_path else None
        clip_frames = []
        if cfg.get("_videos_dir"):
            clip_path = os.path.join(cfg["_videos_dir"], shot["shot_id"] + ".mp4")
            with av.open(clip_path) as clip:
                clip_frames = [vf.to_image() for vf in clip.decode(video=0)]
            if len(clip_frames) < 2:
                raise RuntimeError("动态镜头缺帧：" + clip_path)
        adapted_frames = adapt_clip_frames(clip_frames, n, c) if clip_frames else None
        shot_grade = shot.get("grade") or grade
        shot_trans = (shot.get("transition") or c.get("transition", "dip")).lower()

        for k in range(n):
            t_sec = k / fps
            if base is not None:
                if adapted_frames:
                    src_f = adapted_frames[k]
                    need_sharpen = max(W / src_f.width, H / src_f.height) > 1.15
                    f = ImageOps.fit(src_f, (W, H), method=Image.LANCZOS)
                else:
                    need_sharpen = max(W / base.width, H / base.height) > 1.15
                    f = camera_frame(base, k, n, motion, W, H, c, step)
                if need_sharpen and float(c.get("sharpen", 0.6)) > 0:
                    f = apply_sharpen(f, float(c.get("sharpen", 0.6)))
                f = kit.apply(f, shot_grade, step=step)
            else:
                f = Image.new("RGB", (W, H), (12, 15, 21))
            if letterbox > 0:
                dl = ImageDraw.Draw(f)
                dl.rectangle((0, 0, W, letterbox), fill=(0, 0, 0))
                dl.rectangle((0, H - letterbox, W, H), fill=(0, 0, 0))

            # 每镜转场：dip (暗场溶解), dissolve (直接溶解), cut (硬切), flash (白闪)
            if k < trans_n:
                a = (k + 1) / trans_n
                if last_frame is not None:
                    if shot_trans == "dip":
                        f = Image.blend(last_frame, f, a)
                        dip = 1.0 - 0.85 * math.sin(math.pi * a)
                        f = Image.blend(black, f, max(0.05, dip))
                    elif shot_trans == "dissolve":
                        f = Image.blend(last_frame, f, a)
                    elif shot_trans == "cut":
                        pass
                    elif shot_trans == "flash":
                        f = Image.blend(last_frame, f, a)
                        flash_amp = 0.9 * math.sin(math.pi * a)
                        f = Image.blend(f, white, max(0.0, min(1.0, flash_amp)))
                elif si == 0:
                    f = Image.blend(black, f, a)

            # 保存纯净画面（不带字幕），供下一镜转场使用，杜绝上一镜字幕残影
            clean_f = f.copy() if base is not None else None

            # 字幕分段显示与逐段淡入淡出
            if sub_on and dialogue:
                sub_segs = split_dialogue_subtitles(dialogue, vstart, vend, subcfg)
                for seg_idx, (s_st, s_ed, s_txt) in enumerate(sub_segs):
                    if s_st <= t_sec <= s_ed:
                        if len(sub_segs) == 1:
                            win_a = min(1.0, max(0.0, (t_sec - s_st) / fade_sub),
                                        max(0.0, (s_ed - t_sec) / fade_sub))
                        elif seg_idx == 0:
                            win_a = min(1.0, max(0.0, (t_sec - s_st) / fade_sub))
                        elif seg_idx == len(sub_segs) - 1:
                            win_a = min(1.0, max(0.0, (s_ed - t_sec) / fade_sub))
                        else:
                            win_a = 1.0
                        f = draw_subtitle(f, s_txt, subcfg, W, H, win_a)
                        break
            push_video(f)
        last_frame = clean_f
        print("  [镜头] %s  %.1fs  %3d 帧  %-10s %s%s" %
              (shot["shot_id"], dur, n, motion,
               "音效:" + (shot.get("sfx") or "—"),
               "  字幕" if (sub_on and dialogue) else ""), flush=True)

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


def preview_episode_audio(shots, cfg=None, characters=None, out_dir=None, out_audio=None, out_srt=None):
    """整集声音样片：
    合成整集配音、音效和配乐，按正式制作规则混成一条音轨；
    计算每个镜头的起点、按配音调整后的实际时长、配音长度和字幕分段；
    写出混合音频和 SRT 文件并返回元数据字典。
    """
    if isinstance(cfg, dict) and ("voice" in cfg or "cinema" in cfg):
        config = cfg
        chars = characters or {}
    elif isinstance(characters, dict) and ("voice" in characters or "cinema" in characters):
        config = characters
        chars = cfg or {}
    else:
        config = cfg or {}
        chars = characters or {}
    cfg = config
    characters = chars

    if out_dir and str(out_dir).endswith('.wav'):
        out_audio = out_audio or out_dir
        out_dir = str(Path(out_dir).parent)

    sr = int(cfg.get("audio", {}).get("sample_rate", 44100))
    vcfg = cfg.get("voice", {})
    lead = float(vcfg.get("lead_in_sec", 0.2))
    vtail = float(cfg.get("cinema", {}).get("voice_tail_sec", 0.5))
    grade = (shots[0].get("grade") if shots else None) or cfg.get("cinema", {}).get("grade", "neutral")
    subcfg = cfg.get("subtitle", {})

    if not out_dir:
        import tempfile
        out_dir = tempfile.mkdtemp(prefix='drama_preview_')
    os.makedirs(out_dir, exist_ok=True)

    try:
        voices = make_voice(shots, out_dir, cfg, sr, characters)
        timeline = []
        cur_t = 0.0
        srt_entries = []
        timeline_info = []

        for (idx, _p, audio, v_dur, voice_name), shot in zip(voices, shots):
            orig_dur = float(shot.get("duration_sec") or 3.0)
            text = (shot.get("dialogue") or "").strip()
            speaker = (shot.get("speaker") or "").strip()
            vstart = lead
            vend = lead + v_dur

            if audio is not None and v_dur > 0:
                actual_dur = max(orig_dur, lead + v_dur + vtail)
            else:
                actual_dur = orig_dur

            elongated = actual_dur > orig_dur + 0.05
            sub_segs = split_dialogue_subtitles(text, cur_t + vstart, cur_t + vend, subcfg) if text else []
            for s_st, s_ed, s_txt in sub_segs:
                srt_entries.append((s_st, s_ed, s_txt))

            timeline.append((shot, cur_t, actual_dur, None, shot.get("motion", "lock"),
                             text, vstart, vend))
            timeline_info.append({
                "shot_id": shot.get("shot_id", "S%02d" % idx),
                "speaker": speaker,
                "dialogue": text,
                "voice": voice_name,
                "start": round(cur_t, 2),
                "start_sec": round(cur_t, 2),
                "original_duration": round(orig_dur, 2),
                "orig_dur": round(orig_dur, 2),
                "actual_duration": round(actual_dur, 2),
                "actual_dur": round(actual_dur, 2),
                "voice_duration": round(v_dur, 2),
                "voice_dur": round(v_dur, 2),
                "elongated": elongated,
                "subtitles": [{"start": round(st, 2), "end": round(ed, 2), "text": txt}
                              for st, ed, txt in sub_segs],
                "subtitle_segments": [{"start_sec": round(st, 2), "end_sec": round(ed, 2), "text": txt}
                                      for st, ed, txt in sub_segs]
            })
            cur_t += actual_dur

        total_sec = cur_t
        mix = build_audio(voices, timeline, total_sec, sr, cfg, grade)

        # 写出 WAV 音频文件
        mix_path = os.path.join(out_dir, "preview_audio.wav")
        import wave
        with wave.open(mix_path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            audio_clipped = np.clip(mix, -1.0, 1.0)
            pcm16 = (audio_clipped * 32767).astype(np.int16)
            interleaved = np.empty((pcm16.shape[1] * 2,), dtype=np.int16)
            interleaved[0::2] = pcm16[0]
            interleaved[1::2] = pcm16[1]
            wf.writeframes(interleaved.tobytes())

        # 写出 SRT 文件
        srt_path = os.path.join(out_dir, "subtitles.srt")
        write_srt(srt_entries, srt_path)
        with open(srt_path, "r", encoding="utf-8") as f:
            srt_content = f.read()

        if out_audio:
            shutil.copy2(mix_path, str(out_audio))
        if out_srt:
            shutil.copy2(srt_path, str(out_srt))

        return {
            "total_sec": round(total_sec, 2),
            "audio_path": str(out_audio) if out_audio else mix_path,
            "srt_path": str(out_srt) if out_srt else srt_path,
            "srt_content": srt_content,
            "timeline": timeline_info
        }
    finally:
        pass


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
    cursor = intro
    timeline, missing = [], []
    for i, s in enumerate(shots):
        try:
            dur = float(s.get("duration_sec") or c.get("min_shot_sec", 2.0))
        except ValueError:
            dur = float(c.get("min_shot_sec", 2.0))
        voice_dur = voices[i][3]
        lead = float(cfg["voice"].get("lead_in_sec", 0.2))
        if voice_dur:
            dur = max(dur, voice_dur + lead + float(c.get("voice_tail_sec", 0.9)))
        dur = max(float(c.get("min_shot_sec", 2.0)), min(15.0, dur))
        dialogue = (s.get("dialogue") or "").strip()
        vstart = lead + 0.12
        vend = (voice_dur + lead + 0.45) if voice_dur else dur
        img = find_image(args.images, s["shot_id"])
        if not img:
            missing.append(s["shot_id"])
        timeline.append((s, cursor, dur, img,
                         (s.get("motion") or "lock").strip(), dialogue,
                         vstart, min(vend, dur)))
        cursor += dur
    total = cursor + outro
    print("[时间轴] %.1fs（片头 %.1fs / 片尾 %.1fs），缺图 %d" %
          (total, intro, outro, len(missing)), flush=True)
    if missing:
        raise RuntimeError("缺少关键帧：" + ", ".join(missing))

    subs = [(start + vstart, start + min(vend, dur), text)
            for _s, start, dur, _img, _m, text, vstart, vend in timeline if text]
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
