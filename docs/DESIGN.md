# How jitter works

## A configuration is a dict of strings

The single decision everything else follows from: a trial's configuration is
nothing but environment variables. Not a Python object, not a closure, not a
patch applied by the parent — a flat `dict[str, str]`.

That constraint costs something. It rules out perturbations that cannot be
expressed as an environment variable, and it means the child has to interpret
`JITTER_FSORDER=8412` for itself. What it buys is worth more:

- A configuration can be stored in JSON, so `jitter show` needs nothing but the
  saved record.
- It can be compared and subsetted, so delta-debugging over configurations is
  just set arithmetic.
- It can be printed back to you as a shell command, so a repro is a repro and
  not "run jitter again and hope".
- There is no hidden parent-side state a replay could silently depend on.

`jitter/knobs.py` is therefore the only place a configuration is ever built,
and `settings_for(knobs, seed, shaken)` is the only function that builds one. A
trial cannot quietly differ from the label the experiment gave it.

## Getting inside the process

Six knobs — `order`, `fsorder`, `sleep`, `gc`, `threads` and `clock` — need to
change how the interpreter behaves, not just what it reads. They are installed
by `jitter/_shim.py`, which reaches the child like this:

`site.py` imports a module named `sitecustomize` during interpreter startup,
and by that point `PYTHONPATH` entries are already on `sys.path`. Dropping a
`sitecustomize.py` into a scratch directory at the front of `PYTHONPATH` is
enough to run code inside an arbitrary Python command — and every Python
subprocess it spawns — without touching a line of the project under test.

Two consequences worth knowing:

- If the project already had its own `sitecustomize`, ours chains to it.
  Shadowing it would change behaviour for reasons unrelated to any knob, and
  jitter would go on to blame that difference on entropy.
- Anything the shim raises is swallowed. A crash there would surface as a test
  failure, and jitter would confidently attribute its own bug to a knob.

The pytest ordering plugin is loaded through `PYTEST_PLUGINS` on *every* trial,
pinned as well as shaken. Loading it only when `order` is shaken would mean the
frozen baseline and the shaken runs differed by one imported module as well as
by the knob.

Import machinery is untouched by `fsorder`, incidentally: `FileFinder` calls
the raw `posix.listdir`, not `os.listdir`, so patching `os` cannot reorder your
imports and turn a knob into a heisenbug.

## Knobs must not correlate

If two knobs drew from the same random stream, shaking one would move the other
and attribution would be meaningless. Every knob gets its own stream, seeded by
the trial seed mixed with the knob's name.

The mixing uses `zlib.crc32`, not `hash()`. `hash()` is affected by
`PYTHONHASHSEED`, which is itself one of the knobs — using it would make the
whole experiment a function of one of its own variables.

The same care applies inside `fsorder`: its per-directory shuffle is seeded
from `crc32(path)`, so shuffling a listing stays independent of the hash seed.

## The experiment

Four cells, each only measured when the previous answer made it worth
measuring:

| cell | shaken | asks |
|---|---|---|
| freeze | nothing | is there a deterministic floor at all? |
| chaos | everything | can jitter reproduce this? |
| solo:*k* | just *k* | is *k* **sufficient**? |
| loo:*k* | all but *k* | is *k* **necessary**? |

`solo` is compared against `freeze` with a one-sided Fisher exact test in the
"greater" direction. `loo` is compared against `chaos` in the "less" direction.
These are different questions asked of different data, so they are corrected as
two Holm families rather than one family of twenty-four — pooling them would
double the correction burden for no gain in honesty, and would routinely acquit
a knob whose own repro was sitting right there.

Two design choices carry most of the statistical weight:

**Paired seeds.** Cell *i* uses the same trial seed in every condition. The
value `fsorder` draws in its solo cell is the value it draws in chaos, so the
comparison is like-for-like rather than two independent samples that happen to
share a name.

**Shuffled execution.** Every phase shuffles its trial list before running.
Without this, cells execute in blocks, and anything that drifts during the run —
thermal throttling, another process starting, a filling disk — lands entirely on
whichever cell was running at the time and gets reported as an effect.
Shuffling converts confounding into noise, which is the trade you want.

The adaptive schedule screens every knob cheaply first, then spends the rest of
the budget only on knobs that showed something. `--screen` controls the cheap
pass; setting it equal to `-n` disables the shortcut.

## Ablation outranks the screen

After attribution, jitter delta-debugs the shaken set: remove knobs while the
failure survives, stop when removing anything makes it disappear. That is
`jitter/minimize.py`, and it is ddmin over a set of twelve elements.

The result is stronger evidence than the statistical screen, not weaker. The
screen asks whether a difference in failure counts could have come from chance.
Minimisation removed the knob, watched the failure vanish, put it back, and
watched it return. When the two disagree it is because the screen ran out of
trials, so the ablation wins and the knob is reported as `confirmed`.

The final step verifies the minimal configuration by running it repeatedly.
Because a configuration is fully determined by (shaken set, seed), every one of
those runs is the same experiment — so `10 of 10` means deterministic, and
`4 of 10` honestly means the test has residual randomness jitter does not
reach. It reports whichever it measured.

## Measuring your own noise

Trials run concurrently by default, which introduces two ways for jitter to
manufacture the flakiness it is trying to explain:

- Concurrent pytest runs fighting over one `.pytest_cache`. Each trial gets its
  own `cache_dir` instead.
- The `load` knob's busy processes slowing *other* trials. Shuffled execution
  spreads that across cells rather than letting it land on one.

Neither is fully solved. `-j 1` runs strictly sequentially and is the honest
choice when a test is timing-sensitive; the default of up to four workers trades
a little fidelity for finishing this decade.

## Where it can still mislead you

- **A knob can be blamed for a neighbour it happens to disturb.** `load` slows
  everything; a test with a hard-coded timeout will blame `load` when the real
  bug is the timeout. That is arguably the right answer, but it is worth
  knowing that is what happened.
- **Absence of evidence at n = 12 is weak.** A knob that causes a 5% failure
  rate will usually be cleared. Raise `-n` when a `cleared` verdict is load
  bearing.
- **The frozen world is not the real world.** Pinning `fsorder` to *sorted*
  order is itself a change from what your filesystem does. A test that only
  passes in true ext4 order will look stable under freeze.
- **`clock` is shaken but never pinned**, so the freeze baseline includes real
  wall-clock time. A test that fails only in the last minute of the day shows up
  as unexplained noise in the freeze cell, on that one run of the day.
