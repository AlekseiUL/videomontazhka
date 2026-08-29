# Project format

## Required artifacts

```text
<videos_dir>/
├── <untouched source media>
└── edit/
    ├── project.json
    ├── source_manifest.json
    ├── transcripts/                 # audio sources: <source_id>.json plus .metadata/<source_id>.json
    ├── takes_packed.md
    ├── takes_packed_manifest.json
    ├── research.md                 # only when relevance research is used
    ├── semantic_plan.json
    ├── approval.json
    ├── creative/
    │   ├── tool-registry.json      # current local readiness snapshot
    │   ├── <visual-id>.features.json
    │   └── <visual-id>.decision.json
    ├── creative_treatment_plan.json # compiled purpose/tool/none decisions
    ├── edl_<deliverable>.json
    ├── animations/
    │   └── hyperframes/instances/<visual-id>/  # offline Motion Kit source + source-manifest.json
    ├── mattes/<visual-id>/          # local Apple Vision matte/foreground artifacts
    ├── audio/ab/                    # A/B WAVs, ab_decision.json, ab_approval.json
    ├── audio/rhythm_map.json        # candidate-only local beat/onset analysis
    ├── analysis/shot_boundaries.json # source-local resets, never auto-cuts
    ├── camera/<deliverable>.json    # reviewed shot-reset camera plan
    ├── work/
    ├── cache/                       # segment .mov + content-attestation sidecars
    ├── verify/visual_preview_<id>_<signature>/ # mandatory 3–4-frame effect sheet
    ├── verify/transition_qa_<id>_<signature>.json
    ├── verify/<artifact-key>/<draft|preview|final>/
    ├── <deliverable-preview>.mp4
    ├── render_manifest_<artifact-key>_preview.json
    ├── preview_approval_<artifact-key>.json
    ├── <deliverable-final>.mp4
    ├── render_manifest_<artifact-key>_final.json
    └── release_manifest_<artifact-key>.json
```

`semantic_plan.json` is the human-approved editorial contract. `approval.json`
binds approval to the exact plan hash. Each canonical
`edl_<deliverable>.json` is an implementation of that
approved plan and must not silently change its promise, evidence, narrative, or
chosen ending. A changed plan, EDL, source, preview, subtitle, overlay, or SFX
asset invalidates the corresponding approval.

Creative routing is a production decision derived from—not a replacement
for—the approved semantic plan. Every feature input and decision binds the
exact visual ID, section, meaning IDs, plan hash, and approval hash. A registry
snapshot records what is actually ready on the current machine; a map entry for
an unavailable tool does not authorize it. `decision: "none"` is a first-class
result. The compiled creative treatment plan records one primary visual or
none, at most one supporting audio accent, density state, rejected fallbacks,
and required QA for each routed beat.

`<artifact-key>` is derived from the exact EDL `deliverable_id`: a lowercase
ASCII-safe slug (maximum 48 characters) plus the first 12 hex characters of the
ID's SHA-256. The hash suffix prevents sanitized IDs from colliding, while the
restricted alphabet prevents path traversal. The original `deliverable_id` and
derived key are both recorded in every render, approval, QA, and release
manifest. Singleton render/approval/release aliases are not canonical.

Schemas use JSON Schema Draft 2020-12 and reject unknown fields in the stable
objects. Put a new field into the schema and this document before relying on it;
do not hide renderer options in arbitrary metadata.

Before creating a motion card, SFX file, A/B audio preview, or polished audio
intermediate, run `scripts/validate_gate.py --edit-dir <videos_dir>/edit --phase
asset`. This phase validates the complete project/source/transcript/semantic
plan and its exact approval hash but deliberately does not require an EDL. The
bundled asset writers run it automatically with the current Python executable
and refuse paths outside the canonical `edit/` directory. Audio `analyze` is
the sole pre-approval exception and may write only its measurement report under
that directory.

Motion Kit instances are source assets, not finished overlays. Their
`source-manifest.json` binds the approved visual item, template, local GSAP,
fonts, OFL files, configuration, and generator. A rendered overlay still needs
`normalize_hyperframes_alpha.py` when its WebM carries alpha, followed by
`record_visual_asset.py` provenance. A novel effect additionally needs a
`build_visual_preview_sheet.py` artifact and explicit visual review; a cover
wipe also needs a passing `qa_transition_asset.py` report. These review files
never replace full-preview approval. Person-matte outputs likewise stay under
`edit/`, contain no audio, bind the local input hash, and are intended only for
short approved effect intervals.

