#!/usr/bin/env python3
"""剧本分镜 CSV -> ComfyUI 批量出图（Z-Image-Turbo）

把短剧剧本先拆成逐镜头 CSV，本脚本负责：角色锚点前缀拼装、seed 管理、
逐镜头替换官方 Z-Image-Turbo API 工作流、提交 /prompt、轮询结果、写回台账。

用法（在任意目录执行均可）：
  python3 drama/batch.py --dry-run      # 只预览提示词与 seed
  python3 drama/batch.py --limit 2      # 实跑前 2 个镜头
  python3 drama/batch.py                # 跑全部（自动断点续跑）
  python3 drama/batch.py --no-resume    # 强制重跑全部

工作流参数与 Comfy-Org 官方 image_z_image_turbo 模板一致：
CLIPLoader=lumina2 / shift=3 / 8 步 / cfg 1 / res_multistep+simple /
ConditioningZeroOut / VAE=ae.safetensors。
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.request
if __package__:
    from .workflow_build import require_inference
else:
    from workflow_build import require_inference

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.environ.get("COMFYUI_OUTPUT_DIR", os.path.join(ROOT, "..", "output"))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

REQUIRED_COLUMNS = ("shot_id", "description")
DEFAULT_SIZE = (1024, 576)  # 16:9，便于后续送进图生视频模型；Z-Image 也可用 1024x1024
LEDGER_FIELDS = ("shot_id", "character", "scene", "dialogue", "duration_sec",
                 "prompt", "seed", "width", "height", "status",
                 "image_path", "elapsed_sec", "error", "finished_at", "prompt_id")


# ---------- ComfyUI API ----------
def api_get(host, path, timeout=30):
    with OPENER.open(host + path, timeout=timeout) as r:
        return json.load(r)


def api_post(host, path, data, timeout=30):
    if path in ('/prompt', '/v1/chat/completions'):
        require_inference()
    req = urllib.request.Request(
        host + path, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read() or b'{}')


def preflight(host):
    info = api_get(host, "/object_info")
    wanted = {
        "UNETLoader": ("unet_name", "z_image_turbo_bf16.safetensors"),
        "CLIPLoader": ("clip_name", "qwen_3_4b.safetensors"),
        "VAELoader": ("vae_name", "ae.safetensors"),
    }
    for node, (key, name) in wanted.items():
        if name not in info[node]["input"]["required"][key][0]:
            raise RuntimeError(f"{node}.{key} 找不到 {name}，请确认权重已放入 models 目录")
    samplers = info["KSampler"]["input"]["required"]["sampler_name"][0]
    sampler = "res_multistep" if "res_multistep" in samplers else "euler"
    print(f"[检查] 三个权重就位，采样器 {sampler}", flush=True)
    return sampler


def build_workflow(prompt, seed, width, height, prefix, sampler, vae):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "4": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["3", 0]}},
        "5": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "6": {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["5", 0], "seed": seed, "steps": 8, "cfg": 1.0,
            "sampler_name": sampler, "scheduler": "simple",
            "positive": ["3", 0], "negative": ["4", 0],
            "latent_image": ["6", 0], "denoise": 1.0}},
        "8": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["8", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {
            "images": ["9", 0], "filename_prefix": prefix}},
    }


# ---------- 分镜 / 角色 ----------
def load_shots(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise RuntimeError(f"CSV 缺少必需列: {missing}；现有列 {reader.fieldnames}")
        rows = []
        for i, raw in enumerate(reader, 1):
            row = {k: (v or "").strip() for k, v in raw.items() if k}
            if not row.get("shot_id"):
                continue
            row["_index"] = i
            rows.append(row)
        return rows


def load_characters(path):
    with open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    return cfg.get("characters", {}), cfg.get("global_style", "")


GRADE_PROMPT = {
    "cool": "冷蓝色调，低饱和，青灰阴影",
    "warm": "暖橙色调，柔和肤色",
    "golden": "金色暖光，逆光轮廓，柔和高光",
    "noir": "高对比黑白影调，硬光",
    "neutral": "中性自然色调",
}


def make_prompt(shot, characters, global_style):
    """角色锚点 + 机位景别 + 画面 + 布光/色调 + 全局风格；台词绝不进画面。

    新增的电影级字段（camera/lighting/grade）会拼进提示词，让 27B 写的镜头语言
    真正作用到出图，而不只是留在 CSV 里给渲染器用。
    """
    parts = []
    names = [n.strip() for n in shot.get("character", "").split("|") if n.strip()]
    for name in names:
        c = characters.get(name)
        if not c:
            raise RuntimeError(f"角色表里没有「{name}」，请补进 characters.json")
        if c.get("anchor"):
            parts.append(c["anchor"])
    parts.append(shot.get("camera") or shot.get("scene") or "中景")
    if shot.get("scene") and shot.get("camera") and shot["scene"] not in shot["camera"]:
        parts.append(shot["scene"])
    if shot.get("description"):
        parts.append(shot["description"])
    if shot.get("lighting"):
        parts.append(shot["lighting"])
    grade = (shot.get("grade") or "").strip().lower()
    if grade in GRADE_PROMPT:
        parts.append(GRADE_PROMPT[grade])
    if shot.get("extra_prompt"):
        parts.append(shot["extra_prompt"])
    if global_style:
        parts.append(global_style)
    parts.append("画面中不要出现任何文字、字幕、水印、logo")
    return "，".join(parts)


def resolve_seed(shot, characters, index):
    """CSV 显式指定优先；否则取该角色 base_seed + 镜头序号，保证可复现。"""
    if shot.get("seed") not in ("", None):
        return int(shot["seed"])
    names = [n.strip() for n in shot.get("character", "").split("|") if n.strip()]
    base = 1000
    for name in names:
        c = characters.get(name, {})
        if c.get("base_seed") is not None:
            base = int(c["base_seed"])
            break
    return base + index


def resolve_size(shot):
    w = int(shot.get("width") or DEFAULT_SIZE[0])
    h = int(shot.get("height") or DEFAULT_SIZE[1])
    if w % 16 or h % 16:
        raise RuntimeError(f"宽高必须是 16 的倍数，当前 {w}x{h}")
    return w, h


def safe_prefix(shot_id, subfolder="drama"):
    clean = "".join(c if c.isalnum() or c in "-_" else "_" for c in shot_id)
    return (subfolder.strip("/") + "/" if subfolder.strip("/") else "") + clean


# ---------- 台账 ----------
def read_ledger(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["shot_id"]: r for r in csv.DictReader(f)}


def write_ledger(path, records):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        w.writeheader()
        for r in records.values():
            w.writerow({k: r.get(k, "") for k in LEDGER_FIELDS})
    os.replace(tmp, path)


# ---------- 单镜头执行 ----------
def render_one(host, shot, characters, global_style, sampler, vae, timeout_sec, subfolder="drama"):
    prompt = make_prompt(shot, characters, global_style)
    seed = resolve_seed(shot, characters, shot["_index"])
    w, h = resolve_size(shot)
    prefix = safe_prefix(shot["shot_id"], subfolder)
    wf = build_workflow(prompt, seed, w, h, prefix, sampler, vae)

    res = api_post(host, "/prompt", {"prompt": wf})
    pid = res.get("prompt_id")
    if res.get("node_errors"):
        raise RuntimeError("节点校验失败: " + json.dumps(res["node_errors"], ensure_ascii=False)[:400])
    if not pid:
        raise RuntimeError("未返回 prompt_id: " + str(res)[:300])

    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        hist = api_get(host, "/history/" + pid)
        if pid in hist:
            entry = hist[pid]
            st = entry.get("status", {})
            if st.get("completed") or st.get("status_str") == "error":
                imgs = []
                for out in entry.get("outputs", {}).values():
                    for img in out.get("images", []):
                        imgs.append(os.path.join(OUTPUT, img.get("subfolder", ""), img["filename"]))
                if st.get("status_str") == "error":
                    msgs = [str(m)[:200] for m in st.get("messages", [])[-3:]]
                    raise RuntimeError("执行失败: " + " | ".join(msgs))
                if not imgs:
                    raise RuntimeError("执行完成但没有图片输出")
                return {
                    "prompt": prompt, "seed": seed, "width": w, "height": h,
                    "status": "ok", "image_path": imgs[0],
                    "elapsed_sec": round(time.time() - t0, 1),
                    "error": "", "prompt_id": pid,
                }
        time.sleep(5)
    raise RuntimeError(f"超时 {timeout_sec}s 未完成 (prompt_id={pid})")


def main():
    ap = argparse.ArgumentParser(description="分镜 CSV 批量提交 ComfyUI 出图")
    ap.add_argument("--csv", default=os.path.join(ROOT, "shots.csv"))
    ap.add_argument("--characters", default=os.path.join(ROOT, "characters.json"))
    ap.add_argument("--ledger", default=os.path.join(ROOT, "ledger.csv"))
    ap.add_argument("--host", default="http://127.0.0.1:8188")
    ap.add_argument("--vae", default="ae.safetensors")
    ap.add_argument("--subfolder", default="drama", help="输出子目录（output/ 下），多剧集用不同值隔离")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个待跑镜头")
    ap.add_argument("--start", default="", help="从该 shot_id 开始（含）")
    ap.add_argument("--dry-run", action="store_true", help="只打印提示词与 seed，不提交")
    ap.add_argument("--no-resume", action="store_true", help="忽略台账，全部重跑")
    ap.add_argument("--stop-on-error", action="store_true")
    ap.add_argument("--timeout", type=int, default=1800, help="单镜头超时秒数")
    args = ap.parse_args()

    shots = load_shots(args.csv)
    characters, global_style = load_characters(args.characters)
    ledger = {} if args.no_resume else read_ledger(args.ledger)

    if args.start:
        ids = [s["shot_id"] for s in shots]
        if args.start not in ids:
            raise RuntimeError(f"--start {args.start} 不在 CSV 的 shot_id 中")
        shots = shots[ids.index(args.start):]

    print(f"[载入] {len(shots)} 个镜头 | 角色 {len(characters)} 个 | 全局风格: {global_style or '(无)'}", flush=True)

    if not args.dry_run:
        sampler = preflight(args.host)
    else:
        sampler = "res_multistep"

    todo, skipped = [], 0
    for s in shots:
        old = ledger.get(s["shot_id"])
        if not args.no_resume and old and old.get("status") == "ok" and os.path.exists(old.get("image_path", "")):
            skipped += 1
            continue
        todo.append(s)
        if args.limit and len(todo) >= args.limit:
            break
    print(f"[计划] 待处理 {len(todo)} 个，跳过已完成 {skipped} 个"
          + ("（dry-run）" if args.dry_run else ""), flush=True)

    failures = 0
    for n, shot in enumerate(todo, 1):
        sid = shot["shot_id"]
        print(f"\n--- [{n}/{len(todo)}] {sid} ---", flush=True)
        try:
            prompt = make_prompt(shot, characters, global_style)
            seed = resolve_seed(shot, characters, shot["_index"])
            w, h = resolve_size(shot)
            print(f"  seed={seed}  {w}x{h}", flush=True)
            print(f"  提示词: {prompt}", flush=True)
            base = {k: shot.get(k, "") for k in
                    ("shot_id", "character", "scene", "dialogue", "duration_sec")}
            if args.dry_run:
                continue
            t = time.time()
            rec = render_one(args.host, shot, characters, global_style,
                             sampler, args.vae, args.timeout, args.subfolder)
            rec.update(base)
            rec["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            ledger[sid] = rec
            write_ledger(args.ledger, ledger)
            print(f"  [完成] {rec['image_path']}  耗时 {rec['elapsed_sec']}s", flush=True)
        except Exception as exc:
            failures += 1
            print(f"  [失败] {exc}", flush=True)
            ledger[sid] = {
                **{k: shot.get(k, "") for k in
                   ("shot_id", "character", "scene", "dialogue", "duration_sec")},
                "status": "error", "error": str(exc)[:500],
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            write_ledger(args.ledger, ledger)
            if args.stop_on_error:
                return 1

    if not args.dry_run:
        ok = sum(1 for r in ledger.values() if r.get("status") == "ok")
        print(f"\n[汇总] 台账 {args.ledger} | 成功 {ok} | 失败 "
              f"{sum(1 for r in ledger.values() if r.get('status') == 'error')}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
