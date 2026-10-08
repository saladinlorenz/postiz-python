import json
import logging
import threading
import time
from datetime import datetime, timedelta

from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models import Integration, Post
from app.integrations.base import BadBody, Disconnect, RefreshTokenError, SocialAbstract
from app.integrations.manager import manager
from app.services import integration_service, posts_service

logger = logging.getLogger("postiz.scheduler")

_pending_lock = threading.Lock()


def publish_post(post_id: str) -> None:
    db = SessionLocal()
    try:
        post = db.get(Post, post_id)
        if not post or post.state not in ("QUEUE",):
            return
        integration = db.get(Integration, post.integrationId)
        if not integration:
            return
        if integration.disabled or integration.refreshNeeded:
            posts_service.mark_error(db, post, "Integration disabled or needs reconnection")
            return

        provider = manager.get(integration.providerIdentifier)
        details = post_details_list(post)

        try:
            if getattr(provider, "refresh_token", None) and integration.refreshToken and integration_service.needs_refresh(integration):
                if not integration_service.refresh_integration(db, integration):
                    posts_service.mark_error(db, post, "Token refresh failed, reconnect required")
                    return
                integration = db.get(Integration, post.integrationId)

            if hasattr(provider, "post_pending") and _has_pending(provider):
                results = provider.post_pending(integration.internalId, integration.token, details, integration)
                first = results[0]
                if first.status == "pending":
                    post.pendingData = json.dumps(_merge_pending(post, first.pendingData))
                    post.pendingChecks = 0
                    db.commit()
                    return
                posts_service.mark_published(db, post, first.postId, first.releaseURL)
            else:
                results = provider.post(integration.internalId, integration.token, details, integration)
                first = results[0]
                posts_service.mark_published(db, post, first.postId, first.releaseURL)
        except RefreshTokenError as exc:
            if integration_service.refresh_integration(db, integration):
                publish_post(post_id)
                return
            posts_service.mark_error(db, post, f"Refresh token failed: {exc.value}")
        except Disconnect as exc:
            integration.refreshNeeded = True
            db.commit()
            posts_service.mark_error(db, post, f"Disconnected: {exc.value}")
        except BadBody as exc:
            posts_service.mark_error(db, post, exc.message or str(exc))
        except Exception as exc:
            logger.exception("publish failed for %s", post_id)
            posts_service.mark_error(db, post, f"Unexpected error: {exc}")
    finally:
        db.close()


def resolve_pending(post_id: str) -> None:
    db = SessionLocal()
    try:
        post = db.get(Post, post_id)
        if not post or not post.pendingData:
            return
        integration = db.get(Integration, post.integrationId)
        if not integration:
            return
        provider = manager.get(integration.providerIdentifier)
        pending_data = json.loads(post.pendingData)

        if post.pendingChecks >= settings.pending_max_checks:
            posts_service.mark_error(db, post, "Pending post timed out")
            post.pendingData = None
            db.commit()
            return

        try:
            result = provider.check_post_status(integration.token, pending_data, integration)
            pending_data = result.pendingData or pending_data

            if result.status == "failed":
                posts_service.mark_error(db, post, result.error or "Pending post failed")
                post.pendingData = None
                db.commit()
                return

            if result.status == "completed":
                posts_service.mark_published(db, post, result.postId, result.releaseURL)
                post.pendingData = None
                db.commit()
                return

            if result.status == "ready":
                finalized = provider.finalize_post(integration.token, pending_data, integration)
                if finalized.status == "completed":
                    posts_service.mark_published(db, post, finalized.postId, finalized.releaseURL)
                    post.pendingData = None
                    db.commit()
                    return
                pending_data = finalized.pendingData or pending_data

            post.pendingData = json.dumps(pending_data)
            post.pendingChecks = (post.pendingChecks or 0) + 1
            db.commit()
        except RefreshTokenError:
            integration_service.refresh_integration(db, integration)
        except Disconnect as exc:
            integration.refreshNeeded = True
            db.commit()
            posts_service.mark_error(db, post, f"Disconnected: {exc.value}")
        except BadBody as exc:
            posts_service.mark_error(db, post, exc.message or str(exc))
            post.pendingData = None
            db.commit()
        except Exception as exc:
            logger.exception("resolve_pending failed for %s", post_id)
            posts_service.mark_error(db, post, f"Unexpected error: {exc}")
    finally:
        db.close()


