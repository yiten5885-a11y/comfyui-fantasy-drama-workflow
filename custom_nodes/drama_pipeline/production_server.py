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
from drama.workflow_contract import VIDEO_PRESETS, configure, input_payloads, load_storyboard, storyboard_summary
from drama.workflow_build import __version__, require_inference, build_status, certificate_status
from drama import readiness, voice as voicekit

APP = Path(__file__).resolve().parents[2]
INPUT = Path(folder_paths.get_input_directory()) / 'drama'
JOBS = Path(folder_paths.get_output_directory()) / 'drama_jobs'
EXAMPLES = APP / 'examples'
# A fresh ComfyUI has neither folder; create both so the first visit works.
INPUT.mkdir(parents=True, exist_ok=True)
JOBS.mkdir(parents=True, exist_ok=True)
PREVIEW = Path(folder_paths.get_output_directory()) / 'drama_preview'
PROCESSES = {}
_VOICE_STATUS = {'at': 0.0, 'value': None}
BACKEND_ID = '%s-%s' % (os.getpid(), time.time_ns())


def write_json(path, value):
    tmp = path.with_suffix('.tmp'); tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2)); tmp.replace(path)


def contained(root, name):
    if not re.fullmatch(r'[\w.-]{1,100}', name) or name in ('.', '..'):
        raise ValueError('无效名称')
    path = (root / name).resolve(); path.relative_to(root.resolve()); return path


def projects():
    return sorted(p.name for p in INPUT.iterdir() if p.is_dir() and ((p / 'storyboard.json').is_file() or (p / 'shots.csv').is_file()))


def base_config():
    return json.loads((APP / 'drama/production_config.json').read_text())


def effective_config(preset='production'):
    """The config a job submitted from the page would run with (canvas defaults applied)."""
    from drama.workflow_graph import DEFAULT_API, capture, graph_settings
    base = base_config()
    try:
        graph, _ = capture(json.loads(DEFAULT_API.read_text()))
        settings = graph_settings(graph, base)
        if preset == 'preview':settings['dynamic'].update(VIDEO_PRESETS['preview'])
        return configure(base, preset, settings)
    except (OSError, ValueError, KeyError):return configure(base, preset)


def workflow_node_types():
    """(node types the API graphs use, node types this ComfyUI registered); None outside ComfyUI."""
    registry = getattr(sys.modules.get('nodes'), 'NODE_CLASS_MAPPINGS', None)
    if registry is None:return None
    from drama.workflow_graph import DEFAULT_API, ANIMATE2_API
    try:wanted = {node['class_type'] for path in (DEFAULT_API, ANIMATE2_API) for node in json.loads(path.read_text()).values()}
    except (OSError, ValueError, KeyError):return None
    return wanted, set(registry)


def service_reachable(url, timeout=0.6):
    import socket
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)), timeout=timeout):return True
    except (OSError, TypeError, ValueError):return False


def log_tail(path, lines=80, limit=64 * 1024):
    if not path.is_file():return ''
    with path.open('rb') as f:
        f.seek(0, os.SEEK_END);size = f.tell();f.seek(max(0, size - limit))
        text = f.read().decode('utf-8', 'replace')
    return '\n'.join(text.splitlines()[-lines:])


def voice_status(cfg, max_age=30):
    """Engine status, cached briefly: listing macOS voices takes a moment."""
    key = json.dumps(cfg.get('voice', {}), sort_keys=True, ensure_ascii=False)
    if _VOICE_STATUS['value'] is None or _VOICE_STATUS.get('key') != key or time.time() - _VOICE_STATUS['at'] > max_age:
        _VOICE_STATUS.update(at=time.time(), key=key, value=voicekit.engine_status(cfg))
    return _VOICE_STATUS['value']


def project_board(name, cfg):
    source = contained(INPUT, name)
    if name not in projects():raise LookupError('找不到分镜项目')
    board, shots, characters, style = load_storyboard(source, cfg)
    return source, board, shots, characters


def preview_path(digest, suffix):
    PREVIEW.mkdir(parents=True, exist_ok=True)
    old = sorted(PREVIEW.glob('*'), key=lambda p: p.stat().st_mtime)
    for stale in old[:-80]:
        if stale.is_file():stale.unlink(missing_ok=True)
        else:shutil.rmtree(stale, ignore_errors=True)
    return PREVIEW / (digest + suffix)


