"""One self-contained HTML page per study, and the small shared formatters.

No CDN, no fonts to fetch, no JavaScript. Open the file on a plane, mail it to
a colleague, attach it to the bug. The terminal report and this page are
rendered from the same saved record, so they cannot disagree.

Called by: jitter/cli.py.
"""

from __future__ import annotations

import html
import os

from .attribute import ROLES
from .term import bar


# --------------------------------------------------------------------------
# Formatters shared with the terminal report
# --------------------------------------------------------------------------


def text_bar(fraction_value, cells=10):
    return bar(fraction_value, cells)


def fraction(failures, total, cells=6):
    if not total:
        return "—"
    return "{0} {1}/{2}".format(bar(failures / total, cells), failures, total)


def pvalue(value):
    if value < 0.001:
        return "<0.001"
    if value > 0.999:
        return "1.0"
    return "{0:.3f}".format(value)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

_CSS = """
:root {
  --ink: #d7dce3; --dim: #7f8896; --bg: #0f1216; --panel: #161b22;
  --line: #262d38; --bad: #f0685f; --good: #4fbe73; --warn: #d8a33c;
  --accent: #6ba6ff;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
main { max-width: 900px; margin: 0 auto; padding: 40px 20px 80px; }
h1 { font-size: 18px; font-weight: 600; margin: 0 0 4px; letter-spacing: .2px; }
h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 1.4px;
     color: var(--dim); font-weight: 600; margin: 40px 0 14px;
     border-bottom: 1px solid var(--line); padding-bottom: 8px; }
.cmd { color: var(--accent); word-break: break-all; }
.verdict { font-size: 22px; font-weight: 600; margin: 18px 0 6px; }
.verdict.guilty { color: var(--bad); }
.verdict.broken, .verdict.unknown { color: var(--warn); }
.verdict.clean { color: var(--good); }
.sub { color: var(--dim); }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-weight: 600; color: var(--dim); font-size: 11px;
     letter-spacing: 1px; text-transform: uppercase; padding: 0 10px 8px 0; }
td { padding: 8px 10px 8px 0; border-top: 1px solid var(--line);
     vertical-align: middle; }
td.knob { font-weight: 600; white-space: nowrap; }
.meter { display: inline-block; width: 92px; height: 8px; background: #222934;
         border: 1px solid var(--line); border-radius: 2px; overflow: hidden;
         vertical-align: middle; margin-right: 8px; }
.meter > i { display: block; height: 100%; background: var(--bad); }
.meter.good > i { background: var(--good); }
.role { font-size: 11px; letter-spacing: .6px; text-transform: uppercase;
        padding: 2px 8px; border-radius: 10px; white-space: nowrap; }
.role.confirmed, .role.prime, .role.cause { background: #3b1d1d; color: var(--bad); }
.role.required { background: #3a2f16; color: var(--warn); }
.role.suspect, .role.cleared { background: #1d232c; color: var(--dim); }
.strip { line-height: 0; margin: 6px 0 2px; }
.strip i { display: inline-block; width: 9px; height: 14px; margin-right: 2px;
           border-radius: 1px; background: var(--good); }
.strip i.f { background: var(--bad); }
pre { background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
      padding: 14px 16px; overflow-x: auto; font-size: 12.5px; margin: 12px 0; }
.note { border-left: 3px solid var(--warn); background: #221c0e;
        padding: 10px 14px; margin: 12px 0; color: #e8d6a8; }
.kv { display: grid; grid-template-columns: 130px 1fr; gap: 4px 16px;
      margin: 10px 0; }
.kv b { font-weight: 600; color: var(--dim); }
footer { margin-top: 50px; color: var(--dim); font-size: 12px;
         border-top: 1px solid var(--line); padding-top: 16px; }
a { color: var(--accent); }
"""


def _esc(text):
    return html.escape(str(text), quote=True)


def _meter(value, good=False):
    return '<span class="meter{0}"><i style="width:{1:.0f}%"></i></span>'.format(
        " good" if good else "", max(0.0, min(1.0, value)) * 100
    )


def _strip(trials, limit=60):
    marks = "".join(
        '<i class="f"></i>' if not trial["ok"] else "<i></i>" for trial in trials[:limit]
    )
    return '<div class="strip">{0}</div>'.format(marks)


