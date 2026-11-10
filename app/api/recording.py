"""Records the system's lines each project call runs, for its session's coverage report (TEST-02).

`RecordCoverage` wraps `system_app`. A request carrying `X-Test-Run` runs inside a recording;
`calls.call()` claims it for that session once the identity headers pass, and the lines are
saved before the response's last body chunk is sent, so a report requested after the reply
already counts them. Requests no project route claims, header errors and test control
included, save nothing and touch no database.
"""

import logging

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import code_coverage

logger = logging.getLogger(__name__)


class RecordCoverage:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not any(name == b"x-test-run" for name, _ in scope["headers"])
            or not code_coverage.start()
        ):
            await self.app(scope, receive, send)
            return
        with code_coverage.recording() as current:

            async def send_after_saving(message: Message) -> None:
                if message["type"] == "http.response.body" and not message.get("more_body"):
                    await _save(current)
                await send(message)

            await self.app(scope, receive, send_after_saving)


async def _save(current: code_coverage.Recording) -> None:
    """Stop the recording and store its lines, if a project route claimed it."""
    if not current.active:
        return
    current.active = False
    if current.session_id is None:
        return
    try:
        await run_in_threadpool(code_coverage.save, current.session_id, frozenset(current.hits))
    except Exception:
        # Measuring must never change a project call's answer, whatever went wrong storing it.
        logger.warning("coverage not saved", exc_info=True)