### Audio A/B approval

`audio_polish.py preview` writes a version-2 `ab_decision.json` beside
`A_original.wav` and `B_processed.wav`. The decision records the immutable
source path and SHA-256, exact excerpt start/end/duration, canonical FFmpeg
filter, restoration reason, and SHA-256 of each preview WAV. It remains in
`awaiting_user_ab_approval` status.

After the user listens to those exact bytes, run `audio_polish.py approve` with
the decision and exact user quote. The resulting `ab_approval.json` has
`status: "approved"`, hashes the complete decision, repeats every approved
source/bounds/filter/artifact field, and carries a canonical binding SHA-256
over those fields plus the quote and status. Full processing accepts only this
approval artifact through `--approval`. It rejects a changed source, decision,
preview WAV, approval field, filter/reason argument, missing quote, or non-
approved status before creating or replacing output media.

## Project fields

- `version`: format version.
- `name`: project name.
- `source_mode`: `auto`, `long_stream`, `multi_take`, or `mixed`. `auto` is
  analysis-only; before EDL it must be resolved and exactly match the approved
  semantic plan.
- `paid_api_allowlist`: exactly `["elevenlabs"]`.
- `brand`: palette and optional logo/font paths.
- `presenter`: unresolved `auto` during analysis; resolved geometry before render.
- `deliverables`: platform, aspect, dimensions, FPS, subtitles, and target duration.
- `evidence`: required modality and transcript/description binding defaults.
- `audio`: analysis and processing policy, including default unapproved-silence limits (`max_unapproved_boundary_silence_s: 0.35` and `max_unapproved_internal_silence_s: 0.75`).
- `transitions`: semantic transition defaults.
- `qa`: blocking checks and safe areas.

Keep paths relative to the `edit/` directory whenever possible. Absolute source
paths are allowed in `source_manifest.json` when media resides outside the
project. An EDL source ID and path must resolve to the same immutable file that
was hashed in `source_manifest.json`.

New source manifests record coded dimensions, display dimensions, and the
right-angle display rotation reported by FFprobe. ROIs always use the display
orientation. Non-right-angle rotation is rejected at ingest because crop and
protected-region geometry would otherwise be ambiguous.

## Semantic plan

The schema lives at `assets/semantic-plan.schema.json`. Set `status` to
`pending` until explicit approval. Do not edit it manually to bypass the gate;
use `scripts/record_approval.py` to create `approval.json`.

### Source truth and narrative

Every `source_truth` item has:

- stable `id`;
- concise `meaning` stated without adding claims;
- one or more evidence records containing `source`, `start`, `end`, and an
  explicit `modality`;
- `speech` evidence contains only a verbatim `quote` and requires an audio
  source with a canonical word-level transcript;
- `visual` evidence contains only a factual `description` of what is visible
  in the cited interval and is not transcript-bound.

Every evidence record also has a stable, plan-wide unique `id`. An evidence
interval may span at most 180 seconds; split longer material into several
focused evidence IDs instead of using a whole stream as a universal proof.
Before EDL or render validation, the gate binds every normalized speech
evidence quote to a contiguous sequence of word-timestamp tokens in the
matching raw `transcripts/<source_id>.json`, allowing at most 0.25 seconds
outside the declared evidence boundaries. Audio sources require canonical
Scribe metadata and current transcript hashes. A manifest source whose `audio`
value is `null` instead requires the canonical packed entry
`{"source":"<id>","source_sha256":"<64-hex>","visual_only":true,"duration_s":12.345,"phrases":0}`;
it has no transcript or metadata fields and accepts visual evidence only. Both
paths require the exact source-ID set, current source hashes, and the packed
Markdown hash recorded by `takes_packed_manifest.json`; rerun transcription and
`pack_transcripts_safe.py` when any required record is missing or stale.

Every narrative section has `id`, `title`, `purpose`, `meaning_ids`, `payoff`,
and `estimated_duration_s`. `meaning_ids` must point back to source truth. The
optional `bridge_from_previous` describes the logical connection, not a visual
transition.

`keep`, `cut`, and `clarify` accept either a concise string or a structured
decision with `text`, `reason`, and optional `meaning_ids`. Use the structured
form whenever the decision could otherwise be ambiguous.

