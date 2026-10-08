import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import Integration, Post


def create_post(
    db: Session,
    organization_id: str,
    posts: list[dict],
) -> list[Post]:
    created: list[Post] = []
    group = posts[0].get("group") if posts and posts[0].get("group") else None
    for entry in posts:
        integration = db.get(Integration, entry.get("integrationId"))
        if not integration or integration.organizationId != organization_id:
            raise HTTPException(404, "Integration not found")
        if integration.disabled or integration.refreshNeeded:
            raise HTTPException(409, "Integration is disabled or needs reconnection")

        publish_date = _parse_date(entry.get("publishDate"))
        state = "DRAFT" if entry.get("draft") else "QUEUE"
        post = Post(
            organizationId=organization_id,
            integrationId=integration.id,
            state=state,
            publishDate=publish_date,
            content=entry.get("content", ""),
            image=json.dumps(entry.get("image", [])),
            settings=json.dumps(entry.get("settings", {})),
            group=group or _new_group(),
            delay=int(entry.get("delay", 0)),
            parentPostId=entry.get("parentPostId"),
        )
        db.add(post)
        created.append(post)
    db.commit()
    for post in created:
        db.refresh(post)
    return created


def _new_group() -> str:
    import uuid

    return uuid.uuid4().hex


def _parse_date(value) -> datetime:
    if not value:
        raise HTTPException(400, "publishDate is required")
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(400, "Invalid publishDate")


def serialize_post(post: Post) -> dict:
    integration = post.integration
    return {
        "id": post.id,
        "group": post.group,
        "state": post.state,
        "publishDate": post.publishDate.isoformat(),
        "content": post.content,
        "image": json.loads(post.image or "[]"),
        "settings": json.loads(post.settings or "{}"),
        "delay": post.delay,
        "releaseId": post.releaseId,
        "releaseURL": post.releaseURL,
        "error": post.error,
        "parentPostId": post.parentPostId,
        "integration": {
            "id": integration.id,
            "name": integration.name,
            "picture": integration.picture,
            "providerIdentifier": integration.providerIdentifier,
        }
        if integration
        else None,
    }


def mark_error(db: Session, post: Post, message: str) -> None:
    post.state = "ERROR"
    post.error = message[:2000]
    db.commit()


def mark_published(db: Session, post: Post, release_id: str, release_url: str) -> None:
    post.state = "PUBLISHED"
    post.releaseId = release_id
    post.releaseURL = release_url
    post.error = None
    db.commit()
