"""The deployed ASGI app; Vercel loads `app` from here (pyproject.toml, [tool.vercel]).

The system is served at /system and the tester at /tester, so each has its own base URL.
GET / is a landing page with buttons for the system's health check, the system and the tester.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api import system_app
from tester import tester_app

TITLE = "Team 7 — Tracer"
LINKS = [("Health", "/system/health"), ("System", "/system/"), ("Tester", "/tester/")]
BUTTON = '<a href="{href}">{label}</a>'
# The same colors as the system and tester pages: dark when the browser asks for dark.
PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
:root{{color-scheme:light dark;--fg:#1d1d1f;--bg:#fff}}
@media (prefers-color-scheme:dark){{:root{{--fg:#e6edf3;--bg:#0d1117}}}}
body{{margin:0;min-height:100vh;box-sizing:border-box;padding:24px 16px;display:grid;place-content:center;justify-items:center;gap:20px;background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif}}
h1{{margin:0;font-size:1.5rem}}
nav{{display:flex;flex-wrap:wrap;justify-content:center;gap:12px}}
nav a{{padding:12px 24px;border:1px solid currentColor;border-radius:8px;color:inherit;text-decoration:none;font-size:1.25rem}}
</style></head>
<body>
<h1>{title}</h1>
<nav>{buttons}</nav>
</body>
</html>
"""

app = FastAPI(title="Tracer", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
def landing() -> HTMLResponse:
    # Not part of either spec. It never touches the database, so it also works on previews.
    buttons = "".join(BUTTON.format(href=href, label=label) for label, href in LINKS)
    return HTMLResponse(PAGE.format(title=TITLE, buttons=buttons))


app.mount("/system", system_app)
app.mount("/tester", tester_app)
