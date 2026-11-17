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

## Dynamic production

The complete motion path has not passed visual acceptance. Earlier Wan/Animate2 outputs showed identity, costume, pose, or style drift; the final Animate2 attempt did not complete. The job did not reach final voice/subtitle composition or MP4 delivery. No sample output video is included. Nothing in v1.0.0 changes this.

Do not treat a loaded canvas, a green readiness checklist, a successful ComfyUI queue submission, a technically encoded clip, or a passing still-frame check as proof of a complete short-film deliverable. Validate the generated frames and final audio/video on your own setup.

## Safe default

`drama/build_gate.json` sets `inference_enabled` to `false`. This prevents the included production workers from submitting generation requests until an operator reviews the graph, model paths, and machine capacity and explicitly changes the local gate. The installer writes the gate back to this closed state whenever it installs or replaces code or workflow files.
