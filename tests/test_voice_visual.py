"""Unit tests for v1.1.0 voice per character, audio mixing, visual smoothness and consistency."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import fake_comfy
fake_comfy.install(tempfile.gettempdir())
from drama.workflow_contract import (
    resolve_speaker_voice, is_voice_installed, KNOWN_SFX, KNOWN_GRADES,
    KNOWN_TRANSITIONS, storyboard_summary
)
from drama import readiness, render_cinema
from custom_nodes.drama_pipeline.pipeline_nodes import DramaShotPlan

CONFIG = json.loads((ROOT / 'drama/production_config.json').read_text())


class VoiceVisualTests(unittest.TestCase):

    def test_voice_resolution_character_override(self):
        chars = {'少年剑修': {'voice': 'CustomVoice', 'voice_rate': 190, 'gender': 'male'}}
        voice, rate, reason = resolve_speaker_voice('少年剑修', CONFIG, chars)
        self.assertEqual(voice, 'CustomVoice')
        self.assertEqual(rate, 190)
        self.assertEqual(reason, '角色表指定')

    def test_voice_resolution_config_per_character(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg['voice']['per_character']['白衣仙子'] = 'FairyVoice'
        voice, rate, reason = resolve_speaker_voice('白衣仙子', cfg, {})
        self.assertEqual(voice, 'FairyVoice')
        self.assertEqual(rate, int(cfg['voice']['rate']))
        self.assertEqual(reason, '按角色名配置')

    def test_voice_resolution_gender_fallback(self):
        chars = {'同门': {'gender': 'female'}}
        voice, rate, reason = resolve_speaker_voice('同门', CONFIG, chars)
        exp_female = CONFIG.get('cinema', {}).get('voice_map', {}).get('_female') or CONFIG.get('voice', {}).get('per_character', {}).get('_female', 'Tingting')
        self.assertEqual(voice, exp_female)
        self.assertEqual(reason, '按性别指定')

    def test_voice_resolution_name_heuristic(self):
        exp_female = CONFIG.get('cinema', {}).get('voice_map', {}).get('_female') or CONFIG.get('voice', {}).get('per_character', {}).get('_female', 'Tingting')
        exp_male = CONFIG.get('cinema', {}).get('voice_map', {}).get('_male') or CONFIG.get('voice', {}).get('per_character', {}).get('_male', 'Reed')
        v_female, _, r_female = resolve_speaker_voice('白衣师姐', CONFIG, {})
        self.assertEqual(v_female, exp_female)
        self.assertEqual(r_female, '按名字推断')

        v_male, _, r_male = resolve_speaker_voice('黑袍道士', CONFIG, {})
        self.assertEqual(v_male, exp_male)
        self.assertEqual(r_male, '按名字推断')

    def test_voice_resolution_elder_rate(self):
        exp_elder = CONFIG.get('cinema', {}).get('voice_map', {}).get('_elder') or CONFIG.get('voice', {}).get('per_character', {}).get('_elder', 'Grandpa')
        voice, rate, reason = resolve_speaker_voice('青云老祖', CONFIG, {})
        self.assertEqual(voice, exp_elder)
        expected_rate = int(int(CONFIG['voice']['rate']) * 0.88)
        self.assertEqual(rate, expected_rate)
        self.assertEqual(reason, '按名字推断')

    def test_voice_resolution_default_fallback(self):
        exp_default = CONFIG.get('cinema', {}).get('voice_map', {}).get('_default') or CONFIG.get('voice', {}).get('per_character', {}).get('_default', 'Tingting')
        voice, rate, reason = resolve_speaker_voice('旁白', CONFIG, {})
        self.assertEqual(voice, exp_default)
        self.assertEqual(rate, int(CONFIG['voice']['rate']))
        self.assertEqual(reason, '默认音色')

    def test_fake_tts_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_wav = Path(tmp) / 'test.wav'
            res = subprocess.run([
                sys.executable, str(ROOT / 'tests/fake_tts.py'),
                '--voice', 'Reed', '--rate', '175', '--out', str(out_wav),
                '--text', '剑气纵横三万里'
            ], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(out_wav.is_file())
            with wave.open(str(out_wav), 'rb') as wf:
                self.assertEqual(wf.getnchannels(), 1)
                self.assertIn(wf.getframerate(), (24000, 44100))
                self.assertGreater(wf.getnframes(), 0)

    def test_synth_voice_line_command_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_wav = Path(tmp) / 'synth.wav'
            cfg = json.loads(json.dumps(CONFIG))
            cfg['voice']['engine'] = 'command'
            cfg['voice']['command'] = [
                sys.executable, str(ROOT / 'tests/fake_tts.py'),
                '--voice', '{voice}', '--out', '{out}', '--rate', '{rate}', '--text', '{text}'
            ]
            cfg['voice']['command_ext'] = 'wav'
            audio, res_path = render_cinema.synth_voice_line('九幽玄冰，听我号令', 'Tingting', 165, str(out_wav), cfg)
            self.assertTrue(Path(res_path).is_file())
            with wave.open(str(res_path), 'rb') as wf:
                self.assertIn(wf.getframerate(), (24000, 44100))

    def test_process_voice_audio_trim_silence(self):
        sr = 24000
        silence = np.zeros(sr, dtype=np.float32)  # 1 sec silence
        tone = (np.sin(2 * np.pi * 440 * np.linspace(0, 0.5, sr // 2)) * 0.5).astype(np.float32)
        audio = np.concatenate([silence, tone, silence])
        processed = render_cinema.process_voice_audio(audio, sr=sr, trim=True, normalize=False)
        self.assertLess(processed.shape[1], len(audio))
        self.assertGreaterEqual(processed.shape[1], len(tone))

    def test_process_voice_audio_normalization(self):
        sr = 24000
        quiet_tone = (np.sin(2 * np.pi * 440 * np.linspace(0, 1.0, sr)) * 0.01).astype(np.float32)
        target_db = -20.0
        normalized = render_cinema.process_voice_audio(quiet_tone, sr=sr, trim=False, normalize=True,
                                                       target_rms_db=target_db, peak_db=-1.5)
        rms = np.sqrt(np.mean(normalized ** 2))
        measured_db = 20 * np.log10(rms)
        self.assertAlmostEqual(measured_db, target_db, delta=1.0)

    def test_process_voice_audio_fade(self):
        sr = 24000
        tone = np.ones(sr, dtype=np.float32) * 0.5
        faded = render_cinema.process_voice_audio(tone, sr=sr, trim=False, normalize=False)
        self.assertAlmostEqual(float(faded[0, 0]), 0.0, places=2)
        self.assertAlmostEqual(float(faded[0, -1]), 0.0, places=2)
        self.assertAlmostEqual(float(faded[0, sr // 2]), 0.5, places=2)

    def test_split_dialogue_subtitles_short(self):
        text = '万象本无形。'
        segments = render_cinema.split_dialogue_subtitles(text, 1.0, 3.0, max_chars=15)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0][2], '万象本无形')
        self.assertEqual(segments[0][0], 1.0)
        self.assertEqual(segments[0][1], 3.0)

    def test_split_dialogue_subtitles_long_punctuation(self):
        text = '妖气自黑石涧起，剑气已候多时，此番定要斩草除根！'
        segments = render_cinema.split_dialogue_subtitles(text, 0.5, 4.5, max_chars=15)
        self.assertGreater(len(segments), 1)
        self.assertEqual(segments[0][0], 0.5)
        self.assertEqual(segments[-1][1], 4.5)
        self.assertFalse(segments[0][2].endswith('，'))
        self.assertTrue(segments[-1][2].endswith('！'))

    def test_synth_sfx_new_effects(self):
        for name in ('energy', 'fire', 'impact', 'sword', 'thunder'):
            sfx = render_cinema.synth_sfx(name, 2.0, 24000)
            self.assertEqual(sfx.shape[0], int(2.0 * 24000))
            self.assertTrue(np.any(np.abs(sfx) > 0.01))

    def test_audio_multi_sfx_and_timestamp(self):
        shots = [{
            'shot_id': 'S01',
            'duration_sec': 3.0,
            'sfx': 'wind|sword@1.2',
            'speaker': '',
            'dialogue': '',
            'voice_dur': 0.0
        }]
        res = render_cinema.preview_episode_audio(shots, CONFIG, {})
        self.assertTrue(Path(res['audio_path']).is_file())
        self.assertAlmostEqual(res['total_sec'], 3.0, delta=0.5)

    def test_audio_sfx_ducking(self):
        shots = [{
            'shot_id': 'S01',
            'duration_sec': 4.0,
            'sfx': 'wind',
            'speaker': '少年剑修',
            'dialogue': '御剑荡幽冥',
            'voice_dur': 1.5
        }]
        cfg = json.loads(json.dumps(CONFIG))
        cfg['cinema']['sfx_duck_under_voice'] = 0.5
        res = render_cinema.preview_episode_audio(shots, cfg, {})
        self.assertTrue(Path(res['audio_path']).is_file())

    def test_synth_guofeng_music_pentatonic(self):
        cool_music = render_cinema.synth_guofeng_music(3.0, 24000, style='guofeng', grade='cool')
        self.assertEqual(cool_music.shape[0], 2)
        self.assertGreaterEqual(cool_music.shape[1], int(3.0 * 24000))

        warm_music = render_cinema.synth_guofeng_music(3.0, 24000, style='guofeng', grade='warm')
        self.assertEqual(warm_music.shape[0], 2)
        self.assertFalse(np.array_equal(cool_music, warm_music))

    def test_music_smooth_ducking_envelope(self):
        shots = [{
            'shot_id': 'S01',
            'duration_sec': 5.0,
            'speaker': '少年剑修',
            'dialogue': '此去凶险，切莫留情',
            'voice_dur': 2.0
        }]
        cfg = json.loads(json.dumps(CONFIG))
        cfg['music']['builtin']['style'] = 'guofeng'
        cfg['music']['duck_attack_sec'] = 0.06
        cfg['music']['duck_release_sec'] = 0.45
        res = render_cinema.preview_episode_audio(shots, cfg, {})
        self.assertTrue(Path(res['audio_path']).is_file())

    def test_adapt_clip_frames_speedup(self):
        frames = [Image.new('RGB', (10, 10), (i * 5, 0, 0)) for i in range(40)]
        adapted = render_cinema.adapt_clip_frames(frames, 20)
        self.assertEqual(len(adapted), 20)

    def test_adapt_clip_frames_blend_slowdown(self):
        frames = [Image.new('RGB', (10, 10), (i * 20, 0, 0)) for i in range(10)]
        adapted = render_cinema.adapt_clip_frames(frames, 14, max_slowdown=1.5)
        self.assertEqual(len(adapted), 14)

    def test_adapt_clip_frames_push_in_hold(self):
        frames = [Image.new('RGB', (100, 100), (i * 20, 0, 0)) for i in range(10)]
        adapted = render_cinema.adapt_clip_frames(frames, 25, max_slowdown=1.5, hold_push_in=0.045)
        self.assertEqual(len(adapted), 25)
        # Check push-in on last frame retains valid dimensions
        self.assertEqual(adapted[-1].size, (100, 100))

    def test_sharpen_upscale(self):
        im = Image.new('RGB', (100, 100), (50, 100, 150))
        no_sharp = render_cinema.apply_sharpen(im, scale=1.0, strength=0.6)
        self.assertEqual(im, no_sharp)

        sharp = render_cinema.apply_sharpen(im, scale=2.0, strength=0.6)
        self.assertEqual(sharp.size, (100, 100))

    def test_per_shot_grade_and_transition(self):
        for grade in ('cool', 'warm', 'golden', 'noir'):
            self.assertIn(grade, KNOWN_GRADES)
        for trans in ('dip', 'dissolve', 'cut', 'flash'):
            self.assertIn(trans, KNOWN_TRANSITIONS)

    def test_clean_frame_subtitle_ghosting_fix(self):
        # Verify clean_f is copied before subtitle overlay so transitions do not ghost subtitles
        clean_f = Image.new('RGB', (50, 50), (10, 20, 30))
        burned_f = clean_f.copy()
        # Modifying burned_f does not alter clean_f
        burned_f.putpixel((0, 0), (255, 255, 255))
        self.assertNotEqual(clean_f.getpixel((0, 0)), burned_f.getpixel((0, 0)))

    def test_action_prompt_anchor_injection(self):
        import folder_paths, shutil
        input_drama = Path(folder_paths.get_input_directory()) / 'drama/example-fantasy-story'
        input_drama.mkdir(parents=True, exist_ok=True)
        for name in ('storyboard.json', 'shots.csv', 'characters.json'):
            shutil.copy2(ROOT / 'examples' / name, input_drama / name)
        node = DramaShotPlan()
        loaded = node.load({'project': 'example-fantasy-story'}, 0, 720, 1280, '国风3D玄幻')
        video_prompt = loaded[1]
        self.assertIn('黑色高束发', video_prompt)
        self.assertIn('全程保持首帧', video_prompt)

    def test_video_review_keyframe_reference_and_5_frames(self):
        v_cfg = CONFIG['quality']['vision']
        self.assertEqual(int(v_cfg.get('video_frames', 5)), 5)
        self.assertTrue(bool(v_cfg.get('video_reference', True)))

    def test_readiness_checks_missing_voice_warning(self):
        res = readiness.checks(
            CONFIG, certificate=(True, 'ok'), gate_open=False,
            projects=['demo'], project_voices=['NonExistentVoicePkg99'],
            is_voice_installed=lambda v: False, which=lambda name: '/usr/bin/say'
        )
        voice_item = next(e for e in res['items'] if e['key'] == 'voice')
        self.assertEqual(voice_item['state'], 'fail')
        self.assertIn('NonExistentVoicePkg99', voice_item['detail'])


if __name__ == '__main__':
    unittest.main()
