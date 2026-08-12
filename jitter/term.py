"""Terminal colour and small formatting helpers.

Called by: jitter/cli.py, jitter/report.py.
"""

from __future__ import annotations

import os
import shutil
import sys


def colour_enabled(no_colour: bool = False, stream=None) -> bool:
    """True when ANSI escapes are worth emitting."""
    if no_colour or os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stdout
    try:
        return stream.isatty()
    except Exception:
        return False


class Style:
    """A tiny colour palette that degrades to plain text."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return "\033[{0}m{1}\033[0m".format(code, text)

    def bold(self, text):
        return self._wrap("1", text)

    def dim(self, text):
        return self._wrap("2", text)

    def red(self, text):
        return self._wrap("31", text)

    def green(self, text):
        return self._wrap("32", text)

    def yellow(self, text):
        return self._wrap("33", text)

    def blue(self, text):
        return self._wrap("34", text)

    def magenta(self, text):
        return self._wrap("35", text)

    def cyan(self, text):
        return self._wrap("36", text)

    def grey(self, text):
        return self._wrap("90", text)


def width(default: int = 80) -> int:
    try:
        return max(60, min(shutil.get_terminal_size((default, 24)).columns, 110))
    except Exception:
        return default


def rule(title: str, style: Style, cols: int = None) -> str:
    """`── title ─────────────────────`, the house header."""
    cols = cols or width()
    text = "── {0} ".format(title)
    return style.grey(text + "─" * max(0, cols - len(text)))


def plural(count: int, singular: str, plural_form: str = None) -> str:
    word = singular if count == 1 else (plural_form or singular + "s")
    return "{0:,} {1}".format(count, word)


def human_time(seconds: float) -> str:
    if seconds < 1:
        return "{0:.0f} ms".format(seconds * 1000)
    if seconds < 60:
        return "{0:.1f} s".format(seconds)
    minutes, rest = divmod(seconds, 60)
    return "{0:.0f} m {1:.0f} s".format(minutes, rest)


_BLOCKS = " ▏▎▍▌▋▊▉█"


def bar(fraction: float, cells: int = 12) -> str:
    """A sub-character-resolution bar, so 1-in-12 does not render as nothing."""
    fraction = max(0.0, min(1.0, fraction))
    full = fraction * cells
    whole = int(full)
    out = "█" * whole
    if whole < cells:
        out += _BLOCKS[int((full - whole) * 8)]
    return out.ljust(cells)


def clip(text: str, limit: int) -> str:
    """Like :func:`truncate`, but keeps leading whitespace.

    Captured output is usually a traceback, and a traceback with its
    indentation stripped is much harder to read than one that is too wide.
    """
    text = text.replace("\t", "    ").rstrip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").replace("\t", " ").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"
