"""Ten tests, each flaky for exactly one reason.

This file exists so you can watch jitter be right. Every test here passes in a
frozen world and fails when one specific knob is shaken, so pointing jitter at
a single test should name that knob and nothing else::

    jitter run -- pytest examples/flaky/test_flaky.py -k fsorder
    jitter run -- pytest examples/flaky/test_flaky.py -k state

None of these are contrived. Every one is a bug shape that has cost somebody a
week: golden output recorded under a different hash seed, a test that only
passes because it runs second, a cache key that straddles midnight, a sleep
standing in for synchronisation.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from datetime import datetime, timezone


# -- order: state left behind by another test -------------------------------

_LEDGER = []


def test_state_is_written():
    _LEDGER.append("row")
    assert _LEDGER


def test_state_is_read():
    # Only true when the test above already ran. Shuffle the order and this is
    # an empty list. `pytest -k state` to see it.
    assert len(_LEDGER) == 1


# -- fsorder: the filesystem is not sorted ----------------------------------


def test_fsorder_first_file_is_alphabetical():
    workspace = tempfile.mkdtemp()
    try:
        for name in ("apple.txt", "cherry.txt", "banana.txt", "damson.txt"):
            open(os.path.join(workspace, name), "w").close()
        # The bug: os.listdir has no defined order, but this code reads the
        # "first" file as though it did.
        assert os.listdir(workspace)[0] == "apple.txt"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# -- hashseed: golden output recorded on another machine --------------------

_WINNER = "print(next(iter({'alpha','beta','gamma','delta','epsilon'})))"


def _canonical_winner():
    """What this interpreter picks with hashing pinned -- the golden value."""
    env = dict(os.environ, PYTHONHASHSEED="0")
    out = subprocess.run(
        [sys.executable, "-c", _WINNER], stdout=subprocess.PIPE, env=env
    )
    return out.stdout.decode().strip()


def test_hashseed_matches_the_golden_value():
    # The bug: a result whose order comes from a set, compared against a value
    # somebody recorded once. It agrees only while the hash seed agrees.
    assert next(iter({"alpha", "beta", "gamma", "delta", "epsilon"})) == (
        _canonical_winner()
    )


# -- tz: local time and UTC are not the same calendar day -------------------


def test_tz_same_calendar_day():
    stamp = 1700000000  # 2023-11-14 22:13:20 UTC, late enough to roll over
    local_day = datetime.fromtimestamp(stamp).date()
    utc_day = datetime.fromtimestamp(stamp, timezone.utc).date()
    assert local_day == utc_day


# -- clock: a cache key that straddles midnight -----------------------------


def test_clock_daily_cache_has_a_sane_ttl():
    now = datetime.now()
    midnight = now.replace(hour=23, minute=59, second=59, microsecond=0)
    ttl = (midnight - now).total_seconds()
    # The bug: "expire at end of day" is fine at noon and useless at 23:59:50.
    assert ttl > 60, "daily cache would expire in {0:.0f}s".format(ttl)


# -- threads: check-then-act on a lazy singleton -----------------------------


def test_threads_cache_entry_is_built_once():
    ROUNDS = 10
    cache, builds = {}, []
    gate = threading.Barrier(2)  # both workers enter each round together

    def worker():
        for key in range(ROUNDS):
            gate.wait()
            # The bug: no lock between the check and the act. The gap is only
            # as wide as the work inside it -- about forty microseconds -- so
            # at the default 5ms switch interval it is almost never preempted,
            # and at a microsecond interval it always is.
            if key not in cache:
                total = 0
                for value in range(2000):
                    total += value
                builds.append(key)
                cache[key] = total

    workers = [threading.Thread(target=worker) for _ in range(2)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    assert len(builds) == ROUNDS, "built {0} times, wanted {1}".format(
        len(builds), ROUNDS
    )


# -- sleep: a sleep standing in for synchronisation -------------------------


def test_sleep_waits_long_enough_for_the_worker():
    done = []

    def worker():
        end = time.monotonic() + 0.25  # real work, on an unshifted clock
        while time.monotonic() < end:
            pass
        done.append(True)

    threading.Thread(target=worker, daemon=True).start()
    time.sleep(0.6)  # the bug: no event, no join, just a hopeful sleep
    assert done


# -- env: output asserted at one terminal width -----------------------------


def test_env_report_wraps_to_three_lines():
    columns = shutil.get_terminal_size((80, 24)).columns
    body = "the quick brown fox jumps over the lazy dog " * 4
    # The bug: a golden line count, recorded on an 80-column terminal.
    assert len(textwrap.wrap(body, columns)) == 3


# -- tmpdir: an unquoted path in a shell-out --------------------------------


def test_tmpdir_shell_out_survives_the_path():
    workspace = tempfile.mkdtemp()
    try:
        target = os.path.join(workspace, "payload.txt")
        with open(target, "w") as handle:
            handle.write("ok\n")
        # The bug: string-built shell command, no quoting.
        code = subprocess.call("cat " + target + " > /dev/null", shell=True)
        assert code == 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# -- locale: an assertion written against English ---------------------------


def test_locale_month_name_is_english():
    assert time.strftime("%B", time.gmtime(1700000000)) == "November"
