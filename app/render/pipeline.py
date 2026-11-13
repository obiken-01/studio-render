import logging
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from app.editlist import FPS, EditList
from app.models import FileMeta
from app.render.assemble import assemble
from app.render.ffmpeg import RenderError, run_ffmpeg
from app.render.segments import photo_args, segment_frames, video_args

logger = logging.getLogger(__name__)

FAILED = "The video could not be rendered."


def _clear(workdir: Path) -> None:
    """Empty `workdir`. A missing workdir is fine (it must not mask the real error)."""
    try:
        children = list(workdir.iterdir())
    except FileNotFoundError:
        return
    for child in children:
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def render_edit_list(el: EditList, files: Mapping[str, FileMeta], out_path: Path, workdir: Path,
                     on_progress: Callable[[float], None], cancel: threading.Event,
                     timeout_sec: float) -> None:
    """Render a validated edit list to `out_path`. Always empties `workdir` on exit."""
    workdir = Path(workdir)
    deadline = time.monotonic() + timeout_sec
    last = 0.0

    def report(fraction: float) -> None:
        nonlocal last
        last = max(last, min(max(fraction, 0.0), 1.0))
        on_progress(last)

    try:
        _render(el, files, Path(out_path), workdir, report, cancel, deadline)
        report(1.0)
    except RenderError:
        Path(out_path).unlink(missing_ok=True)
        raise
    except Exception:
        Path(out_path).unlink(missing_ok=True)
        logger.exception("unexpected render failure")
        raise RenderError(FAILED) from None
    finally:
        _clear(workdir)


def _render(el, files, out_path, workdir, report, cancel, deadline) -> None:
    n = len(el.timeline)
    out_w, out_h = el.project.width, el.project.height
    if any(seg.type == "empty" for seg in el.timeline):
        raise RenderError(FAILED)

    parts: list[Path] = []
    frames: list[int] = []
    for i, seg in enumerate(el.timeline):
        part = workdir / f"seg_{i:03d}.mp4"
        meta = files[seg.source.fileId]
        if seg.type == "video":
            args = video_args(seg, meta, out_w, out_h, part)
        else:
            # Photos are stored as upright RGB JPEGs at upload, so ffmpeg reads them directly.
            args = photo_args(seg, meta.path, meta.width, meta.height, out_w, out_h, part)
        nframes = segment_frames(seg)
        run_ffmpeg(args, nframes / FPS,
                   lambda f, i=i: report(0.9 * (i + f) / n), cancel, deadline)
        parts.append(part)
        frames.append(nframes)
        report(0.9 * (i + 1) / n)

    music = files[el.music.fileId] if el.audioMode == "music" else None
    assemble(el, parts, frames, music, out_path, workdir,
             lambda f: report(0.9 + 0.1 * f), cancel, deadline)
