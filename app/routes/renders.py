from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.auth import require_user
from app.editlist import EditList
from app.errors import ok
from app.render.worker import DOWNLOAD_TOKEN_TTL_SEC, RenderQueue, get_queue

router = APIRouter(dependencies=[Depends(require_user)])
# The download link is authorised by its token alone, so a browser can fetch it natively.
file_router = APIRouter()


@router.post("/renders", status_code=202)
def submit_render(body: EditList, queue: RenderQueue = Depends(get_queue)):
    return ok({"jobId": queue.submit(body).job_id})


@router.get("/renders/{job_id}")
def render_status(job_id: str, queue: RenderQueue = Depends(get_queue)):
    job, position = queue.get(job_id)
    return ok(job.public(position))


@router.post("/renders/{job_id}/download-link")
def download_link(job_id: str, queue: RenderQueue = Depends(get_queue)):
    token = queue.make_download_token(job_id)
    return ok({"url": f"/renders/{job_id}/file?token={token}",
               "expiresInSec": DOWNLOAD_TOKEN_TTL_SEC})


@router.delete("/renders/{job_id}")
def delete_render(job_id: str, queue: RenderQueue = Depends(get_queue)):
    queue.delete(job_id)
    return ok(None)


@file_router.get("/renders/{job_id}/file")
def download_file(job_id: str, token: str = "", queue: RenderQueue = Depends(get_queue)):
    path = queue.open_download(job_id, token)
    stamp = datetime.fromtimestamp(queue.clock(), UTC).strftime("%Y%m%d-%H%M")
    return FileResponse(path, media_type="video/mp4", filename=f"studio-{stamp}.mp4",
                        background=BackgroundTask(queue.mark_downloaded, job_id))
