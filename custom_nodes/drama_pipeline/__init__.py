# -*- coding: utf-8 -*-
"""ComfyUI 自定义节点：让界面直接读取 input/drama/ 下的精品短剧分镜。

一键脚本 auto_drama.py 会把 27B 生成的分镜同步到：
    ComfyUI/input/drama/<剧名>/shots.csv        含电影级镜头语言列
    ComfyUI/input/drama/<剧名>/characters.json   人物外貌锚点与全局风格
    ComfyUI/input/drama/<剧名>/manifest.json     制作信息
    ComfyUI/input/drama/latest/                 总是最新一部剧

ComfyUI 只允许节点读取 input/ 目录，所以分镜必须放在这里。

节点：
  DramaShotLoader   取一个镜头，输出提示词 / seed / 尺寸 / 镜头语言，可直接接到
                    CLIPTextEncode、KSampler、EmptySD3LatentImage
  DramaManifestInfo 剧名与镜头数摘要
  DramaProjectPanel 在界面里显示整部剧的全部预置配置（人物形象、配音、
                    台词、音效、插曲、转场、调色），方便人工核对

注意：新增/修改自定义节点后需要重启一次 ComfyUI 才会生效。
"""

import csv
import json
import os

import folder_paths

DRAMA_ROOT = os.path.join(folder_paths.get_input_directory(), "drama")

VOICE_FALLBACK = {
    "_default": "Tingting (中文（中国大陆）)",
    "_male": "Reed (中文（中国大陆）)",
    "_elder": "Grandpa (中文（中国大陆）)",
}
MALE_HINT = ("先生", "哥", "叔", "伯", "父", "男", "老板", "陆屿")
ELDER_HINT = ("老", "爷", "婆", "奶", "父", "母", "船老大")
GRADE_LABEL = {"cool": "冷蓝", "warm": "暖橙", "golden": "金色暖光",
               "noir": "黑白高反差", "neutral": "中性自然"}
MOTION_LABEL = {"push_in": "推镜", "pull_out": "拉镜", "pan_left": "左摇",
                "pan_right": "右摇", "tilt_up": "上摇", "tilt_down": "下摇",
                "handheld": "手持", "lock": "固定机位"}
SFX_LABEL = {"rain": "雨声", "wind": "风声", "waves": "水浪", "boat": "渡轮引擎/汽笛",
             "steps": "脚步", "clock": "钟摆", "engine": "引擎", "city": "城市环境",
             "birds": "鸟鸣", "door": "门响", "paper": "纸页", "silence": "静音"}


def _projects():
    names = ["latest"]
    if os.path.isdir(DRAMA_ROOT):
        names += sorted(
            d for d in os.listdir(DRAMA_ROOT)
            if d != "latest" and os.path.isdir(os.path.join(DRAMA_ROOT, d))
        )
    return names


def _read_json(path, fallback):
    if not os.path.isfile(path):
        return fallback
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return fallback


def _rows(project):
    csv_path = os.path.join(DRAMA_ROOT, project, "shots.csv")
    if not os.path.isfile(csv_path):
        raise RuntimeError(
            "找不到分镜表 %s。请先双击桌面「一键生成短剧.command」。" % csv_path)
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = [{k: (v or "").strip() for k, v in r.items() if k}
                for r in csv.DictReader(f) if r.get("shot_id")]
    if not rows:
        raise RuntimeError("分镜表是空的：" + csv_path)
    return rows


def _voice_for(name, cfg):
    vmap = (cfg.get("voice") or {}).get("per_character") or {}
    if name in vmap:
        return vmap[name]
    if any(k in name for k in ELDER_HINT):
        return vmap.get("_elder", VOICE_FALLBACK["_elder"])
    if any(k in name for k in MALE_HINT):
        return vmap.get("_male", VOICE_FALLBACK["_male"])
    return vmap.get("_default", (cfg.get("voice") or {}).get("name", "Tingting"))


