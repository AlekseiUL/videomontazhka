# SPRUT local font pack

These font files are bundled locally so video renders never depend on a web
font request or a paid font service.

- **Unbounded**: short expressive hooks and hero keywords. Use one expressive
  face per scene and keep copy brief.
- **Golos Text**: readable Russian body copy and explanatory labels.
- **JetBrains Mono**: technical metadata, counters, code-like labels, and data.

All three files cover basic Cyrillic (`U+0400-U+045F`) and include their own
SIL Open Font License 1.1 text. `manifest.json` records upstream locations and
the exact SHA-256 of every font. A project scaffold must copy the selected font
files and licenses into its own canonical `edit/` tree and include their hashes
in the visual source manifest.

Do not silently replace a font after preview approval: font bytes are a render
input and must remain hash-stable through final QA.
