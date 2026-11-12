# Validation boundary

## Static build

The source workflow was recorded at 40 nodes and 71 links. The original local build report recorded 15 offline structure/configuration checks and 27 mocked production-logic tests passing, with model inference disabled. No model was started during those checks.

This public distribution replaces host-specific paths and sample story data, then regenerates its hash certificate over the published build files. Its bundled logic tests are provided for local use and were not rerun after the publication-only edits.

## Dynamic production

The complete motion path has not passed visual acceptance. Earlier Wan/Animate2 outputs showed identity, costume, pose, or style drift; the final Animate2 attempt did not complete. The job did not reach final voice/subtitle composition or MP4 delivery. No sample output video is included.

Do not treat a loaded canvas, a successful ComfyUI queue submission, a technically encoded clip, or a passing still-frame check as proof of a complete short-film deliverable. Validate the generated frames and final audio/video on your own setup.

## Safe default

`drama/build_gate.json` sets `inference_enabled` to `false`. This prevents the included production workers from submitting generation requests until an operator reviews the graph, model paths, and machine capacity and explicitly changes the local gate.
