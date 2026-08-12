"""Runs inside the command under test, before any of its own code.

``site.py`` imports a module named ``sitecustomize`` if one is importable, and
by that point ``PYTHONPATH`` entries are already on ``sys.path``. Dropping a
copy of this file into a scratch directory at the front of ``PYTHONPATH`` is
therefore enough to perturb an arbitrary command -- and every Python subprocess
it spawns -- without touching the target project at all.

Everything here is read from the environment and applied defensively: a knob
that cannot be installed must leave the program running exactly as it would
have. A crash in this file would surface as a test failure, and jitter would
then confidently blame a knob for its own bug.

Copied next to ``sitecustomize.py`` as ``_jitter_shim.py`` by jitter/runner.py.
"""

import os
import random
import zlib


def _int(name, default=0):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# fsorder -- what order the filesystem hands directories back in
# --------------------------------------------------------------------------


class _Scandir:
    """A stand-in for os.scandir's result: an iterator that also closes."""

    def __init__(self, entries):
        self._it = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    next = __next__

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self._it = iter(())


def _install_fsorder(seed):
    """Sort or shuffle every directory listing in the process."""
    import glob
    import pathlib

    real_listdir = os.listdir
    real_scandir = os.scandir

    def arrange(items, key, where):
        if seed == 0:
            return sorted(items, key=key)
        rng = random.Random(seed ^ zlib.crc32(repr(where).encode()))
        out = list(items)
        rng.shuffle(out)
        return out

    def listdir(path=None):
        return arrange(real_listdir(path), lambda n: n, path)

    def scandir(path=None):
        handle = real_scandir() if path is None else real_scandir(path)
        with handle:
            entries = list(handle)
        return _Scandir(arrange(entries, lambda e: e.name, path))

    os.listdir = listdir
    os.scandir = scandir

    # os.walk routes through os.scandir, so it is already covered. Path.iterdir
    # and glob.glob capture their own references on some versions, so wrap them
    # by hand rather than hoping.
    real_iterdir = pathlib.Path.iterdir

    def iterdir(self):
        return iter(arrange(list(real_iterdir(self)), lambda p: p.name, str(self)))

    pathlib.Path.iterdir = iterdir

    real_glob = glob.glob

    def globbed(*args, **kwargs):
        return arrange(real_glob(*args, **kwargs), lambda p: p, args[:1])

    glob.glob = globbed


# --------------------------------------------------------------------------
# clock -- where in time the process believes it is
# --------------------------------------------------------------------------


def _install_clock(anchor):
    """Anchor wall-clock time to a fixed instant, then run forward from it.

    Monotonic clocks are deliberately untouched: shifting them would break
    every timeout in the program, and a timeout firing early is a bug jitter
    would have caused rather than found.
    """
    import datetime as dt
    import time

    real_time = time.time
    offset = anchor - real_time()

    def now():
        return real_time() + offset

    real_localtime = time.localtime
    real_gmtime = time.gmtime
    real_strftime = time.strftime

    time.time = now
    time.time_ns = lambda: int(now() * 1000000000)
    time.localtime = lambda secs=None: real_localtime(now() if secs is None else secs)
    time.gmtime = lambda secs=None: real_gmtime(now() if secs is None else secs)

    def strftime(fmt, t=None):
        return real_strftime(fmt, time.localtime() if t is None else t)

    time.strftime = strftime
    time.ctime = lambda secs=None: strftime("%a %b %d %H:%M:%S %Y", time.localtime(secs))

    real_datetime = dt.datetime
    real_date = dt.date

    class Datetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(now(), tz)

        @classmethod
        def utcnow(cls):
            return cls.utcfromtimestamp(now())

        @classmethod
        def today(cls):
            return cls.fromtimestamp(now())

    class Date(real_date):
        @classmethod
        def today(cls):
            return cls.fromtimestamp(now())

    dt.datetime = Datetime
    dt.date = Date


# --------------------------------------------------------------------------
# The remaining knobs, each a few lines
# --------------------------------------------------------------------------


def _install_sleep(scale):
    import time

    real_sleep = time.sleep

    def sleep(seconds):
        return real_sleep(seconds * scale)

    time.sleep = sleep


def _install_gc(spec):
    import gc

    parts = [int(n) for n in spec.split(",")]
    if parts == [0, 0, 0]:
        gc.disable()
        return
    gc.enable()
    gc.set_threshold(*parts)


def _install_order(seed):
    """Shuffle unittest method order. pytest is handled by its own plugin."""
    import unittest

    def compare(a, b):
        left = zlib.crc32("{0}:{1}".format(seed, a).encode())
        right = zlib.crc32("{0}:{1}".format(seed, b).encode())
        return (left > right) - (left < right)

    unittest.TestLoader.sortTestMethodsUsing = staticmethod(compare)


def _install_switchinterval(value):
    import sys

    sys.setswitchinterval(value)


def _install_tz():
    import time

    time.tzset()


#: (env var, is-this-value-a-no-op, apply). Skipping the no-ops keeps a pinned
#: run as close to an unmodified interpreter as it can be, so the frozen
#: baseline is not quietly paying for machinery the shaken runs need.
_STEPS = (
    ("JITTER_FSORDER", None, lambda: _install_fsorder(_int("JITTER_FSORDER"))),
    ("JITTER_ORDER", "0", lambda: _install_order(_int("JITTER_ORDER"))),
    ("JITTER_SLEEP", "1.0", lambda: _install_sleep(_float("JITTER_SLEEP", 1.0))),
    ("JITTER_GC", "700,10,10", lambda: _install_gc(os.environ["JITTER_GC"])),
    (
        "JITTER_SWITCHINTERVAL",
        None,
        lambda: _install_switchinterval(_float("JITTER_SWITCHINTERVAL", 0.005)),
    ),
    ("TZ", None, _install_tz),
    ("JITTER_CLOCK_AT", None, lambda: _install_clock(_float("JITTER_CLOCK_AT", 0.0))),
)


def install():
    """Apply every knob the environment asks for. Never raises."""
    problems = []
    for key, noop, apply in _STEPS:
        value = os.environ.get(key)
        if value is None or (noop is not None and value == noop):
            continue
        try:
            apply()
        except Exception as exc:  # pragma: no cover - defensive by design
            problems.append("{0}: {1}".format(key, exc))
    if problems and os.environ.get("JITTER_DEBUG"):
        import sys

        sys.stderr.write("jitter shim: " + "; ".join(problems) + "\n")
