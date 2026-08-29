# SPRUT creative browser runtime

This is a pinned, local-only browser graphics runtime for approved SPRUT
motion assets. It is shared by video projects; source footage never belongs
here.

- `vendor/sprut-pixi.js` is the curated PixiJS + filter bundle.
- `vendor/rough-notation.iife.js` provides hand-drawn annotations.
- `vendor/lottie-light.min.js` plays user-owned, locally supplied Lottie JSON.
- `vendor/three.module.min.js` provides the approved local Three.js module.
- `vendor/transitions/` contains only the separately licensed shader allowlist.
- `RUNTIME_MANIFEST.json` binds packages, licenses, tools, policies, and hashes.

No CDN is required at render time. Do not use URL-based animation or media
inputs. Lottie must receive local/inline `animationData`; PixiJS compressed
texture CDN transcoders are not included in the curated bundle.

The `gl-transitions` npm package is an audited source collection, not blanket
permission to use every shader. Only files listed in
`RUNTIME_MANIFEST.json.gl_transition_policy.allowlist` may be copied into a
composition, and a new shader requires a license/header review plus transition
preview QA before it can join that list.

Run the installer with `--verify-only` to re-hash the complete runtime. A video
asset still requires the normal semantic approval, visual sheet, provenance,
full-preview approval, and release QA gates.
