import json
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from fastapi import Depends
from PIL import Image, ImageOps

from app.config import Settings, get_settings
from app.errors import ApiError
from app.models import FileMeta, Upload
from app.probe import probe_media

CHUNK_SIZE = 8 * 1024 * 1024

ALLOWED_EXTENSIONS = {
    "video": {"mp4", "mov"},
    "photo": {"jpg", "jpeg", "png", "heic", "heif"},
    "audio": {"mp3", "m4a", "wav"},
}
_HEIC = {"heic", "heif"}
_GB = 1024**3


def _gb(n: int) -> str:
    return f"{n / _GB:g}"


def _display_name(filename: str) -> str:
    """The client's name, for display only. It is never used to build a path."""
    base = re.split(r"[\\/]", filename)[-1]
    return "".join(c for c in base if c.isprintable())[:200]


def _new_id() -> str:
    return "up_" + secrets.token_urlsafe(12)


def _unknown_upload() -> ApiError:
    return ApiError(404, "unknown_upload",
                    "This upload was not found. It may have expired. Please start again.")


def _unknown_file() -> ApiError:
    return ApiError(404, "unknown_file", "This file was not found. It may have expired.")


def _disk_full() -> ApiError:
    return ApiError(507, "disk_full",
                    "The server is out of storage space right now. Please try again later.")