def comment_post(post_id: str) -> None:
    db = SessionLocal()
    try:
        post = db.get(Post, post_id)
        if not post or not post.parentPostId:
            return
        parent = db.get(Post, post.parentPostId)
        integration = db.get(Integration, post.integrationId)
        if not parent or not integration:
            return
        provider = manager.get(integration.providerIdentifier)
        if not hasattr(provider, "comment"):
            return
        delay = (post.delay or 0) * 60
        effective = post.publishDate + timedelta(seconds=delay)
        if effective > datetime.utcnow():
            return
        details = post_details_list(post)
        try:
            result = provider.comment(integration.internalId, integration.token, details[0], parent.releaseId or "", integration)
            posts_service.mark_published(db, post, result.postId, result.releaseURL)
        except RefreshTokenError:
            if integration_service.refresh_integration(db, integration):
                result = provider.comment(integration.internalId, integration.token, details[0], parent.releaseId or "", integration)
                posts_service.mark_published(db, post, result.postId, result.releaseURL)
        except Exception as exc:
            posts_service.mark_error(db, post, str(exc))
    finally:
        db.close()


def refresh_due_tokens() -> None:
    db = SessionLocal()
    try:
        horizon = datetime.utcnow() + timedelta(seconds=settings.refresh_margin_seconds)
        due = (
            db.query(Integration)
            .filter(
                Integration.refreshToken != "",
                Integration.tokenExpiration.isnot(None),
                Integration.tokenExpiration <= horizon,
                Integration.disabled.isnot(True),
                Integration.refreshNeeded.isnot(True),
            )
            .all()
        )
        for integration in due:
            integration_service.refresh_integration(db, integration)
    finally:
        db.close()


def _scheduler_tick() -> None:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        due = (
            db.query(Post)
            .filter(Post.state == "QUEUE", Post.publishDate <= now, Post.parentPostId.is_(None), Post.pendingData.is_(None))
            .all()
        )
        pending = (
            db.query(Post)
            .filter(Post.state == "QUEUE", Post.pendingData.isnot(None))
            .all()
        )
        comments = (
            db.query(Post)
            .filter(Post.state == "QUEUE", Post.publishDate <= now, Post.parentPostId.isnot(None))
            .all()
        )
        due_ids = [p.id for p in due]
        pending_ids = [p.id for p in pending]
        comment_ids = [p.id for p in comments]
    finally:
        db.close()

    for post_id in due_ids:
        with _pending_lock:
            publish_post(post_id)
    if pending_ids:
        with _pending_lock:
            for post_id in pending_ids:
                resolve_pending(post_id)
        time.sleep(settings.pending_check_interval_seconds)
    for post_id in comment_ids:
        comment_post(post_id)
    refresh_due_tokens()


def scheduler_loop() -> None:
    logger.info("scheduler started (interval=%ss)", settings.scheduler_interval_seconds)
    while True:
        try:
            _scheduler_tick()
        except Exception:
            logger.exception("scheduler tick failed")
        time.sleep(settings.scheduler_interval_seconds)


def start_scheduler() -> threading.Thread:
    thread = threading.Thread(target=scheduler_loop, daemon=True, name="postiz-scheduler")
    thread.start()
    return thread


def post_details_list(post: Post):
    from app.integrations.interfaces import MediaContent, PostDetails

    media = []
    for item in json.loads(post.image or "[]"):
        media.append(
            MediaContent(
                type=item.get("type", "image"),
                path=item.get("path", ""),
                alt=item.get("alt", ""),
                thumbnail=item.get("thumbnail", ""),
            )
        )
    return [
        PostDetails(
            id=post.id,
            message=post.content,
            media=media,
            settings=json.loads(post.settings or "{}"),
        )
    ]


def _has_pending(provider: SocialAbstract) -> bool:
    return callable(getattr(provider, "post_pending", None))


def _merge_pending(post: Post, pending_data) -> dict:
    return pending_data if isinstance(pending_data, dict) else {}
