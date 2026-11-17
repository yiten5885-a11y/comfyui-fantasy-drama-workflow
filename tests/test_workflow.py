"""Model-free checks for the build tool, storyboard intake, readiness list and queue API."""
import asyncio
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import fake_comfy
import install as installer
from drama import readiness, workflow_build as build
from drama.workflow_contract import storyboard_summary

CONFIG = json.loads((ROOT / 'drama/production_config.json').read_text())


class BuildTests(unittest.TestCase):
    def test_static_checks_pass_on_the_repository(self):
        results = build.static_checks()
        self.assertEqual([r['name'] for r in results if not r['ok']], [])
        self.assertEqual(len(results), 11)

    def test_committed_certificate_matches_every_build_file(self):
        valid, reason = build.certificate_status()
        self.assertTrue(valid, reason)

    def test_release_ships_with_the_inference_gate_closed(self):
        self.assertFalse(json.loads(build.GATE.read_text())['inference_enabled'])
        self.assertFalse(build.build_status()[0])
        with self.assertRaises(RuntimeError):build.require_inference()

    def test_dangling_canvas_link_is_reported(self):
        canvas = json.loads((ROOT / build.BUILD_FILES[-1]).read_text())
        self.assertEqual(build.canvas_problems(canvas), [])
        canvas['nodes'] = canvas['nodes'][1:]
        self.assertTrue(build.canvas_problems(canvas))

    def test_edited_build_file_invalidates_certificate_until_recertified(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            for relative in build.BUILD_FILES + ['drama/build_gate.json', 'drama/BUILD_VERIFIED.json']:
                (app / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, app / relative)
            saved = build.APP, build.GATE, build.CERTIFICATE
            build.APP, build.GATE, build.CERTIFICATE = app, app / 'drama/build_gate.json', app / 'drama/BUILD_VERIFIED.json'
            try:
                self.assertTrue(build.certificate_status()[0])
                with (app / 'drama/batch.py').open('a') as f:f.write('\n# local edit\n')
                valid, reason = build.certificate_status()
                self.assertFalse(valid);self.assertIn('drama/batch.py', reason)
                gate = json.loads(build.GATE.read_text());gate['inference_enabled'] = True
                build.GATE.write_text(json.dumps(gate))
                self.assertFalse(build.build_status()[0], 'an open gate must not override a stale certificate')
                certificate = {'files': {relative: build.file_hash(app / relative) for relative in build.BUILD_FILES}}
                build.CERTIFICATE.write_text(json.dumps(certificate))
                self.assertTrue(build.build_status()[0])
            finally:
                build.APP, build.GATE, build.CERTIFICATE = saved


class ReadinessTests(unittest.TestCase):
    def run_checks(self, cfg=None, **overrides):
        cfg = json.loads(json.dumps(cfg or CONFIG))
        options = dict(certificate=(True, 'ok'), gate_open=False, projects=['demo'], platform='darwin',
                       model_lists={kind: [name] for kind, name in []}, which=lambda name: '/usr/bin/say',
                       find_spec=lambda name: object(), reachable=lambda url: True)
        options.update(overrides)
        return readiness.checks(cfg, **options)

    def states(self, result):return {entry['key']: entry['state'] for entry in result['items']}

    def ready_config(self, tmp):
        cfg = json.loads(json.dumps(CONFIG))
        font = Path(tmp) / 'font.ttc';font.write_bytes(b'font')
        cfg['subtitle']['font'] = str(font);cfg['quality']['vision']['model'] = 'local-vision'
        cfg['delivery']['dir'] = str(Path(tmp) / 'delivery')
        models = {}
        for kind, name in readiness.required_models(cfg):models.setdefault(kind, []).append(name)
        return cfg, models

    def test_shipped_defaults_flag_placeholder_and_missing_models(self):
        states = self.states(self.run_checks(model_lists={}))
        self.assertEqual(states['vision'], 'fail');self.assertEqual(states['models'], 'fail')
        self.assertEqual(states['gate'], 'off')

    def test_everything_ready_leaves_only_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, models = self.ready_config(tmp)
            result = self.run_checks(cfg, model_lists=models)
            self.assertEqual(result['blocking'], []);self.assertTrue(result['ready_for_gate'])
            self.assertFalse(result['can_start'])
            self.assertEqual({s for k, s in self.states(result).items() if k != 'gate'}, {'ok'})
            self.assertTrue(self.run_checks(cfg, model_lists=models, gate_open=True)['can_start'])

    def test_each_missing_piece_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, models = self.ready_config(tmp)
            self.assertEqual(self.run_checks(cfg, model_lists=models, which=lambda name: None)['blocking'], ['voice'])
            self.assertEqual(self.run_checks(cfg, model_lists=models, platform='win32')['blocking'], ['platform'])
            self.assertEqual(self.run_checks(cfg, model_lists=models, reachable=lambda url: False)['blocking'], ['vision'])
            self.assertEqual(self.run_checks(cfg, model_lists=models, find_spec=lambda name: None if name == 'av' else 1)['blocking'], ['python'])
            self.assertEqual(self.run_checks(cfg, model_lists=models, certificate=(False, 'changed'))['blocking'], ['certificate'])
            lost = {kind: names[1:] for kind, names in models.items()}
            detail = next(e for e in self.run_checks(cfg, model_lists=lost)['items'] if e['key'] == 'models')['detail']
            self.assertIn('z_image_turbo_bf16.safetensors', detail)
            result = self.run_checks(cfg, model_lists=models, node_types=({'KSampler', 'WanAnimate2ToVideo'}, {'KSampler'}))
            self.assertEqual(result['blocking'], ['nodes'])
            self.assertIn('WanAnimate2ToVideo', next(e for e in result['items'] if e['key'] == 'nodes')['detail'])
            cfg['subtitle']['font'] = '/nonexistent/font.ttc'
            self.assertEqual(self.run_checks(cfg, model_lists=models)['blocking'], ['font'])

    def test_stale_certificate_blocks_start_even_with_open_gate(self):
        self.assertFalse(self.run_checks(certificate=(False, 'changed'), gate_open=True)['can_start'])


class StoryboardTests(unittest.TestCase):
    def test_example_summary(self):
        summary = storyboard_summary(ROOT / 'examples', CONFIG)
        self.assertEqual((summary['title'], summary['shot_count'], summary['duration_sec']), ('云海剑台演示', 2, 5.5))
        self.assertEqual(summary['warnings'], [])

    def test_warnings_name_the_shots(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = {'shots': [{'description': '山门', 'dialogue': '开'}, {'description': '云海', 'video_prompt': 'clouds drift'}]}
            (Path(tmp) / 'storyboard.json').write_text(json.dumps(board, ensure_ascii=False))
            summary = storyboard_summary(tmp, CONFIG)
            self.assertEqual(len(summary['warnings']), 4)
            self.assertIn('S01', summary['warnings'][0]);self.assertNotIn('S02', summary['warnings'][0])


class InstallTests(unittest.TestCase):
    def comfy(self, tmp):
        root = Path(tmp) / 'ComfyUI';(root / 'custom_nodes').mkdir(parents=True);(root / 'main.py').write_text('')
        return root

    def test_dry_run_changes_nothing_then_apply_installs_a_valid_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.comfy(tmp)
            plan = installer.install(root, apply=False)
            self.assertTrue(plan['copy']);self.assertFalse((root / 'drama').exists())
            installer.install(root, apply=True)
            for relative in build.BUILD_FILES:self.assertTrue((root / relative).is_file(), relative)
            self.assertTrue((root / 'input/drama/example-fantasy-story/storyboard.json').is_file())
            self.assertEqual(installer.install(root, apply=True)['copy'], [])

    def test_upgrade_keeps_config_backs_up_code_and_closes_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.comfy(tmp);installer.install(root, apply=True)
            config = root / 'drama/production_config.json';config.write_text('{"mine": true}')
            gate = root / 'drama/build_gate.json';gate.write_text('{"build_complete": true, "inference_enabled": true}')
            self.assertEqual(installer.install(root, apply=True)['kept'], ['drama/production_config.json'])
            self.assertTrue(json.loads(gate.read_text())['inference_enabled'], 'nothing changed, so the gate is left alone')
            (root / 'drama/batch.py').write_text('# older release\n')
            plan = installer.install(root, apply=True)
            self.assertEqual(plan['replace'], ['drama/batch.py']);self.assertTrue(plan['gate_closed'])
            self.assertEqual(config.read_text(), '{"mine": true}')
            self.assertEqual([p.read_text() for p in root.glob('drama/batch.py.bak-*')], ['# older release\n'])
            self.assertFalse(json.loads(gate.read_text())['inference_enabled'])
            installer.install(root, apply=True, reset_config=True)
            self.assertEqual(json.loads(config.read_text())['version'], 2)
            self.assertTrue(list(root.glob('drama/production_config.json.bak-*')))

    def test_refuses_a_folder_that_is_not_comfyui(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):installer.install(Path(tmp), apply=True)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.work = Path(self.tmp.name)
        self.server = fake_comfy.install(self.work)

    def tearDown(self):self.tmp.cleanup()

    def call(self, run):
        async def session():
            async with TestClient(TestServer(fake_comfy.application(self.server))) as client:return await run(client)
        return asyncio.run(session())

    def upload(self, client, files):
        form = FormData()
        for name, content in files.items():form.add_field('file', io.BytesIO(content), filename=name)
        return client.post('/api/drama/upload', data=form)

    def test_first_visit_on_a_fresh_install(self):
        async def run(client):
            page = await client.get('/drama/production')
            projects = await (await client.get('/api/drama/projects')).json()
            build_state = await (await client.get('/api/drama/build')).json()
            ready = await (await client.get('/api/drama/readiness')).json()
            presets = await (await client.get('/api/drama/presets')).json()
            return page.status, projects, build_state, ready, presets
        status, projects, build_state, ready, presets = self.call(run)
        self.assertEqual(status, 200)
        self.assertEqual(projects, {'projects': [], 'default_project': 'example-fantasy-story', 'example_available': True})
        self.assertEqual((build_state['inference_allowed'], build_state['certificate_valid'], build_state['version']), (False, True, '1.0.0'))
        self.assertFalse(ready['can_start']);self.assertIn('models', ready['blocking'])
        self.assertEqual(presets['presets']['production']['transfer']['width'], 480)
        self.assertEqual(presets['presets']['preview']['draft']['frames'], 17)

    def test_example_install_then_preview(self):
        async def run(client):
            first = await client.post('/api/drama/examples', json={})
            again = await client.post('/api/drama/examples', json={})
            summary = await client.get('/api/drama/projects/example-fantasy-story')
            missing = await client.get('/api/drama/projects/nope')
            traversal = await client.get('/api/drama/projects/..')
            return first.status, again.status, await summary.json(), missing.status, traversal.status
        first, again, summary, missing, traversal = self.call(run)
        self.assertEqual((first, again, missing), (200, 200, 404));self.assertIn(traversal, (400, 404))
        self.assertEqual(summary['shot_count'], 2)

    def test_upload_validates_before_import(self):
        good = (ROOT / 'examples/storyboard.json').read_bytes()
        bad = json.dumps({'shots': [{'shot_id': 'S01', 'description': '山门', 'character': '无名氏'}]}).encode()
        async def run(client):
            rejected = await self.upload(client, {'storyboard.json': bad})
            body = await rejected.json()
            after_reject = await (await client.get('/api/drama/projects')).json()
            wrong_name = await self.upload(client, {'notes.txt': b'x'})
            accepted = await self.upload(client, {'storyboard.json': good})
            return rejected.status, body, after_reject['projects'], wrong_name.status, accepted.status, await accepted.json()
        rejected, body, projects, wrong_name, accepted, result = self.call(run)
        self.assertEqual((rejected, wrong_name, accepted), (400, 400, 200))
        self.assertIn('无名氏', body['error']);self.assertEqual(projects, [])
        self.assertEqual(list((self.work / 'input/drama').iterdir()), [self.work / 'input/drama' / result['project']])
        self.assertEqual(result['shot_count'], 2)

    def test_submission_stays_locked_while_the_gate_is_closed(self):
        async def run(client):
            await client.post('/api/drama/examples', json={})
            response = await client.post('/api/drama/jobs', json={'project': 'example-fantasy-story', 'preset': 'preview'})
            return response.status, await response.json()
        status, body = self.call(run)
        self.assertEqual(status, 423);self.assertIn('模型启动已锁定', body['error'])
        self.assertEqual(list((self.work / 'output/drama_jobs').iterdir()), [])

    def test_readiness_reports_nodes_missing_from_this_comfyui(self):
        import types
        registry = types.ModuleType('nodes');registry.NODE_CLASS_MAPPINGS = {'KSampler': object}
        sys.modules['nodes'] = registry
        try:
            async def run(client):return await (await client.get('/api/drama/readiness')).json()
            ready = self.call(run)
        finally:del sys.modules['nodes']
        entry = next(e for e in ready['items'] if e['key'] == 'nodes')
        self.assertEqual(entry['state'], 'fail');self.assertIn('WanAnimate2ToVideo', entry['detail'])
        self.assertNotIn('KSampler、', entry['detail'])

    def test_job_log_tail_and_path_safety(self):
        job = self.work / 'output/drama_jobs/abc123';job.mkdir(parents=True)
        (job / 'state.json').write_text(json.dumps({'job_id': 'abc123', 'project': 'p', 'status': 'needs_attention'}))
        (job / 'worker.log').write_text('\n'.join('line %d' % n for n in range(500)))
        async def run(client):
            log = await (await client.get('/api/drama/jobs/abc123/log?lines=3')).json()
            missing = await client.get('/api/drama/jobs/zzz/log')
            traversal = await client.get('/api/drama/jobs/..%2F..%2Fetc/log')
            return log, missing.status, traversal.status
        log, missing, traversal = self.call(run)
        self.assertEqual(log['worker'], 'line 497\nline 498\nline 499');self.assertEqual(log['compose'], '')
        self.assertEqual(missing, 404);self.assertIn(traversal, (400, 404))


if __name__ == '__main__':unittest.main()
