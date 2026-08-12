"""Where a study lives on disk.

One JSON file per study, under ``.jitter/runs/``. Plain enough to read, diff
and commit; complete enough that ``jitter show``, ``jitter report`` and
``jitter replay`` never need to re-run anything.

Called by: jitter/cli.py, jitter/report.py.
"""

from __future__ import annotations

import json
import os
import re

VERSION = 1


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return cleaned or "run"


def runs_dir(root: str) -> str:
    return os.path.join(root, ".jitter", "runs")


def run_path(root: str, name: str) -> str:
    return os.path.join(runs_dir(root), safe_name(name) + ".json")


def list_runs(root: str):
    """Every stored study, newest first."""
    directory = runs_dir(root)
    found = []
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    for entry in sorted(names):
        if not entry.endswith(".json"):
            continue
        path = os.path.join(directory, entry)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        data["_path"] = path
        found.append(data)
    found.sort(key=lambda d: d.get("started", ""), reverse=True)
    return found


def load(root: str, name: str = None):
    """Load a study by name, or the most recent one."""
    if name:
        path = run_path(root, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError:
            raise SystemExit(
                "jitter: no study named {0!r}. Try: jitter list".format(name)
            )
        except json.JSONDecodeError as exc:
            raise SystemExit("jitter: {0} is not readable JSON: {1}".format(path, exc))
        data["_path"] = path
        return data
    everything = list_runs(root)
    if not everything:
        raise SystemExit("jitter: nothing recorded yet. Try: jitter run -- <command>")
    return everything[0]


def save(root: str, data: dict) -> str:
    path = run_path(root, data["name"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {k: v for k, v in data.items() if not k.startswith("_")}
    payload["version"] = VERSION
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=False)
        handle.write("\n")
    return path
