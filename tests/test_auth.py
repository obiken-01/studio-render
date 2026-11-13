import base64
import json
import time

import pytest

from tests.conftest import AUDIENCE, ISSUER


def _b64(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_health_is_public(client):
    assert client.get("/health").json() == {"data": {"status": "ok"}, "message": None}


def test_session_allowed(client, auth):
    r = client.get("/session", headers=auth)
    assert r.status_code == 200 and r.json()["data"] == {"allowed": True}


@pytest.mark.parametrize("kwargs", [
    {"secret": "another-signing-key-that-is-long-enough-!!!!"},
    {"iss": "someone-else"}, {"aud": "someone-else"},
    {"exp_delta": -1},
])
def test_bad_tokens_are_401(client, make_token, kwargs):
    r = client.get("/session", headers={"Authorization": f"Bearer {make_token(**kwargs)}"})
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"


def test_missing_and_garbage_tokens_are_401(client):
    assert client.get("/session").status_code == 401
    assert client.get("/session", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_alg_none_is_401(client):
    header = _b64({"alg": "none", "typ": "JWT"})
    payload = _b64({"sub": "7", "user_type": "Work", "iss": ISSUER, "aud": AUDIENCE,
                    "exp": int(time.time()) + 900})
    token = f"{header}.{payload}."
    r = client.get("/session", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_admin_token_is_403(client, make_token):
    token = make_token(user_type="Ralphy")
    r = client.get("/session", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


def test_other_work_user_is_403(client, make_token):
    r = client.get("/session", headers={"Authorization": f"Bearer {make_token(sub='8')}"})
    assert r.status_code == 403 and r.json()["message"] == "Studio is private."


def test_error_envelope_shape(client):
    body = client.get("/session").json()
    assert body == {"data": None, "message": "Please sign in again.",
                    "code": "unauthorized", "details": None}


def test_cors_allows_only_configured_origin(client):
    def preflight(origin):
        return client.options("/session", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        })

    allowed = preflight("https://tools.ralphalcaide.com")
    assert allowed.headers["access-control-allow-origin"] == "https://tools.ralphalcaide.com"
    other = preflight("https://evil.example")
    assert "access-control-allow-origin" not in other.headers
