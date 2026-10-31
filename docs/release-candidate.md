# Release candidate v0.1.2

Historical preparation record. v0.1.2 was published on 2026-10-27; the [GitHub Release](https://github.com/AlekseiUL/videomontazhka/releases/tag/v0.1.2) is the publication record for accepted commit `8a5eadfe6dbb418f93bd97eb8cfff02059eb255c`. The current checkout can include later fixes. Existing tags must not move. The following instructions describe the pre-publication gate, not a pending release.

Distribution: complete source archive. This candidate includes all changes on main since v0.1.1, plus pinned GitHub Actions and review-first dependency updates. This is not an npm/PyPI publication.

Install into a fresh temporary environment from the built archive, not an editable checkout. Run the documented offline core command/fixture before publishing; do not enable profiles, contact paid providers or start services. After release, download the exact public artifact and repeat this check.

Rollback: close the unmerged PR; after merge, submit a revert PR. Do not rewrite the release tag or delete old artifacts. A bad release must be superseded and clearly marked, with owner approval.
