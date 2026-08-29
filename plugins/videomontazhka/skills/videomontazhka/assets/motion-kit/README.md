# SPRUT Motion Kit v1

This directory contains audited, local-only HyperFrames source templates. The
templates generate motion assets; `sprut-render-6` remains the canonical editor
and delivery renderer.

## Audited templates

| Template | Intended use | Approved asset types | Text contract |
|---|---|---|---|
| `kinetic-keyword` | Short keyword or pull-quote hit | `title`, `quote` | required |
| `lower-third` | Name and necessary context | `title` | required |
| `screen-callout` | Label and pointer attached to UI | `diagram` | required |
| `diagram-focus` | White diagram plus orange focus | `diagram` | optional |
| `stat-hit` | Number or concise factual hit | `title`, `diagram`, `quote` | required |
| `chapter-bridge-premium` | Full-frame topic bridge | `chapter` | required |
| `cover-wipe-transition` | Alpha cover wipe around a hard cut | `chapter` | forbidden |
| `text-behind-subject` | Experimental title between background and foreground | `title`, `quote` | required |

Standard title, definition, comparison, process, quote, chapter, and CTA cards
should continue to use `render_motion_card.py` unless HTML motion materially
improves the explanation.

## Scaffold after semantic approval

```bash
python scripts/scaffold_motion_kit.py \
  --edit-dir /project/edit \
  --visual-id visual-memory-title \
  --template kinetic-keyword \
  --gsap-bundle '<runtime-dir>/hyperframes/node_modules/gsap/dist/gsap.min.js' \
  --accept-gsap-terms
```

GSAP is optional and is not distributed with Videomontazhka. Replace
`<runtime-dir>` with the path reported by `scripts/doctor.py` only after the
user has accepted the upstream GSAP terms.

The command validates the asset gate and the exact approved visual-plan item
before writing anything. It creates only:

```text
edit/animations/hyperframes/instances/<visual-id>/
  index.html
  config.json
  config.js
  DESIGN.md
  motion-kit.schema.v1.json
  motion-kit.css
  sprut-motion-runtime.js
  template.json
  fonts/                       # audited Cyrillic font pack + OFL files
  vendor/gsap.min.js
  source-manifest.json
```

The instance does not install npm packages or render. `--gsap-bundle` must
point to the browser bundle already supplied by the pinned local runtime; the
scaffolder copies and hashes it. Keep the HyperFrames package and lockfile
in the pinned studio runtime outside all video projects; never use cloud rendering,
Remotion, a hosted font URL, or a paid media API.

Before an instance can enter an EDL, render it locally, normalize transparent
output with `normalize_hyperframes_alpha.py`, and bind the ProRes 4444 output to the same approved visual with
`record_visual_asset.py --source-spec source-manifest.json`. The complete
preview still requires explicit user approval.

## Local fonts

Every scaffold copies the audited `assets/fonts` pack into the instance:
Unbounded for concise display text, Golos Text for Russian body copy, and
JetBrains Mono for counters and technical labels. Their OFL-1.1 license files
and exact hashes travel with the source. `config.json` selects only relative
local paths. Templates never use `@import`, Google Fonts, a CDN, or another
network source.

## Experimental subject layering

`text-behind-subject` expects reviewed local media paths in `config.json`:

- `media/background`: the original or clean background plate;
- `media/foreground`: a transparent WebM/MOV or another locally prepared
  foreground layer.

The scaffold deliberately leaves these values empty. It does not segment a
person and does not infer consent or source rights.
