from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.auth import require_user
from app.errors import ApiError, ok
from app.render.worker import RenderQueue, get_queue
from app.storage import Store, get_store

router = APIRouter(dependencies=[Depends(require_user)])


class NewUpload(BaseModel):
    filename: str
    size: int
    kind: str


@router.post("/uploads")
def create_upload(body: NewUpload, store: Store = Depends(get_store)):
    up = store.create_upload(body.filename, body.size, body.kind)
    return ok({"uploadId": up.upload_id, "chunkSize": up.chunk_size,
               "totalChunks": up.total_chunks})


@router.put("/uploads/{upload_id}/chunks/{n}")
async def put_chunk(upload_id: str, n: int, request: Request, store: Store = Depends(get_store)):
    up = store.get_upload(upload_id)
    too_big = ApiError(413, "chunk_too_large",
                       "That part of the file is too big to upload at once.")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > up.chunk_size:
        raise too_big
    buf = bytearray()  # never holds more than one chunk
    async for piece in request.stream():
        buf += piece
        if len(buf) > up.chunk_size:
            raise too_big
    up = await run_in_threadpool(store.write_chunk, upload_id, n, [buf])
    return ok({"received": sorted(up.received)})


@router.get("/uploads/{upload_id}")
def upload_status(upload_id: str, store: Store = Depends(get_store)):
    up = store.get_upload(upload_id)
    return ok({"received": sorted(up.received), "totalChunks": up.total_chunks})


@router.delete("/uploads/{upload_id}")
def cancel_upload(upload_id: str, store: Store = Depends(get_store)):
    store.delete_upload(upload_id)
    return ok(None)


@router.post("/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, store: Store = Depends(get_store)):
    return ok(store.complete_upload(upload_id).public())


@router.delete("/files/{file_id}")
def delete_file(file_id: str, store: Store = Depends(get_store)):
    store.delete_file(file_id)
    return ok(None)


@router.get("/projects/current")
def current_project(store: Store = Depends(get_store), queue: RenderQueue = Depends(get_queue)):
    """Everything the server holds for the project, so a reloaded page can pick up again."""
    files = sorted(store.files().values(), key=lambda f: f.created_at)
    return ok({
        "files": [f.public() for f in files],
        "uploads": [u.public() for u in store.uploads()],
        "renders": [job.public(position) for job, position in queue.jobs()],
        "limits": {"maxFileBytes": store.max_file_bytes,
                   "maxProjectBytes": store.max_project_bytes,
                   "ttlHours": store.ttl_hours, "chunkSize": store.chunk_size},
        "usedBytes": store.total_bytes(),
    })


@router.delete("/projects/current")
def clear_project(store: Store = Depends(get_store), queue: RenderQueue = Depends(get_queue)):
    queue.clear()
    store.clear()
    return ok(None)
