# SPRUT Motion Kit

## Brand palette

- Canvas: `#070707`
- Panel: `#121212`
- Action accent: `#FF6A00`
- Primary text and diagrams: `#FFFFFF`
- Secondary text: `#A8A8A8`

Blue, cyan, violet, and decorative rainbow accents are outside the SPRUT visual
language. Source interfaces may retain their own colors, but the kit must not
introduce new accent colors.

## Typography

Every instance receives the audited local font pack and its OFL-1.1 licenses:
Unbounded for concise display text, Golos Text for Russian body copy, and
JetBrains Mono for technical labels and figures. Generated configuration uses
only relative local paths; templates never load a font from a URL.

- Display text: at least 60 px at 1920x1080.
- Body and labels: at least 20 px at 1920x1080.
- Use a clear light/heavy contrast; do not combine two similar sans-serif faces.
- Allow text to wrap naturally. Do not insert decorative forced line breaks.

## Composition rules

- White diagrams remain white. Orange identifies one active node, pointer,
  underline, progress state, or CTA.
- One accent action per frame is the default.
- Prefer empty space and readable holds over continuous motion.
- Essential horizontal content stays inside 100 px left/right and 70 px
  top/bottom at 1920x1080.
- Essential vertical content stays inside 80 px left, 150 px right, 150 px top,
  and 420 px bottom at 1080x1920.
- All animation is deterministic and seekable. No randomness, wall-clock time,
  network media, or infinite loops.
- An internal cover wipe is a generated overlay asset. It does not authorize an
  unsupported transition label in the canonical SPRUT EDL.

## Presenter and subject rules

Do not force the presenter into a circle. `text-behind-subject` is experimental
and requires a reviewed local foreground/matte asset. The template does not
perform segmentation and must never upload footage.
