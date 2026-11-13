"""GET /projects/current and DELETE /uploads/{uploadId}."""
import time

import pytest

from app.storage import CHUNK_SIZE, Store
from tests.media import make_video
from tests.test_worker import Gate, wait_for, write_ok


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    from app.render import worker as worker_mod
    monkeypatch.setattr(worker_mod, "MIN_FREE_BYTES", 0)


def _complete(client, auth, tmp_path, name="c.mp4"):
    data = make_video(tmp_path / name, w=320, h=240, dur=1).read_bytes()
    up = client.post("/uploads", headers=auth,
                     json={"filename": name, "size": len(data), "kind": "video"}).json()["data"]
    client.put(f"/uploads/{up['uploadId']}/chunks/0", headers=auth, content=data)
    return client.post(f"/uploads/{up['uploadId']}/complete", headers=auth).json()["data"]


def _edit_list(file_id):
    return {"version": 1,
            "project": {"aspect": "9:16", "width": 1080, "height": 1920, "fps": 30},
            "music": {"fileId": "none", "startSec": 0, "endSec": 0.5},
            "audioMode": "silent",
            "timeline": [{"type": "video", "slotStartSec": 0, "durationSec": 0.5,
                          "source": {"fileId": file_id, "inSec": 0, "outSec": 0.5}}]}


def test_empty_project(client, auth, settings):
    r = client.get("/projects/current", headers=auth)
    assert r.status_code == 200 and r.json()["message"] is None
    assert r.json()["data"] == {
        "files": [], "uploads": [], "renders": [], "usedBytes": 0,
        "limits": {"maxFileBytes": 3 * 1024**3, "maxProjectBytes": 10 * 1024**3,
                   "ttlHours": 24, "chunkSize": CHUNK_SIZE},
    }


def test_project_lists_files_uploads_and_renders(client, auth, queue, tmp_path):
    before = time.time()
    meta = _complete(client, auth, tmp_path)
    assert before - 1 <= meta["createdAt"] <= time.time() + 1
    pending = client.post("/uploads", headers=auth,
                          json={"filename": "later.mp4", "size": CHUNK_SIZE + 5,
                                "kind": "video"}).json()["data"]
    client.put(f"/uploads/{pending['uploadId']}/chunks/1", headers=auth, content=b"z" * 5)

    gate = Gate()
    queue.render_fn = gate
    running = client.post("/renders", headers=auth,
                          json=_edit_list(meta["fileId"])).json()["data"]["jobId"]
    assert gate.started.wait(5)
    queued = client.post("/renders", headers=auth,
                         json=_edit_list(meta["fileId"])).json()["data"]["jobId"]
    try:
        data = client.get("/projects/current", headers=auth).json()["data"]
    finally:
        gate.release.set()

    assert data["files"] == [meta]
    assert data["uploads"] == [{
        "uploadId": pending["uploadId"], "filename": "later.mp4", "size": CHUNK_SIZE + 5,
        "kind": "video", "chunkSize": CHUNK_SIZE, "totalChunks": 2, "received": [1]}]
    assert [(j["jobId"], j["status"], j["position"]) for j in data["renders"]] == [
        (running, "running", None), (queued, "queued", 1)]
    assert set(data["renders"][0]) == {"jobId", "status", "percent", "position", "message"}
    assert data["usedBytes"] == meta["size"] + CHUNK_SIZE + 5


def test_finished_render_is_listed(client, auth, queue, tmp_path):
    meta = _complete(client, auth, tmp_path)
    queue.render_fn = write_ok
    job_id = client.post("/renders", headers=auth,
                         json=_edit_list(meta["fileId"])).json()["data"]["jobId"]
    wait_for(lambda: queue.get(job_id)[0].status == "done")
    renders = client.get("/projects/current", headers=auth).json()["data"]["renders"]
    assert renders == [{"jobId": job_id, "status": "done", "percent": 100, "position": None,
                        "message": None}]


def test_cancel_upload(client, auth, store):
    up = client.post("/uploads", headers=auth,
                     json={"filename": "big.mp4", "size": CHUNK_SIZE * 2, "kind": "video"}
                     ).json()["data"]
    client.put(f"/uploads/{up['uploadId']}/chunks/0", headers=auth, content=b"a" * CHUNK_SIZE)
    assert list(store.uploads_dir.iterdir())
    assert store.total_bytes() == CHUNK_SIZE * 2

    r = client.delete(f"/uploads/{up['uploadId']}", headers=auth)
    assert r.status_code == 200 and r.json() == {"data": None, "message": None}
    assert list(store.uploads_dir.iterdir()) == []
    assert store.total_bytes() == 0
    r = client.get(f"/uploads/{up['uploadId']}", headers=auth)
    assert r.status_code == 404 and r.json()["code"] == "unknown_upload"
    r = client.delete(f"/uploads/{up['uploadId']}", headers=auth)
    assert r.status_code == 404 and r.json()["code"] == "unknown_upload"


def test_cancelled_upload_stays_gone_after_restart(store, settings):
    up = store.create_upload("big.mp4", 100, "video")
    store.delete_upload(up.upload_id)
    again = Store(settings.studio_data_dir, ttl_hours=24, max_file_bytes=3 * 1024**3,
                  max_project_bytes=10 * 1024**3)
    assert again.uploads() == []


@pytest.mark.parametrize("method,url", [
    ("GET", "/projects/current"),
    ("DELETE", "/uploads/up_x"),
])
def test_new_endpoints_require_auth(client, method, url):
    r = client.request(method, url)
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"


def test_unwritable_data_dir_fails_with_a_clear_error(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    with pytest.raises(RuntimeError) as e:
        Store(blocker / "studio", ttl_hours=24, max_file_bytes=1, max_project_bytes=1)
    assert "STUDIO_DATA_DIR" in str(e.value) and "not_a_dir" in str(e.value)
