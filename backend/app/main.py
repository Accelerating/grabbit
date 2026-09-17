from __future__ import annotations

import logging
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.system import router as system_router
from app.api.tasks import router as tasks_router
from app.api.files import router as files_router, directory_router
from app.api.torrents import router as torrents_router
from app.api.cookies import router as cookies_router
from app.api.video import router as video_router
from app.api.events import create_events_router
from app.core.config import get_settings
from app.core.errors import ApiError, api_error_handler
from app.engines.aria2 import Aria2Engine
from app.services.scheduler import Scheduler
from app.services.video_analysis import VideoAnalysisWorker
from app.services.file_operations import FileOperationWorker
from app.services.media_probe import MediaProbeWorker
from app.services.dependency_install import DependencyInstallWorker
from app.services.event_bus import EventBus

logger = logging.getLogger("grabbit")
logging.basicConfig(level=logging.INFO, format="%(message)s")
settings = get_settings()
event_bus = EventBus()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.download_root.mkdir(parents=True, exist_ok=True)
    settings.private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    engine = Aria2Engine(state_dir=settings.private_root / "aria2", download_root=settings.download_root)
    scheduler = Scheduler(engine, settings, event_bus=event_bus)
    analysis_worker = VideoAnalysisWorker(settings)
    file_worker = FileOperationWorker(settings)
    media_worker = MediaProbeWorker(settings)
    install_worker = DependencyInstallWorker(scheduler)
    _app.state.scheduler = scheduler
    _app.state.analysis_worker = analysis_worker
    _app.state.event_bus = event_bus
    await scheduler.start()
    await analysis_worker.start()
    await file_worker.start()
    await media_worker.start()
    await install_worker.start()
    try:
        yield
    finally:
        await install_worker.close()
        await media_worker.close()
        await file_worker.close()
        await analysis_worker.close()
        await scheduler.close()


app = FastAPI(title="Grabbit", lifespan=lifespan)
app.add_exception_handler(ApiError, api_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, _exc: RequestValidationError) -> JSONResponse:
    return await api_error_handler(request, ApiError(422, "VALIDATION_ERROR", "Invalid request"))


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = str(uuid4())
    started = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    logger.info(json.dumps({
        "event": "http_request", "request_id": request.state.request_id,
        "method": request.method, "route": request.url.path,
        "status": response.status_code, "duration_ms": round((time.monotonic() - started) * 1000),
    }))
    return response


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


app.include_router(auth_router, prefix="/api/v1")
app.include_router(system_router, prefix="/api/v1")
app.include_router(tasks_router, prefix="/api/v1")
app.include_router(files_router, prefix="/api/v1")
app.include_router(directory_router, prefix="/api/v1")
app.include_router(torrents_router, prefix="/api/v1")
app.include_router(cookies_router, prefix="/api/v1")
app.include_router(video_router, prefix="/api/v1")
app.include_router(create_events_router(event_bus), prefix="/api/v1")


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def unknown_api(request: Request):
    raise ApiError(404, "NOT_FOUND", "API endpoint not found")


static_dir = settings.static_dir
if (static_dir / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    icon = static_dir / "favicon.ico"
    if not icon.is_file():
        raise ApiError(404, "NOT_FOUND", "Resource not found")
    return FileResponse(icon)


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str):
    if path.startswith(("api/", "assets/")) or Path(path).suffix:
        raise ApiError(404, "NOT_FOUND", "Resource not found")
    index = static_dir / "index.html"
    if not index.is_file():
        raise ApiError(404, "NOT_FOUND", "Frontend has not been built")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
