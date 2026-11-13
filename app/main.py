import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.errors import register_error_handlers
from app.middleware import RequestGuard
from app.render.worker import RenderQueue, get_queue
from app.routes import projects, renders, session, uploads
from app.storage import Store, get_store

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SEC = 600


def _sweep_loop(store: Store, queue: RenderQueue, stop: threading.Event,
                interval: float) -> None:
    while not stop.wait(interval):
        try:
            now = time.time()
            store.sweep(now)
            queue.sweep(now)
        except Exception:
            logger.exception("sweep failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        store = get_store(settings)
        queue = get_queue(settings, store)
        stop = threading.Event()
        sweeper = threading.Thread(target=_sweep_loop, name="sweeper", daemon=True,
                                   args=(store, queue, stop, SWEEP_INTERVAL_SEC))
        queue.start()
        sweeper.start()
        try:
            yield
        finally:
            stop.set()
            sweeper.join(timeout=5)
            queue.stop()

    app = FastAPI(title="studio-render", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    # Added first so it sits inside CORS: its 413 and 500 answers carry the CORS headers.
    app.add_middleware(RequestGuard)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )
    register_error_handlers(app)
    app.include_router(session.router)
    app.include_router(uploads.router)
    app.include_router(projects.router)
    app.include_router(renders.router)
    app.include_router(renders.file_router)
    return app


app = create_app()
