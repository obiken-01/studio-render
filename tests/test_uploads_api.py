import pytest

from app.storage import CHUNK_SIZE
from tests.media import make_video


def test_upload_flow_over_http(client, auth, tmp_path):
    data = make_video(tmp_path / "c.mp4", w=640, h=360, dur=2).read_bytes()
    r = client.post("/uploads", headers=auth,
                    json={"filename": "c.mp4", "size": len(data), "kind": "video"})
    assert r.status_code == 200
    body = r.json()
    assert body["message"] is None
    up = body["data"]
    assert up["chunkSize"] == CHUNK_SIZE and up["totalChunks"] == 1

    r = client.put(f"/uploads/{up['uploadId']}/chunks/0", headers=auth, content=data)
    assert r.status_code == 200 and r.json()["data"] == {"received": [0]}

    r = client.get(f"/uploads/{up['uploadId']}", headers=auth)
    assert r.json()["data"] == {"received": [0], "totalChunks": 1}

    r = client.post(f"/uploads/{up['uploadId']}/complete", headers=auth)
    assert r.status_code == 200
    meta = r.json()["data"]
    assert meta["fileId"] == up["uploadId"]
    assert meta["kind"] == "video" and meta["filename"] == "c.mp4"
    assert meta["width"] == 640 and meta["durationSec"] > 1


def test_oversized_chunk_over_http(client, auth):
    up = client.post("/uploads", headers=auth,
                     json={"filename": "c.mp4", "size": CHUNK_SIZE * 2, "kind": "video"}
                     ).json()["data"]
    r = client.put(f"/uploads/{up['uploadId']}/chunks/0", headers=auth,
                   content=b"x" * (CHUNK_SIZE + 1))
    assert r.status_code == 413
    body = r.json()
    assert body["code"] == "chunk_too_large" and body["data"] is None


def test_errors_use_the_envelope(client, auth):
    r = client.post("/uploads", headers=auth,
                    json={"filename": "notes.txt", "size": 5, "kind": "video"})
    assert r.status_code == 415
    assert r.json()["code"] == "unsupported_type"
    r = client.get("/uploads/up_missing", headers=auth)
    assert r.status_code == 404 and r.json()["code"] == "unknown_upload"


@pytest.mark.parametrize("method,url", [
    ("POST", "/uploads"),
    ("PUT", "/uploads/up_x/chunks/0"),
    ("GET", "/uploads/up_x"),
    ("POST", "/uploads/up_x/complete"),
    ("DELETE", "/files/up_x"),
    ("DELETE", "/projects/current"),
])
def test_endpoints_require_auth(client, method, url):
    r = client.request(method, url)
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


def test_clear_project(client, auth, store, tmp_path):
    data = make_video(tmp_path / "c.mp4", w=320, h=240, dur=1).read_bytes()
    up = client.post("/uploads", headers=auth,
                     json={"filename": "c.mp4", "size": len(data), "kind": "video"}
                     ).json()["data"]
    client.put(f"/uploads/{up['uploadId']}/chunks/0", headers=auth, content=data)
    client.post(f"/uploads/{up['uploadId']}/complete", headers=auth)
    assert len(store.files()) == 1
    r = client.delete("/projects/current", headers=auth)
    assert r.status_code == 200
    assert store.files() == {}


def test_delete_one_file(client, auth, store, tmp_path):
    data = make_video(tmp_path / "c.mp4", w=320, h=240, dur=1).read_bytes()
    up = client.post("/uploads", headers=auth,
                     json={"filename": "c.mp4", "size": len(data), "kind": "video"}
                     ).json()["data"]
    client.put(f"/uploads/{up['uploadId']}/chunks/0", headers=auth, content=data)
    client.post(f"/uploads/{up['uploadId']}/complete", headers=auth)
    assert client.delete(f"/files/{up['uploadId']}", headers=auth).status_code == 200
    assert store.files() == {}
    assert client.delete(f"/files/{up['uploadId']}", headers=auth).status_code == 404