def render_line(cfg, characters, speaker, text):
    """Synthesize one line exactly as production would (same cast, same polishing) into a WAV."""
    from drama import render_cinema as renderer
    voice, rate, source = voicekit.cast(speaker, cfg, characters)
    sr = int(cfg['audio'].get('sample_rate', 44100))
    digest = hashlib.sha256(json.dumps([voicekit.engine_name(cfg), cfg['voice'].get('command'), voice, rate, text,
                                        {k: cfg['voice'].get(k) for k in ('trim_silence', 'normalize', 'target_rms_db')}],
                                       ensure_ascii=False).encode()).hexdigest()[:24]
    out = preview_path('line-' + digest, '.wav')
    if not out.is_file():
        raw = out.with_suffix(voicekit.output_suffix(cfg) + '.raw')
        try:
            voicekit.synthesize(cfg, voice, rate, text, raw)
            audio = renderer.polish_voice(renderer.decode_audio(str(raw), sr), sr, cfg['voice'])
        finally:
            raw.unlink(missing_ok=True)
        renderer.write_wav(str(out), audio, sr)
    import wave
    with wave.open(str(out)) as handle:duration = handle.getnframes() / handle.getframerate()
    return {'url': '/api/drama/preview/' + out.name, 'voice': voice, 'rate': rate, 'source': source, 'duration': round(duration, 2)}


def render_soundtrack(name, cfg):
    """Voice + SFX + music for a whole storyboard, before any image or video exists."""
    from drama import render_cinema as renderer
    source, board, shots, characters = project_board(name, cfg)
    payloads = input_payloads(source)
    sections = {key: cfg.get(key) for key in ('voice', 'music', 'cinema', 'audio', 'subtitle')}
    digest = hashlib.sha256(json.dumps(sections, sort_keys=True, ensure_ascii=False).encode()
                            + b''.join(k.encode() + payloads[k] for k in sorted(payloads))).hexdigest()[:24]
    folder = preview_path('track-' + digest, '')
    sr = int(cfg['audio'].get('sample_rate', 44100))
    report = folder / 'soundtrack.json'
    if not report.is_file():
        work = folder.with_name(folder.name + '.partial');shutil.rmtree(work, ignore_errors=True);work.mkdir(parents=True)
        voices = renderer.make_voice(shots, str(work), cfg, sr, characters)
        timeline, total = renderer.plan_timeline(shots, voices, cfg, lambda shot: None)
        grade = (shots[0].get('grade') or 'neutral').strip().lower()
        renderer.write_wav(str(work / 'soundtrack.wav'), renderer.build_audio(voices, timeline, total, sr, cfg, grade), sr)
        cues = renderer.timeline_cues(timeline, cfg['subtitle'])
        renderer.write_srt(cues, str(work / 'subtitles.srt'))
        rows = [{'shot_id': tl[0]['shot_id'], 'start': round(tl[1], 2), 'planned': tl[0]['duration_sec'], 'actual': round(tl[2], 2),
                 'voice': line[4], 'voice_sec': round(line[3], 2), 'dialogue': tl[5], 'sfx': tl[0].get('sfx', ''),
                 'cues': [[round(a, 2), round(b, 2), text] for a, b, text in renderer.subtitle_cues(tl[5], tl[6], tl[7], int(cfg['subtitle'].get('max_chars_per_line', 15)))]}
                for tl, line in zip(timeline, voices)]
        write_json(work / 'soundtrack.json', {'total': round(total, 2), 'intro': cfg['cinema'].get('intro_sec'), 'shots': rows, 'cue_count': len(cues)})
        shutil.rmtree(folder, ignore_errors=True);work.replace(folder)
    result = json.loads(report.read_text())
    result.update(url='/api/drama/preview/%s/soundtrack.wav' % folder.name, srt='/api/drama/preview/%s/subtitles.srt' % folder.name)
    return result


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
    board, shots, _, _ = load_storyboard(source, cfg)
    payloads = input_payloads(source)
    digest = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode() + b''.join(name.encode() + payloads[name] for name in sorted(payloads))).hexdigest()
    job_id = digest[:20]; folder = contained(JOBS, job_id)
    if not folder.exists():
        folder.mkdir(); (folder / 'input').mkdir()
        for name, data in payloads.items():(folder / 'input' / name).write_bytes(data)
        write_json(folder / 'config.json', cfg)
        if graph is not None:write_json(folder / 'workflow_api.json', graph)
        if cfg.get('animation', {}).get('enabled'):write_json(folder / 'animate2_workflow_api.json', animate2_graph)
        write_json(folder / 'state.json', {'job_id': job_id, 'project': project, 'preset': preset, 'fingerprint': digest, 'status': 'queued', 'phase': 'queued', 'created_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'shots': {}, 'error': '',
                                           'title': board.get('title') or project, 'shot_ids': [shot['shot_id'] for shot in shots]})
    state = json.loads((folder / 'state.json').read_text())
    if state['status'] not in ('complete', 'needs_attention', 'stopped'):launch(folder)
    return {'job_id': job_id, 'status': state['status'], 'url': '/drama/production?job=' + job_id}


