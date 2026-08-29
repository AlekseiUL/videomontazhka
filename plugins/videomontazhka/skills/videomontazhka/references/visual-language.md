# SPRUT visual language

## Contents

1. Brand foundation
2. Presenter geometry
3. Screen and diagram priority
4. Motion components
5. Captions
6. Transitions
7. Safe areas and review

## 1. Brand foundation

- Background: `#070707` or source video.
- Primary accent: `#FF6A00`.
- Primary text and diagrams: `#FFFFFF`.
- Secondary text: `#A8A8A8`.
- Dark panel: `#121212`.
- Do not introduce blue, cyan, green, or violet accents unless the source UI requires them.
- Keep original white diagrams white. Use orange for one current idea, pointer, progress, underline, or CTA.
- Prefer one accent action per frame and generous empty space.

Use an available legible sans-serif. Prefer a bundled open font when present; otherwise discover a system font and record its path in the release manifest. Do not hardcode Arial as the only option.

## 2. Presenter geometry

Resolve presenter geometry for every retained range before render. A mixed project may keep global `presenter.mode: auto`, but each `layout_plan` entry must select one of:

- `rectangle`: source shows the presenter in a meaningful rectangular composition or with useful background. Crop a clean rectangle; preserve aspect ratio and natural edge treatment.
- `circle`: source is already circular, isolated, or intentionally a face cutout. Do not crop shoulders or chin tightly.
- `full_frame`: the presenter is the main visual and no screen content needs priority.
- `hidden`: the current section is entirely about a diagram, interface, or B-roll and the presenter adds no information.

Never convert a good rectangle into a circle merely because the delivery is vertical. Never add a decorative panel behind subtitles.

For a moving presenter on macOS, build and run the local tracker after semantic approval and before layout previews:

```bash
xcrun clang -fobjc-arc -fblocks -Wall -Wextra -Werror \
  -framework Foundation -framework AVFoundation -framework Vision \
  -framework ImageIO -framework CoreGraphics -framework CoreMedia -framework CoreVideo \
  scripts/track_presenter.m -o <edit>/work/sprut-track-presenter

<edit>/work/sprut-track-presenter <source-video> <edit>/tracking/raw.json \
  --roi x,y,w,h --analysis-fps 6

<doctor-python> scripts/smooth_tracking.py \
  <edit>/tracking/raw.json <edit>/tracking/smoothed.json
```

The ROI uses normalized top-left coordinates. Supply an initial ROI whenever the shared screen may contain other faces. Pass the smoothed result through `scripts/tracking_to_intervals.py`, review the fixed-ROI suggestions, and split retained EDL ranges at accepted boundaries. The tracker and interval adapter do not authorize a circle or choose the final composition.

## 3. Screen and diagram priority

- When the narration refers to a diagram, keep the complete relevant structure visible before zooming into a detail.
- Give the active information roughly 65–85% of the usable frame when it carries the explanation.
- Reveal context first, then focus. Return to the complete diagram before leaving the section when relationships matter.
- Preserve labels and original line color. Use an orange outline, pointer, underline, or glow around the active element instead of recoloring the diagram.
- Do not cover code, controls, nodes, captions, or the cursor target with the presenter.
- Hold any dense explanatory final state for at least one second.

## 4. Motion components

For retention-focused work, select the motion grammar through the approved
creative route in [creative-direction.md](creative-direction.md). Evaluate the
complete relevant arsenal, but emit no more than one primary visual and one
supporting sound accent for a beat. `none` is preferable to an unmotivated
effect. Never use motion merely to conceal a weak edit.

Use data-driven components rather than project-specific hardcoded scripts:

- `title`: promise and scope;
- `chapter`: topic change, 2.5–4.5 seconds;
- `definition`: term plus one precise explanation;
- `compare`: two alternatives with one active difference;
- `process`: 2–5 sequential steps;
- `quote`: one source-backed statement;
- `callout`: pointer/highlight attached to screen content;
- `lower_third`: identity or necessary context only;
- `cta`: one next action, normally 3.5–5.5 seconds.

Animate one new concept at a time with cubic easing. Synchronize the landing
frame to the payoff word. Use `scripts/render_motion_card.py --edit-dir
<videos_dir>/edit --visual-id <approved-visual-id>
<videos_dir>/edit/animations/<card>.json -o
<videos_dir>/edit/animations/<card>.mp4` for standard cards. The spec, output,
optional poster, and generated provenance sidecar must all resolve under that
edit directory; the command refuses to create anything until the approved-plan
asset gate passes. Use local HyperFrames only when HTML/CSS motion materially
improves the explanation; use local Manim for formal graphs and state diagrams.
Bind either external result with `record_visual_asset.py` and inspect it in the
full preview; the recorder hashes declared inputs but does not pretend to OCR
arbitrary pixels.

