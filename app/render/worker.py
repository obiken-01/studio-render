"""A single-worker render queue. One job renders at a time; the rest wait in order."""
import hmac
import json
import logging
import os
import secrets
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from fastapi import Depends

from app.config import Settings, get_settings
from app.editlist import EditList, validate_for_render
from app.errors import ApiError
from app.render.ffmpeg import RenderError
from app.render.pipeline import FAILED, render_edit_list
from app.storage import Store, get_store

logger = logging.getLogger(__name__)

DOWNLOAD_TOKEN_TTL_SEC = 300
POST_DOWNLOAD_KEEP_SEC = 900
MIN_FREE_BYTES = 2 * 1024**3
RESTARTED = "The service restarted, please render again."
OUT_OF_DISK = "The server is out of disk space. Clear files and try again."
JOBS_FILE = "jobs.json"


@dataclass
class Job:
    job_id: str
    status: Literal["queued", "running", "done", "failed"]
    percent: int
    message: str | None
    output_path: Path | None
    created_at: float
    finished_at: float | None = None
    downloaded_at: float | None = None

    def public(self, position: int | None) -> dict:
        return {"jobId": self.job_id, "status": self.status, "percent": self.percent,
                "position": position, "message": self.message}


def _unknown_render() -> ApiError:
    return ApiError(404, "unknown_render", "This render was not found. It may have expired.")


def _bad_link() -> ApiError:
    return ApiError(403, "bad_download_link",
                    "This download link is not valid or has expired. Please ask for a new one.")


