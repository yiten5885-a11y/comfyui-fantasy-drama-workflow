#!/usr/bin/env python3
"""Serve the production page with demo data: no ComfyUI, no models, no GPU.

Voice buttons use tests/fake_tts.py (a tone, not speech) unless --say is given on macOS.

    python tools/preview_page.py            # first-run state: gate closed, nothing configured
    python tools/preview_page.py --ready    # everything configured, gate shown as open

For working on production.html. Demo jobs live in a temporary folder; the
buttons that would start work answer the way a closed gate does.
"""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import fake_comfy


def demo_frame(width, height, hue, phase=0.0):
    import numpy as np
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    frame = np.zeros((height, width, 3), dtype=np.float32)
    frame[..., 0] = 30 + 60 * hue + 40 * np.sin(yy / height * 3 + phase)
    frame[..., 1] = 60 + 90 * (yy / height)
    frame[..., 2] = 120 + 80 * np.cos(xx / width * 2 - phase)
    cx, cy = width * (0.5 + 0.15 * np.sin(phase)), height * 0.55
    figure = ((xx - cx) / (width * 0.12)) ** 2 + ((yy - cy) / (height * 0.3)) ** 2 < 1
    frame[figure] = (235, 240, 230)
    return frame.clip(0, 255).astype(np.uint8)


def demo_video(path, width=270, height=480, frames=36, hue=0.5):
    import av
    with av.open(str(path), 'w') as container:
        stream = container.add_stream('h264', rate=12)
        stream.width, stream.height, stream.pix_fmt = width, height, 'yuv420p'
        for index in range(frames):
            frame = demo_frame(width, height, hue, index / frames * 3)
            for packet in stream.encode(av.VideoFrame.from_ndarray(frame, format='rgb24')):container.mux(packet)
        for packet in stream.encode():container.mux(packet)


def demo_assets(folder, shot_id, hue, transfer=True):
    """A keyframe, a motion draft and a transfer clip for one demo shot."""
    from PIL import Image
    art = folder / 'artifacts';art.mkdir(parents=True, exist_ok=True)
    image = art / ('%s_image_a1_00001_.png' % shot_id)
    Image.fromarray(demo_frame(368, 656, hue)).save(image)
    draft = art / ('%s_video_a1.mp4' % shot_id);demo_video(draft, 240, 432, 24, hue)
    paths = {'image': str(image), 'draft': str(draft)}
    if transfer:
        clip = art / ('%s_video_transfer_a1.mp4' % shot_id);demo_video(clip, 240, 416, 24, hue + 0.2);paths['video'] = str(clip)
    return paths


