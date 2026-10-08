import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, integrations, media, posts
from app.core.config import settings
from app.db.database import init_db
from app.services.scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name)

app.include_router(auth.router)
app.include_router(integrations.router)
app.include_router(posts.router)
app.include_router(media.router)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    Path(settings.upload_directory).mkdir(parents=True, exist_ok=True)
    start_scheduler()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/{full_path:path}")
def spa(full_path: str) -> FileResponse:
    candidate = static_dir / full_path
    if full_path and candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(static_dir / "index.html")
