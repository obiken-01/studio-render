import json
import threading
import time

import pytest

from app.editlist import EditList
from app.errors import ApiError
from app.models import FileMeta
from app.render import worker as worker_mod
from app.render.ffmpeg import RenderError
from app.render.worker import POST_DOWNLOAD_KEEP_SEC, RenderQueue


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    monkeypatch.setattr(worker_mod, "MIN_FREE_BYTES", 0)


@pytest.fixture
def el(store, tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"x")
    store._files["up_a"] = FileMeta("up_a", "video", p, "clip.mp4", 1, 8.0, 320, 180, 30.0, 0, 0.0)
    return EditList.model_validate({
        "version": 1,
        "project": {"aspect": "9:16", "width": 1080, "height": 1920, "fps": 30},
        "music": {"fileId": "m", "startSec": 0, "endSec": 1.0},
        "audioMode": "silent",
        "timeline": [{"type": "video", "slotStartSec": 0, "durationSec": 1.0,
                      "source": {"fileId": "up_a", "inSec": 0, "outSec": 1.0}}]})


@pytest.fixture
def make_queue(store, settings):
    made = []

    def _make(render_fn, **kw):
        q = RenderQueue(store, settings, render_fn=render_fn, **kw)
        q.start()
        made.append(q)
        return q

    yield _make
    for q in made:
        q.stop()


def write_ok(el, files, out, work, on_progress, cancel, timeout):
    out.write_bytes(b"mp4")


