# Editorial workflow

## Contents

1. Source modes
2. Transcript analysis
3. Relevance research
4. Semantic proposal
5. Approval and revision
6. Creative routing
7. EDL construction
8. Short-form completeness

## 1. Source modes

### Long stream

Treat chronology as evidence, not as the finished structure. Extract the actual argument, demonstrations, examples, caveats, and conclusions. Collapse repeated explanations and navigation delays. Reorder only when the new order remains truthful and does not imply a false cause, sequence, or result.

### Multiple short recordings

Treat every file as a take library. Group phrases by semantic beat before choosing a take. Select for correctness first, then completeness, delivery, visual quality, and brevity. A later filename does not automatically belong later in the story.

### Mixed material

Choose one primary narrative spine. Mark other sources as alternate take, B-roll, demonstration, evidence, or visual insert. Never let an attractive insert replace a missing explanation.
Silent or no-audio B-roll remains legitimate source material: describe only what
is visibly present, cite its exact source-local interval as `visual` evidence,
and do not invent a transcript. A visible demonstration can support a visual
fact, but it cannot be presented as something the speaker said.

## 2. Transcript analysis

Use word-level verbatim transcripts for every source with audio. Preserve fillers and false starts in the transcript because they are edit signals. Produce a packed phrase view and inspect the raw word timestamps only at cut decisions. No-audio sources appear in that view as hash-bound visual-only notices instead of fake empty transcripts.

Extract:

- claims and their evidence;
- definitions;
- problems and promised solutions;
- examples and counterexamples;
- actionable steps;
- caveats and uncertainty;
- natural hooks, payoff lines, and endings;
- contradictions, unfinished thoughts, and repeated formulations;
- sections that depend on visible screen information.

Do not score every phrase mechanically. Reason from the whole argument.

## 3. Relevance research

Run this step when the user requests YouTube relevance, retention, virality, packaging, title, or hook optimization.

Use YouTube's current first-party framing as the baseline: the package must create honest appeal, the opening must immediately deliver on that package, the body must sustain engagement, and the ending must leave the viewer satisfied. Treat the first 30 seconds as a distinct intro test for long-form video. When the user's own Studio analytics are available, use retention dips, spikes, top moments, and comparisons with videos of similar length as stronger evidence than generic creator folklore.

Use 5–10 recent public examples aimed at the same audience. Record date observed, URL, publication date, visible views, duration, promise, first 30-second structure, chapter pattern, title angle, and one useful comment theme when available. Compare views in the context of channel size and age; do not declare causation from views alone.

Convert observations into editorial hypotheses such as:

- lead with the surprising correction before definitions;
- show the outcome before the setup;
- demonstrate a failure and then explain the mechanism;
- use a concrete comparison instead of an abstract introduction;
- close each chapter with a local takeaway.

Label each hypothesis as `observed`, `inferred`, or `user-specific`. Never copy phrasing or assets.

## 4. Semantic proposal

Create `semantic_plan.json` and present an equivalent readable version in chat. Include:

1. `viewer_promise`: one sentence stating what the viewer will understand or be able to do.
2. `audience`: who benefits and what they already know.
3. `source_truth`: 3–12 meanings present in the footage, each with source/timecode evidence whose explicit modality is `speech` (verbatim `quote`) or `visual` (factual `description`).
4. `narrative`: ordered sections with purpose, input meanings, payoff, and estimated duration.
5. `keep`: moments that must survive.
6. `cut`: fillers, repetition, dead air, tangents, or weak takes proposed for removal.
7. `clarify`: gaps, contradictions, or places where visuals must carry context.
8. `hooks`: 2–4 truthful hook options with one recommendation.
9. `ending`: the resolved takeaway and optional CTA.
10. `visual_plan`: a required array (empty when no added visual is justified) containing only visuals that improve comprehension or pacing; every item declares its `asset_type`.
11. `audio_plan`: a required object describing cleanup level, music/SFX policy, and loudness target.
12. `deliverables`: aspect, resolution, subtitles, target duration, platform,
    ordered narrative `section_ids`, selected `hook_id`, and final
    `ending_section_id` for each output.
