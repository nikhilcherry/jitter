"""jitter's own tests.

The interesting ones are at the bottom: an end-to-end study against a command
whose flakiness has a known cause, asserting that jitter names it.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jitter import knobs, report, runner, stats  # noqa: E402
from jitter.minimize import _split  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class StatsTests(unittest.TestCase):
    def test_fisher_greater_matches_a_hand_computed_table(self):
        # 3 of 3 against 0 of 3: the only more-extreme table is the one we saw,
        # so P = C(3,3)C(3,0)/C(6,3) = 1/20.
        self.assertAlmostEqual(stats.fisher_greater(3, 0, 0, 3), 0.05, places=9)

    def test_fisher_greater_is_one_when_nothing_happened(self):
        self.assertEqual(stats.fisher_greater(0, 5, 0, 5), 1.0)

    def test_fisher_less_is_the_mirror_image(self):
        self.assertAlmostEqual(
            stats.fisher_less(0, 3, 3, 0), stats.fisher_greater(3, 0, 0, 3), places=9
        )

    def test_more_evidence_lowers_the_p_value(self):
        few = stats.fisher_greater(4, 0, 0, 4)
        many = stats.fisher_greater(12, 0, 0, 12)
        self.assertLess(many, few)

    def test_wilson_stays_inside_the_unit_interval(self):
        for successes, trials in ((0, 5), (5, 5), (1, 12), (7, 13)):
            low, high = stats.wilson(successes, trials)
            self.assertGreaterEqual(low, 0.0)
            self.assertLessEqual(high, 1.0)
            self.assertLess(low, high)

    def test_wilson_does_not_collapse_at_zero(self):
        low, high = stats.wilson(0, 8)
        self.assertEqual(low, 0.0)
        self.assertGreater(high, 0.0)  # a textbook interval would say 0.0 here

    def test_holm_is_stricter_than_raw_alpha(self):
        self.assertEqual(
            stats.holm([0.001, 0.02, 0.04, 0.5]), [True, False, False, False]
        )

    def test_holm_admits_an_overwhelming_result(self):
        self.assertTrue(stats.holm([1e-9, 0.9, 0.9])[0])

    def test_holm_on_nothing(self):
        self.assertEqual(stats.holm([]), [])


class KnobTests(unittest.TestCase):
    def test_pinning_is_stable_forever(self):
        for knob in knobs.ALL:
            self.assertEqual(knob.pin(), knob.pin())

    def test_shaking_depends_only_on_the_seed(self):
        for knob in knobs.ALL:
            self.assertEqual(knob.shake(99), knob.shake(99))

    def test_two_knobs_do_not_share_a_stream(self):
        # If they did, attribution would be measuring one knob through another.
        self.assertNotEqual(
            knobs.stream(5, "order").random(), knobs.stream(5, "fsorder").random()
        )

    def test_settings_pin_everything_not_named(self):
        settings = knobs.settings_for(knobs.ALL, 42, {"fsorder"})
        self.assertNotEqual(settings["JITTER_FSORDER"], "0")
        self.assertEqual(settings["JITTER_ORDER"], "0")
        self.assertEqual(settings["PYTHONHASHSEED"], "0")
        self.assertEqual(settings["TZ"], "UTC")

    def test_the_frozen_world_does_not_depend_on_the_seed(self):
        frozen = knobs.settings_for(knobs.ALL, 42, set())
        self.assertEqual(frozen, knobs.settings_for(knobs.ALL, 7777, set()))
        self.assertNotEqual(
            frozen, knobs.settings_for(knobs.ALL, 42, {k.name for k in knobs.ALL})
        )

    def test_unknown_knob_is_refused_by_name(self):
        with self.assertRaises(SystemExit):
            knobs.resolve(["nonsense"])


class RunnerTests(unittest.TestCase):
    def test_managed_variables_are_stripped_from_the_baseline(self):
        os.environ["TZ"] = "Pacific/Kiritimati"
        os.environ["JITTER_ORDER"] = "12345"
        try:
            base = runner.base_environment()
            self.assertNotIn("TZ", base)
            self.assertNotIn("JITTER_ORDER", base)
        finally:
            del os.environ["TZ"]
            del os.environ["JITTER_ORDER"]

    def test_awkward_temp_directories_actually_exist(self):
        with tempfile.TemporaryDirectory() as root:
            for shape in ("plain", "long", "spaces", "unicode", "dots"):
                path = runner._tmpdir_for(shape, os.path.join(root, shape))
                self.assertTrue(os.path.isdir(path), shape)

    def test_repro_script_carries_the_configuration(self):
        script = runner.repro_script(
            ["pytest", "-q"], {"JITTER_ORDER": "77", "TZ": "UTC"}, "/tmp/project"
        )
        self.assertIn("JITTER_ORDER=77", script)
        self.assertIn("PYTHONPATH=", script)
        self.assertIn("/tmp/project", script)

    def test_repro_script_quotes_a_hostile_value(self):
        script = runner.repro_script(["true"], {"JITTER_TMPDIR": "a b; rm -rf /"}, ".")
        self.assertNotIn("=a b; rm -rf /", script)


class MinimiseTests(unittest.TestCase):
    def test_split_keeps_every_item_exactly_once(self):
        items = list(range(7))
        for count in range(1, 8):
            flat = [item for chunk in _split(items, count) for item in chunk]
            self.assertEqual(sorted(flat), items)


class ShimTests(unittest.TestCase):
    """The shim runs in another process, so test it the same way."""

    def _run(self, snippet, **env):
        with tempfile.TemporaryDirectory() as scratch:
            harness = runner.Runner([sys.executable, "-c", "pass"], scratch=scratch)
            child = dict(os.environ)
            child.update(env)
            child["PYTHONPATH"] = harness.inject
            out = subprocess.run(
                [sys.executable, "-c", snippet],
                env=child,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            return out.stdout.decode().strip()

    LIST_TMP = (
        "import os, tempfile\n"
        "d = tempfile.mkdtemp()\n"
        "[open(os.path.join(d, n), 'w').close() for n in 'dcba']\n"
        "print(','.join(os.listdir(d)))\n"
    )

    def test_pinned_fsorder_sorts_the_listing(self):
        self.assertEqual(self._run(self.LIST_TMP, JITTER_FSORDER="0"), "a,b,c,d")

    def test_shaken_fsorder_still_returns_every_entry(self):
        out = self._run(self.LIST_TMP, JITTER_FSORDER="98765")
        self.assertEqual(sorted(out.split(",")), ["a", "b", "c", "d"])

    def test_os_walk_survives_the_scandir_replacement(self):
        out = self._run(
            "import os, tempfile\n"
            "d = tempfile.mkdtemp()\n"
            "os.makedirs(os.path.join(d, 'sub'))\n"
            "open(os.path.join(d, 'sub', 'f'), 'w').close()\n"
            "print(sum(len(f) for _, _, f in os.walk(d)))\n",
            JITTER_FSORDER="4242",
        )
        self.assertEqual(out, "1")

    def test_the_clock_can_be_anchored(self):
        out = self._run(
            "import time; print(time.strftime('%Y-%m-%d', time.gmtime()))",
            JITTER_CLOCK_AT="1709251190.0",
            TZ="UTC",
        )
        self.assertEqual(out, "2024-02-29")

    def test_datetime_now_follows_the_anchor(self):
        out = self._run(
            "from datetime import datetime; print(datetime.utcnow().year)",
            JITTER_CLOCK_AT="1709251190.0",
        )
        self.assertEqual(out, "2024")

    def test_sleep_can_be_shortened(self):
        out = self._run(
            "import time\n"
            "s = time.monotonic()\n"
            "time.sleep(1.0)\n"
            "print('fast' if time.monotonic() - s < 0.5 else 'slow')\n",
            JITTER_SLEEP="0.05",
        )
        self.assertEqual(out, "fast")

    def test_a_broken_knob_never_breaks_the_program(self):
        out = self._run("print('alive')", JITTER_GC="not,a,number", JITTER_SLEEP="huh")
        self.assertEqual(out, "alive")


class ReportTests(unittest.TestCase):
    RECORD = {
        "name": "demo",
        "command": ["pytest", "-q"],
        "started": "2026-08-12T00:00:00Z",
        "executed": 40,
        "seed": 1,
        "knobs": ["order", "fsorder"],
        "headline": "caused by order",
        "headline_kind": "guilty",
        "wall_s": 3.0,
        "cells": [
            {
                "label": "freeze",
                "kind": "freeze",
                "knob": None,
                "shaken": [],
                "trials": [{"ok": True, "seed": 1, "exit_code": 0}],
            },
            {
                "label": "chaos",
                "kind": "chaos",
                "knob": None,
                "shaken": ["order"],
                "trials": [{"ok": False, "seed": 1, "exit_code": 1}],
            },
        ],
        "verdicts": [
            {
                "knob": "order",
                "role": "confirmed",
                "solo_fail": 9,
                "solo_n": 12,
                "loo_fail": 0,
                "loo_n": 12,
                "solo_p": 0.0001,
                "loo_p": 0.0002,
                "solo_sig": True,
                "loo_sig": True,
                "lift": 0.75,
                "drop": 0.75,
                "interval": [0.4, 0.9],
            }
        ],
        "minimal": {
            "shaken": ["order"],
            "seed": 5,
            "env": {"JITTER_ORDER": "5"},
            "failures": 10,
            "trials": 10,
            "probes": 8,
            "tail": "assert 0 == 1",
            "describe": [["order", "shuffled (seed 5)"]],
        },
        "notes": [],
    }

    def test_html_is_self_contained_and_says_the_headline(self):
        page = report.build(self.RECORD)
        self.assertIn("caused by order", page)
        self.assertIn("<style>", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("<script", page)

    def test_html_escapes_a_hostile_command(self):
        record = dict(self.RECORD, command=["pytest", "<script>alert(1)</script>"])
        self.assertNotIn("<script>alert", report.build(record))

    def test_a_fraction_of_nothing_is_not_a_crash(self):
        self.assertEqual(report.fraction(0, 0), "—")
        self.assertEqual(report.pvalue(0.0), "<0.001")


class EndToEndTests(unittest.TestCase):
    """The whole point: does jitter name the right knob?"""

    #: Fails if and only if the `order` knob is shaken. No amount of statistics
    #: can make this ambiguous, so if jitter gets it wrong, jitter is wrong.
    PROBE = (
        "import os, sys\n"
        "sys.exit(1 if os.environ.get('JITTER_ORDER', '0') != '0' else 0)\n"
    )

    def _jitter(self, root, *args):
        return subprocess.run(
            [sys.executable, "-m", "jitter", "--root", root, "--no-color"] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=ROOT,
            timeout=900,
        )

    def test_it_blames_the_knob_that_actually_did_it(self):
        with tempfile.TemporaryDirectory() as root:
            out = self._jitter(
                root,
                "run", "-n", "4", "--screen", "2", "--verify", "3",
                "--seed", "11", "-q", "--",
                sys.executable, "-c", self.PROBE,
            )
            text = out.stdout.decode()
            self.assertEqual(out.returncode, 0, text)
            self.assertIn("caused by order", text)
            self.assertIn("failed 3 of 3 replays", text)

    def test_freeze_reports_a_stable_command_as_stable(self):
        with tempfile.TemporaryDirectory() as root:
            out = self._jitter(
                root, "freeze", "-n", "2", "--", sys.executable, "-c", "pass"
            )
            self.assertEqual(out.returncode, 0, out.stdout.decode())
            self.assertIn("stable", out.stdout.decode())

    def test_a_command_that_always_fails_is_called_broken(self):
        with tempfile.TemporaryDirectory() as root:
            out = self._jitter(
                root,
                "run", "-n", "3", "--screen", "2", "-q", "--",
                sys.executable, "-c", "raise SystemExit(1)",
            )
            self.assertIn("broken", out.stdout.decode())


if __name__ == "__main__":
    unittest.main()
