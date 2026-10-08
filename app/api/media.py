import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.db.database import get_db
from app.db.models import Media, User

router = APIRouter(prefix="/media", tags=["media"])

ALLOWED_IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ALLOWED_VIDEO = {".mp4", ".mov", ".webm"}


@router.post("/upload")
async def upload(file: UploadFile, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext in ALLOWED_IMAGE:
        media_type = "image"
    elif ext in ALLOWED_VIDEO:
        media_type = "video"
    else:
        raise HTTPException(400, "Only images (png/jpg/gif/webp) and videos (mp4/mov/webm) are allowed")

    content = await file.read()
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 100MB)")

    name = f"{uuid.uuid4().hex}{ext}"
    directory = settings.upload_directory
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "wb") as handle:
        handle.write(content)

    media = Media(
        organizationId=user.organizationId,
        name=name,
        originalName=file.filename or name,
        path=name,
        fileSize=len(content),
        type=media_type,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return {"id": media.id, "path": name, "type": media_type, "originalName": media.originalName}


@router.get("/list")
def list_media(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items = (
        db.query(Media)
        .filter(Media.organizationId == user.organizationId)
        .order_by(Media.createdAt.desc())
        .limit(100)
        .all()
    )
    return [
        {"id": m.id, "path": m.path, "type": m.type, "originalName": m.originalName, "fileSize": m.fileSize}
        for m in items
    ]


@router.get("/file/{path:path}")
def get_file(path: str, user: User = Depends(get_current_user)):
    safe = os.path.normpath(path).replace("..", "")
    full = os.path.join(settings.upload_directory, safe)
    if not os.path.isfile(full):
        raise HTTPException(404, "Not found")
    from fastapi.responses import FileResponse

    return FileResponse(full)


@router.get("/public/{path:path}")
def get_public_file(path: str, db: Session = Depends(get_db)):
    safe = os.path.normpath(path).replace("..", "")
    if not db.query(Media).filter(Media.path == safe).first():
        raise HTTPException(404, "Not found")
    full = os.path.join(settings.upload_directory, safe)
    if not os.path.isfile(full):
        raise HTTPException(404, "Not found")
    from fastapi.responses import FileResponse

    return FileResponse(full)
