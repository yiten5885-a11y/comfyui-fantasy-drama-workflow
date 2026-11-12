"""Local production queue attached to ComfyUI; workers never block its executor."""
import hashlib
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import psutil

from aiohttp import web
import folder_paths
from server import PromptServer
from drama.workflow_contract import configure, input_payloads, load_storyboard
from drama.workflow_build import require_inference, build_status

APP = Path(__file__).resolve().parents[2]
INPUT = Path(folder_paths.get_input_directory()) / 'drama'
JOBS = Path(folder_paths.get_output_directory()) / 'drama_jobs'
JOBS.mkdir(exist_ok=True)
PROCESSES = {}
BACKEND_ID = '%s-%s' % (os.getpid(), time.time_ns())


def write_json(path, value):
    tmp = path.with_suffix('.tmp'); tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2)); tmp.replace(path)


def contained(root, name):
    if not re.fullmatch(r'[\w.-]{1,100}', name) or name in ('.', '..'):
        raise ValueError('无效名称')
    path = (root / name).resolve(); path.relative_to(root.resolve()); return path


def projects():
    return sorted(p.name for p in INPUT.iterdir() if p.is_dir() and ((p / 'storyboard.json').is_file() or (p / 'shots.csv').is_file()))


def launch(folder):
    require_inference()
    if (folder / 'stop_requested.json').exists():raise RuntimeError('任务已停止，请使用恢复制作')
    current = PROCESSES.get(folder.name)
    if current is not None and current.poll() is None:return
    state = json.loads((folder / 'state.json').read_text())
    if state.get('pid'):
        try:
            command = psutil.Process(int(state['pid'])).cmdline()
            if '--job' in command and str(folder) in command:return
        except psutil.NoSuchProcess:pass
    with (folder / 'worker.log').open('a') as log:
        process = subprocess.Popen([sys.executable, str(APP / 'drama/production.py'), '--job', str(folder)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    PROCESSES[folder.name] = process


def submit(project, preset='preview', overrides=None, workflow=None):
    require_inference()
    source = contained(INPUT, project)
    if project not in projects():raise ValueError('找不到分镜项目')
    from drama.workflow_graph import DEFAULT_API, capture, capture_animate2, graph_settings
    base = json.loads((APP / 'drama/production_config.json').read_text())
    if workflow is None and DEFAULT_API.is_file():workflow = json.loads(DEFAULT_API.read_text())
    graph, graph_sha = capture(workflow) if workflow is not None else (None, '')
    settings = graph_settings(graph, base) if graph is not None else {}
    animate2_graph, animate2_sha = capture_animate2()
    for section, values in (overrides or {}).items():settings.setdefault(section, {}).update(values)
    if preset == 'preview' and graph is not None:
        from drama.workflow_contract import VIDEO_PRESETS
        settings['dynamic'].update(VIDEO_PRESETS['preview'])
    cfg = configure(base, preset, settings)
    if graph is not None:
        cfg['workflow'] = {'sha256': graph_sha, 'animate2_sha256': animate2_sha, 'source': 'expanded_canvas'}
    load_storyboard(source, cfg)
    payloads = input_payloads(source)
    digest = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode() + b''.join(name.encode() + payloads[name] for name in sorted(payloads))).hexdigest()
    job_id = digest[:20]; folder = contained(JOBS, job_id)
    if not folder.exists():
        folder.mkdir(); (folder / 'input').mkdir()
        for name, data in payloads.items():(folder / 'input' / name).write_bytes(data)
        write_json(folder / 'config.json', cfg)
        if graph is not None:write_json(folder / 'workflow_api.json', graph)
        if cfg.get('animation', {}).get('enabled'):write_json(folder / 'animate2_workflow_api.json', animate2_graph)
        write_json(folder / 'state.json', {'job_id': job_id, 'project': project, 'preset': preset, 'fingerprint': digest, 'status': 'queued', 'phase': 'queued', 'created_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'shots': {}, 'error': ''})
    state = json.loads((folder / 'state.json').read_text())
    if state['status'] not in ('complete', 'needs_attention', 'stopped'):launch(folder)
    return {'job_id': job_id, 'status': state['status'], 'url': '/drama/production?job=' + job_id}


routes = PromptServer.instance.routes


@routes.get('/drama/production')
async def page(request):
    return web.FileResponse(Path(__file__).with_name('production.html'))


@routes.get('/api/drama/projects')
async def list_projects(request):
    cfg = json.loads((APP / 'drama/production_config.json').read_text())
    return web.json_response({'projects': projects(), 'default_project': cfg.get('default_project')})


@routes.get('/api/drama/runtime')
async def runtime(request):
    manager = sys.modules.get('comfy.model_management')
    loaded = len(manager.current_loaded_models) if manager is not None else None
    return web.json_response({'backend_id': BACKEND_ID, 'loaded_model_count': loaded})


@routes.get('/api/drama/build')
async def build_state(request):
    ready, reason = build_status()
    return web.json_response({'inference_allowed': ready, 'reason': reason})


@routes.get('/api/drama/models')
async def model_progress(request):
    path = Path(folder_paths.models_dir) / '.wan22-download/progress.json'
    return web.json_response(json.loads(path.read_text()) if path.exists() else {'status': 'not_started'})


@routes.get('/api/drama/jobs')
async def list_jobs(request):
    result = []
    for path in sorted(JOBS.glob('*/state.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
        result.append(json.loads(path.read_text()))
    return web.json_response({'jobs': result})


@routes.get('/api/drama/jobs/{job_id}')
async def get_job(request):
    try:
        folder = contained(JOBS, request.match_info['job_id'])
        return web.json_response(json.loads((folder / 'state.json').read_text()))
    except (ValueError, OSError) as e:return web.json_response({'error': str(e)}, status=404)


@routes.post('/api/drama/jobs/{job_id}/stop')
async def stop_job(request):
    try:
        folder = contained(JOBS, request.match_info['job_id']); state = json.loads((folder / 'state.json').read_text())
        if state['status'] == 'complete':return web.json_response({'job_id': folder.name, 'status': 'complete'})
        write_json(folder / 'stop_requested.json', {'requested_at': time.time(), 'reason': 'user_stop'})
        state.update(status='stopped', error='制作已停止，成功步骤保留；可恢复后继续')
        write_json(folder / 'state.json', state)
        current = PROCESSES.get(folder.name)
        pid = state.get('pid') or (current.pid if current is not None and current.poll() is None else None)
        if pid:
            try:
                worker = psutil.Process(pid)
                command = worker.cmdline()
                if '--job' not in command or str(folder) not in command:raise ValueError('后台进程不属于本任务，未终止')
                worker.terminate()
                for _ in range(50):
                    if current is not None:current.poll()
                    if not worker.is_running() or worker.status() == psutil.STATUS_ZOMBIE:break
                    await asyncio.sleep(0.1)
                else:raise ValueError('停止请求已保存，后台进程尚未退出')
            except psutil.NoSuchProcess:pass
        queue = PromptServer.instance.prompt_queue
        running, pending = queue.get_current_queue()
        for item in pending:
            if item[3].get('drama_job') == folder.name:
                queue.delete_queue_item(lambda value, prompt=item[1]: value[1] == prompt)
        if any(item[3].get('drama_job') == folder.name for item in running):
            from nodes import interrupt_processing
            interrupt_processing()
        state = json.loads((folder / 'state.json').read_text())
        state.update(status='stopped', pid=None, error='制作已停止，成功步骤保留；当前在途模型如未结束，正在中断', updated_at=time.strftime('%Y-%m-%d %H:%M:%S'))
        write_json(folder / 'state.json', state)
        return web.json_response({'job_id': folder.name, 'status': 'stopped'})
    except (ValueError, OSError, psutil.Error) as e:return web.json_response({'error': str(e)}, status=400)


@routes.post('/api/drama/jobs')
async def start_job(request):
    try:
        data = await request.json(); return web.json_response(submit(data['project'], data.get('preset', 'preview'), data.get('overrides')))
    except RuntimeError as e:return web.json_response({'error': str(e)}, status=423)
    except (ValueError, KeyError, OSError) as e:return web.json_response({'error': str(e)}, status=400)


@routes.post('/api/drama/jobs/{job_id}/resume')
async def resume(request):
    try:
        require_inference()
        folder = contained(JOBS, request.match_info['job_id']); state = json.loads((folder / 'state.json').read_text())
        stopping = (folder / 'stop_requested.json').exists() or state['status'] == 'stopped'
        if stopping:
            running, pending = PromptServer.instance.prompt_queue.get_current_queue()
            if any(item[3].get('drama_job') == folder.name for item in running + pending):
                return web.json_response({'error': '原模型任务正在停止，请结束后再恢复'}, status=409)
        if state['status'] in ('needs_attention', 'stopped'):
            retries = json.loads((folder / 'config.json').read_text())['quality']['max_retries'] + 1
            for shot in state.get('shots', {}).values():
                for stage in shot.values():
                    if stage.get('status') == 'interrupted':
                        stage.update(status='failed', interrupted_prompt_id=stage.pop('prompt_id', None))
                    elif stopping and stage.get('status') in ('running', 'unknown', 'submitting'):
                        stage['resume_after_interrupt'] = True
                    if stage.get('status') == 'failed':
                        stage['allowed_attempts'] = stage.get('attempts', 0) + retries
            (folder / 'stop_requested.json').unlink(missing_ok=True)
            state.update(status='queued', pid=None, retry_round=state.get('retry_round', 0) + 1)
            write_json(folder / 'state.json', state)
        launch(folder); return web.json_response({'job_id': folder.name})
    except RuntimeError as e:return web.json_response({'error': str(e)}, status=423)
    except (ValueError, OSError) as e:return web.json_response({'error': str(e)}, status=400)


@routes.post('/api/drama/upload')
async def upload(request):
    files = {}; reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:break
        name = part.filename or ''
        is_end_frame = bool(re.fullmatch(r'[\w.-]{1,100}\.(?:png|jpe?g|webp)', name, re.I))
        if name not in ('storyboard.json', 'shots.csv', 'characters.json') and not is_end_frame:
            return web.json_response({'error': '分镜文件请使用 storyboard.json、shots.csv、characters.json；可附加分镜中引用的 S01_end.png 等尾帧图'}, status=400)
        content = await part.read()
        limit = 20 * 1024 * 1024 if is_end_frame else 2 * 1024 * 1024
        if len(content) > limit:return web.json_response({'error': '单张尾帧图上限 20 MB，单个分镜文件上限 2 MB'}, status=400)
        if name.endswith('.json'):
            try:json.loads(content.decode('utf-8-sig'))
            except (ValueError, UnicodeError):return web.json_response({'error': name + ' 不是有效 JSON'}, status=400)
        files[name] = content
    if not any(n in files for n in ('storyboard.json', 'shots.csv')):
        return web.json_response({'error': '需要分镜 JSON 或 CSV'}, status=400)
    name = 'upload-' + time.strftime('%Y%m%d-%H%M%S') + '-' + os.urandom(3).hex()
    folder = INPUT / name; folder.mkdir()
    for filename, content in files.items():(folder / filename).write_bytes(content)
    return web.json_response({'project': name})


class DramaRenderEpisode:
    @classmethod
    def INPUT_TYPES(cls):return {'required': {'project': (projects(),), 'preset': (['preview', 'production'],)}}
    RETURN_TYPES = ('STRING',)
    RETURN_NAMES = ('job_url',)
    FUNCTION = 'submit'
    CATEGORY = '短剧/全动态流水线'
    OUTPUT_NODE = True

    def submit(self, project, preset):
        result = submit(project, preset); url = 'http://127.0.0.1:8188' + result['url']
        return {'ui': {'text': ['已提交全动态短剧任务。进度和结果：' + url]}, 'result': (url,)}

    @classmethod
    def IS_CHANGED(cls, **kwargs):return float('nan')


async def recover_workers(app):
    async def monitor():
        while True:
            if not build_status()[0]:
                await asyncio.sleep(15)
                continue
            for path in JOBS.glob('*/state.json'):
                if (path.parent / 'stop_requested.json').exists():continue
                state = json.loads(path.read_text())
                if state.get('status') not in ('queued', 'running'):continue
                pid = state.get('pid')
                if pid:
                    try:
                        command = psutil.Process(pid).cmdline()
                        if '--job' in command and str(path.parent) in command:continue
                    except psutil.NoSuchProcess:pass
                if not pid and time.time() - path.stat().st_mtime < 20:continue
                count = state.get('worker_restarts', 0)
                if count >= 2:
                    state.update(status='needs_attention', error='后台进程连续退出，保留原镜头提交记录，详见 worker.log')
                else:
                    state.update(pid=None, worker_restarts=count + 1)
                write_json(path, state)
                if state['status'] != 'needs_attention':launch(path.parent)
            await asyncio.sleep(15)
    app['drama_monitor'] = asyncio.create_task(monitor())


async def stop_monitor(app):
    app['drama_monitor'].cancel()


PromptServer.instance.app.on_startup.append(recover_workers)
PromptServer.instance.app.on_cleanup.append(stop_monitor)
