# Creative direction and tool routing

## Purpose

Turn an approved meaning plan into deliberate visual and sonic treatment. Use
the complete *relevant* arsenal: evaluate every approved narrative beat, choose
the best justified technique, or record `none` with a concrete reason. Never
interpret “use the arsenal” as permission to stack unrelated effects.

## Required decision pass

After semantic approval and before generating an EDL or asset:

1. Run `scripts/creative_tool_registry.py --json` and use only capabilities
   reported as `ready` or an explicitly approved `pilot`.
2. Create one routing input item for every approved `visual_plan` item and any
   editorial beat that may need camera or sound treatment.
3. Store strict scene-feature documents under `edit/creative/`, run
   `scripts/creative_tool_router.py route` for each approved visual, and keep
   each hash-bound decision beside its feature document. Compile those
   decisions into `edit/creative_treatment_plan.json`; never hand-edit a tool
   from unavailable to ready.
4. Review the plan as an editorial decision. A machine recommendation does not
   authorize new words, unsupported evidence, unsafe geometry, or an unapproved
   asset.
5. Keep the chosen technique, timing motive, protected regions, tool fallback,
   and `none` reasons visible in the project record and asset provenance.

The router is a guardrail and capability matcher. The editor remains
responsible for judging tone, pacing, visual hierarchy, repetition, and whether
a quiet hold is stronger than another accent.

Minimal production sequence:

```bash
python scripts/creative_tool_registry.py --json > <edit>/creative/tool-registry.json
python scripts/creative_tool_router.py availability \
  --registry-report <edit>/creative/tool-registry.json \
  --array-only > <edit>/creative/available-tools.json
python scripts/creative_tool_router.py route \
  --edit-dir <edit> \
  --input <edit>/creative/<visual-id>.features.json \
  --output <edit>/creative/<visual-id>.decision.json
python scripts/compile_creative_treatment_plan.py \
  --edit-dir <edit> \
  --decision creative/<visual-a>.decision.json \
  --decision creative/<visual-b>.decision.json
```

The feature document binds the exact semantic-plan/approval hashes and visual
ID. It records explicit semantic signals; the router does not infer them from
words. Its availability list must be derived from the current registry, not
from what was installed months ago. The decision also records rejected tools,
density state, required QA, and the exact hashes of the router, schemas, and
map. The compiler fails unless there is exactly one current, approval-bound
decision for every approved `visual_plan` entry, including explicit `none`.

## Meaning-to-technique map

| Meaning or screen condition | Preferred treatment | Primary engine | Use only when | Avoid when |
|---|---|---|---|---|
| Surprising correction, concise hook, key term | Kinetic word/line reveal, split-word stagger, restrained scramble | HyperFrames + GSAP SplitText/ScrambleText | The exact visible words are approved and short | Dense explanation or continuous captions |
| Transformation, before/after, one concept becoming another | Shape/icon/text morph | GSAP MorphSVG/Flip | Two states genuinely express the argument | Morph is decorative or source geometry is unclear |
| Route, handoff, information flow, causal path | Drawn line, moving dot, sequential node activation | GSAP DrawSVG/MotionPath or Manim | Order and direction matter | Static comparison would be clearer |
| Number, score, scale, result | Stat hit, counter, progress rail | Motion Kit/GSAP | Number is source-backed | Number is inferred or approximate without context |
| UI instruction or exact screen location | Tracked callout, rough underline/circle, spotlight | Rough Notation/GSAP + source frame | Anchor remains stable and important UI stays visible | Pointer drifts across a source cut |
| Complex system, state machine, algorithm | Vector reconstruction and staged reveal | Manim or Lottie | Relationships need explanation at 1× | A screenshot already communicates it clearly |
| Data cloud, vectors, network, energy or payoff | Deterministic particles, glow, shockwave, displacement | PixiJS + curated filters | Short 0.4–3 s accent supports the spoken idea | Across a whole section or over detailed UI |
| Chapter or genuine time/topic change | Hard cut with branded bridge | Motion Kit | New chapter is approved | Ordinary sentence-to-sentence cuts; shader transitions remain unavailable until their compositor is audited |
| Emotional or technical rupture, failed state | Very short glitch/RGB split/pixel or freeze treatment | Curated frei0r/Pixi recipe | Source meaning itself is disruption/failure | Presenter face or speech is continuously distorted |
| Important face/presenter statement | Shot-aware punch-in with enter/hold/exit | Virtual-camera solver | One continuous shot and protected regions allow it | Across source cuts or when 720p crop becomes visibly soft |
| Diagram detail | Context first, then stable focus and pullback | Virtual camera + diagram focus | Labels remain legible | Continuous tracker-driven micro-motion |
| Foreground/background relationship | Text/particles behind subject | Apple Vision matte; optional depth pilot | Short reviewed interval with stable edges | Long programme, fast hands/hair, weak matte |
| Visual depth or reveal in still/B-roll | 2.5D parallax | Depth Anything V2 Small/CoreML pilot | User-approved short experiment | Hour-long processing or non-commercial model variants |
| Speech under music | Automatic dialogue ducking | FFmpeg sidechaincompress | Licensed/user-owned music is present | Music is already inaudible or absent |
| Graphic hit, list step, UI action | Click/pop/tick/whoosh/hit | Local procedural SFX | Sound has a precise semantic landing | Every cut or every on-screen word |
| Music-led montage | Snap non-speech visuals and accents to nearby onsets | librosa rhythm map | Moving the visual does not cut through speech | Beat timing would damage sentence cadence |
| No improvement from an effect | Intentional clean hold or hard cut | `none` | Clarity and rhythm are already strong | Never invent an effect just to satisfy a quota |

