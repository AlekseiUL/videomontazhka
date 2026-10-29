# NumPy maintenance boundary

The base renderer and optional creative shot-analysis runtime are separate environments, not a single dependency group.

- Base: NumPy 2.4.6, the existing deliberate runtime contract and audit snapshot.
- Creative: NumPy 2.2.6 with OpenCV 4.12.0.88. OpenCV declares `numpy>=2,<2.3.0` on supported Python 3.12. NumPy 2.5.4 cannot resolve in this environment.
- Creative installation is restricted to Apple Silicon macOS / Python 3.12; base CI uses Python 3.12. No support expansion is implied.

PR #9's uniform bump is intentionally not accepted. This PR retains both proven pins and adds the creative contract test to required CI. Dependabot ignores NumPy versions above 2.2.6 in this shared discovery directory so it cannot rewrite both runtime files into an incompatible combination. This also holds newer base-only proposals for manual compatibility review; it is not an assertion that base 2.5.4 is incompatible. Security alerts and audit jobs remain enabled; no advisory is suppressed.

To lift the constraint, review OpenCV package metadata, resolve each environment independently, update the matching installer/contract and audit snapshot, then install both runtimes and execute offline synthetic render and OpenCV frame-analysis tests. Do not merely change the expected version in a test.

Other runtime updates remain individual proposals. Compatible Actions minor/patch updates are grouped; major Actions updates remain separate. Rollback is an ordinary revert PR.