class DramaShotLoader:
    """从分镜 CSV 取一个镜头，拼好带角色锚点与镜头语言的提示词。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project": (_projects(), {"default": "latest"}),
                "index": ("INT", {"default": 0, "min": 0, "max": 999, "step": 1}),
                "with_anchor": ("BOOLEAN", {"default": True}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "FLOAT",
                    "INT", "INT", "INT", "STRING",
                    "STRING", "STRING", "STRING", "STRING",
                    "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("prompt", "shot_id", "character", "dialogue",
                    "duration", "width", "height", "seed", "description",
                    "camera", "motion", "lighting", "mood",
                    "sfx", "speaker", "grade", "video_prompt")
    FUNCTION = "load"
    CATEGORY = "短剧/精品流水线"
    OUTPUT_NODE = False

    def load(self, project, index, with_anchor):
        rows = _rows(project)
        idx = int(index) % len(rows)
        row = rows[idx]

        cfg = _read_json(os.path.join(DRAMA_ROOT, project, "characters.json"), {})
        characters = cfg.get("characters", {}) if isinstance(cfg, dict) else {}
        style = cfg.get("global_style", "") if isinstance(cfg, dict) else ""
        names = [n.strip() for n in row.get("character", "").split("|") if n.strip()]

        parts = []
        if with_anchor:
            for n in names:
                anchor = (characters.get(n) or {}).get("anchor")
                if anchor:
                    parts.append(anchor)
        parts.append(row.get("camera") or row.get("scene") or "中景")
        if row.get("scene") and row.get("camera") and row["scene"] not in row["camera"]:
            parts.append(row["scene"])
        if row.get("description"):
            parts.append(row["description"])
        if row.get("lighting"):
            parts.append(row["lighting"])
        grade = (row.get("grade") or "neutral").lower()
        grade_cn = {"cool": "冷蓝色调，低饱和，青灰阴影",
                    "warm": "暖橙色调，柔和肤色",
                    "golden": "金色暖光，逆光轮廓",
                    "noir": "高对比黑白影调，硬光",
                    "neutral": "中性自然色调"}.get(grade, "")
        if grade_cn:
            parts.append(grade_cn)
        if row.get("extra_prompt"):
            parts.append(row["extra_prompt"])
        if style:
            parts.append(style)
        parts.append("画面中不要出现任何文字、字幕、水印、logo")
        prompt = "，".join(parts)

        seed = row.get("seed")
        if not seed:
            base_seed = 4200 + idx * 4000
            for n in names:
                c = characters.get(n) if isinstance(characters, dict) else None
                if isinstance(c, dict) and c.get("base_seed"):
                    base_seed = int(c["base_seed"])
                    break
            seed = base_seed + idx + 1
        try:
            width = int(row.get("width") or 1024)
            height = int(row.get("height") or 576)
            duration = float(row.get("duration_sec") or 3)
        except (TypeError, ValueError):
            width, height, duration = 1024, 576, 3.0

        return (prompt, row["shot_id"], row.get("character", ""),
                row.get("dialogue", ""), duration, width, height,
                int(seed), row.get("description", ""),
                row.get("camera", ""), row.get("motion", "lock"),
                row.get("lighting", ""), row.get("mood", ""),
                row.get("sfx", "silence"), row.get("speaker", ""), grade,
                row.get("video_prompt") or row.get("description", ""))

    @classmethod
    def IS_CHANGED(cls, project, index, with_anchor):
        p = os.path.join(DRAMA_ROOT, project, "shots.csv")
        return os.path.getmtime(p) if os.path.isfile(p) else float("nan")


class DramaManifestInfo:
    """剧名/镜头数摘要，可接到任意 STRING 输入，也会打印到 ComfyUI 日志。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project": (_projects(), {"default": "latest"})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("summary",)
    FUNCTION = "load"
    CATEGORY = "短剧/精品流水线"
    OUTPUT_NODE = False

    def load(self, project):
        m = _read_json(os.path.join(DRAMA_ROOT, project, "manifest.json"), {})
        text = ("剧名：%s | 镜头：%s | 角色：%s | 生成时间：%s" %
                (m.get("title", "（无）"), m.get("shot_count", "?"),
                 "、".join(m.get("characters", [])), m.get("generated_at", "")))
        print("[短剧分镜] " + text, flush=True)
        return (text,)

    @classmethod
    def IS_CHANGED(cls, project):
        p = os.path.join(DRAMA_ROOT, project, "manifest.json")
        return os.path.getmtime(p) if os.path.isfile(p) else float("nan")


