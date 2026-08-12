# Changelog

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
