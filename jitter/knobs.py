"""The dials jitter can turn.

Every knob is one isolated source of nondeterminism. A knob has exactly two
states -- **pinned** (a single fixed value, never varied) and **shaken** (a
value drawn from a pool using the trial's seed) -- and it communicates both to
the child process through nothing but environment variables.

That constraint is what keeps the tool honest. A configuration is a plain dict
of strings, so it can be stored as JSON, compared, minimised, and printed back
to you as a shell command that reproduces the failure exactly. There is no
hidden state on the parent side that a repro could silently depend on.

Knobs must also be *independent*: shaking two of them in the same trial must
not correlate them, or attribution is meaningless. Every knob therefore draws
from its own stream, seeded by the trial seed mixed with the knob's name, and
none of them uses ``hash()`` -- which is itself one of the things we shake.

Called by: jitter/plan.py, jitter/runner.py, jitter/minimize.py, jitter/cli.py.
"""

from __future__ import annotations

import os
import random
import subprocess
import zlib


def stream(seed: int, name: str) -> random.Random:
    """A private random stream for one knob in one trial.

    Mixed with ``crc32`` rather than ``hash`` so the stream is stable across
    interpreters and, more importantly, unaffected by the ``hashseed`` knob.
    """
    return random.Random((seed * 2654435761) ^ zlib.crc32(name.encode()))


class Knob:
    """One source of nondeterminism, pinnable and shakeable."""

    name = ""
    summary = ""
    detail = ""
    #: True when the perturbation is applied by the injected Python shim, and
    #: so has no effect on a command that never starts a Python interpreter.
    python_only = True

    def pin(self):
        """The frozen value. Identical in every trial, forever."""
        raise NotImplementedError

    def shake(self, seed: int):
        """A value drawn for this trial."""
        raise NotImplementedError

    def available(self) -> str:
        """Empty when usable, else a one-line reason it cannot be shaken."""
        return ""

    def describe(self, setting) -> str:
        """Human summary of a drawn value, for the report."""
        return ", ".join("{0}={1}".format(k, v) for k, v in sorted(setting.items()))


# --------------------------------------------------------------------------
# Interpreter-level entropy
# --------------------------------------------------------------------------


class HashSeed(Knob):
    name = "hashseed"
    summary = "randomise string hashing (set and dict iteration order)"
    detail = (
        "Sets PYTHONHASHSEED. Every str/bytes hash changes, so any code that "
        "iterates a set -- or a dict keyed by anything hashed by value -- sees "
        "a different order. Classic cause of 'passes on my machine'."
    )

    def pin(self):
        return {"PYTHONHASHSEED": "0"}

    def shake(self, seed):
        rng = stream(seed, self.name)
        return {"PYTHONHASHSEED": str(rng.randrange(1, 2**32 - 1))}


class Order(Knob):
    name = "order"
    summary = "shuffle the order tests run in"
    detail = (
        "Reorders collected pytest items and unittest methods. Surfaces tests "
        "that only pass because an earlier test left state behind -- a module "
        "global, a populated cache, a row in a database, a patched import."
    )

    def pin(self):
        return {"JITTER_ORDER": "0"}

    def shake(self, seed):
        rng = stream(seed, self.name)
        return {"JITTER_ORDER": str(rng.randrange(1, 2**31))}

    def describe(self, setting):
        value = setting.get("JITTER_ORDER", "0")
        return "declared order" if value == "0" else "shuffled (seed {0})".format(value)


class FsOrder(Knob):
    name = "fsorder"
    summary = "shuffle the order the filesystem lists directories in"
    detail = (
        "Wraps os.listdir, os.scandir, os.walk and Path.iterdir. Pinned they "
        "return sorted order; shaken they return a shuffled one. ext4 and APFS "
        "hand back different orders for the same directory, which is one "
        "reason 'it works locally' and CI disagree."
    )

    def pin(self):
        return {"JITTER_FSORDER": "0"}

    def shake(self, seed):
        rng = stream(seed, self.name)
        return {"JITTER_FSORDER": str(rng.randrange(1, 2**31))}

    def describe(self, setting):
        value = setting.get("JITTER_FSORDER", "0")
        return "sorted" if value == "0" else "shuffled (seed {0})".format(value)


class Threads(Knob):
    name = "threads"
    summary = "change how often the interpreter switches threads"
    detail = (
        "Sets sys.setswitchinterval. A tiny interval preempts threads between "
        "almost every bytecode, which turns a one-in-a-million race into a "
        "one-in-ten one. A huge interval hides races that a loaded CI box "
        "would hit."
    )

    _POOL = (0.000001, 0.000005, 0.00002, 0.0001, 0.05, 0.25)

    def pin(self):
        return {"JITTER_SWITCHINTERVAL": "0.005"}

    def shake(self, seed):
        rng = stream(seed, self.name)
        return {"JITTER_SWITCHINTERVAL": repr(rng.choice(self._POOL))}

    def describe(self, setting):
        return "{0}s switch interval".format(setting.get("JITTER_SWITCHINTERVAL", "?"))


