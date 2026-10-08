import json
import secrets
import time
from typing import Any
from urllib.parse import quote

from app.core.config import settings
from app.integrations.base import BadBody, SocialAbstract
from app.integrations.interfaces import (
    AuthTokenDetails,
    PendingCheckResponse,
    PostDetails,
    PostResponse,
)
from app.integrations.media import public_url

THREADS_API = "https://graph.threads.net"
TOKEN_TTL_SECONDS = 58 * 24 * 60 * 60
POLL_SECONDS = 2.2
SETTLE_SECONDS = 2
POLL_MAX_ATTEMPTS = 150
POST_TIMEOUT_SECONDS = 8 * 60


class ThreadsProvider(SocialAbstract):
    identifier = "threads"
    name = "Threads"
    picture = "threads.svg"
    description = "Publish to your Threads profile"
    maxLength = 500
    maxConcurrentJob = 20
    refreshCron = True
    scopes = [
        "threads_basic",
        "threads_content_publish",
        "threads_manage_replies",
        "threads_manage_insights",
    ]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        rules: list[tuple[str, str, str]] = [
            ("Error validating access token", "refresh-token", "Threads access token expired"),
            (
                "2207051",
                "bad-body",
                "Error from Meta: We restrict certain activity to protect our community",
            ),
            ("4279013", "bad-body", "User restricted"),
            (
                "The media could not be fetched from this URI",
                "bad-body",
                "One of the media URLs is invalid or inaccessible, make sure it's being uploaded to Postiz first",
            ),
            (
                "4279009",
                "retry",
                "Threads could not find the media container yet, please try again in a few seconds",
            ),
            ("text must be at most 500 characters", "bad-body", "Post text exceeds 500 characters limit"),
        ]
        for needle, kind, message in rules:
            if needle in text:
                return kind, message
        return super().handle_errors(body, status)

    def _redirect_uri(self) -> str:
        base = settings.frontend_url
        if not base.startswith("https"):
            base = f"https://redirectmeto.com/{base}"
        return f"{base}/integrations/social/threads"

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(9)
        url = (
            "https://www.threads.net/oauth/authorize"
            f"?client_id={settings.threads_app_id}"
            f"&redirect_uri={quote(self._redirect_uri(), safe='')}"
            f"&state={state}"
            f"&scope={quote(','.join(self.scopes), safe='')}"
        )
        return {"url": url, "codeVerifier": secrets.token_urlsafe(10), "state": state}

    def _profile(self, access_token: str) -> dict[str, str]:
        data = self.get_json(
            f"{THREADS_API}/v1.0/me",
            params={
                "fields": "id,username,threads_profile_picture_url",
                "access_token": access_token,
            },
        )
        return {
            "id": str(data.get("id") or ""),
            "username": str(data.get("username") or ""),
            "picture": str(data.get("threads_profile_picture_url") or ""),
        }

    @staticmethod
    def _token_details(access_token: str, profile: dict[str, str]) -> AuthTokenDetails:
        return AuthTokenDetails(
            id=profile["id"],
            name=profile["username"],
            accessToken=access_token,
            refreshToken=access_token,
            expiresIn=TOKEN_TTL_SECONDS,
            picture=profile["picture"],
            username=profile["username"],
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        short = self.get_json(
            f"{THREADS_API}/oauth/access_token",
            params={
                "client_id": settings.threads_app_id,
                "redirect_uri": self._redirect_uri(),
                "grant_type": "authorization_code",
                "client_secret": settings.threads_app_secret,
                "code": code,
            },
        )
        long_lived = self.get_json(
            f"{THREADS_API}/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": settings.threads_app_secret,
                "access_token": str(short.get("access_token") or ""),
            },
        )
        access_token = str(long_lived.get("access_token") or "")
        if not access_token:
            raise BadBody("threads", str(long_lived), "{}", "Threads did not return an access token")
        return self._token_details(access_token, self._profile(access_token))

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        data = self.get_json(
            f"{THREADS_API}/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": refresh_token},
        )
        access_token = str(data.get("access_token") or "")
        if not access_token:
            return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)
        return self._token_details(access_token, self._profile(access_token))

    @staticmethod
    def _is_video(path: str) -> bool:
        return ".mp4" in (path or "").lower()

    @staticmethod
    def _user_id(integration: Any, fallback: str = "") -> str:
        return str(getattr(integration, "internalId", "") or fallback)

    @staticmethod
    def _profile_url(integration: Any) -> str:
        return f"https://www.threads.net/@{getattr(integration, 'username', '') or ''}"

    def _create_text(
        self, user_id: str, access_token: str, message: str, reply_to_id: str = ""
    ) -> str:
        params: dict[str, Any] = {"media_type": "TEXT", "text": message, "access_token": access_token}
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        data = self.get_json(f"{THREADS_API}/v1.0/{user_id}/threads", method="POST", params=params)
        return str(data.get("id") or "")

    def _create_single_media(
        self,
        user_id: str,
        access_token: str,
        media: Any,
        message: str,
        carousel_item: bool = False,
        reply_to_id: str = "",
    ) -> str:
        is_video = self._is_video(media.path)
        params: dict[str, Any] = {
            "media_type": "VIDEO" if is_video else "IMAGE",
            "text": message,
            "access_token": access_token,
        }
        params["video_url" if is_video else "image_url"] = public_url(media.path)
        if carousel_item:
            params["is_carousel_item"] = "true"
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        data = self.get_json(f"{THREADS_API}/v1.0/{user_id}/threads", method="POST", params=params)
        return str(data.get("id") or "")

    def _create_carousel(
        self, user_id: str, access_token: str, child_ids: list[str], message: str, reply_to_id: str = ""
    ) -> str:
        params: dict[str, Any] = {
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "text": message,
            "access_token": access_token,
        }
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        data = self.get_json(f"{THREADS_API}/v1.0/{user_id}/threads", method="POST", params=params)
        return str(data.get("id") or "")

    def _publish(self, user_id: str, access_token: str, creation_id: str) -> str:
        data = self.get_json(
            f"{THREADS_API}/v1.0/{user_id}/threads_publish",
            method="POST",
            params={"creation_id": creation_id, "access_token": access_token},
        )
        return str(data.get("id") or "")

    def _container_status(self, container_id: str, access_token: str) -> str:
        data = self.get_json(
            f"{THREADS_API}/v1.0/{container_id}",
            params={"fields": "status,error_message", "access_token": access_token},
        )
        status = str(data.get("status") or "")
        if status in ("ERROR", "EXPIRED"):
            message = str(data.get("error_message") or "")
            raise BadBody(
                "threads",
                json.dumps({"status": status, "error_message": message}),
                "{}",
                message
                if message and message != "UNKNOWN"
                else "Threads could not process the media, please check the media format and try again",
            )
        if status in ("FINISHED", "PUBLISHED"):
            return status
        return "IN_PROGRESS"

    def _wait_for_container(self, container_id: str, access_token: str) -> None:
        for _ in range(POLL_MAX_ATTEMPTS):
            status = self._container_status(container_id, access_token)
            if status in ("FINISHED", "PUBLISHED"):
                time.sleep(SETTLE_SECONDS)
                return
            time.sleep(POLL_SECONDS)
        raise BadBody(
            "threads",
            "{}",
            "{}",
            "Threads took too long to process the media, please try again",
        )

    def _permalink(self, thread_id: str, access_token: str, integration: Any) -> str:
        try:
            data = self.get_json(
                f"{THREADS_API}/v1.0/{thread_id}",
                params={"fields": "id,permalink", "access_token": access_token},
            )
            if data.get("permalink"):
                return str(data["permalink"])
        except Exception:
            pass
        return self._profile_url(integration)

    def post_pending(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        if not post_details:
            return []
        first = post_details[0]
        user_id = self._user_id(integration, id)
        media = first.media or []

        if len(media) > 1:
            child_ids = [
                self._create_single_media(user_id, access_token, item, first.message, carousel_item=True)
                for item in media
            ]
            return [
                PostResponse(
                    id=first.id,
                    status="pending",
                    pendingData={"step": "children", "childIds": child_ids, "message": first.message},
                )
            ]

        if not media:
            container_id = self._create_text(user_id, access_token, first.message)
        else:
            container_id = self._create_single_media(user_id, access_token, media[0], first.message)
        return [
            PostResponse(
                id=first.id,
                status="pending",
                pendingData={"step": "container", "containerId": container_id},
            )
        ]

    def check_post_status(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)

        if data.get("step") == "children":
            for child_id in data.get("childIds") or []:
                if self._container_status(str(child_id), access_token) == "IN_PROGRESS":
                    return PendingCheckResponse(status="pending", pendingData=data)
            return PendingCheckResponse(status="ready", pendingData=data)

        container_id = str(data.get("containerId") or "")
        status = self._container_status(container_id, access_token)
        if status == "IN_PROGRESS":
            return PendingCheckResponse(status="pending", pendingData=data)
        if status == "PUBLISHED":
            return PendingCheckResponse(
                status="completed",
                postId=container_id,
                releaseURL=self._profile_url(integration),
            )
        return PendingCheckResponse(status="ready", pendingData=data)

    def finalize_post(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        user_id = self._user_id(integration)

        if data.get("step") == "children":
            container_id = self._create_carousel(
                user_id,
                access_token,
                [str(child) for child in data.get("childIds") or []],
                str(data.get("message") or ""),
            )
            return PendingCheckResponse(
                status="pending", pendingData={"step": "container", "containerId": container_id}
            )

        thread_id = self._publish(user_id, access_token, str(data.get("containerId") or ""))
        if not thread_id:
            raise BadBody("threads", "{}", "{}", "Threads did not publish the post")
        return PendingCheckResponse(
            status="completed",
            postId=thread_id,
            releaseURL=self._permalink(thread_id, access_token, integration),
        )

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        if not post_details:
            return []
        started = time.time()
        pending = self.post_pending(id, access_token, post_details, integration)[0].pendingData
        while True:
            if time.time() - started > POST_TIMEOUT_SECONDS:
                raise BadBody(
                    "threads",
                    "{}",
                    "{}",
                    "Threads took too long to process the media, please try again",
                )
            check = self.check_post_status(access_token, pending, integration)
            if check.status == "pending":
                pending = check.pendingData
                time.sleep(POLL_SECONDS)
                continue
            result = (
                self.finalize_post(access_token, check.pendingData, integration)
                if check.status == "ready"
                else check
            )
            if result.status == "completed":
                return [
                    PostResponse(
                        id=post_details[0].id,
                        status="success",
                        postId=result.postId,
                        releaseURL=result.releaseURL,
                    )
                ]
            pending = result.pendingData
            time.sleep(POLL_SECONDS)

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        user_id = self._user_id(integration, id)
        reply_to_id = parent_post_id or id
        media = post_details.media or []

        if not media:
            creation_id = self._create_text(user_id, access_token, post_details.message, reply_to_id)
        elif len(media) == 1:
            creation_id = self._create_single_media(
                user_id, access_token, media[0], post_details.message, reply_to_id=reply_to_id
            )
        else:
            child_ids = [
                self._create_single_media(user_id, access_token, item, post_details.message, carousel_item=True)
                for item in media
            ]
            for child_id in child_ids:
                self._wait_for_container(child_id, access_token)
            creation_id = self._create_carousel(
                user_id, access_token, child_ids, post_details.message, reply_to_id
            )

        self._wait_for_container(creation_id, access_token)
        thread_id = self._publish(user_id, access_token, creation_id)
        if not thread_id:
            raise BadBody("threads", "{}", "{}", "Threads did not publish the reply")
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=thread_id,
            releaseURL=self._permalink(thread_id, access_token, integration),
        )