class DramaProjectPanel:
    """输出节点：在 ComfyUI 界面里显示整部剧的全部预置配置。

    人物形象、配音分声、台词、音效、插曲、运镜、转场、调色都在这里一目了然，
    不需要打开任何配置文件。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project": (_projects(), {"default": "latest"})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("panel",)
    FUNCTION = "load"
    CATEGORY = "短剧/精品流水线"
    OUTPUT_NODE = True

    def load(self, project):
        base = os.path.join(DRAMA_ROOT, project)
        m = _read_json(os.path.join(base, "manifest.json"), {})
        chars_cfg = _read_json(os.path.join(base, "characters.json"), {})
        rows = _rows(project)
        cfg_path = m.get("config", "")
        cfg = _read_json(cfg_path, {}) if cfg_path else {}
        cinema = cfg.get("cinema") or {}
        music = cfg.get("music") or {}
        sub = cfg.get("subtitle") or {}

        L = []
        L.append("■ 剧名  %s" % m.get("title", "（未命名）"))
        L.append("  梗概  %s" % m.get("logline", ""))
        L.append("  镜头  %d ｜ 生成于 %s" % (len(rows), m.get("generated_at", "")))
        L.append("")
        L.append("■ 全局画面")
        L.append("  风格  %s" % chars_cfg.get("global_style", ""))
        L.append("  色调  %s ｜ 调色 对比%.2f 饱和%.2f 暗角%.2f 颗粒%.1f" %
                 (GRADE_LABEL.get((rows[0].get("grade") or "neutral").lower(), "—"),
                  float(cinema.get("contrast", 1.07)),
                  float(cinema.get("saturation", 0.95)),
                  float(cinema.get("vignette", 0.30)),
                  float(cinema.get("grain", 2.6))))
        L.append("")
        L.append("■ 人物形象（外貌锚点 + 固定 seed + 配音）")
        for name, c in (chars_cfg.get("characters") or {}).items():
            L.append("  · %s  →  %s" % (name, _voice_for(name, cfg)))
            L.append("      %s" % (c or {}).get("anchor", ""))
            L.append("      base_seed %s" % (c or {}).get("base_seed", "—"))
        L.append("")
        L.append("■ 分镜 / 运镜 / 台词 / 音效")
        for r in rows:
            L.append("  %s  %s ｜ %s ｜ %s ｜ %.0fs" %
                     (r.get("shot_id", ""), r.get("camera", ""),
                      MOTION_LABEL.get(r.get("motion", ""), r.get("motion", "")),
                      r.get("scene", ""), float(r.get("duration_sec") or 3)))
            L.append("      布光 %s ｜ 情绪 %s ｜ 音效 %s" %
                     (r.get("lighting", "—"), r.get("mood", "—"),
                      SFX_LABEL.get(r.get("sfx", ""), r.get("sfx", "—"))))
            if r.get("dialogue"):
                L.append("      %s：「%s」" %
                         (r.get("speaker") or "？", r["dialogue"]))
        L.append("")
        L.append("■ 成片配置")
        L.append("  分辨率 %sx%s @%sfps ｜ 片头 %.1fs ｜ 片尾 %.1fs ｜ 交叉溶解 %s 帧" %
                 (cinema.get("width", 1280), cinema.get("height", 720),
                  cinema.get("fps", 24), float(cinema.get("intro_sec", 2.6)),
                  float(cinema.get("outro_sec", 2.2)),
                  cinema.get("transition_frames", 14)))
        L.append("  字幕 硬烧录 %s ｜ 配音 say 分声 %s ｜ 环境音效 %s" %
                 ("开" if sub.get("burn_in", True) else "关",
                  "开" if (cfg.get("voice") or {}).get("enabled", True) else "关",
                  "开" if cinema.get("sfx_enabled", True) else "关"))
        L.append("  插曲 %s ｜ 增益 %.2f ｜ 人声闪避 %.2f" %
                 (music.get("source", "builtin"), float(music.get("gain", 0.15)),
                  float(music.get("duck_under_voice", 0.42))))
        L.append("  封面 %s ｜ 落幕 %s" %
                 ("开" if cinema.get("cover", True) else "关",
                  cinema.get("outro_text", "— 完 —")))
        L.append("")
        L.append("  分镜目录  %s" % base)
        L.append("  关键帧    %s" % m.get("keyframes_dir", "—"))
        text = "\n".join(L)
        print("[短剧配置]\n" + text, flush=True)
        return {"ui": {"text": [text]}, "result": (text,)}

    @classmethod
    def IS_CHANGED(cls, project):
        p = os.path.join(DRAMA_ROOT, project, "manifest.json")
        return os.path.getmtime(p) if os.path.isfile(p) else float("nan")


NODE_CLASS_MAPPINGS = {
    "DramaShotLoader": DramaShotLoader,
    "DramaManifestInfo": DramaManifestInfo,
    "DramaProjectPanel": DramaProjectPanel,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DramaShotLoader": "① 读取短剧分镜（镜头语言）",
    "DramaManifestInfo": "② 剧名摘要",
    "DramaProjectPanel": "③ 全剧配置总览（人物形象/配音/音效/插曲）",
}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

from .production_server import DramaRenderEpisode
NODE_CLASS_MAPPINGS["DramaRenderEpisode"] = DramaRenderEpisode
NODE_DISPLAY_NAME_MAPPINGS["DramaRenderEpisode"] = "④ 全动态短剧制作（后台队列）"
WEB_DIRECTORY = './web'
from .production_nodes import NODE_CLASS_MAPPINGS as PRODUCTION_NODES, NODE_DISPLAY_NAME_MAPPINGS as PRODUCTION_NAMES
NODE_CLASS_MAPPINGS.update(PRODUCTION_NODES)
NODE_DISPLAY_NAME_MAPPINGS.update(PRODUCTION_NAMES)
from .pipeline_nodes import NODE_CLASS_MAPPINGS as PIPELINE_NODES, NODE_DISPLAY_NAME_MAPPINGS as PIPELINE_NAMES
NODE_CLASS_MAPPINGS.update(PIPELINE_NODES)
NODE_DISPLAY_NAME_MAPPINGS.update(PIPELINE_NAMES)
