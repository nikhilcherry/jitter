"""Run the command once, under one exact configuration.

Everything that makes a trial different from a plain shell invocation happens
here and nowhere else: the shim goes on PYTHONPATH, the knob settings go into
the environment, an awkward temporary directory gets built, and competing
processes get spawned and -- always, even when the trial explodes -- killed.

Called by: jitter/plan.py, jitter/minimize.py, jitter/cli.py.
"""

from __future__ import annotations

import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

#: Environment variables jitter owns. Whatever the user happens to have in
#: their shell for these is discarded, or a stray TZ would quietly become part
#: of the "frozen" baseline and every attribution would be measured against it.
MANAGED = (
    "PYTHONHASHSEED",
    "TZ",
    "LC_ALL",
    "LANG",
    "LANGUAGE",
    "COLUMNS",
    "LINES",
    "TMPDIR",
    "TEMP",
    "TMP",
)

_BUSY = "while True:\n    pass\n"


class Trial:
    """One execution and its verdict."""

    __slots__ = (
        "cell",
        "shaken",
        "seed",
        "ok",
        "exit_code",
        "wall_s",
        "timed_out",
        "tail",
    )

    def __init__(self, cell, shaken, seed, ok, exit_code, wall_s, timed_out, tail):
        self.cell = cell
        self.shaken = tuple(shaken)
        self.seed = seed
        self.ok = ok
        self.exit_code = exit_code
        self.wall_s = wall_s
        self.timed_out = timed_out
        self.tail = tail

    def to_dict(self):
        return {
            "seed": self.seed,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "wall_s": round(self.wall_s, 4),
            "timed_out": self.timed_out,
            "tail": self.tail,
        }


def base_environment():
    """The caller's environment with jitter's own variables stripped out."""
    env = {k: v for k, v in os.environ.items() if k not in MANAGED}
    for key in list(env):
        if key.startswith("JITTER_"):
            del env[key]
    return env


def _tmpdir_for(shape: str, root: str) -> str:
    """Build a temporary directory of a deliberately awkward shape."""
    if shape == "long":
        # Long enough to break a fixed-size buffer, short enough to survive
        # PATH_MAX everywhere jitter runs.
        leaf = os.path.join(root, *(["deeply-nested-" + "d" * 24] * 4))
    elif shape == "spaces":
        leaf = os.path.join(root, "a temp dir with spaces")
    elif shape == "unicode":
        leaf = os.path.join(root, "tmp-café-日本語-Ω")
    elif shape == "dots":
        leaf = os.path.join(root, "tmp.v1.2.3", "release.candidate")
    else:
        leaf = os.path.join(root, "tmp")
    os.makedirs(leaf, exist_ok=True)
    return leaf


