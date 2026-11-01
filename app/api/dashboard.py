"""The incident dashboard for one test session: each incident's state, due date, notices and
filings, with the session's test date. Not part of the spec; no tester calls it."""

import logging
from html import escape
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.api.pages import STYLE
from app.errors import TracerError
from app.models import Flag
from app.services import control

logger = logging.getLogger(__name__)

router = APIRouter()

TITLE = "Team 7 — Tracer dashboard"

# Each notice's text and color, most urgent first; incidents sort by the same order.
NOTICES: dict[str, tuple[str, str]] = {
    Flag.OVERDUE.value: ("Overdue", "bad"),
    Flag.WARNING.value: ("Warning: due within 1 business day", "warn"),
    Flag.AT_RISK.value: ("At risk: assessment past the threshold", "warn"),
}

# On a phone each incident stacks into a block, so its notices don't scroll out of view.
DASHBOARD_STYLE = """
.incidents code{overflow-wrap:anywhere}
@media (max-width:640px){
.incidents thead{display:none}
.incidents,.incidents tbody,.incidents tr,.incidents td{display:block}
.incidents tr{padding:10px 0;border-bottom:1px solid var(--line)}
.incidents td{border:0;padding:2px 0}
.incidents td[data-label]::before{content:attr(data-label) ": ";color:var(--muted)}
}
"""

PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{style}</style></head>
<body><main>
<h1>{title}</h1>
<form method="get" action="dashboard">
<p><label>Test session <input name="session_id" value="{session_id}" required></label>
<button>Show</button></p>
</form>
{content}
</main></body>
</html>
"""

SESSION = """<h2>Session</h2>
<form id="clock" data-settings="test/sessions/{settings_id}/settings" data-time="{time}">
<p><label>Test date <input type="date" name="date" value="{date}" required></label>
<button>Set date</button> <span id="clock-error" class="bad" role="alert"></span></p>
</form>
<p>Registrant type: {registrant_type}. At risk after {threshold} days.</p>
<h2>Incidents</h2>
{incidents}
<script>{script}</script>
"""

# Moves the session's clock through the settings route, keeping its time of day, then reloads.
SCRIPT = """
const form = document.getElementById("clock");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("clock-error");
  error.textContent = "";
  try {
    const response = await fetch(form.dataset.settings, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({test_now: form.elements.date.value + form.dataset.time}),
    });
    if (response.ok) {
      location.reload();
      return;
    }
    error.textContent = (await response.json()).message;
  } catch {
    error.textContent = "The test date could not be changed";
  }
});
"""


@router.get("/dashboard")
def dashboard(session_id: str = "") -> HTMLResponse:
    """The named session's dashboard, or only the session form when none is named."""
    if not session_id.strip():
        return _page("", "")
    try:
        report = control.snapshot(session_id)
    except TracerError as error:
        return _page(session_id, _message(error.message), status=error.status)
    except (ValidationError, ImportError, SQLAlchemyError):
        # The causes /health reports, so the page works on previews, which have no database.
        logger.warning("database unavailable", exc_info=True)
        return _page(session_id, _message("The database is unavailable"), status=503)
    return _page(session_id, _session(report))


def _page(session_id: str, content: str, status: int = 200) -> HTMLResponse:
    style = STYLE + DASHBOARD_STYLE
    html = PAGE.format(title=TITLE, style=style, session_id=escape(session_id), content=content)
    return HTMLResponse(html, status_code=status)


def _message(text: str) -> str:
    return f'<p class="bad">{escape(text)}</p>'


def _session(report: dict[str, Any]) -> str:
    run = report["run"]
    return SESSION.format(
        settings_id=escape(quote(run["run_id"], safe="")),
        date=run["test_now"][:10],
        time=escape(run["test_now"][10:]),
        registrant_type=escape(run["registrant_type"]),
        threshold=run["at_risk_threshold_days"],
        incidents=_incidents(report["incidents"]),
        script=SCRIPT,
    )


def _incidents(incidents: list[dict[str, Any]]) -> str:
    if not incidents:
        return "<p>No incidents in this session.</p>"
    rows = "\n".join(_row(incident) for incident in sorted(incidents, key=_urgency))
    return f"""<div class="wrap"><table class="incidents">
<thead><tr><th>Incident</th><th>State</th><th>Required form</th><th>Due date</th><th>Notices</th>
<th>Filings</th><th>Version</th></tr></thead>
<tbody>
{rows}
</tbody>
</table></div>"""


def _urgency(incident: dict[str, Any]) -> tuple[int, str, str]:
    """The most urgent notice first, then the earliest due date; no due date sorts last."""
    ranks = [rank for rank, flag in enumerate(NOTICES) if flag in incident["flags"]]
    return min(ranks, default=len(NOTICES)), incident["due_date"] or "9999", incident["title"]


def _row(incident: dict[str, Any]) -> str:
    form = incident["required_form"]
    due_date = incident["due_date"] or ("No fixed deadline" if form else "—")
    notices = "<br>".join(
        f'<strong class="{color}">{text}</strong>'
        for flag, (text, color) in NOTICES.items() if flag in incident["flags"]
    )
    filings = "<br>".join(
        escape(f"{filing['form_type']} on {filing['filing_date']} ({filing['accession_number']})")
        for filing in incident["filings"]
    )
    return (
        f"<tr><td>{escape(incident['title'])}<br><code>{escape(incident['incident_id'])}</code></td>"
        f'<td data-label="State">{escape(incident["state"])}</td>'
        f'<td data-label="Required form">{escape(form or "—")}</td>'
        f'<td data-label="Due date">{escape(due_date)}</td>'
        f'<td data-label="Notices">{notices or "None"}</td>'
        f'<td data-label="Filings">{filings or "None"}</td>'
        f'<td data-label="Version">{incident["version"]}</td></tr>'
    )
