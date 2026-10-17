"""The deployed ASGI app; Vercel loads `app` from here (pyproject.toml, [tool.vercel]).

The system is served at /system and the tester at /tester, so each has its own base URL.
"""

from fastapi import FastAPI

from app.api import system_app
from tester import tester_app

app = FastAPI(title="Tracer", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/system", system_app)
app.mount("/tester", tester_app)