class Runner:
    """Materialises the shim once, then runs as many trials as you ask for."""

    def __init__(
        self,
        command,
        *,
        scratch,
        cwd=None,
        timeout=300.0,
        expect_code=0,
        fail_pattern=None,
        keep_tail=4000,
    ):
        self.command = list(command)
        self.cwd = cwd or os.getcwd()
        self.scratch = scratch
        self.timeout = timeout
        self.expect_code = expect_code
        self.fail_pattern = re.compile(fail_pattern) if fail_pattern else None
        self.keep_tail = keep_tail
        self.base = base_environment()
        self.inject = os.path.join(scratch, "inject")
        self._counter = 0
        self._lock = threading.Lock()
        self.executed = 0
        self._prepare()

    # -- setup ------------------------------------------------------------

    def _prepare(self):
        os.makedirs(self.inject, exist_ok=True)
        for source, target in (
            ("_shim.py", "_jitter_shim.py"),
            ("_sitecustomize.py", "sitecustomize.py"),
            ("_pytest_plugin.py", "_jitter_pytest.py"),
        ):
            shutil.copyfile(
                os.path.join(_HERE, source), os.path.join(self.inject, target)
            )

    def close(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    # -- environment ------------------------------------------------------

    def child_env(self, settings, trial_dir):
        """Turn a knob configuration into the environment the child will see."""
        env = dict(self.base)
        env.update(settings)

        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = self.inject + (os.pathsep + existing if existing else "")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

        plugins = [p for p in env.get("PYTEST_PLUGINS", "").split(",") if p]
        if "_jitter_pytest" not in plugins:
            plugins.append("_jitter_pytest")
        env["PYTEST_PLUGINS"] = ",".join(plugins)

        # Trials run concurrently and would otherwise fight over one
        # .pytest_cache. That fight looks exactly like flakiness, so give each
        # trial its own cache rather than letting jitter measure its own noise.
        addopts = env.get("PYTEST_ADDOPTS", "")
        cache = shlex.quote(os.path.join(trial_dir, "pytest-cache"))
        env["PYTEST_ADDOPTS"] = (addopts + " " if addopts else "") + "-o cache_dir=" + cache

        temp = _tmpdir_for(env.get("JITTER_TMPDIR", "plain"), trial_dir)
        env["TMPDIR"] = env["TEMP"] = env["TMP"] = temp

        try:
            env_seed = int(env.get("JITTER_ENV", "0") or 0)
        except ValueError:
            env_seed = 0
        if env_seed:
            rng = random.Random(env_seed)
            for index in range(rng.randint(4, 24)):
                env["JITTER_PAD_{0:02d}".format(index)] = "x" * rng.randint(1, 512)
            items = list(env.items())
            rng.shuffle(items)
            env = dict(items)
        else:
            env = dict(sorted(env.items()))
        return env

    # -- execution --------------------------------------------------------

    def _spawn_load(self, count, env):
        workers = []
        for _ in range(count):
            try:
                workers.append(
                    subprocess.Popen(
                        [sys.executable, "-c", _BUSY],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=env,
                    )
                )
            except OSError:
                break
        return workers

    @staticmethod
    def _stop_load(workers):
        for worker in workers:
            try:
                worker.kill()
            except OSError:
                pass
        for worker in workers:
            try:
                worker.wait(timeout=5)
            except Exception:
                pass

    def run(self, cell, shaken, seed, settings):
        """Execute one trial and judge it."""
        with self._lock:
            self._counter += 1
            index = self._counter
        trial_dir = os.path.join(self.scratch, "t{0:05d}".format(index))
        os.makedirs(trial_dir, exist_ok=True)

        env = self.child_env(settings, trial_dir)
        try:
            load = int(env.get("JITTER_LOAD", "0") or 0)
        except ValueError:
            load = 0
        workers = self._spawn_load(load, env) if load else []

        started = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.run(
                self.command,
                cwd=self.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout,
            )
            code = proc.returncode
            output = proc.stdout or b""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            code = -1
            output = exc.output or b""
        except FileNotFoundError as exc:
            self._stop_load(workers)
            shutil.rmtree(trial_dir, ignore_errors=True)
            raise SystemExit(
                "jitter: cannot run {0!r}: {1}".format(self.command[0], exc.strerror)
            )
        finally:
            self._stop_load(workers)
        wall = time.perf_counter() - started

        text = output.decode("utf-8", "replace")
        ok = (not timed_out) and code == self.expect_code
        if ok and self.fail_pattern is not None and self.fail_pattern.search(text):
            ok = False

        tail = "" if ok else text[-self.keep_tail :]
        with self._lock:
            self.executed += 1
        shutil.rmtree(trial_dir, ignore_errors=True)
        return Trial(cell, shaken, seed, ok, code, wall, timed_out, tail)


def repro_script(command, env, cwd, shim_dir="./.jitter-repro"):
    """A standalone shell script that reproduces one exact configuration.

    Emitted rather than a bare ``VAR=x cmd`` line because two of the knobs need
    more than an environment variable: the shim has to exist on disk, and the
    competing processes have to be spawned and reaped around the command.
    """
    lines = [
        "#!/usr/bin/env bash",
        "# Written by jitter. Reproduces one failing configuration exactly.",
        "set -u",
        "",
        'SHIM="{0}"'.format(shim_dir),
        'mkdir -p "$SHIM"',
        "python3 - \"$SHIM\" <<'PY'",
        "import os, shutil, sys",
        "import jitter.runner as r",
        "d, h = sys.argv[1], os.path.dirname(r.__file__)",
        "for src, dst in (('_shim.py', '_jitter_shim.py'),",
        "                 ('_sitecustomize.py', 'sitecustomize.py'),",
        "                 ('_pytest_plugin.py', '_jitter_pytest.py')):",
        "    shutil.copyfile(os.path.join(h, src), os.path.join(d, dst))",
        "PY",
        "",
        "cd {0}".format(shlex.quote(cwd)),
        "",
    ]
    try:
        load = int(env.get("JITTER_LOAD", "0") or 0)
    except ValueError:
        load = 0
    if load:
        lines += [
            "# {0} competing process(es), reaped when this script exits".format(load),
            "PIDS=()",
            "for _ in $(seq {0}); do".format(load),
            "  python3 -c 'while True: pass' & PIDS+=($!)",
            "done",
            "trap 'kill \"${PIDS[@]}\" 2>/dev/null' EXIT",
            "",
        ]
    lines.append("env \\")
    for key in sorted(env):
        if key in ("PYTHONPATH", "PYTEST_PLUGINS", "TMPDIR", "TEMP", "TMP"):
            continue
        lines.append("  {0}={1} \\".format(key, shlex.quote(env[key])))
    lines.append('  PYTHONPATH="$SHIM${PYTHONPATH:+:$PYTHONPATH}" \\')
    lines.append('  PYTEST_PLUGINS="_jitter_pytest" \\')
    lines.append("  " + " ".join(shlex.quote(part) for part in command))
    return "\n".join(lines) + "\n"
