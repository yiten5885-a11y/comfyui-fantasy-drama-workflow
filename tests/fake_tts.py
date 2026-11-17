#!/usr/bin/env python3
"""Fake TTS engine for tests and preview: outputs clean chime tones.

Accepts placeholder flags from voice.command:
    python tests/fake_tts.py --output_file <out> [--voice <v>] [--rate <r>] [--text <t>] [--text_file <f>]
If neither --text nor --text_file is provided, text is read from stdin.
Standard library only: produces a valid 16-bit PCM mono 44.1kHz WAV file.
"""
import argparse
import math
import os
from pathlib import Path
import struct
import sys
import wave


def generate_tone_wav(out_path, text, voice="test", rate=170):
    rate = max(60, min(400, int(rate)))
    text = (text or "").strip()
    char_count = max(1, len(text))
    # Duration proportional to character count and speech rate
    sec_per_char = 60.0 / rate
    dur = max(0.4, min(10.0, char_count * sec_per_char * 0.9))

    sample_rate = 44100
    n_samples = int(dur * sample_rate)

    # Base frequency determined deterministically by voice name
    v_hash = sum(ord(c) for c in str(voice)) % 12
    base_freq = 220.0 * (2.0 ** (v_hash / 12.0))  # Between 220Hz and 440Hz

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)

        fade_len = int(0.015 * sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            t = i / sample_rate
            # Dual harmonic tone + pleasant envelope
            amp = 0.5 * math.sin(2 * math.pi * base_freq * t) + 0.25 * math.sin(2 * math.pi * base_freq * 2 * t)
            # Envelope with fade-in and exponential decay
            env = min(1.0, i / max(1, fade_len)) * math.exp(-1.5 * t / dur)
            val = int(amp * env * 24000)
            val = max(-32767, min(32767, val))
            frames.extend(struct.pack("<h", val))

        wf.writeframes(frames)


def main():
    parser = argparse.ArgumentParser(description="Fake TTS tool for testing")
    parser.add_argument("--output_file", "-o", "--out", required=True, help="Output audio file path")
    parser.add_argument("--model", help="Model path alias for voice")
    parser.add_argument("--voice", "-v", default="test", help="Voice identifier")
    parser.add_argument("--rate", "-r", type=int, default=170, help="Words per minute")
    parser.add_argument("--text", "-t", help="Dialogue text")
    parser.add_argument("--text_file", "-f", help="Path to text file")

    args = parser.parse_args()

    voice = args.voice or args.model or "test"
    text = args.text
    if text is None and args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    elif text is None:
        if not sys.stdin.isatty():
            text = sys.stdin.read()
        else:
            text = "测试提示音"

    generate_tone_wav(args.output_file, text, voice=voice, rate=args.rate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
