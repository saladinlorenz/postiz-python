from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.db.database import get_db
from app.db.models import Post, User
from app.services import posts_service

router = APIRouter(prefix="/posts", tags=["posts"])


class PostEntry(BaseModel):
    integrationId: str
    content: str = ""
    publishDate: str
    image: list[dict] = []
    settings: dict = {}
    group: str | None = None
    delay: int = 0
    draft: bool = False
    parentPostId: str | None = None


class CreatePostBody(BaseModel):
    posts: list[PostEntry]


@router.post("")
def create_post(body: CreatePostBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entries = [entry.model_dump() for entry in body.posts]
    created = posts_service.create_post(db, user.organizationId, entries)
    return [posts_service.serialize_post(post) for post in created]


@router.get("")
def list_posts(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    posts = (
        db.query(Post)
        .options(joinedload(Post.integration))
        .filter(Post.organizationId == user.organizationId)
        .order_by(Post.publishDate.asc())
        .limit(500)
        .all()
    )
    return [posts_service.serialize_post(post) for post in posts]


@router.get("/group/{group}")
def get_group(group: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    posts = (
        db.query(Post)
        .options(joinedload(Post.integration))
        .filter(Post.organizationId == user.organizationId, Post.group == group)
        .all()
    )
    return [posts_service.serialize_post(post) for post in posts]


@router.post("/{post_id}/now")
def publish_now(post_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if not post or post.organizationId != user.organizationId:
        raise HTTPException(404, "Post not found")
    post.state = "QUEUE"
    post.publishDate = datetime.utcnow()
    post.error = None
    db.commit()
    from app.services.scheduler import publish_post

    publish_post(post.id)
    db.refresh(post)
    return posts_service.serialize_post(post)


@router.put("/{post_id}/date")
def reschedule(post_id: str, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if not post or post.organizationId != user.organizationId:
        raise HTTPException(404, "Post not found")
    if payload.get("publishDate"):
        post.publishDate = posts_service._parse_date(payload["publishDate"])
    if payload.get("action") == "schedule":
        post.state = "QUEUE"
        post.error = None
        post.releaseId = None
        post.releaseURL = None
    db.commit()
    return posts_service.serialize_post(post)


@router.delete("/{post_id}")
def delete_post(post_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if not post or post.organizationId != user.organizationId:
        raise HTTPException(404, "Post not found")
    db.delete(post)
    db.commit()
    return {"ok": True}