routes = PromptServer.instance.routes


@routes.get('/drama/production')
async def page(request):
    return web.FileResponse(Path(__file__).with_name('production.html'))


@routes.get('/api/drama/projects')
async def list_projects(request):
    names = projects();default = base_config().get('default_project') or 'example-fantasy-story'
    return web.json_response({'projects': names, 'default_project': default,
                              'example_available': (EXAMPLES / 'storyboard.json').is_file() and default not in names})


@routes.get('/api/drama/projects/{project}')
async def project_summary(request):
    """Validate a storyboard before any model runs, so mistakes surface at submission time."""
    try:
        name = request.match_info['project'];source = contained(INPUT, name)
        if name not in projects():return web.json_response({'error': '找不到分镜项目'}, status=404)
        cfg = effective_config();summary = storyboard_summary(source, cfg)
        status = await asyncio.get_running_loop().run_in_executor(None, voice_status, cfg)
        for row in summary['cast']:row['installed'] = voicekit.voice_installed(row['voice'], status.get('voices'))
        summary['voice_engine'] = {key: status[key] for key in ('engine', 'available', 'detail')}
        return web.json_response(summary)
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:return web.json_response({'error': str(e)}, status=400)


@routes.post('/api/drama/projects/{project}/voice-preview')
async def voice_preview(request):
    """试听一句台词：音色、语速和后处理与正式制作完全一致。不涉及模型，不受推理门禁限制。"""
    try:
        data = await request.json();cfg = effective_config()
        _, _, shots, characters = project_board(request.match_info['project'], cfg)
        shot = next((s for s in shots if s['shot_id'] == data.get('shot_id')), None) if data.get('shot_id') else \
            next((s for s in shots if s['dialogue'].strip() and (s['speaker'].strip() or s['character'].split('|')[0].strip()) == data.get('speaker', '')), None)
        if shot is None or not shot['dialogue'].strip():raise ValueError('找不到这句台词')
        speaker = shot['speaker'].strip() or shot['character'].split('|')[0].strip()
        result = await asyncio.get_running_loop().run_in_executor(None, render_line, cfg, characters, speaker, shot['dialogue'].strip())
        return web.json_response({**result, 'shot_id': shot['shot_id'], 'text': shot['dialogue'].strip()})
    except LookupError as e:return web.json_response({'error': str(e)}, status=404)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as e:return web.json_response({'error': str(e)}, status=400)


@routes.post('/api/drama/projects/{project}/soundtrack')
async def soundtrack(request):
    """整集声音样片：配音、音效、配乐和字幕时间轴，在生成任何画面之前就能检查。"""
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, render_soundtrack, request.match_info['project'], effective_config())
        return web.json_response(result)
    except LookupError as e:return web.json_response({'error': str(e)}, status=404)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as e:return web.json_response({'error': str(e)}, status=400)


@routes.get('/api/drama/preview/{name:[\\w.-]+}')
@routes.get('/api/drama/preview/{folder:track-[0-9a-f]+}/{name:[\\w.-]+}')
async def preview_file(request):
    try:
        root = contained(PREVIEW, request.match_info['folder']) if 'folder' in request.match_info else PREVIEW
        path = contained(root, request.match_info['name'])
        if not path.is_file():raise ValueError('试听文件已过期，请重新生成')
        return web.FileResponse(path)
    except ValueError as e:return web.json_response({'error': str(e)}, status=404)


@routes.post('/api/drama/examples')
async def install_example(request):
    try:
        name = base_config().get('default_project') or 'example-fantasy-story';folder = contained(INPUT, name)
        if name in projects():return web.json_response({'project': name})
        if not (EXAMPLES / 'storyboard.json').is_file():return web.json_response({'error': '安装目录中没有 examples/，请手动上传分镜'}, status=404)
        folder.mkdir(exist_ok=True)
        for filename in ('storyboard.json', 'shots.csv', 'characters.json'):
            if (EXAMPLES / filename).is_file():shutil.copy2(EXAMPLES / filename, folder / filename)
        return web.json_response({'project': name})
    except (ValueError, OSError) as e:return web.json_response({'error': str(e)}, status=400)


