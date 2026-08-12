"""A pytest plugin that reorders collected tests.

Loaded through ``PYTEST_PLUGINS``, which jitter sets on *every* trial --
pinned as well as shaken. Loading the plugin only when a knob is shaken would
mean the frozen baseline and the shaken runs differed by one imported module
as well as by the knob, and jitter would have no way to tell which mattered.

Copied to ``_jitter_pytest.py`` in the scratch directory by jitter/runner.py.
"""

import os
import random


def pytest_collection_modifyitems(items):
    try:
        seed = int(os.environ.get("JITTER_ORDER", "0") or 0)
    except ValueError:
        return
    if seed == 0:
        return
    random.Random(seed).shuffle(items)


def pytest_report_header(config):
    seed = os.environ.get("JITTER_ORDER", "0")
    if seed and seed != "0":
        return "jitter: test order shuffled with seed {0}".format(seed)
    return None
