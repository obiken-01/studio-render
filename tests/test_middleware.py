"""Request guard: body-size cap, envelopes for framework errors and crashes, CORS on errors."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.middleware import MAX_BODY_BYTES
from app.render.worker import get_queue
from app.storage import CHUNK_SIZE, get_store

ORIGIN = "https://tools.ralphalcaide.com"


def _envelope(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    assert body["data"] is None and body["code"] == code and body["message"]
    assert set(body) == {"data", "message", "code", "details"}
    return body


def test_body_limit_is_one_megabyte():
    assert MAX_BODY_BYTES == 1024 * 1024


@pytest.mark.parametrize("with_auth", [True, False])
@pytest.mark.parametrize("url", ["/renders", "/projects/draft", "/projects/analyze", "/uploads"])
def test_oversized_json_body_is_rejected(client, auth, url, with_auth):
    headers = {"Content-Type": "application/json", "Origin": ORIGIN}
    if with_auth:
        headers.update(auth)
    r = client.post(url, headers=headers, content=b"[" + b"1," * (MAX_BODY_BYTES // 2) + b"1]")
    body = _envelope(r, 413, "body_too_large")
    assert body["message"] == "That request is too large."
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_body_just_under_the_limit_is_read(client, auth):
    padding = " " * (MAX_BODY_BYTES - 100)
    r = client.post("/projects/analyze", headers={**auth, "Content-Type": "application/json"},
                    content=('{"musicFileId": "up_x"}' + padding).encode())
    _envelope(r, 422, "unknown_file")


def _asgi_app(settings, store, queue):
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_queue] = lambda: queue
    return app


def _drive(app, headers, total_bytes, piece=64 * 1024):
    """Call the ASGI app directly with a lazily produced body; return (status, bytes pulled)."""
    pulled = 0
    sent = []

    async def receive():
        nonlocal pulled
        if pulled >= total_bytes:
            return {"type": "http.request", "body": b"", "more_body": False}
        pulled += piece
        return {"type": "http.request", "body": b" " * piece, "more_body": pulled < total_bytes}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/renders", "raw_path": b"/renders",
             "query_string": b"", "root_path": "", "server": ("test", 80),
             "client": ("test", 1), "headers": [(k.lower().encode(), v.encode())
                                                for k, v in headers.items()]}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body), pulled


@pytest.mark.parametrize("with_auth", [True, False])
def test_declared_oversized_body_is_never_read(settings, store, queue, auth, with_auth):
    headers = {"content-type": "application/json", "content-length": str(200 * 1024 * 1024)}
    if with_auth:
        headers.update(auth)
    status, body, pulled = _drive(_asgi_app(settings, store, queue), headers, 200 * 1024 * 1024)
    assert status == 413 and body["code"] == "body_too_large"
    assert pulled == 0


@pytest.mark.parametrize("with_auth", [True, False])
def test_streamed_oversized_body_is_cut_off(settings, store, queue, auth, with_auth):
    headers = {"content-type": "application/json", "transfer-encoding": "chunked"}
    if with_auth:
        headers.update(auth)
    status, body, pulled = _drive(_asgi_app(settings, store, queue), headers, 200 * 1024 * 1024)
    assert status == 413 and body["code"] == "body_too_large"
    assert pulled <= MAX_BODY_BYTES + 64 * 1024


def test_full_chunk_upload_is_not_limited(client, auth):
    up = client.post("/uploads", headers=auth,
                     json={"filename": "c.mp4", "size": CHUNK_SIZE * 2, "kind": "video"}
                     ).json()["data"]
    r = client.put(f"/uploads/{up['uploadId']}/chunks/0", headers=auth,
                   content=b"x" * CHUNK_SIZE)
    assert r.status_code == 200 and r.json()["data"] == {"received": [0]}


def test_unknown_path_uses_the_envelope(client):
    r = client.get("/nope", headers={"Origin": ORIGIN})
    _envelope(r, 404, "not_found")
    assert r.headers["access-control-allow-origin"] == ORIGIN


def test_wrong_method_uses_the_envelope(client):
    r = client.patch("/health")
    _envelope(r, 405, "method_not_allowed")
    assert "GET" in r.headers.get("allow", "")


def test_invalid_body_uses_the_envelope(client, auth):
    r = client.post("/uploads", headers=auth, json={"filename": "a.mp4", "size": "big"})
    body = _envelope(r, 422, "invalid_request")
    assert body["message"] == "The request is not valid."
    locs = [e["loc"] for e in body["details"]["errors"]]
    assert ["body", "size"] in locs and ["body", "kind"] in locs


def test_crash_returns_500_envelope_with_cors(settings, store, queue):
    app = _asgi_app(settings, store, queue)

    def boom():
        raise RuntimeError("secret internals /etc/passwd")

    app.add_api_route("/boom", boom)
    r = TestClient(app).get("/boom", headers={"Origin": ORIGIN})
    body = _envelope(r, 500, "server_error")
    assert body["message"] == "Something went wrong." and body["details"] is None
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "secret" not in r.text and "Traceback" not in r.text