@routes.get('/api/drama/presets')
async def presets(request):
    result = {}
    for name in VIDEO_PRESETS:
        cfg = effective_config(name);animation = cfg['animation']
        result[name] = {'draft': {key: cfg['dynamic'][key] for key in ('width', 'height', 'frames', 'steps', 'fps')},
                        'transfer': {key: animation[key] for key in ('width', 'height', 'frames', 'steps', 'fps')} if animation.get('enabled') else None,
                        'delivery': {key: cfg['cinema'][key] for key in ('width', 'height', 'fps')}}
    return web.json_response({'presets': result, 'max_retries': effective_config()['quality']['max_retries']})


@routes.get('/api/drama/readiness')
async def readiness_state(request):
    cfg = effective_config()
    kinds = sorted({kind for kind, _ in readiness.required_models(cfg)})
    lists = {}
    for kind in kinds:
        try:lists[kind] = folder_paths.get_filename_list(kind)
        except Exception:lists[kind] = []
    reachable = await asyncio.get_running_loop().run_in_executor(None, service_reachable, cfg['quality']['vision']['url'])
    status = await asyncio.get_running_loop().run_in_executor(None, voice_status, cfg)
    needed = set()
    for name in projects()[:30]:
        try:
            _, _, shots, characters = project_board(name, cfg)
            needed.update(voicekit.voices_for(cfg, characters, [s['speaker'] or s['character'].split('|')[0] for s in shots if s['dialogue'].strip()]))
        except (ValueError, LookupError, KeyError, TypeError, OSError):continue
    result = readiness.checks(cfg, certificate_status(), build_status()[0], projects(), lists, reachable=lambda url: reachable,
                              node_types=workflow_node_types(), voice_status=status, voices_needed=sorted(needed))
    return web.json_response({**result, 'version': __version__})


@routes.get('/api/drama/runtime')
async def runtime(request):
    manager = sys.modules.get('comfy.model_management')
    loaded = len(manager.current_loaded_models) if manager is not None else None
    return web.json_response({'backend_id': BACKEND_ID, 'loaded_model_count': loaded})


@routes.get('/api/drama/build')
async def build_state(request):
    ready, reason = build_status()
    return web.json_response({'inference_allowed': ready, 'reason': reason, 'version': __version__,
                              'certificate_valid': certificate_status()[0]})


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


@routes.get('/api/drama/jobs/{job_id}/log')
async def job_log(request):
    try:
        folder = contained(JOBS, request.match_info['job_id'])
        if not (folder / 'state.json').is_file():raise ValueError('找不到任务')
        lines = max(1, min(int(request.query.get('lines', 80)), 400))
        return web.json_response({'job_id': folder.name, 'worker': log_tail(folder / 'worker.log', lines),
                                  'compose': log_tail(folder / 'compose.log', lines)})
    except (ValueError, OSError) as e:return web.json_response({'error': str(e)}, status=404)


MEDIA_KINDS = {'image': ('image', 'path'), 'video': ('video', 'path'), 'draft': ('video', 'driver_path'),
               'video_transfer': ('video_transfer', 'path')}


@routes.get('/api/drama/jobs/{job_id}/media')
async def job_media(request):
    """某个镜头当前的关键帧、动作草稿或成片镜头，只允许读取该任务目录内的文件。"""
    try:
        folder = contained(JOBS, request.match_info['job_id'])
        state = json.loads((folder / 'state.json').read_text())
        stage, field = MEDIA_KINDS[request.query.get('kind', 'image')]
        path = state['shots'][request.query['shot']][stage][field]
        path = Path(path).resolve();path.relative_to(folder.resolve())
        if not path.is_file():raise ValueError('文件不存在')
        return web.FileResponse(path, headers={'Cache-Control': 'no-cache'})
    except (ValueError, KeyError, TypeError, OSError) as e:return web.json_response({'error': '找不到素材：' + str(e)}, status=404)


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
    # Reject a storyboard that would only fail later inside a running job.
    try:summary = storyboard_summary(folder, effective_config())
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError) as e:
        shutil.rmtree(folder, ignore_errors=True)
        return web.json_response({'error': '分镜未通过校验，未导入：' + str(e)}, status=400)
    return web.json_response({'project': name, 'title': summary['title'], 'shot_count': summary['shot_count'], 'warnings': summary['warnings']})


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
