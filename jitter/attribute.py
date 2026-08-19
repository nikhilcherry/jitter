"""Turning failure counts into an accusation.

Two questions get asked of every knob, and they are not the same question:

* **Sufficient** -- shaking only this knob fails more often than freezing
  everything. Compared against the freeze cell with a one-sided Fisher test.
* **Necessary** -- shaking everything *except* this knob fails less often than
  shaking everything. Compared against the chaos cell, the other way round.

A knob can be one, both, or neither. The interesting case is *necessary but
not sufficient*: a knob that does nothing on its own, but without which the
failure disappears. That is an interaction, and it is exactly the thing you
cannot find by rerunning a test with one environment variable changed.

Because a dozen knobs get tested at once, raw p-values would convict someone
about half the time by chance. Holm-Bonferroni fixes that; both the raw and
the corrected answer are kept, so the report can show its working.

Called by: jitter/cli.py, jitter/report.py.
"""

from __future__ import annotations

from .stats import fisher_greater, fisher_less, holm, wilson

#: What each role means, in the order they get ranked.
ROLES = {
    "confirmed": "taking it out of the repro makes the failure go away",
    "prime": "shaking it alone reproduces the failure, and pinning it stops it",
    "cause": "shaking it alone reproduces the failure",
    "required": "does nothing alone, but the failure needs it",
    "suspect": "leans the right way, but not past chance",
    "cleared": "no evidence it matters",
    "untested": "not shaken in this study",
}

GUILTY = ("confirmed", "prime", "cause", "required")

_RANK = {
    "confirmed": 0,
    "prime": 1,
    "cause": 2,
    "required": 3,
    "suspect": 4,
    "cleared": 5,
    "untested": 6,
}

#: Below this, a knob leaning the right way is worth mentioning as a suspect.
#: Above it, the lean is indistinguishable from which way the coin landed.
SUSPECT_P = 0.25


class Verdict:
    """What the study concluded about one knob."""

    __slots__ = (
        "knob",
        "solo_fail",
        "solo_n",
        "loo_fail",
        "loo_n",
        "solo_p",
        "loo_p",
        "solo_sig",
        "loo_sig",
        "lift",
        "drop",
        "role",
        "interval",
    )

    def __init__(self, knob):
        self.knob = knob
        self.solo_fail = self.solo_n = 0
        self.loo_fail = self.loo_n = 0
        self.solo_p = self.loo_p = 1.0
        self.solo_sig = self.loo_sig = False
        self.lift = 0.0
        self.drop = 0.0
        self.role = "untested"
        self.interval = (0.0, 1.0)

    @property
    def solo_rate(self):
        return self.solo_fail / self.solo_n if self.solo_n else 0.0

    @property
    def loo_rate(self):
        return self.loo_fail / self.loo_n if self.loo_n else 0.0

    @property
    def rank(self):
        return (_RANK[self.role], -self.lift, -self.drop, self.knob)

    def to_dict(self):
        return {
            "knob": self.knob,
            "role": self.role,
            "solo_fail": self.solo_fail,
            "solo_n": self.solo_n,
            "loo_fail": self.loo_fail,
            "loo_n": self.loo_n,
            "solo_p": self.solo_p,
            "loo_p": self.loo_p,
            "solo_sig": self.solo_sig,
            "loo_sig": self.loo_sig,
            "lift": self.lift,
            "drop": self.drop,
            "interval": list(self.interval),
        }


