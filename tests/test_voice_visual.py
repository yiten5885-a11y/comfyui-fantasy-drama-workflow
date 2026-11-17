"""v1.1 voice and picture: casting, engines, line polishing, subtitle timing, mix,
SFX, clip retiming, transitions, the motion prompt and the video review payload.
Model-free; the TTS engine is tests/fake_tts.py."""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import av
import numpy as np
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import fake_comfy
from drama import batch, readiness, render_cinema as r, voice, workflow_contract as contract

CONFIG = json.loads((ROOT / 'drama/production_config.json').read_text())
FAKE_TTS = [sys.executable, str(ROOT / 'tests/fake_tts.py'), '{out}', '{voice}', '{rate}']
SR = 22050


def config(**voice_settings):
    cfg = json.loads(json.dumps(CONFIG))
    cfg['voice'].update({'engine': 'command', 'command': FAKE_TTS, **voice_settings})
    cfg['audio']['sample_rate'] = SR
    return cfg


class CastingTests(unittest.TestCase):
    chars = {'少年剑修': {'anchor': '青年男性'}, '白衣师姐': {'gender': 'female'}, '老祖': {},
             '掌门': {'voice': 'Eddy', 'voice_rate': 150}, '阿七': {'gender': 'child'}}

    def test_cast_order(self):
        cfg = json.loads(json.dumps(CONFIG));cfg['voice']['per_character'] = {'少年剑修': 'Rocko'}
        cast = {name: voice.cast(name, cfg, self.chars) for name in list(self.chars) + ['路人']}
        self.assertEqual(cast['掌门'], ('Eddy (中文（中国大陆）)', 150, '角色表指定'))
        self.assertEqual(cast['少年剑修'][:2], ('Rocko (中文（中国大陆）)', 170))
        self.assertEqual(cast['白衣师姐'][0], 'Tingting (中文（中国大陆）)')
        self.assertEqual(cast['老祖'][:2], ('Grandpa (中文（中国大陆）)', int(170 * voice.ELDER_RATE)))
        self.assertEqual(cast['阿七'][0], 'Flo (中文（中国大陆）)')
        self.assertEqual(cast['路人'][2], '默认音色')

    def test_canvas_voice_no_longer_flattens_the_cast(self):
        """Regression: the canvas audio node used to replace per_character, giving everyone one voice."""
        cfg = json.loads(json.dumps(CONFIG));cfg['voice']['per_character'] = {'少年剑修': 'Rocko'}
        settings = {'voice_enabled': True, 'voice_name': 'Reed (中文（中国大陆）)', 'voice_rate': 170, 'subtitles': True, 'music': True}
        for cfg in (contract.configure(cfg, 'preview', {'audio_settings': settings}), contract.apply_audio_settings(cfg, settings)):
            self.assertEqual(voice.cast('少年剑修', cfg, self.chars)[0], 'Rocko (中文（中国大陆）)')
            self.assertEqual(voice.cast('白衣师姐', cfg, self.chars)[0], 'Tingting (中文（中国大陆）)')
            self.assertEqual(voice.cast('路人', cfg, self.chars)[0], 'Reed (中文（中国大陆）)')

    def test_old_config_without_new_keys_still_casts(self):
        cfg = {'voice': {'rate': 170, 'per_character': {}}, 'cinema': {'voice_map': {'_default': 'Tingting'}}}
        self.assertEqual(voice.cast('白衣师姐', cfg, self.chars)[0], 'Tingting (中文（中国大陆）)')

    def test_character_voice_fields_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = {'characters': {'甲': {'voice_rate': 'fast'}}, 'shots': [{'description': '山门', 'character': '甲'}]}
            (Path(tmp) / 'storyboard.json').write_text(json.dumps(board, ensure_ascii=False))
            with self.assertRaisesRegex(ValueError, 'voice_rate'):contract.load_storyboard(tmp, CONFIG)

    def test_say_listing_and_lenient_match(self):
        listing = 'Tingting             zh_CN    # 你好\nReed (中文（中国大陆）) zh_CN    # 你好\nAlex                en_US    # Hello\n'
        installed = voice.parse_say_voices(listing)
        self.assertEqual(installed, ['Tingting', 'Reed (中文（中国大陆）)', 'Alex'])
        self.assertTrue(voice.voice_installed('Tingting (中文（中国大陆）)', installed))
        self.assertTrue(voice.voice_installed('Reed', installed))
        self.assertFalse(voice.voice_installed('Grandpa (中文（中国大陆）)', installed))
        self.assertIsNone(voice.voice_installed('Reed', None))

    def test_engine_commands(self):
        cfg = config()
        argv, stdin = voice.command(cfg, 'Reed', 170, '-v 破阵', '/o.wav', '/t.txt')
        self.assertEqual(argv[-3:], ['/o.wav', 'Reed', '170']);self.assertEqual(stdin, '-v 破阵')
        say_cfg = json.loads(json.dumps(CONFIG))
        argv, stdin = voice.command(say_cfg, 'Reed', 170, '-v 破阵', '/o.aiff', '/t.txt')
        self.assertEqual(argv[-2:], ['-f', '/t.txt'], 'text goes through a file so a leading "-" is never an option')
        self.assertIsNone(stdin)
        with self.assertRaises(ValueError):voice.command(config(command='piper'), 'x', 1, 't', 'o', 'f')
        self.assertFalse(voice.engine_status(config(command=['no-such-tts-program']))['available'])
        self.assertTrue(voice.engine_status(config())['available'])
        self.assertFalse(voice.engine_status(json.loads(json.dumps(CONFIG)), which=lambda name: None)['available'])


