import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.core.config import settings
from app.integrations.base import BadBody, RefreshTokenError, SocialAbstract
from app.integrations.interfaces import (
    AuthTokenDetails,
    PendingCheckResponse,
    PostDetails,
    PostResponse,
)
from app.integrations.media import is_url, read_or_fetch, resolve_local


class MastodonProvider(SocialAbstract):
    identifier = "mastodon"
    name = "Mastodon"
    picture = "mastodon.svg"
    description = "Post to your Mastodon account"
    maxLength = 500
    maxConcurrentJob = 10
    scopes = ["write:statuses", "profile", "write:media"]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        if "Your login is currently disabled" in text:
            return "refresh-token", "Your login is currently disabled"
        if "not finished processing" in text:
            return "bad-body", "The media was still processing when the post was published, please try again"
        if "could not be found" in text:
            return "bad-body", "The uploaded media expired before the post was published, please post again"
        return super().handle_errors(body, status)

    def _instance(self, integration: Any = None) -> str:
        if integration is not None:
            import json

            try:
                extra = json.loads(getattr(integration, "additionalSettings", "{}") or "{}")
                if extra.get("instanceUrl"):
                    return extra["instanceUrl"].rstrip("/")
            except (json.JSONDecodeError, TypeError):
                pass
        return (settings.mastodon_url or "https://mastodon.social").rstrip("/")

    def _redirect_uri(self) -> str:
        return f"{settings.frontend_url}/integrations/social/mastodon"

    def generate_auth_url(self, refresh: bool = False, instance: str = "", **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(9)
        url = (
            f"{(instance or self._instance()).rstrip('/')}/oauth/authorize"
            f"?client_id={settings.mastodon_client_id}&response_type=code"
            f"&redirect_uri={self._redirect_uri()}"
            f"&scope={'+'.join(self.scopes)}&state={state}"
        )
        return {"url": url, "codeVerifier": secrets.token_urlsafe(10), "state": state}

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        instance = (kwargs.get("instanceUrl") or kwargs.get("instance") or "").rstrip("/")
        if not instance:
            instance = self._instance()
        token_info = self.fetch(
            f"{instance}/oauth/token",
            method="POST",
            data={
                "client_id": settings.mastodon_client_id,
                "client_secret": settings.mastodon_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self._redirect_uri(),
                "scope": " ".join(self.scopes),
            },
        ).json()
        account = self.get_json(
            f"{instance}/api/v1/accounts/verify_credentials",
            headers={"Authorization": f"Bearer {token_info['access_token']}"},
        )
        return AuthTokenDetails(
            id=str(account.get("id", "")),
            name=account.get("display_name") or account.get("acct", ""),
            accessToken=token_info["access_token"],
            refreshToken="",
            expiresIn=int((datetime.now() + timedelta(days=36500) - datetime.now()).total_seconds()),
            picture=account.get("avatar") or "",
            username=account.get("username", ""),
            additionalSettings={"instanceUrl": instance},
        )

    def _upload_media(self, instance: str, path: str, access_token: str, alt: str = "") -> str:
        if is_url(path):
            content = self.fetch(path).content
            filename = path.split("/")[-1].split("?")[0] or "file"
        else:
            local = resolve_local(path)
            import os

            content = read_or_fetch(path)
            filename = os.path.basename(local)
        files = {"file": (filename, content)}
        data = {"description": alt} if alt else {}
        response = self.fetch(
            f"{instance}/api/v1/media",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}"},
            data=data,
            files=files,
            timeout=120,
        )
        if response.status_code in (200, 201, 202):
            return response.json().get("id", "")
        raise BadBody(self.identifier, response.text[:500], "{}", "Mastodon media upload failed")

    def _media_status(self, instance: str, media_id: str, access_token: str) -> int:
        import httpx

        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(
                f"{instance}/api/v1/media/{media_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        return response.status_code

    def post_pending(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        instance = self._instance(integration)
        media_ids = [
            uploaded
            for uploaded in (
                self._upload_media(instance, item.path, access_token, item.alt) for item in (first.media or [])
            )
            if uploaded
        ]
        return [
            PostResponse(
                id=first.id,
                status="pending",
                pendingData={
                    "url": instance,
                    "message": first.message,
                    "mediaIds": media_ids,
                    "idempotencyKey": f"{first.id}-{uuid.uuid4().hex[:5]}",
                },
            )
        ]

    def check_post_status(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        media_ids = pending.get("mediaIds") or []
        if not media_ids:
            return PendingCheckResponse(status="ready", pendingData=pending)

        instance = pending.get("url") or self._instance(integration)
        for media_id in media_ids:
            try:
                status = self._media_status(instance, media_id, access_token)
            except Exception:
                return PendingCheckResponse(status="pending", pendingData=pending)
            if status == 206:
                return PendingCheckResponse(status="pending", pendingData=pending)
            if status == 401:
                raise RefreshTokenError("Mastodon token expired", "Mastodon token expired")
            if status == 404 or status == 410:
                continue
            if status == 422:
                raise BadBody(
                    self.identifier,
                    "{}",
                    "{}",
                    "The uploaded media expired before the post was published, please post again",
                )
            if status != 200:
                return PendingCheckResponse(status="pending", pendingData=pending)
        return PendingCheckResponse(status="ready", pendingData=pending)

    def finalize_post(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        instance = pending.get("url") or self._instance(integration)
        data: dict[str, str] = {
            "status": pending.get("message", ""),
            "visibility": "public",
        }
        files = []
        media_ids = pending.get("mediaIds") or []
        if media_ids:
            data["media_ids[]"] = ",".join(media_ids)

        headers = {"Authorization": f"Bearer {access_token}"}
        if pending.get("idempotencyKey"):
            headers["Idempotency-Key"] = pending["idempotencyKey"]

        result = self.fetch(
            f"{instance}/api/v1/statuses",
            method="POST",
            headers=headers,
            data=data,
            files=files or None,
        ).json()
        return PendingCheckResponse(
            status="completed",
            postId=str(result.get("id", "")),
            releaseURL=f"{instance}/statuses/{result.get('id', '')}",
        )

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        import time

        pending = self.post_pending(id, access_token, post_details, integration)[0].pendingData
        started = time.time()
        while True:
            check = self.check_post_status(access_token, pending, integration)
            if check.status == "ready":
                finalize = self.finalize_post(access_token, check.pendingData, integration)
                if finalize.status == "completed":
                    return [
                        PostResponse(
                            id=post_details[0].id,
                            status="success",
                            postId=finalize.postId,
                            releaseURL=finalize.releaseURL,
                        )
                    ]
                pending = finalize.pendingData
            else:
                pending = check.pendingData
            if time.time() - started > 8 * 60:
                raise BadBody(
                    self.identifier,
                    "{}",
                    "{}",
                    "The media took too long to process, please try again",
                )
            time.sleep(20)

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        instance = self._instance(integration)
        data: dict[str, str] = {
            "status": post_details.message,
            "visibility": "public",
            "in_reply_to_id": parent_post_id,
        }
        media_ids = [
            uploaded
            for uploaded in (
                self._upload_media(instance, item.path, access_token, item.alt)
                for item in (post_details.media or [])
            )
            if uploaded
        ]
        if media_ids:
            data["media_ids[]"] = ",".join(media_ids)
        result = self.fetch(
            f"{instance}/api/v1/statuses",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}"},
            data=data,
        ).json()
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=str(result.get("id", "")),
            releaseURL=f"{instance}/statuses/{result.get('id', '')}",
        )
