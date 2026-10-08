import secrets
from typing import Any

import httpx

from app.core.config import settings
from app.integrations.base import BadBody, RefreshTokenError, SocialAbstract
from app.integrations.interfaces import (
    AuthTokenDetails,
    PendingCheckResponse,
    PostDetails,
    PostResponse,
)
from app.integrations.media import is_url, read_or_fetch, resolve_local

GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK_SIZE = 8 * 1024 * 1024
UPLOAD_BATCH_SECONDS = 4 * 60


class YoutubeProvider(SocialAbstract):
    identifier = "youtube"
    name = "YouTube"
    picture = "youtube.svg"
    description = "Upload videos to your YouTube channel"
    maxLength = 5000
    maxConcurrentJob = 200
    scopes = [
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtubepartner",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
    ]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        messages = {
            "invalidTags": "The maximum allowed is 500 characters in total.",
            "invalidTitle": "We have uploaded your video but we could not set the title. Title is too long.",
            "invalidDescription": "Your video description is invalid, it may contain disallowed characters such as < or >.",
            "invalidCategoryId": "The selected video category is invalid.",
            "invalidPublishAt": "The scheduled publishing time is invalid.",
            "invalidRecordingDetails": "The recording details for the video are invalid.",
            "invalidVideoGameRating": "The video game rating is invalid.",
            "invalidFilename": "The video file name is invalid.",
            "defaultLanguageNotSet": "We could not set the localized video details because no default language is set.",
            "invalidVideoMetadata": "Some of the video details are invalid, please review the title, description and tags.",
            "mediaBodyRequired": "The video file is missing or could not be read, please re-upload the video.",
            "imageFormatUnsupported": "We have uploaded your video but the thumbnail format is not supported, please use JPEG or PNG.",
            "imageTooTall": "We have uploaded your video but the thumbnail image is too tall.",
            "imageTooWide": "We have uploaded your video but the thumbnail image is too wide.",
            "rateLimitExceeded": "You are sending requests too quickly, please wait a little while and try again.",
            "failedPrecondition": "We have uploaded your video but we could not set the thumbnail. Thumbnail size is too large.",
            "uploadLimitExceeded": "You have reached your daily upload limit, please try again tomorrow.",
            "youtubeSignupRequired": "You have to link your youtube account to your google account first.",
            "youtube.thumbnail": "Your account is not verified, we have uploaded your video but we could not set the thumbnail. Please verify your account and try again.",
        }
        for key, value in messages.items():
            if key in text:
                return "bad-body", value
        if "Unauthorized" in text:
            return "refresh-token", "Token expired or invalid, please reconnect your YouTube account."
        if "UNAUTHENTICATED" in text or "invalid_grant" in text:
            return "refresh-token", "Please re-authenticate your YouTube account"
        return super().handle_errors(body, status)

    def _redirect_uri(self) -> str:
        return f"{settings.frontend_url}/integrations/social/youtube"

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        from urllib.parse import urlencode

        state = secrets.token_urlsafe(9)
        url = GOOGLE_AUTH + "?" + urlencode(
            {
                "client_id": settings.youtube_client_id,
                "redirect_uri": self._redirect_uri(),
                "response_type": "code",
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "scope": " ".join(self.scopes),
            }
        )
        return {"url": url, "codeVerifier": secrets.token_urlsafe(11), "state": state}

    def _token(self, params: dict[str, str]) -> dict[str, Any]:
        return self.fetch(GOOGLE_TOKEN, method="POST", data=params).json()

    def _userinfo(self, access_token: str) -> dict[str, Any]:
        return self.get_json(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def _details(self, payload: dict[str, Any], user: dict[str, Any]) -> AuthTokenDetails:
        return AuthTokenDetails(
            id=str(user.get("sub") or user.get("id", "")),
            name=user.get("name", ""),
            accessToken=payload["access_token"],
            refreshToken=payload.get("refresh_token", ""),
            expiresIn=payload.get("expires_in"),
            picture=user.get("picture") or "",
            username="",
            additionalSettings={},
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        payload = self._token(
            {
                "code": code,
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                "redirect_uri": self._redirect_uri(),
                "grant_type": "authorization_code",
            }
        )
        return self._details(payload, self._userinfo(payload["access_token"]))

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        payload = self._token(
            {
                "refresh_token": refresh_token,
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                "redirect_uri": self._redirect_uri(),
                "grant_type": "refresh_token",
            }
        )
        details = self._details(payload, self._userinfo(payload["access_token"]))
        details.refreshToken = refresh_token
        return details

    def channels(self, integration: Any) -> list[dict[str, str]]:
        access_token = integration.token if hasattr(integration, "token") else str(integration)
        raw = self.get_json(
            f"{YOUTUBE_API}/channels",
            params={"part": "snippet,contentDetails,statistics", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return [
            {
                "id": item.get("id", ""),
                "name": (item.get("snippet") or {}).get("title", "Unnamed Channel"),
            }
            for item in (raw.get("items") or [])
        ]

    def _media_size(self, path: str) -> int:
        if is_url(path):
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                response = client.head(path, headers={"accept-encoding": "identity"})
            length = response.headers.get("content-length")
            if not length:
                raise BadBody(
                    self.identifier,
                    "{}",
                    "{}",
                    "Could not determine the video size for the YouTube upload",
                )
            return int(length)
        import os

        return os.path.getsize(resolve_local(path))

    def _chunk(self, path: str, start: int, end: int) -> bytes:
        if is_url(path):
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                response = client.get(
                    path,
                    headers={"Range": f"bytes={start}-{end}", "accept-encoding": "identity"},
                )
            if response.status_code != 206:
                raise BadBody(
                    self.identifier,
                    "{}",
                    "{}",
                    "The media storage did not return the requested byte range, please try again",
                )
            return response.content
        with open(resolve_local(path), "rb") as handle:
            handle.seek(start)
            return handle.read(end - start + 1)

    def _probe(self, access_token: str, upload_uri: str, video_size: int) -> dict[str, Any]:
        response = httpx.put(
            upload_uri,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Range": f"bytes */{video_size}",
            },
            timeout=60,
        )
        if response.status_code in (200, 201):
            return {"videoId": str(response.json().get("id", ""))}
        if response.status_code == 308:
            range_header = response.headers.get("range", "")
            uploaded = int(range_header.split("-")[1]) + 1 if range_header else 0
            return {"uploadedBytes": uploaded}
        text = response.text[:2000] if response.text else "{}"
        if response.status_code in (429, 500, 502, 503, 504):
            raise Exception(f"YouTube upload status check failed with {response.status_code}")
        kind, value = self.handle_errors(text, response.status_code)
        if response.status_code in (401, 403) or kind == "refresh-token":
            raise RefreshTokenError(value, value)
        raise BadBody(
            self.identifier,
            text,
            "{}",
            value
            if kind != "refresh-token"
            else "The upload session expired before the video was uploaded, please post again",
        )

    def post_pending(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        media = first.media or []
        if len(media) != 1:
            raise BadBody(self.identifier, "{}", "{}", "YouTube must have one video attachment, it cannot be empty")
        path = media[0].path
        if ".mp4" not in path:
            raise BadBody(self.identifier, "{}", "{}", "Item must be a video")

        config = first.settings or {}
        video_size = self._media_size(path)
        tags = config.get("tags") or []
        snippet: dict[str, Any] = {
            "title": config.get("title") or first.message.split("\n")[0][:100] or "Untitled",
            "description": first.message,
        }
        if tags:
            snippet["tags"] = [t.get("label") if isinstance(t, dict) else str(t) for t in tags]

        response = self.fetch(
            f"{YOUTUBE_UPLOAD}?uploadType=resumable&part=id,snippet,status&notifySubscribers=true",
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(video_size),
            },
            json={
                "snippet": snippet,
                "status": {
                    "privacyStatus": config.get("type", "public"),
                    "selfDeclaredMadeForKids": config.get("selfDeclaredMadeForKids") == "yes",
                },
            },
        )
        upload_uri = response.headers.get("location", "")
        if not upload_uri:
            raise BadBody(self.identifier, "{}", "{}", "Could not start the video upload, please try again")

        return [
            PostResponse(
                id=first.id,
                status="pending",
                pendingData={
                    "uploadUri": upload_uri,
                    "videoSize": video_size,
                    "path": path,
                    "uploadedBytes": 0,
                    "thumbnail": (config.get("thumbnail") or {}).get("path", "") if isinstance(config.get("thumbnail"), dict) else config.get("thumbnail", ""),
                },
            )
        ]

    def check_post_status(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        try:
            probe = self._probe(access_token, data["uploadUri"], data["videoSize"])
        except (RefreshTokenError, BadBody):
            raise
        except Exception:
            return PendingCheckResponse(status="pending", pendingData=data)

        if "videoId" in probe:
            if data.get("thumbnail"):
                return PendingCheckResponse(status="ready", pendingData={**data, "videoId": probe["videoId"]})
            return PendingCheckResponse(
                status="completed",
                postId=probe["videoId"],
                releaseURL=f"https://www.youtube.com/watch?v={probe['videoId']}",
            )
        return PendingCheckResponse(status="ready", pendingData={**data, "uploadedBytes": probe["uploadedBytes"]})

    def finalize_post(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        import time

        data = dict(pending)
        video_id = data.get("videoId", "")

        if not video_id:
            try:
                probe = self._probe(access_token, data["uploadUri"], data["videoSize"])
            except (RefreshTokenError, BadBody):
                raise
            except Exception:
                return PendingCheckResponse(status="pending", pendingData=data)
            if "videoId" in probe:
                video_id = probe["videoId"]
            else:
                uploaded = probe.get("uploadedBytes", 0)
                started = time.time()
                while uploaded < data["videoSize"]:
                    if time.time() - started > UPLOAD_BATCH_SECONDS:
                        return PendingCheckResponse(
                            status="pending", pendingData={**data, "uploadedBytes": uploaded}
                        )
                    end = min(uploaded + CHUNK_SIZE, data["videoSize"]) - 1
                    chunk = self._chunk(data["path"], uploaded, end)
                    response = httpx.put(
                        data["uploadUri"],
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "video/mp4",
                            "Content-Length": str(end - uploaded + 1),
                            "Content-Range": f"bytes {uploaded}-{end}/{data['videoSize']}",
                        },
                        content=chunk,
                        timeout=300,
                    )
                    if response.status_code == 308:
                        range_header = response.headers.get("range", "")
                        if not range_header:
                            return PendingCheckResponse(
                                status="pending", pendingData={**data, "uploadedBytes": uploaded}
                            )
                        uploaded = int(range_header.split("-")[1]) + 1
                        continue
                    if response.status_code in (200, 201):
                        video_id = str(response.json().get("id", ""))
                        break
                    text = response.text[:2000] if response.text else "{}"
                    if response.status_code in (429, 500, 502, 503, 504):
                        return PendingCheckResponse(
                            status="pending", pendingData={**data, "uploadedBytes": uploaded}
                        )
                    kind, value = self.handle_errors(text, response.status_code)
                    if response.status_code in (401, 403) or kind == "refresh-token":
                        raise RefreshTokenError(value, value)
                    raise BadBody(self.identifier, text, "{}", value)

                if not video_id:
                    return PendingCheckResponse(status="pending", pendingData={**data, "uploadedBytes": uploaded})

        thumbnail = data.get("thumbnail", "")
        if thumbnail:
            content = read_or_fetch(thumbnail)
            filename = thumbnail.split("?")[0].split("/")[-1] or "thumb.jpg"
            self.fetch(
                f"{YOUTUBE_API}/thumbnails/set",
                method="POST",
                params={"videoId": video_id},
                headers={"Authorization": f"Bearer {access_token}"},
                files={"file": (filename, content)},
                timeout=120,
            )

        return PendingCheckResponse(
            status="completed",
            postId=video_id,
            releaseURL=f"https://www.youtube.com/watch?v={video_id}",
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
                pending = final.pendingData or pending
            else:
                pending = check.pendingData or pending
            if time.time() - started > 8 * 60:
                raise BadBody(
                    self.identifier,
                    "{}",
                    "{}",
                    "The video upload took too long, please try a smaller video",
                )
            time.sleep(5)

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        raise BadBody(self.identifier, "{}", "{}", "YouTube does not support comments")
