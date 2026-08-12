"""Exact statistics, in the standard library.

jitter compares failure counts between two small samples and has to say
something honest about whether the difference is real. With ten or twenty
trials per cell, the normal approximations everyone reaches for are wrong in
exactly the direction that matters -- they invent significance. So this module
uses Fisher's exact test, computed with integer arithmetic and divided once at
the end, which is both exact and, at these sample sizes, instant.

The alternative was a dependency on SciPy for three functions.

Called by: jitter/attribute.py, jitter/report.py.
"""

from __future__ import annotations

import math


def fisher_greater(a: int, b: int, c: int, d: int) -> float:
    """One-sided p for 'the top row fails more often than the bottom row'.

    The table is::

                 failed   passed
        cell        a        b
        baseline    c        d

    Returns P(X >= a) under the hypergeometric null: the probability of seeing
    at least this many failures in the cell if both rows really came from the
    same underlying failure rate.
    """
    n1, n2, k = a + b, c + d, a + c
    total = math.comb(n1 + n2, k)
    if total == 0:
        return 1.0
    extreme = 0
    for i in range(a, min(n1, k) + 1):
        j = k - i
        if 0 <= j <= n2:
            extreme += math.comb(n1, i) * math.comb(n2, j)
    return min(1.0, extreme / total)


def fisher_less(a: int, b: int, c: int, d: int) -> float:
    """One-sided p for 'the top row fails *less* often than the bottom row'.

    Used for leave-one-out: pinning a knob that matters should drop the failure
    rate below what full chaos produced.
    """
    n1, n2, k = a + b, c + d, a + c
    total = math.comb(n1 + n2, k)
    if total == 0:
        return 1.0
    extreme = 0
    for i in range(max(0, k - n2), a + 1):
        j = k - i
        if 0 <= j <= n2 and i <= n1:
            extreme += math.comb(n1, i) * math.comb(n2, j)
    return min(1.0, extreme / total)


def wilson(successes: int, trials: int, z: float = 1.959963985):
    """A 95% confidence interval for a proportion that still behaves at n = 12.

    The textbook interval puts its bounds outside [0, 1] and collapses to a
    point at zero successes, which would let jitter print '0% (+/- 0%)' for a
    knob it had barely tested. Wilson does neither.
    """
    if trials <= 0:
        return (0.0, 1.0)
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    spread = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def holm(pvalues, alpha: float = 0.05):
    """Holm-Bonferroni: which of these p-values survive testing a dozen knobs.

    Testing twelve knobs at p < 0.05 means roughly a one-in-two chance of
    accusing an innocent knob. Holm fixes that without the brutality of plain
    Bonferroni, and needs no assumption of independence.

    Returns a list of booleans, aligned with the input.
    """
    total = len(pvalues)
    if total == 0:
        return []
    order = sorted(range(total), key=lambda i: pvalues[i])
    survives = [False] * total
    for rank, index in enumerate(order):
        if pvalues[index] <= alpha / (total - rank):
            survives[index] = True
        else:
            break  # Holm stops at the first p-value it cannot reject
    return survives
