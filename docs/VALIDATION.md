# Validation boundary

## Static build

The source workflow was recorded at 40 nodes and 71 links. The original local build report (2026-11-12) recorded 15 offline structure/configuration checks and 27 mocked production-logic tests passing, with model inference disabled. No model was started during those checks. The scripts behind those 15 checks were not part of the public distribution.

## v1.0.0 checks

Rerun on 2026-11-17 in a clean Linux container (Python 3.13) with no ComfyUI, no model files and no GPU:

- `python drama/workflow_build.py --verify`: 11 offline structure checks pass. They cover file presence, Python and JSON syntax, canvas link integrity (40 nodes / 71 links on the main canvas), required node types, per-stage compilation of the main and Animate2 API graphs, every model loader being wired behind the start gate, preset size/frame constraints, the example storyboard, and the gate file format.
- `python -m unittest discover -s tests`: 47 tests pass. 27 are the original mocked production-logic tests (retry, resume, recovery). 20 are new and cover the build certificate, the readiness checklist, storyboard summaries, the installer and the queue HTTP API. The API tests run the real `production_server.py` against stand-ins for ComfyUI's `folder_paths` and `server` modules.
- The production page was exercised in headless Chromium against demo data served by `tools/preview_page.py`: readiness list, storyboard preview, upload accept/reject, start while locked, log viewer, single-job view, dark/light and 390 px width.

`drama/BUILD_VERIFIED.json` and `SHA256SUMS` were regenerated after these changes.

What these checks do not cover: the page and server have not been run inside a real ComfyUI process for this release; the installer was tested against a temporary folder shaped like a ComfyUI root; the macOS-only parts (`say` voices, the default subtitle font) were not executed.

## v1.1.0 checks

Run on 2026-11-17 in the same kind of clean Linux container (Python 3.13, no ComfyUI, no models, no GPU, no macOS `say`):

- `python drama/workflow_build.py --verify`: the same 11 offline structure checks pass. The API graphs and canvases are unchanged in topology; v1.1.0 changes what the nodes compute, not how they are wired.
- `python -m unittest discover -s tests`: 74 tests pass. The 27 new ones in `tests/test_voice_visual.py` cover casting order and the per-character voice regression, engine command construction, `say -v ?` parsing, line trimming and loudness, phrase-level subtitle timing, timeline planning, the ducking envelope, SFX placement and vocabulary, clip retiming and frame blending, sharpening, the four transitions, an end-to-end `encode()` of two clips that checks frame count and that no subtitle leaks into the next shot's transition, the motion prompt, review frame sampling, the video review payload (keyframe first), and the voice-preview, soundtrack, preview-file and job-media endpoints.
- Voice in all of the above comes from `tests/fake_tts.py`, a stand-in that writes a tone. Real speech engines were not run.
- The production page was exercised in headless Chromium against `tools/preview_page.py --ready`: speaker and per-line voice preview, the whole-episode soundtrack (player, adjusted durations, subtitle segments, SRT), keyframe thumbnails and the side-by-side media dialog, and 390 px width without horizontal scrolling.

Not verified in v1.1.0, because they need real models: whether adding character anchors to the Wan/Animate2 action prompt reduces drift, and whether the keyframe-referenced, five-frame video review catches drift without rejecting good clips. Treat both as hypotheses until checked on your own setup.

## Dynamic production

The complete motion path has not passed visual acceptance. Earlier Wan/Animate2 outputs showed identity, costume, pose, or style drift; the final Animate2 attempt did not complete. The job did not reach final voice/subtitle composition or MP4 delivery. No sample output video is included. Nothing in v1.0.0 or v1.1.0 changes this.

Do not treat a loaded canvas, a green readiness checklist, a successful ComfyUI queue submission, a technically encoded clip, or a passing still-frame check as proof of a complete short-film deliverable. Validate the generated frames and final audio/video on your own setup.

## Safe default

`drama/build_gate.json` sets `inference_enabled` to `false`. This prevents the included production workers from submitting generation requests until an operator reviews the graph, model paths, and machine capacity and explicitly changes the local gate. The installer writes the gate back to this closed state whenever it installs or replaces code or workflow files.
