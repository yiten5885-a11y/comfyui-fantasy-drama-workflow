#!/usr/bin/env python3
"""Serve the production page with demo data: no ComfyUI, no models, no GPU.

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


def demo_image(path):
    from PIL import Image
    im = Image.new('RGB', (270, 480), color=(30, 80, 70))
    im.save(str(path))


def demo_video(path):
    import av
    import numpy as np
    with av.open(str(path), 'w') as container:
        stream = container.add_stream('h264', rate=12)
        stream.width, stream.height, stream.pix_fmt = 270, 480, 'yuv420p'
        for index in range(36):
            frame = np.zeros((480, 270, 3), dtype=np.uint8)
            frame[..., 1] = np.linspace(40, 200, 480, dtype=np.uint8)[:, None]
            frame[..., 2] = (index * 6) % 255
            for packet in stream.encode(av.VideoFrame.from_ndarray(frame, format='rgb24')):container.mux(packet)
        for packet in stream.encode():container.mux(packet)


def seed(work, server):
    project = server.INPUT / 'example-fantasy-story';project.mkdir(parents=True, exist_ok=True)
    for name in ('storyboard.json', 'shots.csv', 'characters.json'):
        (project / name).write_bytes((ROOT / 'examples' / name).read_bytes())
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    done = {'status': 'complete', 'attempts': 1, 'vision': {'pass': True, 'issues': []}}
    jobs = [
        {'job_id': 'a1f3c59be20d4471c0aa', 'preset': 'preview', 'status': 'running', 'phase': 'video_transfer', 'current_shot': 'S02',
         'shots': {'S01': {'image': done, 'video': done, 'video_transfer': {'status': 'complete', 'attempts': 1}},
                   'S02': {'image': {**done, 'attempts': 2}, 'video': {'status': 'complete', 'attempts': 1}}}, 'error': ''},
        {'job_id': '7be0d2a4419c03f5e6d8', 'preset': 'production', 'status': 'needs_attention', 'phase': 'video', 'current_shot': 'S02', 'retry_round': 1,
         'shots': {'S01': {'image': done, 'video': done},
                   'S02': {'image': done, 'video': {'status': 'failed', 'attempts': 3, 'vision': {'pass': False},
                           'quality_issues': ['角色肩甲在中段消失，服装与关键帧不一致', '落地动作未完成，尾帧仍为站立姿态']}}},
         'error': 'S02 / video 已达到重试上限，保留全部失败证据'},
        {'job_id': '03c9e7715ab24d0f9e12', 'preset': 'preview', 'status': 'complete', 'phase': 'complete', 'current_shot': 'S02',
         'shots': {'S01': {'image': done, 'video': done}, 'S02': {'image': done, 'video': done}}, 'error': '',
         'qa': {'frames': 36, 'duration': 3.0, 'width': 270, 'height': 480, 'audio': False},
         'delivery_dir': str(work / 'delivery/云海剑台演示_03c9e7715ab24d0f9e12')},
    ]
    for index, job in enumerate(jobs):
        folder = server.JOBS / job['job_id'];folder.mkdir(parents=True, exist_ok=True)
        art_dir = folder / 'artifacts'; art_dir.mkdir(parents=True, exist_ok=True)
        demo_image(art_dir / 'S01_image_a1.png')
        demo_image(art_dir / 'S02_image_a1.png')
        demo_video(art_dir / 'S01_video_a1.mp4')
        demo_video(art_dir / 'S02_video_a1.mp4')
        if 'video_transfer' in job['shots'].get('S01', {}):
            demo_video(art_dir / 'S01_video_transfer_a1.mp4')
            job['shots']['S01']['video_transfer']['path'] = str(art_dir / 'S01_video_transfer_a1.mp4')
        job['shots']['S01']['image']['path'] = str(art_dir / 'S01_image_a1.png')
        job['shots']['S01']['video']['path'] = str(art_dir / 'S01_video_a1.mp4')
        job['shots']['S02']['image']['path'] = str(art_dir / 'S02_image_a1.png')
        job['shots']['S02']['video']['path'] = str(art_dir / 'S02_video_a1.mp4')
        job['shots']['S02']['video']['driver_path'] = str(art_dir / 'S02_video_a1.mp4')
        job.update(project='example-fantasy-story', title='云海剑台演示', shot_ids=['S01', 'S02'], created_at=now, updated_at=now)
        (folder / 'state.json').write_text(json.dumps(job, ensure_ascii=False))
        (folder / 'worker.log').write_text('RETRY S02 video 动态视频视觉复检未通过\nHold S02 / video 已达到重试上限，保留全部失败证据\n')
    demo_video(server.JOBS / jobs[2]['job_id'] / 'episode.mp4')


def main():
    ap = argparse.ArgumentParser(description='用演示数据预览制作页面')
    ap.add_argument('--port', type=int, default=8199)
    ap.add_argument('--ready', action='store_true', help='模拟全部就绪且门禁开启的状态')
    ap.add_argument('--say', action='store_true', help='使用 macOS say 配音，默认使用提示音测试引擎')
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        base = json.loads((ROOT / 'drama/production_config.json').read_text())
        if not args.say:
            base['voice']['engine'] = 'command'
            base['voice']['command'] = [sys.executable, str(ROOT / 'tests/fake_tts.py'), '--voice', '{voice}',
                                        '--out', '{out}', '--rate', '{rate}', '--text', '{text}']
            base['voice']['command_ext'] = 'wav'
        models = {}
        if args.ready:
            from drama import readiness
            base['quality']['vision'].update(model='local-vision-model', url='http://127.0.0.1:%d/v1/chat/completions' % args.port)
            base['subtitle']['font'] = str(ROOT / 'LICENSE');base['delivery']['dir'] = str(work / 'delivery')
            for kind, name in readiness.required_models(base):models.setdefault(kind, []).append(name)
        server = fake_comfy.install(work, models)
        if args.ready:
            server.base_config = lambda: json.loads(json.dumps(base))
            server.build_status = lambda: (True, '工作流构建证书有效（预览模式：不会真的提交任务）')
            checks = server.readiness.checks
            server.readiness.checks = lambda *a, **k: checks(*a, **{**k, 'platform': 'darwin', 'which': lambda name: sys.executable if name == sys.executable else '/usr/bin/say'})
        else:
            server.base_config = lambda: json.loads(json.dumps(base))
        seed(work, server)
        app = fake_comfy.application(server)

        async def view(request):
            path = server.JOBS / request.query.get('subfolder', '').split('/')[-1] / request.query.get('filename', '')
            return web.FileResponse(path) if path.is_file() else web.Response(status=404)
        app.router.add_get('/view', view)
        print('预览地址：http://127.0.0.1:%d/drama/production' % args.port, flush=True)
        web.run_app(app, host='127.0.0.1', port=args.port, print=None)


if __name__ == '__main__':main()
