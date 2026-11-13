import logging
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """A render failed. The message is safe to show to the user."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _read_progress(stream, total_sec: float, on_progress: Callable[[float], None]) -> None:
    for line in stream:
        key, _, value = line.strip().partition("=")
        if key != "out_time_us":
            continue
        try:
            us = int(value)
        except ValueError:
            continue
        if us < 0 or total_sec <= 0:
            continue
        try:
            on_progress(min(us / 1_000_000 / total_sec, 1.0))
        except Exception:
            logger.exception("progress callback failed")


def run_ffmpeg(args: list[str], total_sec: float, on_progress: Callable[[float], None],
               cancel: threading.Event, deadline: float) -> None:
    """Run ffmpeg with `args`, reporting 0..1 progress, until done, cancelled or past deadline.

    `deadline` is a time.monotonic() value.
    """
    if cancel.is_set():
        raise RenderError("The render was cancelled.")
    if time.monotonic() >= deadline:
        raise RenderError("The render took too long.")

    cmd = ["ffmpeg", "-v", "error", "-progress", "pipe:1", "-nostats", "-y", *args]
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                                    stdin=subprocess.DEVNULL)
        except OSError:
            logger.exception("ffmpeg could not start")
            raise RenderError("The video could not be rendered.") from None

        reader = threading.Thread(
            target=_read_progress, args=(proc.stdout, total_sec, on_progress), daemon=True)
        reader.start()
        stop_reason = None
        try:
            while proc.poll() is None:
                if cancel.is_set():
                    stop_reason = "The render was cancelled."
                elif time.monotonic() >= deadline:
                    stop_reason = "The render took too long."
                if stop_reason:
                    proc.kill()
                    break
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            reader.join(timeout=5)
            if proc.stdout:
                proc.stdout.close()

        if stop_reason:
            raise RenderError(stop_reason)
        if proc.returncode != 0:
            err.seek(0)
            tail = err.read()[-2000:].decode("utf-8", errors="replace")
            logger.error("ffmpeg exited with %s: %s", proc.returncode, tail)
            raise RenderError("The video could not be rendered.")