class LineTests(unittest.TestCase):
    def test_polish_trims_silence_levels_and_fades(self):
        tone = 0.05 * np.sin(2 * np.pi * 220 * np.arange(SR) / SR).astype(np.float32)
        raw = np.concatenate([np.zeros(SR // 2, np.float32), tone, np.zeros(SR // 2, np.float32)])
        out = r.polish_voice(np.vstack([raw, raw]), SR, {})
        self.assertLess(out.shape[1], SR * 1.15);self.assertGreater(out.shape[1], SR * 0.95)
        self.assertAlmostEqual(20 * np.log10(np.sqrt(np.mean(out ** 2))), -20, delta=1.0)
        self.assertLess(abs(out[0, 0]), 1e-3);self.assertLess(abs(out[0, -1]), 1e-3)
        untouched = r.polish_voice(np.vstack([raw, raw]), SR, {'trim_silence': False, 'normalize': False})
        self.assertEqual(untouched.shape[1], raw.size)

    def test_make_voice_with_command_engine(self):
        cfg = config()
        chars = {'甲': {'gender': 'male'}, '乙': {'gender': 'female'}}
        shots = [{'shot_id': 'S01', 'dialogue': '此剑，破阵！', 'speaker': '甲'}, {'shot_id': 'S02', 'dialogue': ''},
                 {'shot_id': 'S03', 'dialogue': '随我来。', 'speaker': '乙'}]
        with tempfile.TemporaryDirectory() as tmp:
            lines = r.make_voice(shots, tmp, cfg, SR, chars)
        self.assertEqual([line[2] is None for line in lines], [False, True, False])
        self.assertNotEqual(lines[0][4], lines[2][4])
        raw = 0.3 + 4 * 60 / 170 * 1.6 + 0.4
        self.assertLess(lines[0][3], raw - 0.5, 'leading and trailing silence trimmed')


class SubtitleTests(unittest.TestCase):
    def test_short_line_is_one_cue(self):
        self.assertEqual(r.subtitle_cues('此剑，破阵！', 0.2, 1.5), [(0.2, 1.5, '此剑，破阵！')])

    def test_long_line_is_split_and_fills_the_voice_window(self):
        cues = r.subtitle_cues('你以为躲进云海，我就找不到你了吗？今日之事，必须有个了断。', 0.2, 6.0, 15)
        self.assertGreater(len(cues), 1)
        self.assertAlmostEqual(cues[0][0], 0.2);self.assertAlmostEqual(cues[-1][1], 6.0)
        for (a, b, _), (c, _, _) in zip(cues, cues[1:]):self.assertAlmostEqual(b, c)
        self.assertTrue(all(len(text) <= 15 for _, _, text in cues))
        self.assertTrue(any(text.endswith('？') for _, _, text in cues), 'question marks stay')
        self.assertFalse(any(text[-1] in '，。' for _, _, text in cues), 'commas and full stops at a cue end are dropped')

    def test_timeline_fits_voice_and_srt_matches(self):
        cfg = json.loads(json.dumps(CONFIG))
        shots = [{'shot_id': 'S01', 'duration_sec': 3, 'dialogue': '短句'}, {'shot_id': 'S02', 'duration_sec': 2, 'dialogue': ''}]
        voices = [(1, 'a', np.zeros((2, 10)), 4.0, 'v'), (2, None, None, 0.0, '')]
        timeline, total = r.plan_timeline(shots, voices, cfg, lambda shot: None)
        lead, tail = cfg['voice']['lead_in_sec'], cfg['cinema']['voice_tail_sec']
        self.assertAlmostEqual(timeline[0][2], 4.0 + lead + tail)
        self.assertEqual(timeline[0][6], lead);self.assertAlmostEqual(timeline[0][7], lead + 4.25)
        self.assertEqual(timeline[1][2], max(2, cfg['cinema']['min_shot_sec']))
        self.assertAlmostEqual(total, cfg['cinema']['intro_sec'] + timeline[0][2] + timeline[1][2] + cfg['cinema']['outro_sec'])
        cues = r.timeline_cues(timeline, cfg['subtitle'])
        self.assertEqual(len(cues), 1);self.assertAlmostEqual(cues[0][0], timeline[0][1] + lead)
        with tempfile.TemporaryDirectory() as tmp:
            r.write_srt(cues, tmp + '/a.srt')
            self.assertIn('00:00:00,700 -->', Path(tmp + '/a.srt').read_text())


class MixTests(unittest.TestCase):
    def test_duck_envelope_leads_the_voice_and_recovers(self):
        sr = 8000;mask = np.zeros(sr * 4, np.float32);mask[sr:2 * sr] = 1
        env = r.duck_envelope(mask, sr, 0.4)
        self.assertAlmostEqual(float(env[int(0.5 * sr)]), 1.0, places=3)
        self.assertLess(float(env[sr]), 0.75, 'already dipping when the voice starts')
        self.assertAlmostEqual(float(env[int(1.6 * sr)]), 0.4, places=2)
        self.assertGreater(float(env[int(3.5 * sr)]), 0.95)
        self.assertTrue(np.all(np.abs(np.diff(env)) < 0.01), 'no steps, so no pumping clicks')

    def test_sfx_vocabulary_matches_the_contract(self):
        self.assertEqual(set(r.SFX_NAMES), set(contract.SFX))
        self.assertEqual(set(r.TRANSITIONS), set(contract.TRANSITIONS))
        self.assertEqual(set(r.TINTS), set(contract.GRADES))

    def test_oneshot_lands_where_asked_and_unknown_is_silent(self):
        sr = 8000
        track = r.shot_sfx('impact@1.0', 3.0, sr, 1)
        self.assertEqual(float(np.abs(track[:sr - 10]).max()), 0.0)
        self.assertGreater(float(np.abs(track[sr:sr + 400]).max()), 0.2)
        self.assertEqual(float(np.abs(r.shot_sfx('dragon_roar', 1.0, sr, 1)).max()), 0.0)
        both = r.shot_sfx('wind|sword', 2.0, sr, 1)
        self.assertGreater(float(np.abs(both[:int(0.6 * sr)]).max()), 0.0)

    def test_summary_flags_unknown_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = {'shots': [{'description': '山门', 'sfx': 'wind|dragon', 'motion': 'zoom', 'transition': 'wipe', 'grade': 'cool'}]}
            (Path(tmp) / 'storyboard.json').write_text(json.dumps(board, ensure_ascii=False))
            warnings = ' '.join(contract.storyboard_summary(tmp, CONFIG)['warnings'])
        for word in ('dragon', 'zoom', 'wipe'):self.assertIn(word, warnings)
        self.assertNotIn('cool', warnings)

    def test_guofeng_music_is_pentatonic_and_bounded(self):
        music = r.synth_music(8, 8000, {'builtin': {'style': 'guofeng'}, 'fade_sec': 1}, 'cool')
        self.assertEqual(music.shape[0], 2);self.assertLess(float(np.abs(music).max()), 1.5)
        self.assertGreater(float(np.abs(music[:, 8000 * 3]).max()), 0.0)


def make_clip(path, frames, fps, color, size=(64, 112)):
    with av.open(str(path), 'w') as container:
        stream = container.add_stream('h264', rate=fps)
        stream.width, stream.height, stream.pix_fmt = size[0], size[1], 'yuv420p'
        for k in range(frames):
            frame = np.zeros((size[1], size[0], 3), np.uint8);frame[...] = color;frame[:, : (k * 3) % size[0]] = 255
            for packet in stream.encode(av.VideoFrame.from_ndarray(frame, format='rgb24')):container.mux(packet)
        for packet in stream.encode():container.mux(packet)


class PictureTests(unittest.TestCase):
    def test_clip_positions(self):
        fit = r.clip_positions(73, 24, 48, 24)
        self.assertEqual((len(fit), fit[0], fit[-1]), (48, 0.0, 72.0))
        slow = r.clip_positions(17, 8, 72, 24, max_slowdown=1.5)
        self.assertEqual(len(slow), 68);self.assertLessEqual(slow[-1], 16)
        speed = max((17 / 8) / 3.0, 1 / 1.5)  # clip seconds / shot seconds, capped at 1.5x slow-motion
        self.assertAlmostEqual(slow[1] - slow[0], speed * 8 / 24)
        tiny = r.clip_positions(17, 8, 240, 24, max_slowdown=1.5)
        self.assertLess(len(tiny), 240, 'the rest is a held push-in, not a 10x stretch')

    def test_clip_frame_at_blends(self):
        a, b = Image.new('RGB', (4, 4), (0, 0, 0)), Image.new('RGB', (4, 4), (200, 200, 200))
        self.assertEqual(r.clip_frame_at([a, b], 0.5).getpixel((0, 0)), (100, 100, 100))
        self.assertIs(r.clip_frame_at([a, b], 1.0), b)

    def test_fit_frame_sharpens_only_when_upscaling(self):
        small = Image.new('RGB', (64, 112), (90, 90, 90));small.paste((200, 200, 200), (30, 0, 64, 112))
        sharp, soft = r.fit_frame(small, 128, 224, 1.0), r.fit_frame(small, 128, 224, 0)
        self.assertGreater(np.asarray(sharp).std(), np.asarray(soft).std())
        same = Image.new('RGB', (128, 224), (90, 90, 90))
        self.assertEqual(np.asarray(r.fit_frame(same, 128, 224, 1.0)).std(), 0)

    def test_transitions(self):
        black, white = Image.new('RGB', (8, 8)), Image.new('RGB', (8, 8), (255, 252, 240))
        prev, cur = Image.new('RGB', (8, 8), (200, 0, 0)), Image.new('RGB', (8, 8), (0, 0, 200))
        self.assertIs(r.apply_transition('cut', prev, cur, 0.1, black, white), cur)
        self.assertGreater(sum(r.apply_transition('flash', prev, cur, 0.1, black, white).getpixel((0, 0))), 500)
        self.assertLess(sum(r.apply_transition('dip', prev, cur, 0.5, black, white).getpixel((0, 0))), 60)
        self.assertEqual(r.apply_transition('dissolve', prev, cur, 0.5, black, white).getpixel((0, 0)), (100, 0, 100))
        self.assertEqual(r.apply_transition('dip', None, cur, 0.5, black, white, first=True).getpixel((0, 0)), (0, 0, 100))
        self.assertEqual(r.shot_transition_of({'transition': 'wipe'}, {}), 'dip')
        self.assertEqual(r.shot_grade_of({'grade': 'COOL'}, 'neutral'), 'cool')

    def test_encode_retimes_grades_and_keeps_subtitles_out_of_transitions(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg['cinema'].update(width=128, height=224, fps=12, transition_frames=4, intro_sec=0, outro_sec=0, grain=0, vignette=0, preset='ultrafast')
        cfg['audio']['sample_rate'] = 8000
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp);(tmp / 'clips').mkdir()
            make_clip(tmp / 'clips/S01.mp4', 9, 6, (20, 120, 160));make_clip(tmp / 'clips/S02.mp4', 9, 6, (20, 160, 60))
            Image.new('RGB', (64, 112)).save(tmp / 'k.png')
            cfg['_videos_dir'] = str(tmp / 'clips')
            shots = [{'shot_id': 'S01', 'dialogue': '此剑，破阵！'}, {'shot_id': 'S02', 'dialogue': '', 'transition': 'dissolve', 'grade': 'warm'}]
            timeline = [(shots[0], 0.0, 2.0, str(tmp / 'k.png'), 'lock', '此剑，破阵！', 0.0, 2.0),
                        (shots[1], 2.0, 1.0, str(tmp / 'k.png'), 'lock', '', 0.0, 1.0)]
            red = lambda frame, text, subcfg, W, H, alpha=1.0: Image.blend(frame, Image.new('RGB', (W, H), (255, 0, 0)), alpha)
            original, r.draw_subtitle = r.draw_subtitle, red
            try:frames = r.encode(str(tmp / 'out.mp4'), timeline, 0, 0, None, None, np.zeros((2, 8000 * 3), np.float32), cfg, 'neutral')
            finally:r.draw_subtitle = original
            self.assertEqual(frames, 36)
            with av.open(str(tmp / 'out.mp4')) as video:
                decoded = [np.asarray(f.to_image(), dtype=np.float32) for f in video.decode(video=0)]
        self.assertEqual(len(decoded), 36)
        reddish = lambda a: float((a[..., 0] - a[..., 1:].max(axis=-1)).mean())
        self.assertGreater(reddish(decoded[12]), 120, 'subtitle shown inside its window')
        for frame in decoded[24:28]:self.assertLess(reddish(frame), 30, 'no subtitle ghost in the next shot\'s transition')


class PromptAndReviewTests(unittest.TestCase):
    def test_motion_prompt_carries_the_character_anchor(self):
        chars = {'少年剑修': {'anchor': '黑色高束发，深青色金纹长袍。'}}
        prompt = batch.motion_prompt('He slashes once.', {'character': '少年剑修'}, chars, '3D国风')
        self.assertIn('黑色高束发，深青色金纹长袍', prompt);self.assertIn('3D国风', prompt)
        self.assertTrue(prompt.startswith('He slashes once'))
        self.assertNotIn('角色外观', batch.motion_prompt('Clouds drift.', {'character': ''}, chars, ''))

    def test_review_frame_indices(self):
        from drama import production
        self.assertEqual(production.review_frame_indices(73, 5), [0, 18, 36, 54, 72])
        self.assertEqual(production.review_frame_indices(3, 5), [0, 1, 2])
        self.assertEqual(production.review_frame_indices(17, 2), [0, 16])

    def test_video_review_sends_the_keyframe_first(self):
        from drama import production
        job = production.Job.__new__(production.Job)
        with tempfile.TemporaryDirectory() as tmp:
            job.folder = Path(tmp);job.cfg = json.loads(json.dumps(CONFIG));job.cfg['quality']['vision']['model'] = 'm'
            paths = []
            for name in ('key.png', 'f0.jpg', 'f1.jpg'):
                Image.new('RGB', (16, 16), (len(name) * 20, 30, 30)).save(Path(tmp) / name);paths.append(Path(tmp) / name)
            sent = {}
            def post(host, path, data, timeout=30):
                sent.update(data)
                return {'choices': [{'message': {'content': '{"pass": true, "issues": [], "correction_prompt": ""}'}}]}
            original, production.batch.api_post = production.batch.api_post, post
            try:result = job.vision(paths[1:], 'slash', sequence=True, reference=paths[0])
            finally:production.batch.api_post = original
        content = sent['messages'][0]['content']
        labels = [part['text'] for part in content if part['type'] == 'text']
        self.assertTrue(labels[0].startswith('REFERENCE'));self.assertTrue(labels[1].startswith('FRAME 1/2'))
        self.assertIn('角色外观基准', labels[-1])
        self.assertEqual([item['role'] for item in result['input_receipt']['images']], ['reference', 'sequence_frame', 'sequence_frame'])


class ServerVoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.work = Path(self.tmp.name)
        self.server = fake_comfy.install(self.work)
        cfg = config();cfg['audio']['sample_rate'] = 44100
        self.server.base_config = lambda: json.loads(json.dumps(cfg))

    def tearDown(self):self.tmp.cleanup()

    def call(self, run):
        async def session():
            async with TestClient(TestServer(fake_comfy.application(self.server))) as client:
                await client.post('/api/drama/examples', json={})
                return await run(client)
        return asyncio.run(session())

    def test_cast_preview_and_soundtrack(self):
        async def run(client):
            summary = await (await client.get('/api/drama/projects/example-fantasy-story')).json()
            line = await (await client.post('/api/drama/projects/example-fantasy-story/voice-preview', json={'speaker': '白衣师姐'})).json()
            wav = await client.get(line['url'])
            missing = await client.post('/api/drama/projects/example-fantasy-story/voice-preview', json={'shot_id': 'S99'})
            track = await (await client.post('/api/drama/projects/example-fantasy-story/soundtrack', json={})).json()
            again = await (await client.post('/api/drama/projects/example-fantasy-story/soundtrack', json={})).json()
            audio, srt = await client.get(track['url']), await client.get(track['srt'])
            escape = await client.get('/api/drama/preview/..%2F..%2Fsecret')
            return summary, line, wav.status, missing.status, track, again, audio.status, await srt.text(), escape.status
        summary, line, wav, missing, track, again, audio, srt, escape = self.call(run)
        self.assertEqual([row['voice'] for row in summary['cast']], ['Reed', 'Tingting'])
        self.assertEqual(summary['voice_engine']['engine'], 'command')
        self.assertEqual((line['voice'], line['rate'], line['shot_id']), ('Tingting', 165, 'S03'))
        self.assertEqual((wav, missing, audio, escape), (200, 400, 200, 404))
        self.assertEqual(len(track['shots']), 3);self.assertEqual(track['url'], again['url'], 'cached by content')
        s03 = track['shots'][2]
        self.assertGreater(len(s03['cues']), 1, 'the long line is split');self.assertGreaterEqual(s03['actual'], s03['planned'])
        self.assertIn('随我来', srt)

    def test_readiness_names_voices_that_are_not_installed(self):
        cfg = json.loads(json.dumps(CONFIG))
        self.server.base_config = lambda: json.loads(json.dumps(cfg))
        self.server.voicekit.engine_status = lambda cfg: {'engine': 'say', 'available': True, 'detail': 'macOS say', 'voices': ['Tingting']}
        self.server._VOICE_STATUS['value'] = None
        async def run(client):return await (await client.get('/api/drama/readiness')).json()
        item = next(i for i in self.call(run)['items'] if i['key'] == 'voice')
        self.assertEqual(item['state'], 'fail');self.assertIn('Reed', item['detail']);self.assertNotIn('Tingting', item['detail'])

    def test_job_media_only_serves_files_inside_the_job(self):
        job = self.work / 'output/drama_jobs/abc123';(job / 'artifacts').mkdir(parents=True)
        Image.new('RGB', (8, 8)).save(job / 'artifacts/S01.png')
        outside = self.work / 'outside.png';Image.new('RGB', (8, 8)).save(outside)
        state = {'job_id': 'abc123', 'shots': {'S01': {'image': {'path': str(job / 'artifacts/S01.png')}, 'video': {'path': str(outside)}}}}
        (job / 'state.json').write_text(json.dumps(state))
        async def run(client):
            ok = await client.get('/api/drama/jobs/abc123/media?shot=S01&kind=image')
            escape = await client.get('/api/drama/jobs/abc123/media?shot=S01&kind=video')
            unknown = await client.get('/api/drama/jobs/abc123/media?shot=S01&kind=state')
            return ok.status, ok.headers.get('Content-Type'), escape.status, unknown.status
        self.assertEqual(self.call(run), (200, 'image/png', 404, 404))


if __name__ == '__main__':unittest.main()
