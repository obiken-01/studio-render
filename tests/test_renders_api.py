import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.render import worker as worker_mod
from tests.media import ffprobe_json
from tests.test_draft_api import _body, uploaded  # noqa: F401  (fixture)
from tests.test_worker import Gate, wait_for, write_ok


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    monkeypatch.setattr(worker_mod, "MIN_FREE_BYTES", 0)


def _draft(client, auth, u):
    r = client.post("/projects/draft", headers=auth, json=_body(u))
    assert r.status_code == 200, r.text
    return r.json()["data"]["editList"]


def _poll(client, auth, job_id, timeout=120.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        data = client.get(f"/renders/{job_id}", headers=auth).json()["data"]
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(0.2)
    raise AssertionError("render did not finish")


def test_full_flow_with_real_ffmpeg(client, auth, queue, uploaded, tmp_path):  # noqa: F811
    el = _draft(client, auth, uploaded)
    r = client.post("/renders", headers=auth, json=el)
    assert r.status_code == 202
    job_id = r.json()["data"]["jobId"]
    done = _poll(client, auth, job_id)
    assert done["status"] == "done" and done["percent"] == 100

    r = client.post(f"/renders/{job_id}/download-link", headers=auth)
    link = r.json()["data"]
    assert link["expiresInSec"] == 300
    assert link["url"].startswith(f"/renders/{job_id}/file?token=")

    r = client.get(link["url"])  # no Authorization header
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"].startswith('attachment; filename="studio-')
    assert r.headers["content-disposition"].endswith('.mp4"')
    saved = tmp_path / "saved.mp4"
    saved.write_bytes(r.content)
    video = next(s for s in ffprobe_json(saved)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"], video["codec_name"]) == (1080, 1920, "h264")
    # Streaming finished, so the 15-minute countdown has started.
    assert queue.get(job_id)[0].downloaded_at is not None


@pytest.fixture
def fake_el(uploaded, client, auth):  # noqa: F811
    return _draft(client, auth, uploaded)


@pytest.fixture
def done_job(client, auth, queue, fake_el):
    queue.render_fn = write_ok
    job_id = client.post("/renders", headers=auth, json=fake_el).json()["data"]["jobId"]
    assert _poll(client, auth, job_id, 10)["status"] == "done"
    link = client.post(f"/renders/{job_id}/download-link", headers=auth).json()["data"]["url"]
    return job_id, link


@pytest.fixture
def running_job(client, auth, queue, fake_el):
    gate = Gate()
    queue.render_fn = gate
    job_id = client.post("/renders", headers=auth, json=fake_el).json()["data"]["jobId"]
    assert gate.started.wait(5)
    yield job_id
    gate.release.set()


def test_file_needs_valid_token(client, done_job):
    job_id, link = done_job
    for url in (f"/renders/{job_id}/file", f"/renders/{job_id}/file?token=wrong"):
        r = client.get(url)
        assert r.status_code == 403
        assert r.json()["code"] == "bad_download_link" and r.json()["data"] is None
    assert client.get(link).status_code == 200


def test_token_is_bound_to_its_job(client, auth, queue, fake_el, done_job):
    _, link = done_job
    other = client.post("/renders", headers=auth, json=fake_el).json()["data"]["jobId"]
    wait_for(lambda: queue.get(other)[0].status == "done")
    token = link.split("token=")[1]
    assert client.get(f"/renders/{other}/file?token={token}").status_code == 403


def test_expired_token(client, queue, done_job, monkeypatch):
    _, link = done_job
    now = time.time()
    monkeypatch.setattr(queue, "clock", lambda: now + 301)
    r = client.get(link)
    assert r.status_code == 403 and r.json()["code"] == "bad_download_link"


def test_download_after_delete_is_404(client, auth, done_job):
    job_id, link = done_job
    assert client.delete(f"/renders/{job_id}", headers=auth).status_code == 200
    r = client.get(link)
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "unknown_render" and body["data"] is None and body["message"]


def test_download_link_while_running(client, auth, running_job):
    r = client.post(f"/renders/{running_job}/download-link", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "not_ready"
    status = client.get(f"/renders/{running_job}", headers=auth).json()["data"]
    assert status["status"] == "running" and status["position"] is None


def test_second_render_waits_in_line(client, auth, running_job, fake_el):
    r = client.post("/renders", headers=auth, json=fake_el)
    assert r.status_code == 202
    waiting = r.json()["data"]["jobId"]
    status = client.get(f"/renders/{waiting}", headers=auth).json()["data"]
    assert status == {"jobId": waiting, "status": "queued", "percent": 0, "position": 1,
                      "message": None}
    r = client.post(f"/renders/{waiting}/download-link", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "not_ready"
    assert client.get(f"/renders/{running_job}", headers=auth).json()["data"]["status"] \
        == "running"


def test_queue_full_over_http(client, auth, running_job, fake_el):
    for _ in range(3):
        assert client.post("/renders", headers=auth, json=fake_el).status_code == 202
    r = client.post("/renders", headers=auth, json=fake_el)
    assert r.status_code == 429 and r.json()["code"] == "queue_full"


def test_unknown_render(client, auth):
    r = client.get("/renders/rd_nope", headers=auth)
    assert r.status_code == 404 and r.json()["code"] == "unknown_render"


def test_clear_project_also_clears_renders(client, auth, store, done_job):
    job_id, _ = done_job
    assert list(store.renders_dir.iterdir())
    assert client.delete("/projects/current", headers=auth).status_code == 200
    assert client.get(f"/renders/{job_id}", headers=auth).status_code == 404
    assert list(store.renders_dir.iterdir()) == []
    assert store.files() == {}


@pytest.mark.parametrize("method,url", [
    ("POST", "/renders"),
    ("GET", "/renders/rd_x"),
    ("POST", "/renders/rd_x/download-link"),
    ("DELETE", "/renders/rd_x"),
])
def test_render_routes_require_auth(client, method, url):
    r = client.request(method, url)
    assert r.status_code == 401


def test_lifespan_starts_and_stops_threads(settings):
    ours = {"render-worker", "sweeper"}
    assert not ours & {t.name for t in threading.enumerate()}
    with TestClient(create_app(settings)):
        assert ours <= {t.name for t in threading.enumerate()}
    assert not ours & {t.name for t in threading.enumerate()}