def build(record):
    cells = {cell["label"]: cell for cell in record["cells"]}
    out = []
    add = out.append

    kind = record.get("headline_kind", "clean")
    add("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    add("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    add("<title>jitter — {0}</title>".format(_esc(" ".join(record["command"]))))
    add("<style>{0}</style></head><body><main>".format(_CSS))

    add("<h1>jitter</h1>")
    add("<div class='cmd'>{0}</div>".format(_esc(" ".join(record["command"]))))
    add(
        "<div class='verdict {0}'>{1}</div>".format(
            _esc(kind), _esc(record.get("headline", ""))
        )
    )
    add(
        "<div class='sub'>{0} runs · seed {1} · {2}</div>".format(
            record["executed"], record["seed"], _esc(record.get("started", ""))
        )
    )

    add("<h2>The two ends of the range</h2>")
    add("<table><tr><th>Condition</th><th>Failures</th><th>Every trial</th></tr>")
    for label, title, blurb in (
        ("freeze", "Frozen", "every knob pinned"),
        ("chaos", "Shaken", "every knob randomised"),
    ):
        cell = cells.get(label)
        if not cell:
            continue
        trials = cell["trials"]
        failures = sum(1 for trial in trials if not trial["ok"])
        rate = failures / len(trials) if trials else 0.0
        add(
            "<tr><td class='knob'>{0}<br><span class='sub' "
            "style='font-weight:400'>{1}</span></td>"
            "<td>{2}{3}/{4} &nbsp;<b>{5:.0%}</b></td><td>{6}</td></tr>".format(
                title,
                _esc(blurb),
                _meter(rate, good=not failures),
                failures,
                len(trials),
                rate,
                _strip(trials),
            )
        )
    add("</table>")

    verdicts = [v for v in (record.get("verdicts") or []) if v["role"] != "untested"]
    if verdicts:
        add("<h2>What matters</h2>")
        add(
            "<table><tr><th>Knob</th><th>Shaken alone</th>"
            "<th>All but this</th><th>p</th><th>Verdict</th></tr>"
        )
        for entry in verdicts:
            solo = entry["solo_fail"] / entry["solo_n"] if entry["solo_n"] else 0.0
            loo = entry["loo_fail"] / entry["loo_n"] if entry["loo_n"] else 0.0
            add(
                "<tr><td class='knob'>{0}</td>"
                "<td>{1}{2}/{3}</td><td>{4}{5}/{6}</td><td>{7}</td>"
                "<td><span class='role {8}'>{9}</span></td></tr>".format(
                    _esc(entry["knob"]),
                    _meter(solo, good=not entry["solo_fail"]),
                    entry["solo_fail"],
                    entry["solo_n"],
                    _meter(loo, good=not entry["loo_fail"]),
                    entry["loo_fail"],
                    entry["loo_n"],
                    pvalue(min(entry["solo_p"], entry["loo_p"])),
                    _esc(entry["role"]),
                    _esc("prime cause" if entry["role"] == "prime" else entry["role"]),
                )
            )
        add("</table>")
        shown = {entry["role"] for entry in verdicts}
        add("<div class='kv'>")
        for role in ("confirmed", "prime", "cause", "required", "suspect", "cleared"):
            if role in shown:
                add("<b>{0}</b><span>{1}</span>".format(_esc(role), _esc(ROLES[role])))
        add("</div>")

    minimal = record.get("minimal")
    if minimal:
        add("<h2>The repro</h2>")
        add(
            "<p>{0} of {1} knobs is enough. It failed <b>{2} of {3}</b> "
            "replays.</p>".format(
                len(minimal["shaken"]) or "None",
                len(record["knobs"]),
                minimal["failures"],
                minimal["trials"],
            )
        )
        add("<div class='kv'>")
        for name, description in minimal.get("describe", []):
            add("<b>{0}</b><span>{1}</span>".format(_esc(name), _esc(description)))
        add("</div>")
        add("<pre>jitter replay\njitter repro &gt; bug.sh</pre>")
        add("<h2>The exact environment</h2>")
        add(
            "<pre>{0}</pre>".format(
                _esc(
                    "\n".join(
                        "{0}={1}".format(key, value)
                        for key, value in sorted(minimal["env"].items())
                    )
                )
            )
        )
        tail = (minimal.get("tail") or "").strip()
        if tail:
            add("<h2>What it printed when it failed</h2>")
            add("<pre>{0}</pre>".format(_esc(tail[-6000:])))

    for note in record.get("notes") or []:
        add("<div class='note'>{0}</div>".format(_esc(note)))

    add(
        "<footer>Every cell shares its trial seeds, so comparisons are paired. "
        "Execution order within each phase is shuffled, so machine load spreads "
        "across cells as noise instead of bias. Significance is Fisher's exact "
        "test, one-sided, corrected across all knobs with Holm-Bonferroni at "
        "&alpha;&nbsp;=&nbsp;0.05. Written by "
        "<a href='https://github.com/nikhilcherry/jitter'>jitter</a>.</footer>"
    )
    add("</main></body></html>")
    return "\n".join(out)


def write(record, path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(build(record))
    return path
