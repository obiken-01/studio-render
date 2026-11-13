"""Stage 2: join the normalised segments and add the music."""
import threading
from collections.abc import Callable
from pathlib import Path

from app.editlist import FPS, XFADE_FRAMES, XFADE_SEC, EditList
from app.models import FileMeta
from app.render.ffmpeg import INPUT_PROTOCOLS, run_ffmpeg

_TB = "1/15360"


def _concat_list(parts: list[Path], list_path: Path) -> None:
    lines = []
    for p in parts:
        escaped = str(p).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _filter_graph(el: EditList, frames: list[int]) -> tuple[str, str]:
    """Chain the inputs left to right: xfade at crossfade joins, concat at cuts."""
    n = len(frames)
    parts = [f"[{i}:v]setpts=PTS-STARTPTS,fps={FPS},settb={_TB}[s{i}]" for i in range(n)]
    acc, acc_frames = "s0", frames[0]
    for i in range(1, n):
        out = f"j{i}"
        if el.timeline[i - 1].transitionOut == "crossfade":
            offset = (acc_frames - XFADE_FRAMES) / FPS
            parts.append(
                f"[{acc}][s{i}]xfade=transition=fade:duration={XFADE_SEC:.6f}"
                f":offset={offset:.6f}[x{i}]")
            parts.append(f"[x{i}]fps={FPS},settb={_TB}[{out}]")
            acc_frames += frames[i] - XFADE_FRAMES
        else:
            parts.append(f"[{acc}][s{i}]concat=n=2:v=1:a=0[c{i}]")
            parts.append(f"[c{i}]fps={FPS},settb={_TB}[{out}]")
            acc_frames += frames[i]
        acc = out
    return ";".join(parts), acc


def assemble(el: EditList, parts: list[Path], frames: list[int], music: FileMeta | None,
             out_path: Path, workdir: Path, on_progress: Callable[[float], None],
             cancel: threading.Event, deadline: float) -> None:
    crossfades = any(s.transitionOut == "crossfade" for s in el.timeline[:-1])
    total_frames = sum(round(s.durationSec * FPS) or 1 for s in el.timeline)
    total_sec = total_frames / FPS

    args: list[str] = []
    if crossfades:
        for p in parts:
            args += [*INPUT_PROTOCOLS, "-i", str(p)]
    else:
        list_path = workdir / "concat.txt"
        _concat_list(parts, list_path)
        args += [*INPUT_PROTOCOLS, "-f", "concat", "-safe", "0", "-i", str(list_path)]

    if music is not None:
        dur = el.music.endSec - el.music.startSec
        args += [*INPUT_PROTOCOLS, "-ss", f"{el.music.startSec:.6f}", "-t", f"{dur:.6f}",
                 "-i", str(music.path)]

    if crossfades:
        graph, label = _filter_graph(el, frames)
        args += ["-filter_complex", graph, "-map", f"[{label}]",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        args += ["-map", "0:v:0", "-c:v", "copy"]

    if music is not None:
        fade = min(el.music.fadeOutSec, dur)
        if fade > 0:
            args += ["-af", f"afade=t=out:st={dur - fade:.6f}:d={fade:.6f}"]
        args += ["-map", f"{len(parts) if crossfades else 1}:a:0",
                 "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        args += ["-an"]

    args += ["-frames:v", str(total_frames), "-movflags", "+faststart", str(out_path)]
    run_ffmpeg(args, total_sec, on_progress, cancel, deadline)