class Sleep(Knob):
    name = "sleep"
    summary = "make time.sleep return early"
    detail = (
        "Scales every time.sleep by a factor below 1. A test that only passes "
        "because someone added sleep(0.5) to 'fix the flake' fails here, and "
        "that is the correct answer: the synchronisation is missing, not slow."
    )

    def pin(self):
        return {"JITTER_SLEEP": "1.0"}

    def shake(self, seed):
        rng = stream(seed, self.name)
        return {"JITTER_SLEEP": "{0:.4f}".format(rng.uniform(0.0, 0.35))}

    def describe(self, setting):
        scale = float(setting.get("JITTER_SLEEP", "1.0"))
        if scale >= 1.0:
            return "honoured in full"
        return "shortened to {0:.0%}".format(scale)


class Gc(Knob):
    name = "gc"
    summary = "collect garbage at unusual moments"
    detail = (
        "Randomises gc thresholds, sometimes down to (1, 1, 1) so a collection "
        "runs almost constantly. Surfaces bugs in __del__ ordering, weakref "
        "callbacks, and objects that resurrect themselves during finalisation."
    )

    _POOL = ((1, 1, 1), (5, 2, 2), (10, 3, 3), (2000, 20, 20), (0, 0, 0))

    def pin(self):
        return {"JITTER_GC": "700,10,10"}

    def shake(self, seed):
        rng = stream(seed, self.name)
        return {"JITTER_GC": ",".join(str(n) for n in rng.choice(self._POOL))}

    def describe(self, setting):
        value = setting.get("JITTER_GC", "")
        return "disabled" if value == "0,0,0" else "thresholds ({0})".format(value)


# --------------------------------------------------------------------------
# World-level entropy -- applies to a command in any language
# --------------------------------------------------------------------------


class Timezone(Knob):
    name = "tz"
    summary = "run in a hostile timezone"
    detail = (
        "Sets TZ. The pool is deliberately awful: +14:00, a 45-minute offset, "
        "a half-hour offset, and zones whose DST runs backwards relative to "
        "the northern hemisphere. Catches code that assumes local time and UTC "
        "fall on the same calendar day."
    )
    python_only = False

    _POOL = (
        "Pacific/Kiritimati",  # UTC+14, always a day ahead
        "Pacific/Chatham",  # UTC+12:45, a 45-minute offset
        "Asia/Kolkata",  # UTC+05:30
        "America/St_Johns",  # UTC-03:30
        "Pacific/Marquesas",  # UTC-09:30
        "Australia/Lord_Howe",  # a 30-minute DST shift
        "Pacific/Apia",  # has skipped an entire calendar day
        "America/Santiago",  # southern-hemisphere DST
    )

    def pin(self):
        return {"TZ": "UTC"}

    def shake(self, seed):
        rng = stream(seed, self.name)
        return {"TZ": rng.choice(self._POOL)}

    def describe(self, setting):
        return setting.get("TZ", "UTC")


class Clock(Knob):
    name = "clock"
    summary = "start the process at a nasty instant"
    detail = (
        "Anchors time.time and datetime.now to a moment chosen to break "
        "things: ten seconds before midnight, the end of February, a leap day, "
        "a DST spring-forward. Monotonic clocks are left alone so timeouts "
        "still work. Never pinned -- freezing wall time breaks TLS and caches "
        "-- so the frozen baseline runs on the real clock."
    )

    #: Epoch seconds. Fixed constants, so a repro means the same thing in a
    #: year's time as it does today.
    _POOL = (
        (1735689590.0, "2024-12-31 23:59:50 UTC, ten seconds from a new year"),
        (1709164790.0, "2024-02-28 23:59:50 UTC, the day before a leap day"),
        (1709251190.0, "2024-02-29 23:59:50 UTC, a leap day ending"),
        (1730332800.0, "2024-10-31 00:00:00 UTC, a month boundary at midnight"),
        (1711846800.0, "2024-03-31 01:00:00 UTC, a European DST spring-forward"),
        (2145916790.0, "2037-12-31 23:59:50 UTC, near the 32-bit time_t cliff"),
    )

    def pin(self):
        return {}

    def shake(self, seed):
        rng = stream(seed, self.name)
        at, _ = rng.choice(self._POOL)
        return {"JITTER_CLOCK_AT": repr(at)}

    def describe(self, setting):
        at = setting.get("JITTER_CLOCK_AT")
        if not at:
            return "real clock"
        for epoch, label in self._POOL:
            if repr(epoch) == at:
                return label
        return at


