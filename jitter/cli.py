"""The command line.

Called by: the ``jitter`` console script, and ``python -m jitter``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
import time

from . import __version__
from . import knobs as knobmod
from . import report as reportmod
from . import store
from .attribute import GUILTY, ROLES, confirm, headline, judge
from .minimize import Minimiser
from .plan import investigate
from .runner import Runner, repro_script
from .term import (
    Style,
    clip,
    colour_enabled,
    human_time,
    plural,
    rule,
    truncate,
    width,
)

DEFAULT_TRIALS = 12
DEFAULT_SCREEN = 6
DEFAULT_VERIFY = 10


# --------------------------------------------------------------------------
# Progress, on stderr, so the report on stdout stays pipeable
# --------------------------------------------------------------------------


class Progress:
    def __init__(self, style, enabled=True, stream=None):
        self.style = style
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self.title = ""
        self.expected = 0
        self.done = 0
        self.failed = 0
        self.open = False

    def phase(self, title, expected):
        self.finish()
        self.title, self.expected, self.done, self.failed = title, expected, 0, 0
        self.open = True
        self._paint()

    def tick(self, trial):
        self.done += 1
        if not trial.ok:
            self.failed += 1
        self._paint()

    def _paint(self):
        if not self.enabled or not self.open:
            return
        status = (
            self.style.red("{0} failed".format(self.failed))
            if self.failed
            else self.style.grey("all passing")
        )
        line = "  {0:<34} {1:>4}/{2:<4} {3}".format(
            truncate(self.title, 34), self.done, self.expected, status
        )
        self.stream.write("\r" + line.ljust(width()))
        self.stream.flush()

    def finish(self):
        if self.enabled and self.open:
            self._paint()
            self.stream.write("\n")
            self.stream.flush()
        self.open = False


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def looks_like_python(command):
    """Best-effort check that the command will start a Python interpreter.

    Wrong in the safe direction: when unsure it says no, and the Python-only
    knobs are dropped with a note rather than silently measuring nothing.
    """
    if not command:
        return False
    exe = os.path.basename(command[0]).lower()
    if exe.startswith("python") or exe.endswith(".py"):
        return True
    resolved = shutil.which(command[0])
    if not resolved:
        return False
    try:
        with open(resolved, "rb") as handle:
            first = handle.readline(256)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first.lower()


def choose_knobs(only, exclude, command, warn):
    """Work out which knobs this study can honestly turn."""
    chosen = knobmod.resolve(only) if only else list(knobmod.ALL)
    if exclude:
        dropped = {knob.name for knob in knobmod.resolve(exclude)}
        chosen = [knob for knob in chosen if knob.name not in dropped]

    if not looks_like_python(command):
        deep = [knob for knob in chosen if knob.python_only]
        if deep:
            warn(
                "not a Python command, so {0} cannot be shaken: {1}".format(
                    plural(len(deep), "knob"), ", ".join(k.name for k in deep)
                )
            )
            chosen = [knob for knob in chosen if not knob.python_only]

    usable = []
    for knob in chosen:
        reason = knob.available()
        if reason:
            warn("skipping {0}: {1}".format(knob.name, reason))
        else:
            usable.append(knob)
    if not usable:
        raise SystemExit("jitter: no knobs left to shake.")
    return usable


def scratch_for(root, name):
    path = os.path.join(root, ".jitter", "tmp", "{0}-{1}".format(name, os.getpid()))
    os.makedirs(path, exist_ok=True)
    return path


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wrap(text, limit):
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > limit:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines


# --------------------------------------------------------------------------
# Rendering -- reads the saved record, so `run` and `show` cannot diverge
# --------------------------------------------------------------------------


def render(record, style, out=None):
    out = out or sys.stdout
    cells = {cell["label"]: cell for cell in record["cells"]}

    def counts(label):
        cell = cells.get(label)
        if not cell:
            return 0, 0
        trials = cell["trials"]
        return sum(1 for trial in trials if not trial["ok"]), len(trials)

    def say(text=""):
        out.write(text + "\n")

    command = " ".join(record["command"])
    say(rule("jitter: " + truncate(command, max(20, width() - 14)), style))

    paint = {
        "guilty": style.red,
        "broken": style.yellow,
        "unknown": style.yellow,
        "clean": style.green,
    }.get(record.get("headline_kind", "clean"), style.bold)
    say("  " + style.bold(paint(record.get("headline", ""))))
    say()

    for label, name in (("freeze", "frozen"), ("chaos", "shaken")):
        failed, total = counts(label)
        if not total:
            continue
        rate = failed / total
        colour = style.red if failed else style.green
        say(
            "    {0:<8} {1}  {2:>4.0%}  {3}".format(
                name,
                colour(reportmod.text_bar(rate)),
                rate,
                style.grey("{0} of {1} trials failed".format(failed, total)),
            )
        )
    say()

    verdicts = record.get("verdicts") or []
    # A knob with nothing to say about it is noise in a list of twelve. The
    # HTML report keeps every row; here they collapse to a single line.
    interesting = [v for v in verdicts if v["role"] not in ("untested", "cleared")]
    cleared = [v["knob"] for v in verdicts if v["role"] == "cleared"]
    if interesting:
        say(rule("what matters", style))
        say(
            style.grey(
                "  {0:<10} {1:<16} {2:<16} {3:<9} {4}".format(
                    "KNOB", "SHAKEN ALONE", "ALL BUT THIS", "p", "VERDICT"
                )
            )
        )
        for entry in interesting:
            role = entry["role"]
            tint = {
                "confirmed": style.red,
                "prime": style.red,
                "cause": style.red,
                "required": style.yellow,
                "suspect": style.grey,
                "cleared": style.grey,
            }.get(role, style.grey)
            best = min(entry["solo_p"], entry["loo_p"])
            say(
                "  {0:<10} {1:<16} {2:<16} {3:<9} {4}".format(
                    entry["knob"],
                    reportmod.fraction(entry["solo_fail"], entry["solo_n"]),
                    reportmod.fraction(entry["loo_fail"], entry["loo_n"]),
                    reportmod.pvalue(best) if (entry["solo_n"] or entry["loo_n"]) else "",
                    tint("prime cause" if role == "prime" else role),
                )
            )
        if cleared:
            say(
                style.grey(
                    "  {0:<10} {1}".format(
                        "cleared", truncate(", ".join(cleared), width() - 14)
                    )
                )
            )
        say()
        for role in ("confirmed", "prime", "cause", "required"):
            if any(entry["role"] == role for entry in interesting):
                say(style.grey("  {0:<14} {1}".format(role, ROLES[role])))
        say()

    minimal = record.get("minimal")
    if minimal:
        say(rule("the repro", style))
        say(
            "  {0} of {1} knobs is enough:".format(
                len(minimal["shaken"]) or "none", len(record["knobs"])
            )
        )
        for name, description in minimal.get("describe", []):
            say("    {0:<10} {1}".format(style.bold(name), description))
        rate = minimal["failures"] / minimal["trials"] if minimal["trials"] else 0
        line = "  verified: failed {0} of {1} replays".format(
            minimal["failures"], minimal["trials"]
        )
        say(style.red(line) if rate >= 0.999 else style.yellow(line))
        say()
        say(style.grey("    jitter replay              # run that configuration again"))
        say(style.grey("    jitter repro > bug.sh      # a standalone script"))
        say()

        tail = minimal.get("tail") or ""
        if tail.strip():
            say(rule("what it printed when it failed", style))
            for line in tail.rstrip().splitlines()[-18:]:
                say(style.grey("  │ ") + clip(line, width() - 6))
            say()

    for note in record.get("notes") or []:
        for line in _wrap(note, width() - 4):
            say("  " + style.yellow(line))
        say()

    say(
        style.grey(
            "  {0} in {1}  ·  seed {2}  ·  {3}".format(
                plural(record["executed"], "run"),
                human_time(record["wall_s"]),
                record["seed"],
                record.get("_path", ""),
            )
        )
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


class _Probe:
    """A stand-in so minimisation probes can share the trial progress bar."""

    __slots__ = ("ok",)

    def __init__(self, failed):
        self.ok = not failed


def _minimise(study, runner, knobs, args, progress):
    """Find the smallest configuration that still fails, then prove it."""
    failing = [
        (cell, trial)
        for cell in study.cells.values()
        for trial in cell.trials
        if not trial.ok
    ]
    if not failing:
        return None
    # Prefer a chaos failure: it has the most knobs to take away, so the
    # minimisation is doing real work rather than confirming a single knob.
    failing.sort(
        key=lambda pair: (pair[0].kind != "chaos", -len(pair[0].shaken), pair[1].seed)
    )
    cell, trial = failing[0]

    progress.phase("shrinking the repro", len(cell.shaken) * 2 + args.verify)
    minimiser = Minimiser(
        runner,
        knobs,
        trial.seed,
        probe=args.probe,
        on_probe=lambda subset, failed: progress.tick(_Probe(failed)),
    )
    smallest = minimiser.run(list(cell.shaken))
    failures, tail, settings = minimiser.verify(smallest, args.verify)
    progress.finish()

    lookup = {knob.name: knob for knob in knobs}
    return {
        "shaken": list(smallest),
        "seed": trial.seed,
        "env": settings,
        "failures": failures,
        "trials": args.verify,
        "probes": minimiser.probes,
        "tail": tail or trial.tail,
        "describe": [
            [name, lookup[name].describe(settings)]
            for name in smallest
            if name in lookup
        ],
    }


def _headline_kind(study, verdicts):
    if study.freeze.n and study.freeze.failures == study.freeze.n:
        return "broken"
    if any(verdict.role in GUILTY for verdict in verdicts):
        return "guilty"
    if study.chaos.failures or study.freeze.failures:
        return "unknown"
    return "clean"


def cmd_run(args, command, style):
    if not command:
        raise SystemExit("jitter: give me a command. Try: jitter run -- pytest -q")

    root = os.path.abspath(args.root)
    warnings = []
    knobs = choose_knobs(args.knob, args.exclude, command, warnings.append)
    for text in warnings:
        sys.stderr.write("  " + style.yellow("note: " + text) + "\n")

    seed = args.seed if args.seed is not None else int(time.time()) & 0xFFFFFF
    scratch = scratch_for(root, store.safe_name(args.name))
    runner = Runner(
        command,
        scratch=scratch,
        cwd=os.getcwd(),
        timeout=args.timeout,
        expect_code=args.expect_code,
        fail_pattern=args.fail_on,
    )
    progress = Progress(style, enabled=not args.quiet)
    started_at = now_iso()
    began = time.perf_counter()

    try:
        study = investigate(
            runner,
            knobs,
            trials=args.trials,
            screen=min(args.screen, args.trials),
            seed=seed,
            workers=args.jobs,
            progress=progress.tick,
            phase=progress.phase,
        )
        verdicts = judge(study, alpha=args.alpha)
        minimal = (
            None if args.no_minimise else _minimise(study, runner, knobs, args, progress)
        )
        # Minimisation is an ablation, so it outranks the screen. Fold its
        # result in before anything is written down or printed.
        confirm(verdicts, minimal)
    finally:
        progress.finish()
        runner.close()

    record = {
        "name": store.safe_name(args.name),
        "command": list(command),
        "cwd": os.getcwd(),
        "started": started_at,
        "wall_s": time.perf_counter() - began,
        "executed": runner.executed,
        "seed": seed,
        "knobs": [knob.name for knob in knobs],
        "trials_per_cell": args.trials,
        "timeout": args.timeout,
        "expect_code": args.expect_code,
        "fail_pattern": args.fail_on,
        "headline": headline(study, verdicts, minimal),
        "headline_kind": _headline_kind(study, verdicts),
        "cells": [cell.to_dict() for cell in study.cells.values()],
        "verdicts": [verdict.to_dict() for verdict in verdicts],
        "minimal": minimal,
        "notes": study.notes,
    }
    path = store.save(root, record)
    record["_path"] = os.path.relpath(path, root)
    render(record, style)

    if args.report:
        reportmod.write(record, args.report)
        sys.stderr.write("  report → {0}\n".format(args.report))

    if args.strict and (study.chaos.failures or study.freeze.failures):
        return 1
    return 0


def cmd_freeze(args, command, style):
    """Run the command in the frozen world. A determinism gate for CI."""
    if not command:
        raise SystemExit("jitter: give me a command. Try: jitter freeze -- pytest -q")
    root = os.path.abspath(args.root)
    warnings = []
    knobs = choose_knobs(args.knob, args.exclude, command, warnings.append)
    for text in warnings:
        sys.stderr.write("  " + style.yellow("note: " + text) + "\n")

    runner = Runner(
        command,
        scratch=scratch_for(root, "freeze"),
        cwd=os.getcwd(),
        timeout=args.timeout,
        expect_code=args.expect_code,
        fail_pattern=args.fail_on,
    )
    settings = knobmod.settings_for(knobs, 0, set())
    failures = []
    try:
        print(rule("frozen world", style))
        for index in range(args.trials):
            trial = runner.run("freeze", (), 0, settings)
            mark = style.green("pass") if trial.ok else style.red("FAIL")
            print(
                "  {0:>3}/{1}  {2}  {3}".format(
                    index + 1, args.trials, mark, style.grey(human_time(trial.wall_s))
                )
            )
            if not trial.ok:
                failures.append(trial)
    finally:
        runner.close()

    print()
    if failures:
        print(
            "  "
            + style.red(
                "{0} of {1} runs failed with every knob pinned.".format(
                    len(failures), args.trials
                )
            )
        )
        print(
            style.grey(
                "  Nothing jitter controls explains this. Look at the network, "
                "shared state, or real randomness."
            )
        )
        for line in (failures[0].tail or "").strip().splitlines()[-15:]:
            print(style.grey("  │ ") + truncate(line, width() - 6))
        return 1
    print("  " + style.green("stable: {0} runs, no failures.".format(args.trials)))
    return 0


def cmd_replay(args, command, style):
    root = os.path.abspath(args.root)
    record = store.load(root, args.study)
    minimal = record.get("minimal")
    if not minimal:
        raise SystemExit("jitter: that study has no minimal repro to replay.")

    runner = Runner(
        record["command"],
        scratch=scratch_for(root, "replay"),
        cwd=record.get("cwd") or os.getcwd(),
        timeout=record.get("timeout") or 300.0,
        expect_code=record.get("expect_code", 0),
        fail_pattern=record.get("fail_pattern"),
    )
    print(rule("replaying " + record["name"], style))
    for name, description in minimal.get("describe", []):
        print("    {0:<10} {1}".format(style.bold(name), description))
    print()
    failures = 0
    first_tail = ""
    try:
        for index in range(args.trials):
            trial = runner.run(
                "replay", minimal["shaken"], minimal["seed"], minimal["env"]
            )
            mark = style.green("pass") if trial.ok else style.red("FAIL")
            print("  {0:>3}/{1}  {2}".format(index + 1, args.trials, mark))
            if not trial.ok:
                failures += 1
                first_tail = first_tail or trial.tail
    finally:
        runner.close()

    print()
    line = "  failed {0} of {1}".format(failures, args.trials)
    print(style.red(line) if failures else style.green(line))
    if args.show_output and first_tail:
        print()
        for text in first_tail.strip().splitlines()[-40:]:
            print(style.grey("  │ ") + truncate(text, width() - 6))
    return 1 if failures * 2 >= args.trials else 0


def cmd_repro(args, command, style):
    root = os.path.abspath(args.root)
    record = store.load(root, args.study)
    minimal = record.get("minimal")
    if not minimal:
        raise SystemExit("jitter: that study has no minimal repro to print.")
    script = repro_script(record["command"], minimal["env"], record.get("cwd") or ".")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(args.output, 0o755)
        sys.stderr.write("  wrote {0}\n".format(args.output))
    else:
        sys.stdout.write(script)
    return 0


def cmd_show(args, command, style):
    root = os.path.abspath(args.root)
    record = store.load(root, args.study)
    if args.json:
        json.dump(
            {k: v for k, v in record.items() if k != "_path"}, sys.stdout, indent=1
        )
        sys.stdout.write("\n")
        return 0
    record["_path"] = os.path.relpath(record.get("_path", ""), root)
    render(record, style)
    return 0


def cmd_list(args, command, style):
    found = store.list_runs(os.path.abspath(args.root))
    if not found:
        print("jitter: nothing recorded yet. Try: jitter run -- <command>")
        return 0
    print(rule("studies", style))
    for record in found:
        print(
            "  {0:<16} {1:<12} {2}".format(
                record.get("name", "?"),
                record.get("started", "")[:10],
                truncate(record.get("headline", ""), max(20, width() - 34)),
            )
        )
    return 0


def cmd_knobs(args, command, style):
    print(rule("what jitter can shake", style))
    for knob in knobmod.ALL:
        tag = style.grey("  (python only)") if knob.python_only else ""
        print("  " + style.bold(knob.name) + tag)
        print("    " + knob.summary)
        if args.verbose:
            for line in _wrap(knob.detail, width() - 6):
                print(style.grey("    " + line))
        reason = knob.available()
        if reason:
            print("    " + style.yellow("unavailable here: " + reason))
        print()
    return 0


def cmd_report(args, command, style):
    root = os.path.abspath(args.root)
    record = store.load(root, args.study)
    target = args.output or os.path.join(root, ".jitter", record["name"] + ".html")
    reportmod.write(record, target)
    print("  report → {0}".format(target))
    if args.open:
        import webbrowser

        webbrowser.open("file://" + os.path.abspath(target))
    return 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="jitter",
        description="Find out why a test is flaky, not just that it is.",
        epilog="Put the command after --, e.g.  jitter run -- pytest -q tests/",
    )
    parser.add_argument("--version", action="version", version="jitter " + __version__)
    parser.add_argument("--root", default=".", help="where .jitter lives")
    parser.add_argument("--no-color", action="store_true")
    subs = parser.add_subparsers(dest="command")

    def shared(sub):
        sub.add_argument("--knob", action="append", default=[], metavar="NAME")
        sub.add_argument("--exclude", action="append", default=[], metavar="NAME")
        sub.add_argument("--timeout", type=float, default=300.0, metavar="SECONDS")
        sub.add_argument("--expect-code", type=int, default=0, metavar="N")
        sub.add_argument(
            "--fail-on",
            metavar="REGEX",
            help="also count a run as failed when its output matches",
        )

    run = subs.add_parser("run", help="find out what makes it flaky")
    shared(run)
    run.add_argument("-n", "--trials", type=int, default=DEFAULT_TRIALS)
    run.add_argument("--screen", type=int, default=DEFAULT_SCREEN)
    run.add_argument("-j", "--jobs", type=int, default=0)
    run.add_argument("--seed", type=int, default=None)
    run.add_argument("--name", default="run")
    run.add_argument("--alpha", type=float, default=0.05)
    run.add_argument("--verify", type=int, default=DEFAULT_VERIFY)
    run.add_argument("--probe", type=int, default=2)
    run.add_argument("--no-minimise", action="store_true")
    run.add_argument("--strict", action="store_true", help="exit 1 if flaky")
    run.add_argument("-q", "--quiet", action="store_true")
    run.add_argument("--report", metavar="FILE")

    freeze = subs.add_parser("freeze", help="run it with every knob pinned")
    shared(freeze)
    freeze.add_argument("-n", "--trials", type=int, default=5)

    replay = subs.add_parser("replay", help="run the stored failing configuration")
    replay.add_argument("study", nargs="?")
    replay.add_argument("-n", "--trials", type=int, default=5)
    replay.add_argument("--show-output", action="store_true")

    repro = subs.add_parser("repro", help="print a standalone repro script")
    repro.add_argument("study", nargs="?")
    repro.add_argument("-o", "--output", metavar="FILE")

    show = subs.add_parser("show", help="reprint a study")
    show.add_argument("study", nargs="?")
    show.add_argument("--json", action="store_true")

    subs.add_parser("list", help="every study recorded here")

    knobs = subs.add_parser("knobs", help="what jitter can shake")
    knobs.add_argument("-v", "--verbose", action="store_true")

    html = subs.add_parser("report", help="write a self-contained HTML report")
    html.add_argument("study", nargs="?")
    html.add_argument("-o", "--output", metavar="FILE")
    html.add_argument("--open", action="store_true")

    return parser


HANDLERS = {
    "run": cmd_run,
    "freeze": cmd_freeze,
    "replay": cmd_replay,
    "repro": cmd_repro,
    "show": cmd_show,
    "list": cmd_list,
    "knobs": cmd_knobs,
    "report": cmd_report,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        cut = argv.index("--")
        flags, command = argv[:cut], argv[cut + 1 :]
    else:
        flags, command = argv, []

    parser = build_parser()
    args = parser.parse_args(flags)
    if not args.command:
        parser.print_help()
        return 0

    style = Style(colour_enabled(args.no_color))
    if getattr(args, "jobs", None) == 0:
        args.jobs = max(1, min(4, (os.cpu_count() or 2) // 2))
    return HANDLERS[args.command](args, command, style)
