"""Shrinking a failure down to a repro you can put a breakpoint in.

Attribution says *which* knobs matter. This says what to actually type. It
takes a configuration that failed, deletes knobs from it while the failure
survives, and stops at a set where removing anything makes the failure go
away -- delta debugging, over a set of twelve elements rather than a source
file.

The prize is at the end. Because a configuration is fully determined by
(shaken set, seed), the surviving configuration can be run again and again.
When it fails every time, the flake has stopped being a flake.

Called by: jitter/cli.py.
"""

from __future__ import annotations

from . import knobs as knobmod


class Minimiser:
    """Delta-debugging over the set of shaken knobs."""

    def __init__(self, runner, knobs, seed, *, probe=2, on_probe=None):
        self.runner = runner
        self.knobs = list(knobs)
        self.seed = seed
        self.probe = probe
        self.on_probe = on_probe or (lambda subset, failed: None)
        self.cache = {}
        self.probes = 0

    def _fails(self, subset):
        """Does this subset of shaken knobs still produce a failure?"""
        key = tuple(sorted(subset))
        if key in self.cache:
            return self.cache[key]
        failed = False
        settings = knobmod.settings_for(self.knobs, self.seed, set(key))
        for _ in range(self.probe):
            trial = self.runner.run("minimise", key, self.seed, settings)
            self.probes += 1
            if not trial.ok:
                failed = True
                break
        self.cache[key] = failed
        self.on_probe(key, failed)
        return failed

    def run(self, shaken):
        """Return the smallest subset of ``shaken`` that still fails."""
        items = list(shaken)
        if not items or not self._fails(items):
            return tuple(items)
        granularity = 2
        while len(items) >= 2:
            chunks = _split(items, min(granularity, len(items)))
            reduced = False
            for chunk in chunks:  # can we keep just one chunk?
                if chunk and self._fails(chunk):
                    items, granularity, reduced = chunk, 2, True
                    break
            if not reduced:
                for chunk in chunks:  # can we drop one chunk?
                    rest = [item for item in items if item not in chunk]
                    if rest and self._fails(rest):
                        items = rest
                        granularity = max(granularity - 1, 2)
                        reduced = True
                        break
            if not reduced:
                if granularity >= len(items):
                    break
                granularity = min(len(items), granularity * 2)
        return tuple(items)

    def verify(self, subset, times):
        """Run the minimal configuration repeatedly and count the failures."""
        settings = knobmod.settings_for(self.knobs, self.seed, set(subset))
        failures = 0
        tail = ""
        for _ in range(times):
            trial = self.runner.run("verify", subset, self.seed, settings)
            self.probes += 1
            if not trial.ok:
                failures += 1
                tail = tail or trial.tail
        return failures, tail, settings


def _split(items, count):
    """Split a list into ``count`` roughly equal chunks."""
    size, extra = divmod(len(items), count)
    chunks = []
    start = 0
    for index in range(count):
        stop = start + size + (1 if index < extra else 0)
        if stop > start:
            chunks.append(items[start:stop])
        start = stop
    return chunks
