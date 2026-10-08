import base64
import secrets
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

PINTEREST_API = "https://api.pinterest.com/v5"


class PinterestProvider(SocialAbstract):
    identifier = "pinterest"
    name = "Pinterest"
    picture = "pinterest.svg"
    description = "Publish pins to your boards"
    maxLength = 500
    maxConcurrentJob = 10
    scopes = ["boards:read", "boards:write", "pins:read", "pins:write", "user_accounts:read"]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        if "constraint: maxItems=5" in text:
            return "bad-body", "You can upload a maximum of 5 images per post on Pinterest."
        if "Unable to reach the URL" in text:
            return "bad-body", "Pinterest was unable to reach the URL provided. Please check the link and try again."
        if "does not match '^" in text and "d+$'" in text:
            return "bad-body", "The board ID must be a numeric string. Please check the board ID format."
        if "Board not found" in text:
            return "bad-body", "The specified board was not found. Please check the board ID."
        if "You are not permitted to access that resource" in text:
            return (
                "bad-body",
                "The connected Pinterest account is not permitted to post to this board. "
                "Please check the board ID and that the account owns or can write to the board.",
            )
        if "cover_image_url or cover_image_content_type" in text:
            return (
                "bad-body",
                "When uploading a video, you must add also an image to be used as a cover image.",
            )
        return super().handle_errors(body, status)

    def _redirect_uri(self) -> str:
        return f"{settings.frontend_url}/integrations/social/pinterest"

    def _basic_auth(self) -> dict[str, str]:
        raw = f"{settings.pinterest_client_id}:{settings.pinterest_client_secret}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(9)
        url = (
            "https://www.pinterest.com/oauth/"
            f"?client_id={settings.pinterest_client_id}"
            f"&redirect_uri={self._redirect_uri()}"
            "&response_type=code"
            f"&scope={','.join(self.scopes)}"
            f"&state={state}"
        )
        return {"url": url, "codeVerifier": secrets.token_urlsafe(10), "state": state}

    def _token(self, params: dict[str, str]) -> dict[str, Any]:
        return self.fetch(
            f"{PINTEREST_API}/oauth/token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", **self._basic_auth()},
            data=params,
        ).json()

    def _me(self, access_token: str) -> dict[str, Any]:
        return self.get_json(
            f"{PINTEREST_API}/user_account",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def _details(self, payload: dict[str, Any], me: dict[str, Any]) -> AuthTokenDetails:
        return AuthTokenDetails(
            id=str(me.get("id", "")),
            name=me.get("username", ""),
            accessToken=payload["access_token"],
            refreshToken=payload.get("refresh_token", ""),
            expiresIn=payload.get("expires_in"),
            picture=me.get("profile_image") or "",
            username=me.get("username", ""),
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        payload = self._token(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri()}
        )
        scope = payload.get("scope", "")
        if scope:
            self.check_scopes(self.scopes, [s for s in scope.replace(" ", ",").split(",") if s])
        return self._details(payload, self._me(payload["access_token"]))

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        payload = self._token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": ",".join(self.scopes),
                "redirect_uri": self._redirect_uri(),
            }
        )
        return self._details(payload, self._me(payload["access_token"]))

    def channels(self, integration: Any) -> list[dict[str, str]]:
        access_token = integration.token if hasattr(integration, "token") else str(integration)
        raw = self.get_json(
            f"{PINTEREST_API}/boards?page_size=250",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return [{"id": item.get("id", ""), "name": item.get("name", "")} for item in (raw.get("items") or [])]

    def _has_video(self, media: list[Any]) -> bool:
        return any(item.path.lower().split("?")[0].endswith(".mp4") for item in media)

    def post_pending(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        media = first.media or []
        if not media:
            raise BadBody("pinterest", "{}", "{}", "Requires at least one media")
        if len(media) > 5:
            raise BadBody("pinterest", "{}", "{}", "You can only have up to 5 media items")

        media_id = ""
        cover_path = ""
        video = next((item for item in media if item.path.lower().split("?")[0].endswith(".mp4")), None)
        picture = next((item for item in media if not item.path.lower().split("?")[0].endswith(".mp4")), None)

        if video:
            if not picture:
                raise BadBody(
                    "pinterest",
                    "{}",
                    "{}",
                    "If posting a video you have to also include a cover image as second media",
                )
            if len(media) > 2:
                raise BadBody(
                    "pinterest",
                    "{}",
                    "{}",
                    "If posting a video you can only have two media items",
                )
            session = self.fetch(
                f"{PINTEREST_API}/media",
                method="POST",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={"media_type": "video"},
            ).json()
            content = read_or_fetch(video.path)
            filename = (video.path.split("?")[0].split("/")[-1]) if is_url(video.path) else resolve_local(video.path).split("\\")[-1]
            self.fetch(
                session["upload_url"],
                method="POST",
                data={k: v for k, v in (session.get("upload_parameters") or {}).items() if k},
                files={"file": (filename, content, "video/mp4")},
                timeout=120,
            )
            media_id = session.get("media_id", "")
            cover_path = picture.path

        payload_settings = first.settings or {}
        return [
            PostResponse(
                id=first.id,
                status="pending",
                pendingData={
                    "mediaId": media_id,
                    "message": first.message,
                    "settings": {
                        "link": payload_settings.get("link"),
                        "title": payload_settings.get("title"),
                        "dominant_color": payload_settings.get("dominant_color"),
                        "board": payload_settings.get("board") or payload_settings.get("channel"),
                    },
                    "imagePaths": [item.path for item in media],
                    "coverPath": cover_path,
                },
            )
        ]

    def check_post_status(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        if data.get("attempting") and data.get("confirmed"):
            raise BadBody(
                "pinterest",
                "{}",
                "{}",
                "Pinterest may have already published this pin, please check your account "
                "before posting again to avoid duplicates",
            )

        if not data.get("mediaId"):
            return self._witness(data)

        try:
            media_file = self.get_json(
                f"{PINTEREST_API}/media/{data['mediaId']}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except RefreshTokenError:
            raise
        except Exception:
            return PendingCheckResponse(status="pending", pendingData=data)

        status = media_file.get("status", "")
        if status == "failed":
            raise BadBody("pinterest", "{}", "{}", "The file is corrupted and cannot be uploaded")
        if status != "succeeded":
            return PendingCheckResponse(status="pending", pendingData=data)
        return self._witness(data)

    def _witness(self, data: dict[str, Any]) -> PendingCheckResponse:
        if data.get("attempting") and not data.get("confirmed"):
            return PendingCheckResponse(status="ready", pendingData={**data, "confirmed": True})
        return PendingCheckResponse(status="ready", pendingData=data)

    def finalize_post(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        if not data.get("attempting") or not data.get("confirmed"):
            return PendingCheckResponse(status="pending", pendingData={**data, "attempting": True, "confirmed": False})

        paths = data.get("imagePaths") or []
        media_id = data.get("mediaId", "")
        payload: dict[str, Any] = {"description": data.get("message", ""), "board_id": (data.get("settings") or {}).get("board")}

        config = data.get("settings") or {}
        if config.get("link"):
            payload["link"] = config["link"]
        if config.get("title"):
            payload["title"] = config["title"]
        if config.get("dominant_color"):
            payload["dominant_color"] = config["dominant_color"]

        if media_id:
            payload["media_source"] = {
                "source_type": "video_id",
                "media_id": media_id,
                "cover_image_url": data.get("coverPath") or "",
            }
        elif len(paths) == 1:
            payload["media_source"] = {"source_type": "image_url", "url": paths[0]}
        else:
            payload["media_source"] = {"source_type": "multiple_image_urls", "items": [{"url": p} for p in paths]}

        result = self.fetch(
            f"{PINTEREST_API}/pins",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload,
        ).json()
        pin_id = str(result.get("id", ""))
        return PendingCheckResponse(
            status="completed",
            postId=pin_id,
            releaseURL=f"https://www.pinterest.com/pin/{pin_id}",
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
                final = self.finalize_post(access_token, check.pendingData, integration)
                if final.status == "completed":
                    return [
                        PostResponse(
                            id=post_details[0].id,
                            status="success",
                            postId=final.postId,
                            releaseURL=final.releaseURL,
                        )
                    ]
                pending = final.pendingData
            else:
                pending = check.pendingData
            if time.time() - started > 8 * 60:
                raise BadBody(
                    "pinterest",
                    "{}",
                    "{}",
                    "The file took too long to process, please try again",
                )
            time.sleep(20)

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        raise BadBody("pinterest", "{}", "{}", "Pinterest does not support comments")