13. `research_basis`: current sources and hypotheses, when used.
14. `status`: always `pending` before the user's explicit approval.

The plan must distinguish what the speaker actually said from editorial inference. Mark unavailable support rather than inventing it.

## 5. Approval and revision

Record approval only when the user approves the semantic proposal as a whole. Approval of a color, layout, subtitle style, or sample frame is not semantic approval.

Hash the exact `semantic_plan.json`. If the user later changes the message, audience, section order, promised outcome, or target length materially, create a revised plan and obtain a new approval. Small cut-boundary refinements and visual polish do not invalidate semantic approval.

## 6. Creative routing

For a creative or retention-focused deliverable, route every approved visual
beat after semantic approval and before EDL construction. Inspect the current
local registry, classify explicit semantic and scene signals, and run the
approval-bound creative router. Review its choices as one textual creative
treatment: selected effect, timing motive, protected regions, optional sound
accent, fallback, and explicit `none` decisions. Obtain the user's confirmation
before generating those assets. Compile the confirmed, exact-coverage set with
`compile_creative_treatment_plan.py`; do not hand-author the consolidated plan.

The router never adds meanings or words. It evaluates readiness, screen
legibility, presenter geometry, density, cooldown, visual coverage, and recent
effect history. It may choose at most one primary visual and one supporting
audio event per beat. A tool must be both locally ready and exposed through an
audited adapter; an installed package alone is insufficient. See
[creative-direction.md](creative-direction.md).

## 7. EDL construction

Map every EDL range to a narrative section and a source/timecode. A speech-backed range keeps its exact transcript `quote` and `audio_mode: "source"`; a visual-backed range keeps the approved factual `description` and `audio_mode: "mute"`; a mixed range keeps both and uses source audio because it has speech evidence. Always include a short editorial reason. Snap speech boundaries to words and apply 30–200 ms padding after checking the sound; visual-only ranges stay inside their approved visual evidence envelope. The default gate rejects more than 0.35 seconds of boundary silence or 0.75 seconds between timed audible transcript items. Keep an intentional rhetorical pause only by stating its approved purpose in `intentional_pause_reason`.

If a deliverable uses burned or sidecar subtitles, concatenate their visible cue text and compare it with the speech-backed range quotes in output order. They must match after the standard Unicode/punctuation normalization. Cue timing is also semantic: the gate rebuilds the renderer's cumulative frame-quantized output timeline, requires finite ordered cues inside the programme, and binds each cue's sequential tokens only to the speech range interval(s) that supplied them. A cue may cross a muted, visual-only, or unrelated speech interval by at most 50 ms as boundary tolerance. An editorial CTA, title, or rewritten summary belongs in an approved visual overlay, never in the caption file.

Create a separate EDL for every approved deliverable. Its stable
`deliverable_id` selects both the approved output contract and the deterministic
artifact namespace used for render manifests, preview approval, default QA,
and final release state; never reuse singleton artifact names across a long-form
video and its shorts. Retain exactly that deliverable's ordered `section_ids`,
open with its approved `hook_id`, and finish on its `ending_section_id`; a short
must not borrow sections or hooks approved only for another deliverable.

For long streams:

- preserve factual and causal context;
- remove duplicate passes while retaining the clearest full explanation;
- keep enough setup for demonstrations to make sense;
- avoid a cold open that begins halfway through a sentence.

For multiple takes:

- choose one best take per beat;
- avoid visual continuity problems when two fragments from the same setup touch;
- use B-roll, a purposeful punch-in, or a semantic card only when it helps the cut;
- never splice syllables to manufacture a sentence.

## 8. Short-form completeness

A useful short follows one of these complete shapes:

- misconception → correction → mechanism → takeaway;
- problem → cause → solution → result;
- question → answer → example → implication;
- mistake → consequence → better action;
- claim → evidence → boundary/caveat → conclusion.

Do not ship a short that contains only a hook and problem. If the footage lacks the answer, choose another topic or explicitly frame the unresolved question.
