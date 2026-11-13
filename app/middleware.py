"""A small pure-ASGI guard that sits just inside CORS, so everything it answers carries the
CORS headers:

- Request bodies are capped at MAX_BODY_BYTES before any route (or auth dependency) reads
  them. FastAPI parses a JSON body before it resolves dependencies, so without this an
  unauthenticated caller could make the service buffer an unbounded body. The chunk upload
  route is exempt: it streams the body and enforces its own one-chunk cap.
- An unhandled exception becomes the `server_error` envelope here, inside CORS, so the browser
  can read it (the app-level catch-all runs outside CORS).
"""
import json
import logging
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024
_CHUNK_PUT = re.compile(r"^/uploads/[^/]+/chunks/[^/]+/?$")


class _BodyTooLarge(Exception):
    pass


def _envelope(status: int, code: str, message: str) -> tuple[Message, Message]:
    body = json.dumps({"data": None, "message": message, "code": code,
                       "details": None}).encode()
    start = {"type": "http.response.start", "status": status,
             "headers": [(b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode())]}
    return start, {"type": "http.response.body", "body": body, "more_body": False}


def _exempt(scope: Scope) -> bool:
    return scope["method"] == "PUT" and bool(_CHUNK_PUT.match(scope["path"]))


class RequestGuard:
    def __init__(self, app: ASGIApp, max_body_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = None if _exempt(scope) else self.max_body_bytes
        if limit is not None:
            declared = dict(scope.get("headers") or []).get(b"content-length", b"")
            if declared.isdigit() and int(declared) > limit:
                await self._reply(send, *_envelope(413, "body_too_large",
                                                   "That request is too large."))
                return

        state = {"started": False, "too_large": False, "seen": 0}

        async def guarded_receive() -> Message:
            message = await receive()
            if limit is not None and message["type"] == "http.request":
                state["seen"] += len(message.get("body", b""))
                if state["seen"] > limit:
                    state["too_large"] = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message: Message) -> None:
            if state["too_large"]:
                return  # whatever the app makes of the cut-off body, the answer is 413
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except Exception as exc:
            if state["too_large"]:
                pass
            elif state["started"]:
                logger.error("Unhandled error after the response started", exc_info=exc)
                return
            else:
                logger.error("Unhandled error", exc_info=exc)
                await self._reply(send, *_envelope(500, "server_error", "Something went wrong."))
                return
        if state["too_large"] and not state["started"]:
            await self._reply(send, *_envelope(413, "body_too_large",
                                               "That request is too large."))

    @staticmethod
    async def _reply(send: Send, start: Message, body: Message) -> None:
        await send(start)
        await send(body)