class Store:
    def __init__(self, data_dir: Path, ttl_hours: float, max_file_bytes: int,
                 max_project_bytes: int, *, chunk_size: int = CHUNK_SIZE):
        self.data_dir = Path(data_dir)
        self.ttl_seconds = ttl_hours * 3600
        self.max_file_bytes = max_file_bytes
        self.max_project_bytes = max_project_bytes
        self.chunk_size = chunk_size
        self.uploads_dir = self.data_dir / "uploads"
        self.files_dir = self.data_dir / "files"
        self.renders_dir = self.data_dir / "renders"
        self.work_dir = self.data_dir / "work"
        for d in (self.uploads_dir, self.files_dir, self.renders_dir, self.work_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._uploads: dict[str, Upload] = {}
        self._files: dict[str, FileMeta] = {}
        self._state_path = self.data_dir / "state.json"
        self._load()

    # ---- persistence (callers hold the lock, except __init__) ----

    def _load(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        for u in raw.get("uploads", []):
            up = Upload(**{**u, "received": set(u["received"])})
            if self._part_path(up).exists():
                self._uploads[up.upload_id] = up
        for f in raw.get("files", []):
            meta = FileMeta(**{**f, "path": Path(f["path"])})
            if meta.path.exists():
                self._files[meta.file_id] = meta

    def _save(self) -> None:
        state = {
            "uploads": [{**u.__dict__, "received": sorted(u.received)}
                        for u in self._uploads.values()],
            "files": [{**f.__dict__, "path": str(f.path)} for f in self._files.values()],
        }
        tmp = self._state_path.with_name("state.json.tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self._state_path)

    def _part_path(self, up: Upload) -> Path:
        return self.uploads_dir / f"{up.upload_id}.{up.ext}.part"

    @staticmethod
    def _unlink(path: Path) -> None:
        path.unlink(missing_ok=True)

    def _reserved_bytes(self) -> int:
        return (sum(f.size for f in self._files.values())
                + sum(u.size for u in self._uploads.values()))

    def total_bytes(self) -> int:
        """Completed files plus the full declared size of uploads still in progress."""
        with self._lock:
            return self._reserved_bytes()

    # ---- uploads ----

    def create_upload(self, filename: str, size: int, kind: str) -> Upload:
        name = _display_name(filename)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if kind not in ALLOWED_EXTENSIONS or ext not in ALLOWED_EXTENSIONS[kind]:
            raise ApiError(
                415, "unsupported_type",
                "This file type isn't supported. Use MP4 or MOV for video, JPG, PNG or HEIC "
                "for photos, and MP3, M4A or WAV for audio.")
        if size <= 0:
            raise ApiError(422, "invalid_request", "The file size is not valid.")
        if size > self.max_file_bytes:
            raise ApiError(413, "file_too_large",
                           f"This file is too large. The limit is {_gb(self.max_file_bytes)} GB.")
        upload = Upload(
            upload_id=_new_id(), filename=name, size=size, kind=kind, ext=ext,
            chunk_size=self.chunk_size, total_chunks=-(-size // self.chunk_size),
            received=set(), created_at=time.time())
        with self._lock:
            if self._reserved_bytes() + size > self.max_project_bytes:
                raise ApiError(
                    413, "project_too_large",
                    f"Your project is full. The limit is {_gb(self.max_project_bytes)} GB "
                    "in total. Remove some files and try again.")
            if shutil.disk_usage(self.data_dir).free < size:
                raise _disk_full()
            part = self._part_path(upload)
            try:
                with open(part, "wb") as fh:
                    fh.truncate(size)
            except OSError:
                self._unlink(part)
                raise _disk_full() from None
            self._uploads[upload.upload_id] = upload
            self._save()
        return upload

    def get_upload(self, upload_id: str) -> Upload:
        with self._lock:
            return self._get_upload(upload_id)

    def _get_upload(self, upload_id: str) -> Upload:
        up = self._uploads.get(upload_id)
        if up is None:
            raise _unknown_upload()
        return up

    def write_chunk(self, upload_id: str, n: int, data: Iterable[bytes]) -> Upload:
        with self._lock:
            up = self._get_upload(upload_id)
            if not 0 <= n < up.total_chunks:
                raise ApiError(400, "bad_chunk", "That part of the file does not belong here.")
            # A resend overwrites the region, so it is not trustworthy until it fully lands.
            if n in up.received:
                up.received.discard(n)
                self._save()
        offset = n * up.chunk_size
        expected = min(up.chunk_size, up.size - offset)
        written = 0
        try:
            with open(self._part_path(up), "r+b") as fh:
                fh.seek(offset)
                for piece in data:
                    if written + len(piece) > expected:
                        if written + len(piece) > up.chunk_size:
                            raise ApiError(413, "chunk_too_large",
                                           "That part of the file is too big to upload at once.")
                        raise ApiError(400, "bad_chunk",
                                       "That part of the file arrived with the wrong size.")
                    fh.write(piece)
                    written += len(piece)
        except FileNotFoundError:
            raise _unknown_upload() from None
        except OSError:
            raise _disk_full() from None
        if written != expected:
            raise ApiError(400, "bad_chunk", "That part of the file arrived with the wrong size.")
        with self._lock:
            up = self._get_upload(upload_id)
            up.received.add(n)
            self._save()
            return up

    def complete_upload(self, upload_id: str) -> FileMeta:
        with self._lock:
            up = self._get_upload(upload_id)
            missing = [i for i in range(up.total_chunks) if i not in up.received]
            if missing:
                raise ApiError(409, "upload_incomplete",
                               "Some parts of the file have not arrived yet.",
                               {"missing": missing})
            # Taking it out of the registry makes a second complete a clean 404.
            del self._uploads[upload_id]
            self._save()
        part = self._part_path(up)
        final = self.files_dir / f"{up.upload_id}.{up.ext}"
        try:
            os.replace(part, final)
            if up.ext in _HEIC:
                final = self._heic_to_jpeg(final)
            info = probe_media(final, up.kind)
            meta = FileMeta(
                file_id=up.upload_id, kind=up.kind, path=final, filename=up.filename,
                size=final.stat().st_size, duration_sec=info["duration_sec"],
                width=info["width"], height=info["height"], fps=info["fps"],
                rotation=info["rotation"], created_at=time.time())
        except BaseException:
            for p in (part, final, self.files_dir / f"{up.upload_id}.jpg",
                      self.files_dir / f"{up.upload_id}.{up.ext}"):
                self._unlink(p)
            raise
        with self._lock:
            self._files[meta.file_id] = meta
            self._save()
        return meta

    @staticmethod
    def _heic_to_jpeg(src: Path) -> Path:
        dest = src.with_suffix(".jpg")
        try:
            with Image.open(src) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.save(dest, "JPEG", quality=95)
        except Exception:
            dest.unlink(missing_ok=True)
            raise ApiError(
                422, "unreadable_file",
                "This file can't be read as a video, photo or audio file.") from None
        src.unlink()
        return dest

    # ---- files ----

    def get_file(self, file_id: str) -> FileMeta:
        with self._lock:
            meta = self._files.get(file_id)
        if meta is None:
            raise _unknown_file()
        return meta

    def files(self) -> dict[str, FileMeta]:
        with self._lock:
            return dict(self._files)

    def delete_file(self, file_id: str) -> None:
        with self._lock:
            meta = self._files.pop(file_id, None)
            if meta is None:
                raise _unknown_file()
            self._unlink(meta.path)
            self._save()

    def clear(self) -> None:
        with self._lock:
            for meta in self._files.values():
                self._unlink(meta.path)
            for up in self._uploads.values():
                self._unlink(self._part_path(up))
            self._files.clear()
            self._uploads.clear()
            self._save()

    def sweep(self, now: float) -> int:
        """Remove completed files and unfinished uploads older than the TTL."""
        removed = 0
        with self._lock:
            for fid, meta in list(self._files.items()):
                if now - meta.created_at > self.ttl_seconds:
                    self._unlink(meta.path)
                    del self._files[fid]
                    removed += 1
            for uid, up in list(self._uploads.items()):
                if now - up.created_at > self.ttl_seconds:
                    self._unlink(self._part_path(up))
                    del self._uploads[uid]
                    removed += 1
            registered = {f.path for f in self._files.values()}
            registered |= {self._part_path(u) for u in self._uploads.values()}
            # Anything on disk the registry does not know about (crash, lost state.json).
            for d in (self.uploads_dir, self.files_dir):
                for path in d.iterdir():
                    try:
                        if (path.is_file() and path not in registered
                                and now - path.stat().st_mtime > self.ttl_seconds):
                            path.unlink()
                            removed += 1
                    except FileNotFoundError:
                        pass
            if removed:
                self._save()
        return removed


_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()


def get_store(settings: Settings = Depends(get_settings)) -> Store:
    key = str(settings.studio_data_dir)
    with _stores_lock:
        if key not in _stores:
            _stores[key] = Store(
                settings.studio_data_dir, settings.studio_ttl_hours,
                int(settings.studio_max_file_gb * _GB), int(settings.studio_max_project_gb * _GB))
        return _stores[key]