def judge(study, alpha: float = 0.05):
    """Score every knob in the study and rank the accusations."""
    freeze = study.freeze
    chaos = study.chaos
    verdicts = []

    for knob in study.knobs:
        verdict = Verdict(knob.name)
        solo = study.solo(knob.name)
        loo = study.loo(knob.name)

        if solo is not None and solo.n:
            verdict.solo_fail, verdict.solo_n = solo.failures, solo.n
            verdict.solo_p = fisher_greater(
                solo.failures,
                solo.n - solo.failures,
                freeze.failures,
                freeze.n - freeze.failures,
            )
            verdict.lift = solo.rate - freeze.rate
            verdict.interval = wilson(solo.failures, solo.n)
        if loo is not None and loo.n:
            verdict.loo_fail, verdict.loo_n = loo.failures, loo.n
            verdict.loo_p = fisher_less(
                loo.failures,
                loo.n - loo.failures,
                chaos.failures,
                chaos.n - chaos.failures,
            )
            verdict.drop = chaos.rate - loo.rate
        verdicts.append(verdict)

    # Two families, corrected separately. "Is this knob sufficient" and "is
    # this knob necessary" are different questions asked of different data;
    # pooling them into one family of 24 would make each correction twice as
    # brutal for no gain in honesty, and would routinely acquit a knob whose
    # own repro is sitting right there.
    for family, source, target in (
        ([v for v in verdicts if v.solo_n], "solo_p", "solo_sig"),
        ([v for v in verdicts if v.loo_n], "loo_p", "loo_sig"),
    ):
        flags = holm([getattr(v, source) for v in family], alpha)
        for verdict, flag in zip(family, flags):
            setattr(verdict, target, bool(flag))

    for verdict in verdicts:
        leaning = verdict.lift > 0 or verdict.drop > 0
        if not (verdict.solo_n or verdict.loo_n):
            verdict.role = "untested"
        elif verdict.solo_sig and verdict.loo_sig:
            verdict.role = "prime"
        elif verdict.solo_sig:
            verdict.role = "cause"
        elif verdict.loo_sig:
            verdict.role = "required"
        elif leaning and min(verdict.solo_p, verdict.loo_p) < SUSPECT_P:
            verdict.role = "suspect"
        else:
            verdict.role = "cleared"

    verdicts.sort(key=lambda v: v.rank)
    return verdicts


def confirm(verdicts, minimal, freeze=None, alpha: float = 0.05):
    """Promote every knob the minimal repro proved by taking it away.

    This is stronger evidence than the statistical screen, not weaker. The
    screen asks whether a difference in failure counts could have come from
    chance; minimisation removed the knob and watched the failure disappear,
    then put it back and watched it return. When the two disagree it is
    because the screen ran out of trials, so the ablation wins.

    That only holds while the repro actually reproduces. Minimisation accepts a
    subset on a single failing probe, so against a test that fails for a reason
    jitter cannot shake it converges on an arbitrary subset and then "verifies"
    it at the background flake rate. Requiring only one failure out of ten
    replays promoted that arbitrary knob to the strongest verdict available,
    overriding a screen that had put it at p = 0.2 -- and the headline went on
    to blame it by name.

    So the repro has to beat the freeze cell on the same one-sided test the
    screen uses. A configuration that fails no more often than pinning
    everything has explained nothing, whatever minimisation settled on.
    """
    if not minimal or not minimal.get("failures"):
        return verdicts
    if freeze is not None and freeze.n:
        trials = minimal.get("trials") or 0
        failures = minimal.get("failures") or 0
        if trials:
            p = fisher_greater(
                failures,
                trials - failures,
                freeze.failures,
                freeze.n - freeze.failures,
            )
            if p > alpha:
                return verdicts
    inside = set(minimal.get("shaken") or ())
    for verdict in verdicts:
        if verdict.knob in inside:
            verdict.role = "confirmed"
    verdicts.sort(key=lambda verdict: verdict.rank)
    return verdicts


def headline(study, verdicts, minimal=None):
    """The one sentence a person actually wants."""
    freeze, chaos = study.freeze, study.chaos
    guilty = [v for v in verdicts if v.role in GUILTY]
    if freeze.n and freeze.failures == freeze.n:
        return "not flaky -- broken: it failed every frozen trial"
    if not chaos.n or (chaos.failures == 0 and freeze.failures == 0):
        return "no flakiness reproduced"
    if not guilty:
        if freeze.failures:
            return "flaky, but not from anything jitter controls"
        return "flaky under chaos, but no single knob explains it"
    names = ", ".join(v.knob for v in guilty[:3])
    if len(guilty) > 3:
        names += " and {0} more".format(len(guilty) - 3)
    return "caused by " + names