Every `visual_plan` item has a stable `id`, one
approved `section_id`, non-empty `meaning_ids`, `treatment`, `purpose`, and
`approved_text`, plus a required `asset_type` (`none`, `title`, `chapter`,
`diagram`, `comparison`, `process`, `quote`, `cta`, or `b_roll`). Set
`approved_text` to `null` for a genuinely no-text visual. The root
`visual_plan` array itself is required and may be empty only when the approved
edit needs no added visual. The root `audio_plan` object is also required, even
when its policy is simply to preserve/mute source audio without processing.
The meaning IDs must belong to the declared section. This metadata is part of
the textual approval and is not an informal production note.

### Hooks, ending, and deliverables

Each hook is a real candidate, not an untyped note:

```json
{
  "id": "hook_problem_solution",
  "text": "Память агента — это не пересказ разговора.",
  "payoff": "Покажем, что именно нужно сохранять и почему.",
  "meaning_ids": ["memory_result"],
  "opening_visual": "Крупный тезис, затем исходная схема",
  "why_it_works": "Разрушает распространённое ожидание",
  "estimated_duration_s": 8.0
}
```

Provide at least two hooks, give every hook one or more evidence-backed
`meaning_ids`, and set the required `recommended_hook_id`. A hook may improve
clarity and relevance, but its promised payoff must exist in the approved
narrative and source evidence. The recommended hook meanings belong to the
first global narrative section. Each deliverable separately selects its
approved `hook_id`; that hook's meanings must belong to the first section in
that deliverable's scope.

`ending.section_id`, `ending.meaning_ids`, and `ending.takeaway` are required.
The section must be the final narrative section and its ending meanings must
belong to that section. Optional `closing_line`, `cta`, and `end_card` make the
conclusion explicit. `cta` may be `null`; never invent an offer or destination
that the author did not approve.

Every deliverable explicitly declares a stable, unique `id`, `platform`, even
`width` and `height`, `fps`, `target_duration_s`, `subtitle_mode`, ordered
`section_ids`, `hook_id`, and `ending_section_id`. The section IDs must be
unique and exist in the global narrative. Their array order is the explicitly
approved order for that deliverable; a short may omit or intentionally reorder
global sections. `ending_section_id` must be the last scoped section. Optional
fields are `format`, `language`, and `notes`.
A separate long-form video and each short are separate deliverables, even when
they share source meanings.
Give each deliverable its own EDL and explicit in-tree preview/final media path;
the renderer derives all supporting artifact namespaces from the EDL ID.

## EDL

The schema lives at `assets/edl.schema.json`. The EDL root contains an
`approval_plan_sha256` matching `approval.json`; the renderer must reject a
mismatch. It also declares `deliverable_id`, `hook_id`, explicit `output`,
resolved `layout_plan`, `subtitle_mode`, and `audio.filters`. `deliverable_id`
selects exactly one deliverable from the approved semantic plan; `hook_id` must
equal that selected deliverable's `hook_id`. The gate requires the EDL width,
height, rational FPS value, subtitle mode, scoped sections, opening hook, and
ending section to match the selected deliverable exactly. The sum of retained
ranges must stay within 5% of the approved target duration (with a two-second
minimum tolerance). A larger change, or any change to the exact output/scope
fields, requires a revised semantic approval.

### Ranges and editorial traceability

Each range contains:

- `source`, `start`, and `end`;
- `section_id` and one or more `meaning_ids` from the approved plan;
- one or more `evidence_ids` that bind those meanings to approved source truth;
- required `audio_mode`: `source` for speech evidence, otherwise `mute`;
- the exact speech `quote`, factual visual `description`, or both as required
  by the selected evidence modalities, plus editorial `reason`;
- optional `intentional_pause_reason` when a long rhetorical pause is deliberately retained;
- explicit `transition_after`;
- optional `crop`, `view_filter`, and `transition_reason`.

Every narrative section scoped by the selected deliverable must appear in at
least one retained range; sections outside that scope are rejected. Within each
scoped section, the ranges must collectively carry every `meaning_id` approved
for that section. Range section indices must be non-decreasing in the
deliverable's approved order: after entering a later scoped section, the edit
cannot silently return to an earlier one. The first range must use the first
scoped section and contain all meanings of the deliverable hook. The final
range must use `ending_section_id`; the section's ranges collectively retain
all meanings approved for that ending section.