class Locale(Knob):
    name = "locale"
    summary = "run under a different locale"
    detail = (
        "Sets LC_ALL and LANG. Changes month and weekday names, decimal "
        "separators, currency formatting and collation order. Catches "
        "assertions written against English output, and sorts that assumed "
        "ASCII ordering."
    )
    python_only = False

    _POOL = (
        "de_DE.UTF-8",  # comma as the decimal separator
        "fr_FR.UTF-8",
        "tr_TR.UTF-8",  # the dotless i
        "ja_JP.UTF-8",
        "es_ES.UTF-8",
        "ru_RU.UTF-8",
    )

    _installed = None

    @classmethod
    def _have(cls):
        if cls._installed is None:
            found = set()
            try:
                out = subprocess.run(
                    ["locale", "-a"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                for line in out.stdout.decode("utf-8", "replace").splitlines():
                    name = line.strip()
                    found.add(name)
                    found.add(name.replace("utf8", "UTF-8"))
            except Exception:
                pass
            cls._installed = found
        return cls._installed

    def _pool(self):
        have = self._have()
        return tuple(name for name in self._POOL if name in have)

    def available(self):
        if not self._pool():
            return "no extra locales installed (try: locale-gen de_DE.UTF-8)"
        return ""

    def pin(self):
        return {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}

    def shake(self, seed):
        pool = self._pool()
        if not pool:
            return self.pin()
        rng = stream(seed, self.name)
        choice = rng.choice(pool)
        return {"LC_ALL": choice, "LANG": choice}

    def describe(self, setting):
        return setting.get("LC_ALL", "C.UTF-8")


class Environ(Knob):
    name = "env"
    summary = "resize the terminal and reshuffle the environment"
    detail = (
        "Randomises COLUMNS and LINES, reorders the whole environment block, "
        "and pads it with junk variables. Catches output assertions that "
        "depend on terminal width, and anything that reads `env` positionally."
    )
    python_only = False

    _WIDTHS = (20, 40, 62, 100, 137, 200, 400)

    def pin(self):
        return {"COLUMNS": "80", "LINES": "24", "JITTER_ENV": "0"}

    def shake(self, seed):
        rng = stream(seed, self.name)
        return {
            "COLUMNS": str(rng.choice(self._WIDTHS)),
            "LINES": str(rng.choice((5, 12, 24, 60, 120))),
            "JITTER_ENV": str(rng.randrange(1, 2**31)),
        }

    def describe(self, setting):
        cols = setting.get("COLUMNS", "80")
        if setting.get("JITTER_ENV", "0") == "0":
            return "{0} columns, sorted".format(cols)
        return "{0} columns, shuffled + padded".format(cols)


class TmpDir(Knob):
    name = "tmpdir"
    summary = "give the process an awkward temporary directory"
    detail = (
        "Points TMPDIR, TEMP and TMP at a path that is very long, or contains "
        "a space, or contains non-ASCII characters. Catches shell-outs that "
        "forgot to quote, and buffers sized for a short path."
    )
    python_only = False

    _SHAPES = ("long", "spaces", "unicode", "dots", "plain")

    def pin(self):
        return {"JITTER_TMPDIR": "plain"}

    def shake(self, seed):
        rng = stream(seed, self.name)
        return {"JITTER_TMPDIR": rng.choice(self._SHAPES)}

    def describe(self, setting):
        return "{0} path".format(setting.get("JITTER_TMPDIR", "plain"))


class Load(Knob):
    name = "load"
    summary = "compete for the CPU"
    detail = (
        "Spawns busy processes alongside the command for as long as it runs. "
        "Changes scheduling, lengthens every wall-clock measurement, and makes "
        "the machine behave like a contended CI runner rather than an idle "
        "laptop."
    )
    python_only = False

    def pin(self):
        return {"JITTER_LOAD": "0"}

    def shake(self, seed):
        rng = stream(seed, self.name)
        cpus = os.cpu_count() or 4
        return {"JITTER_LOAD": str(rng.randint(1, max(2, cpus)))}

    def describe(self, setting):
        count = int(setting.get("JITTER_LOAD", "0"))
        return "idle" if count == 0 else "{0} competing process(es)".format(count)


ALL = (
    Order(),
    FsOrder(),
    HashSeed(),
    Threads(),
    Sleep(),
    Gc(),
    Timezone(),
    Clock(),
    Locale(),
    Environ(),
    TmpDir(),
    Load(),
)

BY_NAME = {knob.name: knob for knob in ALL}
NAMES = tuple(knob.name for knob in ALL)


def resolve(names):
    """Turn user-supplied knob names into knobs, or explain why they are not."""
    chosen = []
    for raw in names:
        knob = BY_NAME.get(raw)
        if knob is None:
            raise SystemExit(
                "jitter: unknown knob {0!r}. Known: {1}".format(raw, ", ".join(NAMES))
            )
        if knob not in chosen:
            chosen.append(knob)
    return chosen


def settings_for(knobs, seed, shaken):
    """Build one trial's environment fragment.

    ``shaken`` is the set of knob names to draw fresh values for; every other
    knob is pinned. This is the only place a configuration is ever built, so a
    trial can never quietly differ from the label we gave it.
    """
    env = {}
    for knob in knobs:
        env.update(knob.pin())
        if knob.name in shaken:
            env.update(knob.shake(seed))
    return env
