# SPRUT Creative Browser Effects

This is a small, audited source library for approval-bound HyperFrames overlays.
It does not render or edit media by itself. The only supported writer is
`scripts/scaffold_creative_browser_effect.py`, which runs the SPRUT asset gate,
binds one exact approved `visual_plan` item, verifies the installed runtime, and
copies every required browser bundle into the project `edit/` tree.
Because the current templates also consume a separately installed GSAP bundle,
the writer refuses to create an instance unless `--accept-gsap-terms` is passed
after the user reviews the license URL reported from `gsap/package.json`.

Audited effects:

- `pixi-semantic-accent`: deterministic PixiJS particles, bloom, and shockwave.
- `rough-screen-annotation`: deterministic Rough Notation annotation.
- `lottie-local-icon`: local, user-owned, pure-vector Lottie JSON only.
- `three-spatial-system`: experimental Three.js system map; off unless an
  explicit CLI flag is present.

`shader-transition` is deliberately not callable. The runtime has a per-file
licensed GLSL allowlist, but a seek-safe compositor and exact-boundary QA path
have not been audited. See `effects.catalog.v1.json` for the machine-readable
blocked record.

All templates are offline, transparent, seekable, deterministic, and use the
SPRUT black/orange/white/gray visual language. A generated source instance still
requires a visual sheet and full-preview user approval before it can enter an
EDL or final render.
