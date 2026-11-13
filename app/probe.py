import json
import logging
import math
import subprocess
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageOps

from app.errors import ApiError

logger = logging.getLogger(__name__)


def _unreadable() -> ApiError:
    return ApiError(
        422, "unreadable_file", "This file can't be read as a video, photo or audio file.")


def _ffprobe(path: Path) -> dict:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
             str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("ffprobe could not run")
        raise _unreadable() from None
    if proc.returncode != 0:
        logger.warning("ffprobe failed: %s", proc.stderr[-500:])
        raise _unreadable()
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise _unreadable() from None


def _positive_float(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _rate(value) -> float | None:
    try:
        f = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    try:
        return int(round(float((stream.get("tags") or {}).get("rotate", 0)))) % 360
    except (TypeError, ValueError):
        return 0


def _duration(info: dict, stream: dict) -> float:
    d = _positive_float(stream.get("duration")) or _positive_float(
        (info.get("format") or {}).get("duration"))
    if d is None:
        raise _unreadable()
    return d


def _probe_photo(path: Path) -> dict:
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            w, h = img.size
    except Exception:  # Pillow raises many types for broken files
        logger.warning("Pillow could not open %s", path.name, exc_info=True)
        raise _unreadable() from None
    if w <= 0 or h <= 0:
        raise _unreadable()
    return {"duration_sec": None, "width": w, "height": h, "fps": None, "rotation": 0}


def probe_media(path: Path, kind: str) -> dict:
    """Return duration_sec, width, height (display size), fps and rotation for a file."""
    path = Path(path)
    if kind == "photo":
        return _probe_photo(path)

    info = _ffprobe(path)
    wanted = "video" if kind == "video" else "audio"
    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == wanted), None)
    if stream is None:
        raise _unreadable()
    duration = _duration(info, stream)

    if kind == "audio":
        return {"duration_sec": duration, "width": None, "height": None, "fps": None,
                "rotation": 0}

    w, h = stream.get("width"), stream.get("height")
    if not isinstance(w, int) or not isinstance(h, int) or w <= 0 or h <= 0:
        raise _unreadable()
    rotation = _rotation(stream)
    if rotation in (90, 270):
        w, h = h, w
    fps = _rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate"))
    return {"duration_sec": duration, "width": w, "height": h, "fps": fps, "rotation": rotation}
