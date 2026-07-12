"""ncloud — self-hosted personal cloud storage."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import admin, auth, files, sync
from .database import init_db
from .webdav import DAV_METHODS, webdav_endpoint

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="ncloud", version="0.1.0")
init_db()

app.include_router(auth.router)
app.include_router(files.router)
app.include_router(admin.router)
app.include_router(sync.router)

# WebDAV: /dav 및 그 하위 경로를 모든 WebDAV 메서드로 처리
app.add_route("/dav", webdav_endpoint, methods=DAV_METHODS)
app.add_route("/dav/{path:path}", webdav_endpoint, methods=DAV_METHODS)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
