from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import require_user
from app.beats import analyze_cached
from app.draft import DraftRequest, build_draft
from app.errors import ApiError, ok
from app.storage import Store, get_store

router = APIRouter(dependencies=[Depends(require_user)])


class AnalyzeRequest(BaseModel):
    musicFileId: str


@router.post("/projects/analyze")
def analyze_music(body: AnalyzeRequest, store: Store = Depends(get_store)):
    meta = store.files().get(body.musicFileId)
    if meta is None:
        raise ApiError(422, "unknown_file",
                       "The chosen music was not found. It may have expired.")
    if meta.kind != "audio":
        raise ApiError(422, "wrong_file_kind", "The chosen file is not a song.")
    a = analyze_cached(meta)
    return ok({"beats": a.beats, "tempo": a.tempo, "durationSec": a.duration_sec})


@router.post("/projects/draft")
def make_draft(body: DraftRequest, store: Store = Depends(get_store)):
    edit_list, warnings = build_draft(body, store)
    return ok({"editList": edit_list.model_dump(), "warnings": warnings})
