"""Smoke check for the prod image. Run it inside the image as its normal (non-root) user:

    docker run --rm studio-render python scripts/prodcheck.py

It makes a short click track as wav, mp3 and m4a, runs the real beat analysis on each, and
encodes one second of H.264 video into the data dir. It prints one line per check and exits
non-zero if any check fails. It needs no secrets and starts no server.
"""
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

BPM = 120
CLICK_SEC = 10.0


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True,
                   capture_output=True, timeout=120)


def _click_track(path: Path) -> Path:
    period = 60.0 / BPM
    expr = f"sin(2*PI*1000*t)*lt(mod(t,{period:.6f}),0.05)"
    _ffmpeg(["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=22050:d={CLICK_SEC}", "-ac", "1",
             str(path)])
    return path


def main() -> int:
    data_dir = Path(os.environ.get("STUDIO_DATA_DIR", "/tmp/studio"))
    work = data_dir / "prodcheck"
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            results.append((name, True, detail or ""))
        except Exception as e:  # report every failure, keep going
            results.append((name, False, f"{type(e).__name__}: {e}"))
            traceback.print_exc(file=sys.stderr)

    print(f"user uid={os.getuid()} gid={os.getgid()} home={os.environ.get('HOME')}")
    print(f"data dir {data_dir}")

    def writable() -> str:
        work.mkdir(parents=True, exist_ok=True)
        (work / "probe").write_bytes(b"ok")
        return str(work)

    check("data dir is writable", writable)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the image's /app
    from app.beats import analyze  # the real code path, librosa and numba included

    for ext in ("wav", "mp3", "m4a"):
        def beats(ext=ext) -> str:
            a = analyze(_click_track(work / f"click.{ext}"))
            if abs(a.tempo - BPM) > 2 or len(a.beats) < 15:
                raise AssertionError(f"tempo {a.tempo}, {len(a.beats)} beats")
            return f"tempo {a.tempo}, {len(a.beats)} beats, {a.duration_sec} s"

        check(f"beat analysis of {ext}", beats)

    def encode() -> str:
        out = work / "one_second.mp4"
        _ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-t", "1",
                 "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)])
        codec = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,nb_frames", "-of", "csv=p=0", str(out)],
            check=True, capture_output=True, text=True, timeout=60).stdout.strip()
        if not codec.startswith("h264"):
            raise AssertionError(f"unexpected stream: {codec}")
        return f"{out.stat().st_size} bytes ({codec})"

    check("libx264 encode into the data dir", encode)

    shutil.rmtree(work, ignore_errors=True)
    for name, ok, detail in results:
        print(f"{'OK  ' if ok else 'FAIL'} {name}: {detail}")
    failed = [r for r in results if not r[1]]
    print("prodcheck passed" if not failed else f"prodcheck FAILED ({len(failed)} checks)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