Provenance is blocking, not advisory. Every range meaning must be covered by at
least one selected evidence ID, and every selected evidence must belong to one
of that range's meanings. Its source ID must equal the range source, and the
range must intersect every selected evidence interval. The full range must stay
inside the combined selected-evidence envelope, allowing at most 0.25 seconds
of padding at either outside boundary. For quote comparison the gate applies
Unicode NFKC normalization, case folding, `ё`→`е`, removes punctuation and
underscores, and collapses whitespace. Every normalized approved speech quote
must occur in the range `quote`, and every normalized approved visual
description must occur in the range `description`. The complete normalized
range quote—not only the shorter approved evidence quote—must equal every
transcript word whose timestamp midpoint survives the exact range boundaries.
This prevents fabricated additions, hidden retained words, and a quote that
pretends an audible hesitation was cut. Unambiguous vocalized fillers such as
`um`, `uh`, `эээ`, and `эм` are blocking whether Scribe labels them as `word`
or `audio_event`; split the range around them. The gate retains timed `word`,
`spacing`, and `audio_event` entries and measures silence around/between audible
items. By default, boundary silence over 0.35 seconds and an internal gap over
0.75 seconds fail. A non-empty `intentional_pause_reason` is the only exception
and makes that decision reviewable. Lexical habits such as `ну` still require
editorial judgment rather than a blind dictionary deletion.

Every range declares its sound explicitly. A speech-backed range must use
`audio_mode: "source"`. A range without speech evidence must use `mute`, even
when its video file contains an audio track. A manifest source with `audio:
null` also requires `mute`.

`crop` may be a safe FFmpeg crop string, `[width, height, x, y]`, or an object
with `w`/`h` (or `width`/`height`) and optional `x`/`y`. `view_filter` is a simple
comma-separated FFmpeg filter chain. Both reject filtergraph separators `;`,
`[` and `]`; do not use them to inject extra inputs, outputs, or maps.

Any transition other than `hard_cut` requires `transition_reason` tied to a
meaningful editorial purpose. The canonical renderer currently has verified
hard cuts only and must reject the other declared transition types until their
picture and audio behavior has its own boundary QA. Use a separately rendered
full-frame chapter card as an overlay when a semantic bridge is needed today.

### Presenter geometry, composition, ROIs, and output boxes

One `layout_plan` entry must cover every retained range. The entry separates
what exists in the source from where it belongs in the output:

- `source_class`: `already_circular`, `isolated_subject`,
  `rectangular_with_context`, `full_frame_presenter`, or `screen_only`;
- `output_shape`: `rectangle`, `circle`, `full_frame`, `hidden`, or `none`;
- optional `composition`: `preserve_source`, `presenter_only`,
  `presenter_with_screen`, or `screen_only`;
- source-space `presenter_roi`, `screen_roi`, and `important_screen_roi`;
- output-space `presenter_box`, `screen_box`, and `caption_safe_box`;
- human-readable `reason` and optional `user_override`.

A source ROI is `[x, y, width, height]` or the equivalent object, normalized to
the source frame as it is displayed after phone/camera rotation metadata is
applied (`0..1`), not to the underlying coded orientation. The renderer probes
the display matrix, uses display-oriented dimensions for every ROI, and records
both coded and display geometry in the render manifest. Non-right-angle display
rotations are rejected until the source is normalized. An output box may use the same normalized array or an
object with `space` set to `normalized` or `pixels`, plus `x`, `y`, `width`,
`height`, and optional `z_index`. For normalized geometry, verify `x + width <= 1` and
`y + height <= 1`; for pixel geometry, verify the same bounds against declared
output dimensions. JSON Schema checks individual values, while the layout QA
checks these sums, visibility, overlap, and safe areas.

Preserve source geometry by default:

- a meaningful rectangular presenter feed stays a clean rectangle;
- a circle is allowed only for an already circular/isolated subject or an
  explicit `user_override`;
- `important_screen_roi` must remain legible and may require enlarging or
  temporarily making the screen full-frame;
- the canonical renderer rejects `caption_safe_box` because it cannot yet
  enforce it; encode caption placement in a reviewed ASS file instead.

The output shape is constrained by composition: `presenter_only` accepts
`rectangle`, `circle`, or `full_frame`; `presenter_with_screen` accepts
`rectangle` or `circle`; and `screen_only` requires `hidden` or `none`.
`preserve_source` must match the classified source: circular stays circular,
rectangular-with-context stays rectangular, full-frame presenter stays
full-frame, and screen-only has no visible presenter. `unknown` is allowed only
during analysis and is rejected from every EDL composition instead of guessing
a shape. `isolated_subject` may become a circle only in an explicit composed
presenter layout; `preserve_source` keeps it rectangular and cannot claim a
circle mask that the renderer does not apply.