class Gate:
    """A fake render that blocks until released, honouring cancel."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.calls = 0

    def __call__(self, el, files, out, work, on_progress, cancel, timeout):
        self.calls += 1
        out.write_bytes(b"partial")
        (work / "scratch").write_text("x")
        self.started.set()
        while not self.release.is_set():
            if cancel.is_set():
                self.cancelled.set()
                raise RenderError("The render was cancelled.")
            time.sleep(0.01)
        out.write_bytes(b"mp4")


def test_one_job_at_a_time(make_queue, el):
    gate = Gate()
    q = make_queue(gate)
    first, second = q.submit(el), q.submit(el)
    assert gate.started.wait(5)
    job, pos = q.get(first.job_id)
    assert (job.status, pos) == ("running", None)
    job, pos = q.get(second.job_id)
    assert (job.status, pos) == ("queued", 1)
    assert gate.calls == 1
    gate.release.set()
    wait_for(lambda: q.get(second.job_id)[0].status == "done")
    assert gate.calls == 2


def test_progress_and_done(make_queue, el):
    seen = threading.Event()
    proceed = threading.Event()

    def fake(el, files, out, work, on_progress, cancel, timeout):
        on_progress(0.5)
        seen.set()
        assert proceed.wait(5)
        out.write_bytes(b"mp4")

    q = make_queue(fake)
    job = q.submit(el)
    assert seen.wait(5)
    assert q.get(job.job_id)[0].percent == 50
    assert q.get(job.job_id)[0].status == "running"
    proceed.set()
    wait_for(lambda: q.get(job.job_id)[0].status == "done")
    done, pos = q.get(job.job_id)
    assert done.percent == 100 and pos is None
    assert done.output_path.read_bytes() == b"mp4"
    assert done.public(None) == {"jobId": job.job_id, "status": "done", "percent": 100,
                                 "position": None, "message": None}


def test_failure_message(make_queue, el, store):
    def fake(el, files, out, work, on_progress, cancel, timeout):
        out.write_bytes(b"half")
        raise RenderError("The render took too long.")

    q = make_queue(fake)
    job = q.submit(el)
    wait_for(lambda: q.get(job.job_id)[0].status == "failed")
    assert q.get(job.job_id)[0].message == "The render took too long."
    assert list(store.renders_dir.iterdir()) == []
    assert list(store.work_dir.iterdir()) == []


def test_unexpected_exception(make_queue, el):
    def fake(*a):
        raise ValueError("boom")

    q = make_queue(fake)
    job = q.submit(el)
    wait_for(lambda: q.get(job.job_id)[0].status == "failed")
    assert q.get(job.job_id)[0].message == "The video could not be rendered."
    # The worker survived and takes the next job.
    q.render_fn = write_ok
    nxt = q.submit(el)
    wait_for(lambda: q.get(nxt.job_id)[0].status == "done")


def test_queue_full(make_queue, el):
    gate = Gate()
    q = make_queue(gate)
    q.submit(el)
    assert gate.started.wait(5)
    for _ in range(3):  # the running job does not count
        q.submit(el)
    with pytest.raises(ApiError) as e:
        q.submit(el)
    assert (e.value.status, e.value.code) == (429, "queue_full")
    gate.release.set()


def test_submit_validates_first(make_queue, el):
    q = make_queue(write_ok)
    bad = EditList.model_validate({**el.model_dump(), "timeline": [
        {"type": "empty", "slotStartSec": 0, "durationSec": 1.0}]})
    with pytest.raises(ApiError) as e:
        q.submit(bad)
    assert e.value.code == "unfilled_slots"
    assert q._jobs == {} and not q._pending


def test_delete_running_job_cancels_it(make_queue, el, store):
    gate = Gate()
    q = make_queue(gate)
    job = q.submit(el)
    assert gate.started.wait(5)
    q.delete(job.job_id)
    assert gate.cancelled.wait(5)
    with pytest.raises(ApiError) as e:
        q.get(job.job_id)
    assert e.value.code == "unknown_render"
    wait_for(lambda: not list(store.renders_dir.iterdir()) and not list(store.work_dir.iterdir()))
    # The queue is free for the next job.
    gate.release.set()
    nxt = q.submit(el)
    wait_for(lambda: q.get(nxt.job_id)[0].status == "done")


def test_delete_unknown_job(make_queue):
    with pytest.raises(ApiError) as e:
        make_queue(write_ok).delete("rd_nope")
    assert (e.value.status, e.value.code) == (404, "unknown_render")


def test_delete_queued_job_removes_it_from_the_line(make_queue, el):
    gate = Gate()
    q = make_queue(gate)
    q.submit(el)
    assert gate.started.wait(5)
    a, b = q.submit(el), q.submit(el)
    q.delete(a.job_id)
    assert q.get(b.job_id)[1] == 1
    gate.release.set()


def test_clear_removes_everything(make_queue, el, store):
    gate = Gate()
    q = make_queue(gate)
    run, wait = q.submit(el), q.submit(el)
    assert gate.started.wait(5)
    q.clear()
    for j in (run, wait):
        with pytest.raises(ApiError):
            q.get(j.job_id)
    assert gate.cancelled.wait(5)
    wait_for(lambda: not list(store.renders_dir.iterdir()) and not list(store.work_dir.iterdir()))
    assert gate.calls == 1


def test_restart_marks_unfinished_failed(store, settings, el):
    q = RenderQueue(store, settings)  # never started, so both jobs stay queued
    first, second = q.submit(el), q.submit(el)
    q._jobs[first.job_id].status = "running"
    q._pending.popleft()
    q._save()
    again = RenderQueue(store, settings)
    for jid in (first.job_id, second.job_id):
        job, pos = again.get(jid)
        assert job.status == "failed" and pos is None
        assert job.message == "The service restarted, please render again."


GOOD_JOB = {"job_id": "rd_good", "status": "failed", "percent": 0, "message": "Nope.",
            "output_path": None, "created_at": 1.0, "finished_at": 1.0}


@pytest.mark.parametrize("content", [
    "[]",
    json.dumps([GOOD_JOB]),
    json.dumps({"jobs": "nope"}),
    json.dumps({"jobs": [5, "x", None, GOOD_JOB]}),
    json.dumps({"jobs": [{"job_id": "rd_bad"}, GOOD_JOB]}),
    json.dumps({"jobs": [{**GOOD_JOB, "job_id": "rd_bad", "output_path": 5}, GOOD_JOB]}),
])
def test_bad_jobs_file_does_not_stop_startup(store, settings, content):
    (store.data_dir / "jobs.json").write_text(content)
    q = RenderQueue(store, settings)
    with pytest.raises(ApiError):
        q.get("rd_bad")
    if "rd_good" in content and not content.startswith("["):
        assert q.get("rd_good")[0].status == "failed"


def test_jobs_file_is_written_atomically(make_queue, el, store):
    q = make_queue(write_ok)
    job = q.submit(el)
    wait_for(lambda: q.get(job.job_id)[0].status == "done")
    data = json.loads((store.data_dir / "jobs.json").read_text())
    assert data["jobs"][0]["status"] == "done"
    assert not (store.data_dir / "jobs.json.tmp").exists()


def done_job(q, el):
    job = q.submit(el)
    wait_for(lambda: q.get(job.job_id)[0].status == "done")
    return job.job_id


def test_output_deleted_15_min_after_download(make_queue, el):
    q = make_queue(write_ok, clock=lambda: 1000.0)
    jid = done_job(q, el)
    out = q.get(jid)[0].output_path
    q.mark_downloaded(jid)
    t0 = 1000.0
    q.sweep(t0 + POST_DOWNLOAD_KEEP_SEC - 1)
    assert out.exists() and q.get(jid)[0].status == "done"
    q.sweep(t0 + POST_DOWNLOAD_KEEP_SEC + 1)
    assert not out.exists()
    with pytest.raises(ApiError):
        q.get(jid)


def test_undownloaded_output_deleted_at_ttl(make_queue, el, store):
    q = make_queue(write_ok, clock=lambda: 1000.0)
    jid = done_job(q, el)
    out = q.get(jid)[0].output_path
    q.sweep(1000.0 + store.ttl_seconds - 1)
    assert out.exists()
    q.sweep(1000.0 + store.ttl_seconds + 1)
    assert not out.exists()
    with pytest.raises(ApiError):
        q.get(jid)


def test_download_tokens(make_queue, el):
    t = [1000.0]
    q = make_queue(write_ok, clock=lambda: t[0])
    jid = done_job(q, el)
    token = q.make_download_token(jid)
    assert q.open_download(jid, token) == q.get(jid)[0].output_path
    assert q.open_download(jid, token)  # reusable until expiry
    for bad in ("", "nope", token + "x"):
        with pytest.raises(ApiError) as e:
            q.open_download(jid, bad)
        assert (e.value.status, e.value.code) == (403, "bad_download_link")
    t[0] += worker_mod.DOWNLOAD_TOKEN_TTL_SEC + 1
    with pytest.raises(ApiError) as e:
        q.open_download(jid, token)
    assert e.value.code == "bad_download_link"


def test_token_for_unfinished_job_is_not_ready(make_queue, el):
    gate = Gate()
    q = make_queue(gate)
    job = q.submit(el)
    with pytest.raises(ApiError) as e:
        q.make_download_token(job.job_id)
    assert (e.value.status, e.value.code) == (409, "not_ready")
    gate.release.set()


def test_out_of_disk_fails_the_job(make_queue, el, monkeypatch):
    monkeypatch.setattr(worker_mod, "MIN_FREE_BYTES", 1 << 62)
    q = make_queue(write_ok)
    job = q.submit(el)
    wait_for(lambda: q.get(job.job_id)[0].status == "failed")
    assert q.get(job.job_id)[0].message == (
        "The server is out of disk space. Clear files and try again.")


def test_stop_cancels_running_job_and_joins(make_queue, el):
    gate = Gate()
    q = make_queue(gate)
    q.submit(el)
    assert gate.started.wait(5)
    q.stop()
    assert not q._thread.is_alive()


def test_job_running_at_graceful_stop_shows_restart_message(make_queue, el, store, settings):
    gate = Gate()
    q = make_queue(gate)
    job = q.submit(el)
    assert gate.started.wait(5)
    q.stop()
    again = RenderQueue(store, settings)
    loaded, pos = again.get(job.job_id)
    assert loaded.status == "failed" and pos is None
    assert loaded.message == "The service restarted, please render again."
    assert list(store.renders_dir.iterdir()) == []
    assert list(store.work_dir.iterdir()) == []
