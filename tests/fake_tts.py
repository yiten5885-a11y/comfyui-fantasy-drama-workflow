"""Stand-in text-to-speech program for tests and tools/preview_page.py.

    python fake_tts.py OUT.wav VOICE RATE < text

Writes a mono WAV: 0.3 s of silence, a tone whose length follows the text and
the rate (pitch follows the voice name), then 0.4 s of silence. The silence is
there so the renderer's trimming can be checked.
"""
import math
import struct
import sys
import wave
import zlib


def main():
    out, voice, rate = sys.argv[1], sys.argv[2], int(sys.argv[3])
    text = sys.stdin.read().strip()
    if not text:
        print('empty text', file=sys.stderr)
        return 2
    sr = 22050
    pitch = 160 + zlib.crc32(voice.encode()) % 160
    spoken = max(1, sum(1 for ch in text if ch.strip() and ch not in '，。！？,.!?')) * 60.0 / rate * 1.6
    samples = [0.0] * int(0.3 * sr)
    for i in range(int(spoken * sr)):
        t = i / sr
        syllable = abs(math.sin(math.pi * t * rate / 60.0 / 1.6))
        samples.append(0.3 * syllable * math.sin(2 * math.pi * pitch * t))
    samples += [0.0] * int(0.4 * sr)
    with wave.open(out, 'wb') as handle:
        handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(sr)
        handle.writeframes(b''.join(struct.pack('<h', int(x * 32767)) for x in samples))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
