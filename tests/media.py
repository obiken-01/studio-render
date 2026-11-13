"""Tiny generated media for tests. Everything is made by ffmpeg lavfi or Pillow."""
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


def make_video(path: Path, w: int = 1920, h: int = 1080, dur: float = 6.0, fps: int = 30,
               rotate: int = 0) -> Path:
    """H.264 test clip, no audio. With rotate=90 the stored stream stays w x h and only the
    display-matrix rotation is set (no re-encode), so players show it as h x w."""
    path = Path(path)
    target = path if rotate == 0 else path.with_name(path.stem + "_unrotated.mp4")
    _run([
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}",
        "-t", str(dur), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "38",
        "-pix_fmt", "yuv420p", str(target),
    ])
    if rotate != 0:
        _run(["-display_rotation", str(rotate), "-i", str(target), "-c", "copy", str(path)])
        target.unlink()
    return path


def make_video_with_cover(path: Path, w: int = 320, h: int = 240, dur: float = 2.0) -> Path:
    """An mp4 that also carries a 64x64 cover picture (an attached-picture video stream)."""
    path = Path(path)
    clip = make_video(path.with_name(path.stem + "_clip.mp4"), w, h, dur)
    cover = path.with_name(path.stem + "_cover.jpg")
    Image.new("RGB", (64, 64), (255, 0, 0)).save(cover, "JPEG")
    _run(["-i", str(cover), "-i", str(clip), "-map", "0", "-map", "1", "-c", "copy",
          "-disposition:0", "attached_pic", str(path)])
    clip.unlink()
    cover.unlink()
    return path


def make_photo(path: Path, w: int = 4000, h: int = 3000, exif_orientation: int = 1) -> Path:
    path = Path(path)
    img = Image.new("RGB", (w, h), (30, 90, 160))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, w // 2, h // 2), fill=(200, 60, 40))
    draw.ellipse((w // 3, h // 3, w * 2 // 3, h * 2 // 3), fill=(240, 220, 60))
    exif = Image.Exif()
    exif[0x0112] = exif_orientation
    img.save(path, "JPEG", quality=70, exif=exif)
    return path


def make_click_track(path: Path, bpm: int = 120, dur: float = 30.0) -> Path:
    path = Path(path)
    period = 60.0 / bpm
    expr = f"sin(2*PI*1000*t)*lt(mod(t,{period:.6f}),0.05)"
    _run(["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=22050:d={dur}", "-ac", "1", str(path)])
    return path


def make_silence(path: Path, dur: float = 10.0) -> Path:
    path = Path(path)
    _run(["-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", str(dur), str(path)])
    return path


def ffprobe_json(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)