class RenderQueue:
    def __init__(self, store: Store, settings: Settings,
                 render_fn: Callable = render_edit_list,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.settings = settings
        self.render_fn = render_fn
        self.clock = clock
        self._lock = threading.Condition(threading.Lock())
        self._jobs: dict[str, Job] = {}
        self._pending: deque[tuple[str, EditList]] = deque()
        self._cancels: dict[str, threading.Event] = {}
        self._tokens: dict[str, tuple[str, float]] = {}  # token -> (job_id, expires_at)
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._path = store.data_dir / JOBS_FILE
        self._load()

    # ---- persistence (callers hold the lock, except __init__) ----

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            raw = {}
        for j in raw.get("jobs", []):
            try:
                job = Job(**{**j, "output_path": Path(j["output_path"])
                             if j.get("output_path") else None})
            except (TypeError, ValueError):
                continue
            if job.status in ("queued", "running"):
                job.status, job.message = "failed", RESTARTED
                job.finished_at = self.clock()
            if job.status == "done" and not (job.output_path and job.output_path.exists()):
                continue
            self._jobs[job.job_id] = job
        # Whatever a crashed run left behind is unreachable now.
        keep = {j.output_path for j in self._jobs.values() if j.output_path}
        for f in self.store.renders_dir.iterdir():
            if f not in keep:
                f.unlink(missing_ok=True)
        for d in self.store.work_dir.iterdir():
            shutil.rmtree(d, ignore_errors=True)
        self._save()

    def _save(self) -> None:
        state = {"jobs": [{**asdict(j), "output_path": str(j.output_path)
                           if j.output_path else None} for j in self._jobs.values()]}
        tmp = self._path.with_name(JOBS_FILE + ".tmp")
        try:
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("could not save render jobs")

    # ---- lifecycle ----

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="render-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            for ev in self._cancels.values():
                ev.set()
            self._lock.notify_all()
            thread = self._thread
        if thread:
            thread.join(timeout=30)

    # ---- public API ----

    def submit(self, el: EditList) -> Job:
        validate_for_render(el, self.store.files())
        with self._lock:
            if len(self._pending) >= self.settings.studio_queue_max:
                raise ApiError(429, "queue_full",
                               "The server is busy with other renders. Please try again soon.")
            job = Job(job_id="rd_" + secrets.token_urlsafe(12), status="queued", percent=0,
                      message=None, output_path=None, created_at=self.clock())
            self._jobs[job.job_id] = job
            self._pending.append((job.job_id, el))
            self._save()
            self._lock.notify_all()
            return job

    def get(self, job_id: str) -> tuple[Job, int | None]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise _unknown_render()
            position = None
            if job.status == "queued":
                position = next(i for i, (jid, _) in enumerate(self._pending, 1)
                                if jid == job_id)
            return Job(**asdict(job)), position

    def delete(self, job_id: str) -> None:
        with self._lock:
            if job_id not in self._jobs:
                raise _unknown_render()
            self._drop(job_id)
            self._save()
        self._remove_files(job_id)

    def clear(self) -> None:
        """Cancel and remove every job, with its output and work directory."""
        with self._lock:
            ids = list(self._jobs)
            for jid in ids:
                self._drop(jid)
            self._save()
        for jid in ids:
            self._remove_files(jid)

    def make_download_token(self, job_id: str) -> str:
        now = self.clock()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise _unknown_render()
            if job.status != "done":
                raise ApiError(409, "not_ready", "This video is not ready to download yet.")
            self._tokens = {t: v for t, v in self._tokens.items() if v[1] > now}
            token = secrets.token_urlsafe(32)
            self._tokens[token] = (job_id, now + DOWNLOAD_TOKEN_TTL_SEC)
            return token

    def open_download(self, job_id: str, token: str) -> Path:
        now = self.clock()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise _unknown_render()
            valid = False
            for t, (jid, expires) in self._tokens.items():
                if hmac.compare_digest(t.encode(), token.encode()) and jid == job_id \
                        and expires > now:
                    valid = True
            if not valid:
                raise _bad_link()
            path = job.output_path if job.status == "done" else None
        if path is None or not path.exists():
            raise _unknown_render()
        return path

    def mark_downloaded(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.downloaded_at = self.clock()
                self._save()

    def sweep(self, now: float) -> None:
        ttl = self.store.ttl_seconds
        with self._lock:
            self._tokens = {t: v for t, v in self._tokens.items() if v[1] > now}
            old = [
                jid for jid, j in self._jobs.items()
                if (j.downloaded_at is not None and now - j.downloaded_at > POST_DOWNLOAD_KEEP_SEC)
                or (j.finished_at is not None and now - j.finished_at > ttl)
            ]
            for jid in old:
                self._drop(jid)
            if old:
                self._save()
        for jid in old:
            self._remove_files(jid)

    # ---- internals ----

    def _drop(self, job_id: str) -> None:
        """Forget a job (lock held). A running job is cancelled; the worker cleans up."""
        self._jobs.pop(job_id, None)
        self._pending = deque(p for p in self._pending if p[0] != job_id)
        ev = self._cancels.get(job_id)
        if ev:
            ev.set()
        for t in [t for t, v in self._tokens.items() if v[0] == job_id]:
            del self._tokens[t]

    def _out_path(self, job_id: str) -> Path:
        return self.store.renders_dir / f"{job_id}.mp4"

    def _remove_files(self, job_id: str) -> None:
        self._out_path(job_id).unlink(missing_ok=True)
        shutil.rmtree(self.store.work_dir / job_id, ignore_errors=True)

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._pending and not self._stopping:
                    self._lock.wait()
                if self._stopping:
                    return
                job_id, el = self._pending.popleft()
                job = self._jobs[job_id]
                job.status = "running"
                cancel = self._cancels[job_id] = threading.Event()
                self._save()
            try:
                self._render_one(job_id, job, el, cancel)
            except Exception:  # the worker must never die
                logger.exception("render worker error")
                self._finish(job_id, "failed", FAILED)

    def _render_one(self, job_id: str, job: Job, el: EditList,
                    cancel: threading.Event) -> None:
        work = self.store.work_dir / job_id
        status, message = "done", None
        try:
            if shutil.disk_usage(self.store.data_dir).free < MIN_FREE_BYTES:
                raise RenderError(OUT_OF_DISK)
            files = self.store.files()
            try:
                validate_for_render(el, files)
            except ApiError as e:
                raise RenderError(e.message) from None
            work.mkdir(parents=True, exist_ok=True)

            def on_progress(fraction: float) -> None:
                with self._lock:
                    job.percent = max(job.percent, min(int(fraction * 100), 100))

            self.render_fn(el, files, self._out_path(job_id), work, on_progress, cancel,
                           self.settings.studio_render_timeout_min * 60)
        except RenderError as e:
            status, message = "failed", e.message
        except Exception:
            logger.exception("unexpected render failure")
            status, message = "failed", FAILED
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self._finish(job_id, status, message)

    def _finish(self, job_id: str, status: str, message: str | None) -> None:
        out = self._out_path(job_id)
        with self._lock:
            self._cancels.pop(job_id, None)
            job = self._jobs.get(job_id)
            if job is not None:
                if status == "done" and not out.exists():
                    status, message = "failed", FAILED
                if self._stopping and status != "done":
                    message = RESTARTED  # shutdown cancelled it, not the user
                job.status, job.message = status, message
                job.finished_at = self.clock()
                if status == "done":
                    job.percent = 100
                    job.output_path = out
                self._save()
            keep = job is not None and status == "done"
        if not keep:  # failed, or deleted while it ran
            self._remove_files(job_id)


_queues: dict[str, RenderQueue] = {}
_queues_lock = threading.Lock()


def get_queue(settings: Settings = Depends(get_settings),
              store: Store = Depends(get_store)) -> RenderQueue:
    key = str(settings.studio_data_dir)
    with _queues_lock:
        if key not in _queues:
            _queues[key] = RenderQueue(store, settings)
        return _queues[key]