Composition fields are strict: `preserve_source` accepts no non-null ROIs or
output boxes; `presenter_only` rejects `screen_roi`, `important_screen_roi`, and
`screen_box`, and additionally rejects `presenter_box` for `full_frame`;
`screen_only` rejects `presenter_roi` and `presenter_box`.
`presenter_with_screen` continues to materialize the declared presenter and
screen ROIs/boxes. Omitted or `null` fields make no rendering claim. A field
that the selected composition would ignore is a gate error.

`layout_plan` is the approved composition contract. The canonical renderer
materializes `screen_roi`, `presenter_roi`, `screen_box`, `presenter_box`, and
circle/rectangle shape. It validates `important_screen_roi` as a protected
subregion of `screen_roi`, maps it through the exact contain-and-pad transform
into output pixels, rejects presenter overlap, and requires it to stay inside
the scaled vertical/horizontal platform safe area from `project.qa`. Declare
`important_screen_roi` for every diagram, UI element, or source text that must
remain readable. If the protected region cannot fit, enlarge/reposition the
screen or use a full-screen interval. A renderer that cannot realize a declared
field must reject that layout or render an approved composition asset; it must
never silently ignore the field.

Apple Vision tracking remains local and produces moving rectangle samples.
`scripts/smooth_tracking.py` stabilizes those samples;
`scripts/tracking_to_intervals.py --source-id <manifest-id>` groups them into
reviewable fixed source-space ROIs bound to the exact source-manifest ID. Copy
an accepted ROI into `layout_plan` and split a retained range at each accepted
interval boundary. Neither helper chooses the final shape or layout.

### Visual overlays

Every overlay requires `visual_id`, `file`, `provenance`, `purpose`, `section_id`, non-empty
`meaning_ids`, present `semantic_text` (a string or `null`), positive `duration`, and exactly one timing
anchor:

- `start_in_output` (preferred absolute output time);
- legacy `start`;
- `start_at_range_index`;
- `start_after_range_index`; or
- `align_to_end: true`.

`visual_id` must resolve to one item in the approved `semantic_plan.visual_plan`.
The overlay `purpose`, `section_id`, `meaning_ids`, and `semantic_text` must
exactly equal that item's `purpose`, `section_id`, `meaning_ids`, and
`approved_text`; its section must also belong to the selected deliverable.
This prevents a rendered title or CTA from drifting after textual approval.
`provenance` must name the canonical `<file>.provenance.json` sidecar under the
same `edit/` tree. For a local motion card, `render_motion_card.py --visual-id`
creates it automatically and binds the approved text, JSON spec, generated
bytes, plan, approval, implementation, and optional poster. For HyperFrames,
Manim, or another user-owned local asset, create it with
`record_visual_asset.py`; text-bearing assets require the exact approved words,
and no-text assets require `approved_text: null`. External provenance explicitly
requires full-preview user review and makes no OCR claim. Both the media and
sidecar hashes enter preview approval, final authorization, and release QA.

`offset_s` adjusts a range/end anchor. Optional render fields are `full_frame`,
even `width`/`height`, `x`, `y`, and a safe simple `filter`. An optional overlay
`id` may identify the rendered instance; the semantic fields above are required.
Use them to prove the overlay supports the approved meaning; decorative motion
alone is not sufficient.

Overlay anchors are resolved on the same cumulative frame-quantized timeline as
the canonical renderer. The complete resolved overlay interval must fit within
one contiguous output block belonging to its approved `section_id`; only 50 ms
of boundary tolerance is accepted. Therefore an overlay approved for a later
chapter cannot be anchored over an earlier chapter even when all of its text and
metadata otherwise match the global visual plan.

### SFX and audio filters

Every `audio_overlays` item requires `file`, positive `duration`, and exactly
one of the same timing anchors. `gain_db` is constrained to `-60..+12 dB`;
normal editorial SFX should start conservatively (commonly around `-18..-12
dB`) and be verified against speech. Optional `purpose` and `section_id` explain
why the sound exists.