def seed(work, server):
    project = server.INPUT / 'example-fantasy-story';project.mkdir(parents=True, exist_ok=True)
    for name in ('storyboard.json', 'shots.csv', 'characters.json'):
        (project / name).write_bytes((ROOT / 'examples' / name).read_bytes())
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    done = {'status': 'complete', 'attempts': 1, 'vision': {'pass': True, 'issues': []}}
    def assets(job_id, shot_id, hue, transfer=True):
        paths = demo_assets(server.JOBS / job_id, shot_id, hue, transfer)
        image = {**done, 'path': paths['image'], 'sha256': shot_id + job_id[:4]}
        video = {**done, 'path': paths.get('video', paths['draft']), 'sha256': 'v' + shot_id + job_id[:4]}
        if transfer:video.update(driver_path=paths['draft'], driver_sha256='d' + shot_id)
        return image, video
    running, failing, finished = 'a1f3c59be20d4471c0aa', '7be0d2a4419c03f5e6d8', '03c9e7715ab24d0f9e12'
    r1, r2, f1, f2, d1, d2, d3 = (assets(running, 'S01', 0.3), assets(running, 'S02', 0.6), assets(failing, 'S01', 0.3, False),
                                   assets(failing, 'S02', 0.6, False), assets(finished, 'S01', 0.3), assets(finished, 'S02', 0.6), assets(finished, 'S03', 0.9))
    jobs = [
        {'job_id': running, 'preset': 'preview', 'status': 'running', 'phase': 'video_transfer', 'current_shot': 'S02',
         'shots': {'S01': {'image': r1[0], 'video': r1[1], 'video_transfer': {'status': 'complete', 'attempts': 1}},
                   'S02': {'image': {**r2[0], 'attempts': 2}, 'video': {'status': 'complete', 'attempts': 1}}}, 'error': ''},
        {'job_id': failing, 'preset': 'production', 'status': 'needs_attention', 'phase': 'video', 'current_shot': 'S02', 'retry_round': 1,
         'shots': {'S01': {'image': f1[0], 'video': f1[1]},
                   'S02': {'image': f2[0], 'video': {**f2[1], 'status': 'failed', 'attempts': 3, 'vision': {'pass': False},
                           'quality_issues': ['角色肩甲在中段消失，服装与关键帧不一致', '落地动作未完成，尾帧仍为站立姿态']}}},
         'error': 'S02 / video 已达到重试上限，保留全部失败证据'},
        {'job_id': finished, 'preset': 'preview', 'status': 'complete', 'phase': 'complete', 'current_shot': 'S03',
         'shots': {'S01': {'image': d1[0], 'video': d1[1]}, 'S02': {'image': d2[0], 'video': d2[1]}, 'S03': {'image': d3[0], 'video': d3[1]}}, 'error': '',
         'qa': {'frames': 36, 'duration': 3.0, 'width': 270, 'height': 480, 'audio': False},
         'delivery_dir': str(work / 'delivery/云海剑台演示_03c9e7715ab24d0f9e12')},
    ]
    for index, job in enumerate(jobs):
        folder = server.JOBS / job['job_id'];folder.mkdir(parents=True, exist_ok=True)
        job.update(project='example-fantasy-story', title='云海剑台演示', shot_ids=list(job['shots']) if index == 2 else ['S01', 'S02', 'S03'], created_at=now, updated_at=now)
        (folder / 'state.json').write_text(json.dumps(job, ensure_ascii=False))
        (folder / 'worker.log').write_text('RETRY S02 video 动态视频视觉复检未通过\nHold S02 / video 已达到重试上限，保留全部失败证据\n')
    demo_video(server.JOBS / jobs[2]['job_id'] / 'episode.mp4')


def main():
    ap = argparse.ArgumentParser(description='用演示数据预览制作页面')
    ap.add_argument('--port', type=int, default=8199)
    ap.add_argument('--ready', action='store_true', help='模拟全部就绪且门禁开启的状态')
    ap.add_argument('--say', action='store_true', help='在 macOS 上用真实的 say 配音试听（默认用测试音代替）')
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        base = json.loads((ROOT / 'drama/production_config.json').read_text())
        if not args.say:
            base['voice'].update(engine='command', command=[sys.executable, str(ROOT / 'tests/fake_tts.py'), '{out}', '{voice}', '{rate}'])
        models = {}
        if args.ready:
            from drama import readiness
            base['quality']['vision'].update(model='local-vision-model', url='http://127.0.0.1:%d/v1/chat/completions' % args.port)
            base['subtitle']['font'] = str(ROOT / 'LICENSE');base['delivery']['dir'] = str(work / 'delivery')
            for kind, name in readiness.required_models(base):models.setdefault(kind, []).append(name)
        server = fake_comfy.install(work, models)
        server.base_config = lambda: json.loads(json.dumps(base))
        if args.ready:
            server.build_status = lambda: (True, '工作流构建证书有效（预览模式：不会真的提交任务）')
            checks = server.readiness.checks
            server.readiness.checks = lambda *a, **k: checks(*a, **{**k, 'platform': 'darwin', 'which': lambda name: '/usr/bin/say'})
        seed(work, server)
        app = fake_comfy.application(server)

        async def view(request):
            path = server.JOBS / request.query.get('subfolder', '').split('/')[-1] / request.query.get('filename', '')
            return web.FileResponse(path) if path.is_file() else web.Response(status=404)
        app.router.add_get('/view', view)
        print('预览地址：http://127.0.0.1:%d/drama/production' % args.port, flush=True)
        web.run_app(app, host='127.0.0.1', port=args.port, print=None)


if __name__ == '__main__':main()
