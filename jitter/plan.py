"""The experiment.

Four questions, asked in order, each one only asked when the previous answer
made it worth asking:

1. **Freeze.** With every knob pinned, does the command still fail sometimes?
   If it always fails, it is broken, not flaky. If it never fails, we have a
   deterministic floor to measure everything else against.
2. **Chaos.** With every knob shaken, how often does it fail? If chaos is no
   worse than freeze, jitter cannot explain this one, and says so.
3. **Solo.** One knob shaken, everything else pinned. A knob that fails here
   is *sufficient* on its own.
4. **Leave-one-out.** Everything shaken except one knob. A knob whose absence
   collapses the failure rate is *necessary*, even when it does nothing alone.

Two details do most of the work for correctness. Trials are **paired**: cell
*i* uses the same seed everywhere, so the value a knob draws in its solo cell
is the value it draws in chaos, and the comparison is like-for-like. And the
execution order within each phase is **shuffled**, so the machine getting
busier partway through smears across every cell as noise instead of landing on
whichever cell happened to run last.

Called by: jitter/cli.py.
"""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor

from . import knobs as knobmod


class Cell:
    """One experimental condition and the trials run under it."""

    def __init__(self, label, kind, knob, shaken):
        self.label = label
        self.kind = kind
        self.knob = knob
        self.shaken = tuple(shaken)
        self.trials = []

    @property
    def n(self):
        return len(self.trials)

    @property
    def failures(self):
        return sum(1 for trial in self.trials if not trial.ok)

    @property
    def rate(self):
        return self.failures / self.n if self.n else 0.0

    def to_dict(self):
        return {
            "label": self.label,
            "kind": self.kind,
            "knob": self.knob,
            "shaken": list(self.shaken),
            "trials": [trial.to_dict() for trial in self.trials],
        }


class Study:
    """Everything the experiment learned, in the order it learned it."""

    def __init__(self, knobs, trials, screen, seed):
        self.knobs = list(knobs)
        self.trials = trials
        self.screen = screen
        self.seed = seed
        self.cells = {}
        self.notes = []
        self.verdicts = []
        self.minimal = None

    def cell(self, label, kind, knob, shaken):
        found = self.cells.get(label)
        if found is None:
            found = self.cells[label] = Cell(label, kind, knob, shaken)
        return found

    @property
    def freeze(self):
        return self.cells["freeze"]

    @property
    def chaos(self):
        return self.cells["chaos"]

    def solo(self, name):
        return self.cells.get("solo:" + name)

    def loo(self, name):
        return self.cells.get("loo:" + name)

    def note(self, text):
        self.notes.append(text)


def seeds_for(base, count):
    """Trial seeds, shared across every cell so comparisons stay paired."""
    return [base * 1000003 + index for index in range(count)]


def _execute(runner, study, requests, workers, progress):
    """Run a batch of (cell, seed) pairs in a deliberately random order."""
    tasks = list(requests)
    random.Random(study.seed ^ len(tasks)).shuffle(tasks)

    def one(task):
        cell, seed = task
        settings = knobmod.settings_for(study.knobs, seed, set(cell.shaken))
        return cell, runner.run(cell.label, cell.shaken, seed, settings)

    if workers <= 1:
        for task in tasks:
            cell, trial = one(task)
            cell.trials.append(trial)
            progress(trial)
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cell, trial in pool.map(one, tasks):
            cell.trials.append(trial)
            progress(trial)


def investigate(runner, knobs, *, trials, screen, seed, workers, progress, phase):
    """Run the whole study. Returns a :class:`Study`."""
    study = Study(knobs, trials, screen, seed)
    names = [knob.name for knob in knobs]
    pool = seeds_for(seed, max(trials, screen))

    # -- 1 and 2: the two ends of the range, measured together so any drift in
    # machine load lands on both equally.
    freeze = study.cell("freeze", "freeze", None, ())
    chaos = study.cell("chaos", "chaos", None, names)
    phase("freezing, then shaking everything", 2 * trials)
    _execute(
        runner,
        study,
        [(freeze, s) for s in pool[:trials]] + [(chaos, s) for s in pool[:trials]],
        workers,
        progress,
    )

    if freeze.n and freeze.failures == freeze.n:
        study.note(
            "The command failed in all {0} frozen trials. That is not flaky, "
            "it is broken -- fix it before asking why it is intermittent.".format(
                freeze.n
            )
        )
        return study
    if chaos.failures == 0 and freeze.failures == 0:
        study.note(
            "No failure in {0} frozen trials or {1} fully shaken ones. Either "
            "this command is stable, or its flakiness comes from something "
            "jitter does not control -- the network, a shared database, "
            "another machine.".format(freeze.n, chaos.n)
        )
        return study
    if freeze.failures and chaos.rate <= freeze.rate:
        study.note(
            "Shaking every knob did not make things worse than freezing them "
            "({0:.0%} against {1:.0%}). The flakiness is real, but its source "
            "is outside what jitter perturbs.".format(chaos.rate, freeze.rate)
        )

    # -- 3: solo. Screen every knob cheaply, then spend the rest of the budget
    # only on the ones that showed something.
    phase("one knob at a time", screen * len(names))
    _execute(
        runner,
        study,
        [
            (study.cell("solo:" + name, "solo", name, [name]), seed_value)
            for name in names
            for seed_value in pool[:screen]
        ],
        workers,
        progress,
    )

    promising = [name for name in names if study.solo(name).failures]
    if promising and trials > screen:
        phase(
            "confirming " + ", ".join(promising),
            (trials - screen) * len(promising),
        )
        _execute(
            runner,
            study,
            [
                (study.solo(name), seed_value)
                for name in promising
                for seed_value in pool[screen:trials]
            ],
            workers,
            progress,
        )

    # -- 4: leave-one-out. Only worth asking when chaos actually fails, and
    # the whole point is the knobs that could not do it alone.
    if chaos.failures:
        phase("everything but one knob", screen * len(names))
        _execute(
            runner,
            study,
            [
                (
                    study.cell(
                        "loo:" + name,
                        "loo",
                        name,
                        [other for other in names if other != name],
                    ),
                    seed_value,
                )
                for name in names
                for seed_value in pool[:screen]
            ],
            workers,
            progress,
        )
        rescued = [
            name
            for name in names
            if study.loo(name).rate < chaos.rate and study.loo(name).n < trials
        ]
        if rescued and trials > screen:
            phase(
                "confirming " + ", ".join(rescued) + " is needed",
                (trials - screen) * len(rescued),
            )
            _execute(
                runner,
                study,
                [
                    (study.loo(name), seed_value)
                    for name in rescued
                    for seed_value in pool[screen:trials]
                ],
                workers,
                progress,
            )
    return study
