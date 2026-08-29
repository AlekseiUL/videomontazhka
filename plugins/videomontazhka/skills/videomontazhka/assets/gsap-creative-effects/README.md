# Approval-bound GSAP creative effects

This source pack turns the already installed local GSAP 3.14.2 plugins into five audited, meaning-specific HyperFrames sources. The scaffolder creates source only; it never renders video.

| Effect type | Meaning | Plugin(s) | Approved asset types |
|---|---|---|---|
| `kinetic_split_keyword` | One phrase or keyword deserves emphasis | SplitText | `title`, `quote` |
| `morph_concept` | One concept transforms into another | MorphSVG | `title`, `diagram`, `comparison`, `process` |
| `route_draw` | A route, mechanism, or sequence unfolds | DrawSVG, MotionPath | `diagram`, `process` |
| `data_scramble` | A data/technical statement resolves from uncertainty | ScrambleText | `title`, `quote`, `diagram` |
| `flip_before_after` | A before/after or structural rearrangement | Flip | `comparison`, `process` |

The request JSON must satisfy `gsap-creative-effect-spec.schema.v1.json`, live under the approved project's `edit/` directory, and repeat the exact approved `visual_id`. It carries no free-form display copy: all visible semantic text is read from the already approved `semantic_plan.json`.

Discover the catalog without writing anything:

```bash
python scripts/scaffold_gsap_creative_effect.py --describe-json
```

After semantic approval, scaffold one source instance:

```bash
python scripts/scaffold_gsap_creative_effect.py \
  --edit-dir <videos_dir>/edit \
  --visual-id <approved-visual-id> \
  --spec <videos_dir>/edit/animations/<approved-visual-id>-gsap.json \
  --accept-gsap-terms
```

Pass `--accept-gsap-terms` only after the user has reviewed and accepted the
license URL reported from the installed `gsap/package.json`. The generated
source manifest records that acceptance and hashes the copied package metadata.

The result is written to `edit/animations/hyperframes/gsap-creative/<visual-id>/`. It includes only local runtime files, the strict request/config schemas, the exact request, local fonts/licenses, and `source-manifest.json` with SHA-256 for every copied transitive input. Existing instances are never overwritten.

Next, use the pinned local HyperFrames CLI to lint/inspect/render the source, record the rendered asset with `record_visual_asset.py --source-spec source-manifest.json`, build the mandatory 3–4-frame visual sheet, and obtain visual approval before a complete render.
