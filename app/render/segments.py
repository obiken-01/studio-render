"""Stage 1: argument lists that turn one timeline segment into a normalised mp4."""
from pathlib import Path

from app.editlist import FPS, XFADE_FRAMES, PhotoSegment, VideoSegment
from app.models import FileMeta
from app.render.crop import cover_crop

# A crossfade overlaps two segments by XFADE_FRAMES (defined with the validation rules), so
# the segment before it is rendered that many frames longer to keep the total length unchanged.
ZOOM_MAX = 1.15

_ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", str(FPS),
           "-video_track_timescale", "15360"]


def base_frames(seg) -> int:
    return max(1, round(seg.durationSec * FPS))


def segment_frames(seg) -> int:
    """Frames rendered in stage 1 (includes the crossfade overlap, if any)."""
    extra = XFADE_FRAMES if getattr(seg, "transitionOut", "cut") == "crossfade" else 0
    return base_frames(seg) + extra


def video_args(seg: VideoSegment, meta: FileMeta, out_w: int, out_h: int, out: Path) -> list[str]:
    sw, sh, x, y = cover_crop(meta.width, meta.height, out_w, out_h,
                              seg.crop.focusX, seg.crop.focusY)
    vf = f"scale={sw}:{sh},crop={out_w}:{out_h}:{x}:{y},fps={FPS},setsar=1,format=yuv420p"
    return ["-ss", f"{seg.source.inSec:.6f}", "-i", str(meta.path), "-vf", vf, "-an",
            "-frames:v", str(segment_frames(seg)), *_ENCODE, str(out)]


def _motion_exprs(kind: str, frames: int) -> tuple[str, str, str]:
    """zoompan z, x and y expressions. `on` is the output frame number."""
    den = max(frames - 1, 1)
    spare = f"(iw-iw/{ZOOM_MAX})"
    centre_y = "ih/2-(ih/zoom/2)"
    if kind == "zoom_in":
        return f"1+{ZOOM_MAX - 1:.2f}*on/{den}", "iw/2-(iw/zoom/2)", centre_y
    if kind == "zoom_out":
        return f"{ZOOM_MAX}-{ZOOM_MAX - 1:.2f}*on/{den}", "iw/2-(iw/zoom/2)", centre_y
    if kind == "pan_right":
        return f"{ZOOM_MAX}", f"{spare}*on/{den}", centre_y
    return f"{ZOOM_MAX}", f"{spare}*(1-on/{den})", centre_y  # pan_left


def photo_args(seg: PhotoSegment, image: Path, img_w: int, img_h: int,
               out_w: int, out_h: int, out: Path) -> list[str]:
    """`image` is an upright (EXIF already applied) picture of size img_w x img_h."""
    frames = segment_frames(seg)
    # Work at twice the output size so zoompan's whole-pixel steps do not show as jitter.
    sw, sh, x, y = cover_crop(img_w, img_h, out_w * 2, out_h * 2,
                              seg.crop.focusX, seg.crop.focusY)
    z, zx, zy = _motion_exprs(seg.motion.kind, frames)
    vf = (f"scale={sw}:{sh},crop={out_w * 2}:{out_h * 2}:{x}:{y},"
          f"zoompan=z='{z}':x='{zx}':y='{zy}':d={frames}:s={out_w}x{out_h}:fps={FPS},"
          f"setsar=1,format=yuv420p")
    return ["-loop", "1", "-i", str(image), "-vf", vf, "-an",
            "-frames:v", str(frames), *_ENCODE, str(out)]
