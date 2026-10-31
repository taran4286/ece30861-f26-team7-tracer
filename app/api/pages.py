"""The system's status page at its base URL: health, the shared spec, and the spec operations
this system answers, built or refused by design. Not part of the spec; no tester calls it."""

from functools import cache
from html import escape
from pathlib import Path
from typing import Any

import yaml

TITLE = "Team 7 — Tracer system"
SPEC = Path(__file__).resolve().parents[2] / "system-openapi.yaml"
SPEC_LINK = "https://github.com/taran4286/ece30861-f26-team7-tracer/blob/main/system-openapi.yaml"
METHODS = ("get", "post", "put", "patch", "delete")

BUILT, REFUSED, NOT_BUILT = "Built", "405 by design", "501 not built"

# How this system answers each operation in system-openapi.yaml. tests/test_server.py checks
# that it lists exactly the spec's operations, so a new spec version can't slip past it.
STATUS: dict[tuple[str, str], str] = {
    ("POST", "/test/setup"): BUILT,
    ("GET", "/test/report/{run_id}"): BUILT,
    ("GET", "/test/report"): BUILT,
    ("PATCH", "/test/sessions/{session_id}/settings"): BUILT,
    ("POST", "/incidents"): BUILT,
    ("GET", "/incidents/{incident_id}"): BUILT,
    ("PUT", "/incidents/{incident_id}"): REFUSED,
    ("DELETE", "/incidents/{incident_id}"): REFUSED,
    ("POST", "/incidents/{incident_id}/evidence/import"): BUILT,
    ("PATCH", "/incidents/{incident_id}/evidence/{evidence_id}"): NOT_BUILT,
    ("DELETE", "/incidents/{incident_id}/evidence/{evidence_id}"): REFUSED,
    ("POST", "/incidents/{incident_id}/determination"): BUILT,
    ("POST", "/incidents/{incident_id}/transitions"): BUILT,
    ("POST", "/incidents/{incident_id}/filings"): BUILT,
    ("POST", "/incidents/{incident_id}/drafts"): NOT_BUILT,
    ("POST", "/drafts/{draft_id}/accept"): NOT_BUILT,
    ("GET", "/incidents/{incident_id}/audit"): BUILT,
    ("PUT", "/incidents/{incident_id}/audit"): REFUSED,
    ("PATCH", "/incidents/{incident_id}/audit"): REFUSED,
    ("DELETE", "/incidents/{incident_id}/audit"): REFUSED,
    ("POST", "/groups"): NOT_BUILT,
}
STATUS_CLASS = {BUILT: "ok", REFUSED: "muted", NOT_BUILT: "warn"}

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
code{font-size:13px}a{color:inherit}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
"""

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{style}</style></head>
<body><main>
<h1>{title}</h1>
<h2>Status</h2>
<p>Database: <strong class="{health_class}">{health}</strong></p>
<p>Implements <a href="{spec_link}">{spec_title} {spec_version}</a>. Every project request needs
<code>X-User-Id</code>, <code>X-User-Role</code> and <code>X-Test-Run</code>, the session ID from
<code>POST /test/setup</code>; test control takes no identity headers.</p>
<h2>Operations</h2>
<div class="wrap"><table>
<tr><th>Method</th><th>Path</th><th>Status</th><th>Summary</th></tr>
{rows}
</table></div>
</main></body>
</html>
"""


@cache
def _spec() -> dict[str, Any]:
    return yaml.safe_load(SPEC.read_text(encoding="utf-8"))


def operations() -> list[tuple[str, str, str]]:
    """Every operation in the spec as (method, path, summary), in the spec's order."""
    return [
        (method.upper(), path, item[method].get("summary", ""))
        for path, item in _spec()["paths"].items()
        for method in item
        if method in METHODS
    ]


def shown_operations() -> list[tuple[str, str, str]]:
    """The operations the page lists: built or refused by design, leaving out the 501s."""
    return [op for op in operations() if STATUS[(op[0], op[1])] != NOT_BUILT]


def render(database_ok: bool) -> str:
    """The page. Reads the spec when first needed, not at import, so a missing file breaks only
    this page."""
    info = _spec()["info"]
    rows = "\n".join(
        f'<tr><td><code>{method}</code></td><td><code>{escape(path)}</code></td>'
        f'<td class="{STATUS_CLASS[STATUS[(method, path)]]}">{STATUS[(method, path)]}</td>'
        f"<td>{escape(summary)}</td></tr>"
        for method, path, summary in shown_operations()
    )
    return PAGE.format(
        style=STYLE,
        title=TITLE,
        health="reachable" if database_ok else "unavailable",
        health_class="ok" if database_ok else "bad",
        spec_link=SPEC_LINK,
        spec_title=escape(info["title"]),
        spec_version=escape(info["version"]),
        rows=rows,
    )
