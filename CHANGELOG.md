# Changelog

## Unreleased

### Fixed
- **A repro that barely reproduces no longer convicts a knob.** `confirm()`
  promoted every knob in the minimal configuration to `confirmed` — the
  strongest verdict, documented as "taking it out of the repro makes the
  failure go away" — on the strength of *one* failure out of ten verification
  replays. Minimisation accepts a subset on a single failing probe, so against
  a test that fails for a reason jitter cannot shake it converges on an
  arbitrary knob and then "verifies" it at the background flake rate. The
  ablation overrode a screen that had put that knob at p = 0.2, and the
  headline went on to name it as the cause.

  On an unseeded `random.random()` test — flaky for a reason no knob controls,
  with the freeze cell itself failing 3 of 12 — jitter reported
  `caused by fsorder` from a repro that failed 2 of 10 replays. It now reports
  `flaky, but not from anything jitter controls`.

  The minimal configuration must now beat the freeze cell on the same
  one-sided Fisher test the screen uses. A configuration that fails no more
  often than pinning everything has explained nothing, whatever minimisation
  settled on.


## 0.1.0

First release.

- `jitter run` — the study: freeze, chaos, one knob at a time, then everything
  but one knob. Attribution by one-sided Fisher exact test with Holm–Bonferroni
  correction across two hypothesis families.
- Twelve knobs: `order`, `fsorder`, `hashseed`, `threads`, `sleep`, `gc`, `tz`,
  `clock`, `locale`, `env`, `tmpdir`, `load`.
- Delta-debugging down to the smallest failing configuration, then replayed to
  measure whether it is actually deterministic.
- `jitter freeze` — run any command with every knob pinned; exits non-zero on
  failure, so it works as a CI determinism gate.
- `jitter replay` and `jitter repro` — rerun the stored configuration, or emit a
  standalone bash script that reproduces it without jitter installed in the loop.
- `jitter report` — one self-contained HTML page, no CDN and no JavaScript.
- No dependencies.