`audio.filters` is an ordered list of simple comma-separated FFmpeg audio
filters. Filtergraph separators `;`, `[` and `]` are forbidden. The EDL gate and
renderer both allow only duration/PTS-preserving cleanup filters:
`acompressor`, `adeclick`, `adeclip`, `afftdn`, `alimiter`, `anlmdn`, `deesser`,
`dialoguenhance`, `equalizer`, `highpass`, `lowpass`, and `volume`. Timing
filters such as `adelay`, `atempo`, `asetpts`, and `atrim` are rejected because
they would invalidate exact cut and audio alignment checks. Keep the list empty
when analysis gives no reason to process. Restoration, de-essing, or strong
compression still requires the separate analysis/A-B approval workflow; a
schema-valid filter is not automatically an artistically approved filter.

### Subtitles

`subtitle_mode` is explicit:

- `none`: `subtitles` is absent or `null`;
- `burned`: render the declared subtitle file into the picture;
- `sidecar`: copy the declared subtitle file alongside the video.

`burned` and `sidecar` require a subtitle path ending in `.srt`, `.ass`, `.ssa`,
or `.vtt` (case-insensitive). Other extensions are rejected. Caption timing,
line length, reading speed, safe-area fit, and overlap with important ROIs are
separate blocking QA checks.

The EDL gate parses that actual cue file. After removing ASS/HTML styling and
normalizing text, concatenated visible cues must exactly equal the concatenated
speech-backed EDL quotes in output order. Captions may not add an editorial CTA,
title, summary, or claim; author that copy as an approved overlay instead.

For `sidecar`, the subtitle file copied beside the preview is an independent
user-visible preview artifact, not merely a derivative implementation detail.
Preview QA must attest its output-derived path and SHA-256, and
`preview_approval_<artifact-key>.json` records both as `preview_sidecar`.
Final authorization re-hashes those exact approved preview-sidecar bytes; final
QA and the release manifest fail closed if that file is replaced, removed, or
moved after approval. The final video's own sidecar remains separately bound by
the final render manifest and release QA.

`sprut-render-6` treats subtitle fonts as render inputs. For burned ASS/SSA it
collects style and inline `\\fn` families, resolves declared bold/italic style
variants, and conservatively binds every weight/slant variant for inline family
overrides. For burned SRT/VTT it binds the libass default (`Arial`), any
declared HTML `font face`, and markup-requested bold/italic variants. Each face
is resolved with local `fc-match`, and the matched file path and SHA-256 must
stay identical from preview through approval, final, and release QA. This binds
the declared/default faces; glyph-level font fallback chosen internally by
libass is not inferred, so captions needing multilingual fallback should use a
reviewed ASS file with explicit font families.

Every v6 render also records one exact renderer identity: SHA-256 for the
renderer, gate, schema validator, both semantic/EDL schemas, approval helpers,
caption validator, cut/boundary QA, release QA, and provenance helper. The
tool identity contains resolved FFmpeg/ffprobe paths and binary hashes, exact
first-line version strings, hashes of their complete `-version` output, and a
linked libass path/hash when discoverable on macOS/Linux. Preview approval,
final rendering, and release QA re-compute and require exact equality.

Release QA parses the declared caption file itself (legacy caption-plan JSON is
accepted only by the standalone validator). It rejects malformed, non-finite,
out-of-order, overlapping, non-positive, or shorter-than-0.40-second cues; more
than two lines; more than 42 visible characters on one line; reading speed over
20 visible characters per second; and a final cue later than the rendered video
plus 0.05 seconds. ASS/SSA checks follow the `[Events]` `Format:` mapping,
interpret `\N` as a line break, and ignore override tags when measuring visible
text. `qa_release.py` records both structured evidence and `caption_fit.log`.

## Output profiles

Use explicit even dimensions and FPS:

- YouTube horizontal: normally 1920×1080 at source-compatible 25 or 30 fps.
- Instagram/YouTube vertical: normally 1080×1920 at 30 fps.
- Draft: scale down while preserving the declared aspect and FPS.

Pixel-space output boxes and numeric overlay `width`, `height`, `x`, and `y`
are always authored against the declared output canvas. Draft rendering scales
them onto its smaller canvas. Because rewriting arbitrary FFmpeg coordinate
expressions is not safe, draft mode rejects expression-based overlay `x`/`y`;
use numeric coordinates for drafts or render the full-size preview. A contained
non-full-frame overlay pads with transparency; only a full-frame card may use
opaque black containment bars.

Never derive final FPS from a generic default when it conflicts with the source.
For mixed FPS, choose one declared CFR output after inspecting motion and
screen-capture cadence. Rational FPS such as `30000/1001` must remain rational
through the EDL, renderer, and QA.