Use strong virtual-camera motion as a short semantic gesture, not continuous
drift: reset at every shot/source cut, enter over 8–12 frames, hold still, and
exit over 8–12 frames. A justified punch may reach 1.20–1.35 when the source
remains sharp. Render with sub-pixel transforms; face-tracker micro-motion is
not editorial emphasis. For a diagram, establish context before focus and
return to context when the relationship matters.

The browser creative layer supports short deterministic Pixi particles and
filters, Rough Notation callouts, local Lottie icons, explicit Three.js hero
scenes. All callable effects are local, hash-bound source assets. A small
allowlist of shader sources is installed for future work, but it is not
callable and the router reports it unavailable until a seek-safe compositor
and exact-boundary QA adapter exist. Use a 3–4-frame sheet and full-composite
review; a library being available is never itself a reason to place an effect.

For more expressive local motion, use one audited SPRUT Motion Kit template:
kinetic keyword, lower third, screen callout, diagram focus, stat hit, premium
chapter bridge, cover wipe, or experimental text behind subject. Each instance
must use the copied local GSAP bundle and bundled Cyrillic fonts. The
experimental subject layer is reserved for short approved slots and requires a
reviewed local Apple Vision foreground matte; it never changes a rectangular
presenter into a circle.

HyperFrames alpha WebM must pass through
`normalize_hyperframes_alpha.py`; the helper forces the alpha-preserving
`libvpx-vp9` decoder and verifies video-only ProRes 4444 output before
compositing.

```bash
python scripts/person_matte.py \
  --edit-dir <videos_dir>/edit \
  --input <videos_dir>/edit/work/<approved-cfr-effect-clip>.mov \
  --foreground animations/<visual-id>/person-foreground.mov \
  --matte mattes/<visual-id>/person-matte.mov \
  --quality accurate
```

For production, `--foreground` is sufficient; request the separate matte when
it helps edge QA. Evaluate the short result on black, white, and orange
backgrounds at normal speed and slow motion before using it.

## 5. Captions

Captions are a per-deliverable choice. They are normally burned into vertical
shorts and normally omitted from horizontal YouTube videos unless the user asks
for them; never add on-screen transcription merely because a transcript exists.

- Use at most two lines, no more than 42 visible characters per line, and no
  more than 20 characters per second. The release validator enforces these
  limits on the actual SRT/VTT/ASS/SSA file.
- For 1080×1920, start around 58–72 px with white text, a dark outline/shadow,
  and orange emphasis on only the active word or short phrase. Scale
  proportionally for other sizes.
- Keep captions inside the platform safe area and away from the presenter,
  `important_screen_roi`, source labels, controls, and diagrams. Do not create a
  separate decorative subtitle window.
- Break on meaning and natural speech rhythm. Do not leave a one-word orphan,
  flash a caption too briefly, or cover a demonstration at its payoff moment.
- Keep captions verbatim to the retained speech. Editorial titles, summaries,
  and CTA text use approved overlays rather than caption cues.
- Author reviewed ASS placement when geometry varies; preview approval remains
  mandatory because timing/line-fit checks cannot prove visual legibility.

## 6. Transitions

The canonical renderer is release-verified for `hard_cut`. Implement a chapter bridge as a timed full-frame branded overlay adjacent to a hard cut. The remaining families below are editorial choices only until a separate renderer implements them and passes the same exact-boundary QA; never label a hard concat as one of them.

An approved cover-wipe asset may visually hide the hard cut while the EDL still
declares `hard_cut`. Before use, `qa_transition_asset.py` must pass for decoded
duration/FPS, coverage, black frames, and severe adjacent-frame flashes. A
passing asset test does not replace N−1/N/N+1 inspection after compositing.

- `hard_cut`: default for pace and clarity.
- `j_cut` or `l_cut`: continue thought or preserve natural audio flow.
- `dissolve`: only a real time/place change; normally 6–12 frames.
- `chapter_bridge`: a full-frame brand card for a new subject.
- `match_cut`: only when two frames share motion, direction, or geometry.
- `punch_in`: an emphasis/edit-cover technique, not a transition family.

Avoid glitch, whip, flare, spin, and zoom transitions unless the content itself motivates them. Never place a black frame between ordinary cuts.

## 7. Safe areas and review

For vertical 1080×1920 delivery, reserve by default:

- top 150 px for platform chrome;
- bottom 420 px for captions, username, description, and controls;
- right 150 px for action icons;
- left 80 px as a minimum breathing margin.

For horizontal 1920×1080 delivery, keep essential text within 100 px horizontally and 70 px vertically unless platform-specific evidence requires more.

Before the full render, create and show a 3–4-frame sheet for every new effect
or materially new layout: entry/build, readable hold, payoff, and exit. Before
release, inspect first frame, last frame, every overlay midpoint, every
presenter-mode change, every matte edge, every diagram crop, and exact cut
boundaries. Treat machine collision checks as assistance, never as a
replacement for visual review.
