# Free local toolchain

## Cost rule

ElevenLabs transcription is the only permitted paid API. Everything else must run locally without a per-minute, per-render, seat, subscription, or cloud-compute charge. If pricing or licensing is unclear, do not use the component until the user explicitly decides.

## Approved default components

| Purpose | Default | Cost boundary |
|---|---|---|
| Decode, encode, filters, loudness | FFmpeg/ffprobe | Local open-source binary |
| Cards and frame inspection | Pillow + NumPy | Local open-source Python packages |
| Transcription | ElevenLabs Scribe | Only allowed paid API |
| macOS face tracking and short person mattes | Apple Vision | Local OS framework; no upload or model charge |
| Cross-platform tracking fallback | MediaPipe | Local Apache-2.0 package; input remains on device |
| HTML/CSS motion | HyperFrames 0.7.106 + GSAP 3.14.2 | Pinned local runtime; HyperFrames Apache-2.0, GSAP standard no-charge package license |
| Browser creative graphics | PixiJS + pixi-filters + Rough Notation + lottie-web + Three.js | Pinned audited local bundles; MIT packages, no render network |
| Future shader transitions | Reviewed gl-transitions source allowlist | Eight pinned MIT sources; currently non-callable until a seek-safe compositor and boundary-QA adapter exist |
| Shot boundaries and camera briefs | PySceneDetect + OpenCV headless | Isolated pinned local arm64 Python; analysis never chooses cuts |
| Rhythm/onset candidates | librosa + soundfile | Existing local Python runtime; candidates never override speech meaning |
| Creative sound accents | SPRUT deterministic SFX generator | Local procedural synthesis; no downloaded audio or API |
| Cyrillic typography | Unbounded, Golos Text, JetBrains Mono | Bundled local OFL-1.1 fonts and licenses |
| Technical diagrams | Manim Community | Local MIT-licensed renderer |
| Metadata/downloads | yt-dlp | Local open-source CLI; obey source rights and platform rules |

## Excluded by default

- Remotion, because current automation/company licensing may require payment.
- HeyGen generation/render APIs. HyperFrames local CLI is distinct and allowed.
- OpenAI image/video generation, avatar services, TTS, cloud render farms, paid stock, paid music, and paid analytics.
- Any tool that silently uploads source video or biometric data.

## Installation policy

Do not install every optional engine in advance. Use FFmpeg and Pillow for
standard graphics. Shared pinned runtimes live below the portable runtime root
reported by `doctor.py` (or configured with `VIDEOMONTAZHKA_RUNTIME_DIR`):
`hyperframes`, `creative-browser`, isolated `creative-python`, and isolated
`manim`. Do not run `npm install`, `pip install`, or an unpinned `npx --yes`
inside a video project. Verify the browser runtime with
`install_creative_browser_runtime.py --verify-only --smoke`, the camera runtime
with `install_creative_python_runtime.py --verify-only`, and Manim with
`install_manim_runtime.py --verify-only`. Scaffold an approval-bound local
effect only when the creative router selects it. Prefer Apple Vision on macOS;
use MediaPipe only when Apple Vision is unavailable or a cross-platform package
is required.

The creative browser installer removes `node_modules` after producing and
hashing the minimal runtime. Render pages may load only the copied bundles and
local assets; remote media, CDN fallbacks, and unlisted transition shaders are
blocked. The creative Python environment is separate from the base
Videomontazhka runtime so OpenCV cannot change the production NumPy/media stack.

HyperFrames telemetry must remain disabled. A render may read only the copied
local GSAP bundle, local fonts, user media, and project files. `doctor.py`
reports the exact CLI path/version and whether local rendering is ready.

Use Apple Vision person segmentation only for short approved effects. It
outputs video-only ProRes 4444, which is intentionally large. Do not process a
whole stream merely because a masked effect may be useful for a few seconds.
For VFR source material, first create the approved CFR effect interval. Vision
selects all people in the frame; inspect the matte and do not imply individual
subject selection.

Run `scripts/doctor.py` before each new project. `sprut-render-6` records the
exact FFmpeg/ffprobe paths, binary hashes, version strings/full-output hashes,
linked libass identity when discoverable, and renderer/approval/QA/schema code hashes in every
namespaced render manifest and `release_manifest_<artifact-key>.json`.
Burned subtitles additionally require local `fc-match`; the matched declared or
default font file path and SHA-256 become release-bound inputs.

## Audio policy

Analyze before processing. Apply only filters justified by the recording:

- high-pass for low-frequency rumble;
- narrow hum removal only when a stable mains tone exists;
- conservative denoise for steady background noise;
- de-essing only for excessive sibilance;
- gentle compression only when speech dynamics require it;
- two-pass loudness normalization last.

Create an A/B excerpt before applying denoise, de-essing, or compression to the complete program. Never repair a recording by making speech metallic.

Keep all reports and processed outputs in the canonical edit directory:

```bash
python scripts/audio_polish.py analyze \
  --edit-dir <videos_dir>/edit <source.mov> \
  -o <videos_dir>/edit/audio/analysis.json

python scripts/audio_polish.py preview \
  --edit-dir <videos_dir>/edit <source.mov> \
  --output-dir <videos_dir>/edit/audio/ab --denoise 8 \
  --reason 'steady background noise is audible'

python scripts/audio_polish.py approve \
  --edit-dir <videos_dir>/edit \
  --decision <videos_dir>/edit/audio/ab/ab_decision.json \
  --quote '<exact user approval of preview B>'

python scripts/audio_polish.py apply \
  --edit-dir <videos_dir>/edit <source.mov> \
  -o <videos_dir>/edit/audio/polished.mov --denoise 8 \
  --reason 'steady background noise is audible' \
  --approval <videos_dir>/edit/audio/ab/ab_approval.json
```

`analyze` is allowed before semantic approval because it only measures the source and writes an analysis report. `preview`, `approve`, and `apply` invoke the no-EDL asset gate before creating a directory or file; a missing approval or changed semantic plan blocks them. Preview writes `ab_decision.json` with the exact source SHA-256, excerpt start/end/duration, filter, reason, and hashes of both WAVs. After the user listens, `approve` records the exact approval quote and an approval binding hash in `ab_approval.json`. Apply accepts only `--approval`; it re-hashes the source, decision, and both previews and requires the current filter/reason arguments to match before creating its output.

For creative timing, `analyze_rhythm.py` may produce a local candidate-only
beat/onset map. `generate_creative_sfx.py` provides deterministic semantic hit,
soft pop, UI tick, digital reveal, whoosh, reverse swell, sub drop, glitch
accent, and marker-stroke presets. Both are approval-bound for production use;
neither downloads music or sound effects.

## Asset policy

Prefer user-owned footage, programmatic shapes, screenshots the user may use, and locally generated diagrams. For external music, images, video, or SFX, use only assets whose license permits the intended publication. Store the source URL, license, author, and retrieval date in `edit/assets_manifest.json`. Do not assume that a publicly downloadable asset is reusable.
