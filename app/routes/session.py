from fastapi import APIRouter, Depends

from app.auth import require_user
from app.errors import ok

router = APIRouter()


@router.get("/health")
def health():
    return ok({"status": "ok"})


@router.get("/session")
def session(_user_id: str = Depends(require_user)):
    return ok({"allowed": True})
