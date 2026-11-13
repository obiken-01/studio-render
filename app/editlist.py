from collections.abc import Mapping
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from app.errors import ApiError
from app.models import FileMeta

ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "4:5": (1080, 1350)}
FPS = 30
XFADE_SEC = 0.15
FRAME = 1 / 30
CONTIGUITY_TOL = 0.001


def snap(t: float) -> float:
    """Round a time to the nearest frame on the 30 fps grid."""
    return round(round(t * FPS) / FPS, 6)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Project(_Strict):
    aspect: str
    width: int
    height: int
    fps: int


class Music(_Strict):
    fileId: str
    startSec: float = Field(ge=0)
    endSec: float
    fadeOutSec: float = Field(default=0.0, ge=0)
    beats: list[float] = Field(default_factory=list)


class Crop(_Strict):
    focusX: float = Field(default=0.5, ge=0, le=1)
    focusY: float = Field(default=0.5, ge=0, le=1)


class Motion(_Strict):
    kind: Literal["zoom_in", "zoom_out", "pan_left", "pan_right"]


class VideoSource(_Strict):
    fileId: str
    inSec: float
    outSec: float


class PhotoSource(_Strict):
    fileId: str


class VideoSegment(_Strict):
    type: Literal["video"]
    slotStartSec: float = Field(ge=0)
    durationSec: float = Field(gt=0)
    source: VideoSource
    crop: Crop = Field(default_factory=Crop)
    transitionOut: Literal["cut", "crossfade"] = "cut"


class PhotoSegment(_Strict):
    type: Literal["photo"]
    slotStartSec: float = Field(ge=0)
    durationSec: float = Field(gt=0)
    source: PhotoSource
    motion: Motion
    crop: Crop = Field(default_factory=Crop)
    transitionOut: Literal["cut", "crossfade"] = "cut"


class EmptySegment(_Strict):
    type: Literal["empty"]
    slotStartSec: float = Field(ge=0)
    durationSec: float = Field(gt=0)


Segment = Annotated[
    Union[VideoSegment, PhotoSegment, EmptySegment], Field(discriminator="type")
]


class EditList(_Strict):
    version: Literal[1]
    project: Project
    music: Music
    audioMode: Literal["music", "silent"]
    timeline: list[Segment] = Field(min_length=1)


def _err(code: str, message: str, details: dict | None = None) -> ApiError:
    return ApiError(422, code, message, details)


def validate_structure(el: EditList) -> None:
    """Rules that need no uploaded files."""
    p = el.project
    if ASPECTS.get(p.aspect) != (p.width, p.height) or p.fps != FPS:
        raise _err(
            "bad_project",
            "The video size is not one of the supported formats "
            "(9:16, 16:9 or 4:5 at 30 frames per second).",
        )

    expected = 0.0
    for i, seg in enumerate(el.timeline):
        if abs(seg.slotStartSec - expected) > CONTIGUITY_TOL:
            raise _err(
                "timeline_gap",
                f"Slot {i + 1} does not start where the previous slot ends.",
                {"index": i},
            )
        expected = seg.slotStartSec + seg.durationSec

    music_len = el.music.endSec - el.music.startSec
    if abs(expected - music_len) > FRAME:
        raise _err(
            "duration_mismatch",
            "The slots do not add up to the length of the chosen music section.",
            {"timelineSec": round(expected, 3), "musicSec": round(music_len, 3)},
        )

    last = el.timeline[-1]
    if getattr(last, "transitionOut", "cut") == "crossfade":
        raise _err(
            "bad_transition",
            "The last slot cannot fade into another slot because there is none after it.",
            {"index": len(el.timeline) - 1},
        )


def _lookup(files: Mapping[str, FileMeta], file_id: str, kind: str, label: str) -> FileMeta:
    meta = files.get(file_id)
    if meta is None:
        raise _err("unknown_file", f"{label} uses a file that was not found.", {"fileId": file_id})
    if meta.kind != kind:
        raise _err(
            "wrong_file_kind",
            f"{label} needs a {kind} file, but the chosen file is a {meta.kind}.",
            {"fileId": file_id},
        )
    return meta


def validate_for_render(el: EditList, files: Mapping[str, FileMeta]) -> None:
    empties = [i for i, s in enumerate(el.timeline) if s.type == "empty"]
    if empties:
        nums = ", ".join(str(i + 1) for i in empties)
        word = "Slot" if len(empties) == 1 else "Slots"
        raise _err(
            "unfilled_slots",
            f"{word} {nums} still need a photo or video before this can be rendered.",
            {"indexes": empties},
        )

    validate_structure(el)

    for i, seg in enumerate(el.timeline):
        label = f"Slot {i + 1}"
        if seg.type == "photo":
            _lookup(files, seg.source.fileId, "photo", label)
            continue
        meta = _lookup(files, seg.source.fileId, "video", label)
        src = seg.source
        length = meta.duration_sec or 0.0
        tail = XFADE_SEC if seg.transitionOut == "crossfade" else 0.0
        if (
            src.inSec < 0
            or src.inSec >= src.outSec
            or src.outSec + tail > length + 1e-9
            or abs((src.outSec - src.inSec) - seg.durationSec) > FRAME
        ):
            raise _err(
                "bad_trim",
                f"{label} uses a part of the video that does not exist or does not "
                "match the slot length.",
                {"index": i},
            )

    if el.audioMode == "music":
        _lookup(files, el.music.fileId, "audio", "The music")