## Density and variation

- Protect comprehension first. Do not reveal two independent new concepts at
  the same time.
- Reserve strong accents for the hook, major turn, proof, and payoff. Use
  medium treatments for mechanisms and examples; let setup and caveats breathe.
- Do not repeat the same strong treatment in adjacent beats. Prefer a different
  visual grammar or `none`.
- A long-form chapter should normally contain breathing intervals without
  overlays. A short may be denser, but every accent still needs a semantic
  landing.
- Use one dominant visual action per frame. SFX may reinforce that action, not
  compete with it.
- Never let beat synchronization move a speech boundary inside a word or erase
  a rhetorical pause approved by the semantic plan.

## Virtual-camera contract

- Detect/reset at every source or shot boundary. Never run one continuous
  `zoompan` trajectory across edited source ranges.
- Build a deliberate sequence: 8–12 frame enter, static or nearly static hold,
  8–12 frame exit. Use a stronger 1.20–1.35 punch only when source resolution
  and protected regions permit it.
- Smooth position, scale, velocity, and acceleration. Render with sub-pixel
  transforms or an enlarged intermediate canvas, then downscale.
- Ignore face micro-jitter with a dead zone. Do not let a tracker choose
  editorial emphasis.
- For diagrams, show context before detail and return to context when the
  relationship is the payoff.
- `plan_virtual_camera.py` requires `--edit-dir` and an approved brief under
  that directory. A brief below 1.12× is rejected as imperceptible motion; use
  a clean hold instead. Render one continuous approved event with
  `render_virtual_camera.py` in the isolated creative-Python runtime. It uses
  sub-pixel Lanczos affine sampling, produces a silent source-backed ProRes
  intermediate, and must be reviewed at exact entry/exit boundaries.

## Browser and shader safety

- Load only pinned local JavaScript bundles. Disable network access, telemetry,
  nondeterministic time, and unseeded randomness.
- Treat the gl-transitions package as a source collection, not an automatic
  license grant or a callable effect. The router must report it unavailable
  until a dedicated seek-safe compositor and boundary-QA adapter exist. If
  that adapter is added later, only curated shaders with recorded
  header/license and hash may render.
- Render novel alpha effects to a fixed local asset, normalize to ProRes 4444,
  record approval-bound provenance, build a 3–4-frame sheet, and inspect the
  full composite.
- Use Pixi filters and frei0r as short recipes with fixed parameters. A library
  containing 100 effects is not permission to expose all of them to automatic
  selection.

The installed shared browser runtime currently provides pinned local PixiJS,
pixi-filters, Rough Notation, lottie-web, Three.js, and a reviewed eight-shader
gl-transitions source allowlist. The last item is deliberately non-callable.
Verify the runtime before a new browser effect:

```bash
python scripts/install_creative_browser_runtime.py --verify-only --smoke
```

The isolated scene-analysis runtime lives beside it. Verify it with
`install_creative_python_runtime.py --verify-only`; run `detect_shots.py` with
that runtime's Python. Do not install either dependency set into the base
Videomontazhka runtime or into an individual video project.

## Audio routing

- Generate `edit/audio/rhythm_map.json` only from local media. Treat BPM/onsets
  as suggestions and store confidence.
- Keep dialogue as the timing master. Snap graphic/SFX landings to a nearby
  onset only inside an editorially safe window.
- Prefer deterministic local SFX. External audio requires author, URL, license,
  retrieval date, and hash in `edit/assets_manifest.json`.
- Duck licensed music under speech and normalize the final mix. Do not add
  music merely because the analyzer exists.
- Run exact-boundary audio QA after compositing; no click, spike, clipping, or
  accidental doubled source audio is allowed.

## Experimental tier

Depth Anything, Three.js hero scenes, G'MIC treatments, Blender, Gyroflow,
source separation, and neural denoisers remain opt-in pilots. Require a short
sample, explicit purpose, resource estimate, license check, and user visual/A/B
approval before promotion into the default registry. Never make them required
for an ordinary edit.

At present, Three.js is installed but remains explicit/hero-only; Depth
Anything, G'MIC, Blender, Gyroflow, source separation, and neural denoisers are
not installed. Manim 0.20.1 is installed for vector architecture/technical
diagrams; equations remain unavailable until a local TeX runtime is added. A
map entry is not proof that a runtime is available.
