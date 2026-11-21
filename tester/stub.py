"""The tester's LLM stub: an OpenAI-compatible chat completions endpoint that a session's
llm_base_url can point at, to test a system's drafting (REQ-15 to REQ-17, REQ-20; AC-21).

A system calls {llm_base_url}/chat/completions, so each mode is a base URL under /llm:

- {tester}/llm/fail answers 500 at once.
- {tester}/llm/silent answers nothing for SILENT_SECONDS, past the spec's 60 s, then 504.
- {tester}/llm/reply/{content} answers 200 with a chat completion whose message content is
  `content`, base64url-encoded in the path; reply_url builds it. The case that sets the URL
  decides exactly what the model says, including replies a system must refuse.

The stub ignores the request body and stores nothing, so any instance can answer.
"""

import asyncio
import base64
import binascii
import re
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()

# Longer than the spec's 60 s (REQ-20) plus AC-21's 2 s, so a system without its own timeout
# answers late instead of on time, and well inside Vercel's 300 s function limit.
SILENT_SECONDS = 65.0
# A reply's content, decoded. Four short elements fit many times over, and the encoded path
# stays far below common URL length limits.
MAX_CONTENT_BYTES = 8_000
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
COMPLETION = "/chat/completions"


def fail_url(tester_url: str) -> str:
    """The base URL of the stub that always fails, given the tester's base URL."""
    return f"{tester_url.rstrip('/')}/llm/fail"


def silent_url(tester_url: str) -> str:
    """The base URL of the stub that doesn't answer in time."""
    return f"{tester_url.rstrip('/')}/llm/silent"


def reply_url(tester_url: str, content: str) -> str:
    """The base URL of a stub whose reply's message content is `content`."""
    encoded = base64.urlsafe_b64encode(content.encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"{tester_url.rstrip('/')}/llm/reply/{encoded}"


def decode(encoded: str) -> str | None:
    """reply_url's content back from its path segment; None when it isn't one."""
    if (len(encoded) > -(-MAX_CONTENT_BYTES * 4 // 3) or len(encoded) % 4 == 1
            or not _BASE64URL.fullmatch(encoded)):
        return None
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        content = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return content if len(raw) <= MAX_CONTENT_BYTES else None


def _error(status: int, message: str, kind: str) -> JSONResponse:
    # The error shape OpenAI-compatible providers use.
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)


@router.post(f"/llm/fail{COMPLETION}")
async def fail() -> JSONResponse:
    return _error(500, "The tester's LLM stub always fails", "server_error")


@router.post(f"/llm/silent{COMPLETION}")
async def silent() -> JSONResponse:
    await asyncio.sleep(SILENT_SECONDS)
    return _error(504, f"The tester's LLM stub waited {SILENT_SECONDS:g} s", "timeout")


@router.post("/llm/reply/{encoded}" + COMPLETION)
async def reply(encoded: str) -> JSONResponse:
    content = decode(encoded)
    if content is None:
        return _error(400, "The reply isn't base64url-encoded UTF-8 of at most "
                           f"{MAX_CONTENT_BYTES} bytes", "invalid_request_error")
    return JSONResponse({
        "id": "chatcmpl-tester-stub",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "tester-stub",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })
