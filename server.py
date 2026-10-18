"""The deployed ASGI app; Vercel loads `app` from here (pyproject.toml, [tool.vercel]).

The system is served at /system and the tester at /tester, so each has its own base URL.
GET / is a landing page with buttons for the system's health check, the system and the tester.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api import system_app
from tester import tester_app

LINKS = [("Health", "/system/health"), ("System", "/system/"), ("Tester", "/tester/")]
BUTTON = ('<a href="{href}" style="padding:12px 24px;border:1px solid currentColor;border-radius:8px;'
          'color:inherit;text-decoration:none;font-size:1.25rem">{label}</a>')
PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Team 7 Tracer</title></head>
<body style="margin:0;min-height:100vh;display:grid;place-content:center;justify-items:center;gap:24px;font-family:sans-serif">
<h1 style="margin:0;font-size:1.5rem">Team 7 Tracer</h1>
<nav style="display:flex;flex-wrap:wrap;justify-content:center;gap:12px">{buttons}</nav>
</body>
</html>
"""

app = FastAPI(title="Tracer", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
def landing() -> HTMLResponse:
    # Not part of either spec. It never touches the database, so it also works on previews.
    buttons = "".join(BUTTON.format(href=href, label=label) for label, href in LINKS)
    return HTMLResponse(PAGE.format(buttons=buttons))


app.mount("/system", system_app)
app.mount("/tester", tester_app)
