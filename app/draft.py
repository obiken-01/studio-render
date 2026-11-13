from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.beats import analyze_cached
from app.editlist import ASPECTS, FPS, EditList, Music, Project
from app.errors import ApiError
from app.fill import fill_slots
from app.models import FileMeta
from app.slots import build_slots, slots_from_template
from app.storage import Store

FADE_OUT_SEC = 1.5


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fileIds: list[str] = Field(min_length=1)
    musicFileId: str
    aspect: Literal["9:16", "16:9", "4:5"]
    pace: Literal["calm", "medium", "fast"]
    length: Literal[15, 30, 60, "all"]
    startSec: float | None = Field(default=None, ge=0)
    audioMode: Literal["music", "silent"]
    seed: int
    template: EditList | None = None


def _find(files: dict[str, FileMeta], file_id: str, kinds: set[str], what: str) -> FileMeta:
    meta = files.get(file_id)
    if meta is None:
        raise ApiError(422, "unknown_file",
                       f"{what} was not found. It may have expired. Please upload it again.",
                       {"fileId": file_id})
    if meta.kind not in kinds:
        raise ApiError(422, "wrong_file_kind",
                       f"{what} is not the right kind of file for this step.",
                       {"fileId": file_id})
    return meta


def build_draft(req: DraftRequest, store: Store) -> tuple[EditList, list[str]]:
    files = store.files()
    music_meta = _find(files, req.musicFileId, {"audio"}, "The chosen music")
    media = [_find(files, fid, {"video", "photo"}, "One of the chosen clips or photos")
             for fid in dict.fromkeys(req.fileIds)]
    videos = [m for m in media if m.kind == "video"]
    photos = [m for m in media if m.kind == "photo"]

    analysis = analyze_cached(music_meta)
    if req.template is not None:
        plan = slots_from_template(req.template, analysis.beats, req.startSec)
    else:
        length = None if req.length == "all" else req.length
        plan = build_slots(analysis.beats, req.startSec, length, req.pace)

    timeline, fill_warnings = fill_slots(plan.slots, videos, photos, req.seed)
    warnings = list(dict.fromkeys([*plan.warnings, *fill_warnings]))

    total = plan.end_sec - plan.start_sec
    width, height = ASPECTS[req.aspect]
    edit_list = EditList(
        version=1,
        project=Project(aspect=req.aspect, width=width, height=height, fps=FPS),
        music=Music(
            fileId=req.musicFileId, startSec=plan.start_sec, endSec=plan.end_sec,
            fadeOutSec=FADE_OUT_SEC if total >= 2 * FADE_OUT_SEC else round(total / 2, 3),
            beats=[b for b in analysis.beats if plan.start_sec <= b <= plan.end_sec + 1e-6]),
        audioMode=req.audioMode,
        timeline=timeline,
    )
    return edit_list, warnings
