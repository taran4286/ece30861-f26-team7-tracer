"""The tester's page at its base URL: how to run it, and a stored report as tables.

Not part of the spec. It only reads reports; it never starts or repeats a run. Report
messages can quote another team's system, so everything shown is HTML-escaped.
"""

from html import escape
from typing import Any

from tester.client import CALL_TIMEOUT, SETUP_TIMEOUT_SECONDS
from tester.runner import CALL_CUTOFF_SECONDS

TITLE = "Team 7 — Tracer tester"
OUTCOMES = ("pass", "fail", "error", "skip")
OUTCOME_CLASS = {"pass": "ok", "fail": "bad", "error": "bad", "skip": "muted"}

STYLE = """
:root{color-scheme:light dark;--fg:#1d1d1f;--muted:#5f6368;--line:#d0d4da;--bg:#fff;
--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e}
@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--muted:#9da7b3;--line:#30363d;
--bg:#0d1117;--ok:#3fb950;--warn:#d29922;--bad:#f85149}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px}
h1{font-size:1.5rem;margin:0}h2{font-size:1.15rem;margin:28px 0 8px}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
pre{overflow-x:auto;padding:12px;border:1px solid var(--line);border-radius:8px;font-size:13px}
input{font:inherit;padding:6px 8px;width:min(100%,28rem)}button{font:inherit;padding:6px 12px}
code{font-size:13px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
"""

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{style}</style></head>
<body><main>
<h1>{title}</h1>
<h2>Running it</h2>
<pre>POST /tester/test/setup   {{"target_url": "https://&lt;host&gt;/system"}}   → {{"session_id": "…"}}
POST /tester/test/run     {{"session_id": "…"}}   → the report; one run per session
GET  /tester/test/report?session_id=…   → the stored report</pre>
<p>Setup waits up to {setup:g} s for the target. A run sends no new call after {cutoff:g} s, inside
the 130 s run limit, and each project call waits up to {call:g} s with no retry.</p>
<h2>Report</h2>
<form method="get" action=""><input name="session_id" value="{session_id}"
placeholder="Session ID, e.g. t7-…" aria-label="Session ID"> <button>View report</button></form>
{report}
</main></body>
</html>
"""


def render(session_id: str | None, report: dict[str, Any] | None, problem: str | None) -> str:
    """The page, with the report for `session_id` if one was found, or why not."""
    if report is not None:
        body = _report(report)
    elif problem is not None:
        body = f'<p class="bad">{escape(problem)}</p>'
    else:
        body = '<p class="muted">Enter a session ID to see its stored report.</p>'
    return PAGE.format(
        style=STYLE,
        title=TITLE,
        setup=SETUP_TIMEOUT_SECONDS,
        cutoff=CALL_CUTOFF_SECONDS,
        call=CALL_TIMEOUT,
        session_id=escape(session_id or ""),
        report=body,
    )


def _criterion(test_id: str) -> str:
    # Test IDs start with their criterion: AC-06-DM-to-Closed is AC-06.
    return "-".join(test_id.split("-")[:2])


def _report(report: dict[str, Any]) -> str:
    results = report["results"]
    totals = {outcome: sum(r["outcome"] == outcome for r in results) for outcome in OUTCOMES}
    # A zero count is muted, so a clean run doesn't show "0 fail" in red.
    summary = ", ".join(
        f'<span class="{OUTCOME_CLASS[o] if totals[o] else "muted"}">{totals[o]} {o}</span>'
        for o in OUTCOMES
    )
    criteria: dict[str, dict[str, int]] = {}
    for result in results:
        counts = criteria.setdefault(_criterion(result["test_id"]), dict.fromkeys(OUTCOMES, 0))
        counts[result["outcome"]] += 1
    criterion_rows = "\n".join(
        f"<tr><td>{escape(name)}</td>" + "".join(f"<td>{counts[o] or ''}</td>" for o in OUTCOMES)
        + "</tr>"
        for name, counts in criteria.items()
    )
    coverage_rows = "\n".join(
        f"<tr><td>{escape(kind)}</td><td>{_measure(measure)}</td>"
        f"<td>{escape(str(measure['basis']))}</td></tr>"
        for kind, measure in report["coverage"].items()
    )
    result_rows = "\n".join(
        f"<tr><td><code>{escape(r['test_id'])}</code></td>"
        f'<td class="{OUTCOME_CLASS.get(r["outcome"], "")}">{escape(r["outcome"])}'
        f"{escape(' (' + r['source'] + ')') if r.get('source') else ''}</td>"
        f"<td>{escape(r['message'])}</td></tr>"
        for r in results
    )
    return f"""<p>Session <code>{escape(report['session_id'])}</code> against
<code>{escape(report['target_url'])}</code>: {len(results)} tests, {summary}.</p>
<h2>By criterion</h2>
<div class="wrap"><table>
<tr><th>Criterion</th>{''.join(f'<th>{o}</th>' for o in OUTCOMES)}</tr>
{criterion_rows}
</table></div>
<h2>Coverage</h2>
<div class="wrap"><table>
<tr><th>Measure</th><th>Covered</th><th>Basis</th></tr>
{coverage_rows}
</table></div>
<h2>Every test</h2>
<div class="wrap"><table>
<tr><th>Test</th><th>Outcome</th><th>Message</th></tr>
{result_rows}
</table></div>"""


def _measure(measure: dict[str, Any]) -> str:
    if measure["covered"] is None:
        return "not measured"
    return escape(f"{measure['covered']} of {measure['total']}")
