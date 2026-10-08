import json
import re
import secrets
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.integrations.base import BadBody, Disconnect, RefreshTokenError, SocialAbstract
from app.integrations.interfaces import (
    AuthTokenDetails,
    PendingCheckResponse,
    PostDetails,
    PostResponse,
)
from app.integrations.media import image_dimensions, media_chunk, media_size, public_url

API = "https://open.tiktokapis.com"
TOKEN_ENDPOINT = f"{API}/v2/oauth/token/"
USER_INFO = f"{API}/v2/user/info/?fields=open_id,avatar_url,display_name,union_id,username"
MAX_SINGLE_CHUNK = 64 * 1024 * 1024
CHUNK_SIZE = 10 * 1024 * 1024
EXPIRES_IN = 23 * 3600
ERROR_RULES: list[tuple[str, str, str]] = [
    ("access_token_invalid", "refresh-token", "Access token invalid, please re-authenticate your TikTok account"),
    ("scope_not_authorized", "bad-body", "Missing required permissions, please re-authenticate with all scopes"),
    ("scope_permission_missed", "bad-body", "Additional permissions required, please re-authenticate"),
    ("rate_limit_exceeded", "bad-body", "TikTok API rate limit exceeded, please try again later"),
    ("file_format_check_failed", "bad-body", "File format is invalid, please check video specifications"),
    (
        "app_version_check_failed",
        "bad-body",
        "In order to use the TikTok upload feature, you have to update your app to the latest version",
    ),
    ("duration_check_failed", "bad-body", "Video duration is invalid, please check video specifications"),
    ("frame_rate_check_failed", "bad-body", "Video frame rate is invalid, please check video specifications"),
    ("video_pull_failed", "bad-body", "Failed to pull video from URL, please check the URL"),
    ("photo_pull_failed", "bad-body", "Failed to pull photo from URL, please check the URL"),
    ("spam_risk_user_banned_from_posting", "bad-body", "Account banned from posting, please check TikTok account status"),
    ("spam_risk_too_many_posts", "bad-body", "TikTok says your daily post limit reached, please try again tomorrow"),
    (
        "spam_risk_too_many_pending_share",
        "bad-body",
        "TikTok limits pending posts to 5 within any 24-hour period. Please check your TikTok inbox in the "
        "TikTok mobile app and try again after 24 hours.",
    ),
    ("spam_risk_text", "bad-body", "TikTok detected potential spam in the post text"),
    ("spam_risk", "bad-body", "TikTok detected potential spam"),
    ("reached_active_user_cap", "disconnect", "TikTok daily user limit reached, please re-connect your account"),
    (
        "unaudited_client_can_only_post_to_private_accounts",
        "bad-body",
        "App not approved for public posting, contact support",
    ),
    ("url_ownership_unverified", "bad-body", "You have to upload the picture/video to Postiz when sending a URL"),
    ("privacy_level_option_mismatch", "bad-body", "Privacy level mismatch, please check privacy settings"),
    ("invalid_file_upload", "bad-body", "Invalid file format or specifications not met"),
    ("invalid_params", "bad-body", "Invalid request parameters, please check content format"),
    ("internal", "bad-body", "There is a problem with TikTok servers, please try again later"),
    (
        "picture_size_check_failed",
        "bad-body",
        "Media size not supported by TikTok: images up to 1080px on the shorter side, videos at least 360px on both sides",
    ),
    ("TikTok API error", "bad-body", "TikTok API error, please try again"),
]


def parse_publish_status(raw: str) -> tuple[dict[str, Any], str]:
    match = re.search(r'"publicaly_available_post_id"\s*:\s*\[\s*"?(\d+)', raw or "")
    public_post_id = match.group(1) if match else ""
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    return (parsed if isinstance(parsed, dict) else {}), public_post_id


class TiktokProvider(SocialAbstract):
    identifier = "tiktok"
    name = "TikTok"
    picture = "tiktok.svg"
    description = "Publish videos and photo posts to TikTok"
    maxLength = 2000
    maxConcurrentJob = 10000
    convertToJPEG = True
    scopes = [
        "video.list",
        "user.info.basic",
        "video.publish",
        "video.upload",
        "user.info.profile",
        "user.info.stats",
    ]

    def _error_rule(self, text: str) -> tuple[str, str] | None:
        for needle, kind, message in ERROR_RULES:
            if needle in text:
                return kind, message
        return None

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        rule = self._error_rule(text)
        if rule:
            return rule
        if status == 429:
            return "retry", "TikTok API rate limit exceeded, please try again later"
        return super().handle_errors(body, status)

    def _raise_rule(self, text: str, fallback: str) -> None:
        kind, message = self._error_rule(text) or ("bad-body", fallback)
        if kind == "refresh-token":
            raise RefreshTokenError(message, message)
        if kind == "disconnect":
            raise Disconnect(message, message)
        raise BadBody(self.identifier, text[:500], "{}", message)

    def _raise_if_error(self, body: Any) -> None:
        if not isinstance(body, dict):
            return
        error = body.get("error")
        if not isinstance(error, dict) or not error.get("code"):
            return
        text = f"{error.get('code')} {error.get('message') or ''}"
        self._raise_rule(text, str(error.get("message") or "TikTok API error"))

    def _redirect_uri(self, refresh: str = "") -> str:
        uri = f"{settings.frontend_url}/integrations/social/tiktok"
        if refresh:
            uri += f"?refresh={refresh}"
        if settings.frontend_url.startswith("https"):
            return uri
        return f"https://redirectmeto.com/{uri}"

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(16)
        url = (
            "https://www.tiktok.com/v2/auth/authorize/"
            f"?client_key={settings.tiktok_client_id}"
            f"&redirect_uri={quote(self._redirect_uri(str(refresh) if refresh else ''), safe='')}"
            f"&state={state}"
            "&response_type=code"
            f"&scope={quote(','.join(self.scopes), safe='')}"
        )
        return {"url": url, "codeVerifier": state, "state": state}

    def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        body = self.get_json(
            TOKEN_ENDPOINT,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=payload,
        )
        self._raise_if_error(body)
        if not body.get("access_token"):
            self._raise_rule(str(body), "TikTok did not return an access token")
        return body

    def _user_info(self, access_token: str) -> dict[str, Any]:
        body = self.get_json(USER_INFO, headers={"Authorization": f"Bearer {access_token}"})
        self._raise_if_error(body)
        user = (body.get("data") or {}).get("user") or {}
        return user if isinstance(user, dict) else {}

    @staticmethod
    def _details(token: dict[str, Any], user: dict[str, Any]) -> AuthTokenDetails:
        open_id = str(user.get("open_id") or token.get("open_id") or "")
        return AuthTokenDetails(
            id=open_id.replace("-", ""),
            name=user.get("display_name", ""),
            accessToken=str(token.get("access_token") or ""),
            refreshToken=str(token.get("refresh_token") or ""),
            expiresIn=EXPIRES_IN,
            picture=user.get("avatar_url") or "",
            username=user.get("username", ""),
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        token = self._token_request(
            {
                "client_key": settings.tiktok_client_id,
                "client_secret": settings.tiktok_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "redirect_uri": self._redirect_uri(refresh),
            }
        )
        scope = token.get("scope") or ""
        granted = scope if isinstance(scope, list) else [item.strip() for item in str(scope).split(",") if item.strip()]
        self.check_scopes(self.scopes, granted)
        return self._details(token, self._user_info(str(token["access_token"])))

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        token = self._token_request(
            {
                "client_key": settings.tiktok_client_id,
                "client_secret": settings.tiktok_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        return self._details(token, self._user_info(str(token["access_token"])))

    @staticmethod
    def _asset_boolean(value: Any) -> bool:
        if isinstance(value, str):
            return value.lower() == "true"
        return bool(value)

    @staticmethod
    def _is_photo(path: str) -> bool:
        return ".mp4" not in (path or "").lower()

    def _content_posting_method(self, first: PostDetails) -> str:
        if (first.settings or {}).get("content_posting_method") == "UPLOAD":
            return "UPLOAD"
        return "DIRECT_POST"

    @staticmethod
    def _posting_method(method: str, is_photo: bool) -> str:
        if method == "UPLOAD":
            return "/content/init/" if is_photo else "/inbox/video/init/"
        return "/content/init/" if is_photo else "/video/init/"

    @staticmethod
    def _chunk_plan(video_size: int) -> tuple[int, int]:
        if video_size <= MAX_SINGLE_CHUNK:
            return video_size, 1
        return CHUNK_SIZE, video_size // CHUNK_SIZE

    def _first_path(self, first: PostDetails) -> str:
        return (first.media[0].path if first.media else "")

    def _check_media(self, media: list[Any]) -> None:
        if not media:
            raise BadBody(self.identifier, "{}", "{}", "No video / images selected")
        if len(media) > 1 and any(not self._is_photo(item.path) for item in media):
            raise BadBody(
                self.identifier,
                "{}",
                "{}",
                "Only pictures are supported when selecting multiple items",
            )
        if not self._is_photo(media[0].path) and len(media) != 1:
            raise BadBody(self.identifier, "{}", "{}", "You need one media")
        if all(self._is_photo(item.path) for item in media):
            for index, item in enumerate(media):
                try:
                    width, height = image_dimensions(item.path)
                except Exception:
                    continue
                if min(width, height) > 1080:
                    raise BadBody(
                        self.identifier,
                        "{}",
                        "{}",
                        f"Image {index + 1} is {width}x{height}, TikTok allows a maximum of 1080px on the shorter side",
                    )

    def _post_info(self, first: PostDetails) -> dict[str, Any]:
        config = first.settings or {}
        is_photo = self._is_photo(self._first_path(first))
        post_info: dict[str, Any] = {}

        if is_photo:
            if config.get("title"):
                title = str(config["title"])
                post_info["title"] = title[:90] if self._content_posting_method(first) == "DIRECT_POST" else title
            post_info["description"] = first.message
        elif first.message:
            post_info["title"] = first.message

        if self._content_posting_method(first) != "DIRECT_POST":
            return {"post_info": post_info}

        post_info["privacy_level"] = config.get("privacy_level") or "PUBLIC_TO_EVERYONE"
        if not is_photo:
            post_info["disable_duet"] = not self._asset_boolean(config.get("duet"))
            post_info["disable_stitch"] = not self._asset_boolean(config.get("stitch"))
            post_info["is_aigc"] = self._asset_boolean(config.get("video_made_with_ai"))
        post_info["disable_comment"] = not self._asset_boolean(config.get("comment"))
        post_info["brand_content_toggle"] = self._asset_boolean(config.get("brand_content_toggle"))
        post_info["brand_organic_toggle"] = self._asset_boolean(config.get("brand_organic_toggle"))
        if is_photo:
            post_info["auto_add_music"] = config.get("autoAddMusic") == "yes"
        elif first.media and first.media[0].thumbnailTimestamp:
            post_info["video_cover_timestamp_ms"] = first.media[0].thumbnailTimestamp
        return {"post_info": post_info}

    def _source_info(self, first: PostDetails, video_size: int = 0) -> dict[str, Any]:
        if self._is_photo(self._first_path(first)):
            return {
                "post_mode": "DIRECT_POST" if self._content_posting_method(first) == "DIRECT_POST" else "MEDIA_UPLOAD",
                "media_type": "PHOTO",
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "photo_cover_index": 0,
                    "photo_images": [public_url(item.path) for item in first.media or []],
                },
            }
        chunk_size, total_chunks = self._chunk_plan(video_size)
        return {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunks,
            }
        }

    def _upload_video(self, upload_url: str, path: str, video_size: int) -> None:
        chunk_size, total_chunks = self._chunk_plan(video_size)
        with httpx.Client(timeout=600, follow_redirects=True) as client:
            for index in range(total_chunks):
                start = index * chunk_size
                end = video_size - 1 if index == total_chunks - 1 else start + chunk_size - 1
                if end < start:
                    break
                chunk = media_chunk(path, start, end)
                if len(chunk) != end - start + 1:
                    raise BadBody(
                        self.identifier,
                        "{}",
                        "{}",
                        "The media storage did not return the requested byte range, please try again",
                    )
                response = client.put(
                    upload_url,
                    content=chunk,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{video_size}",
                    },
                )
                if response.status_code not in (200, 201, 206):
                    text = response.text[:2000] if response.text else "{}"
                    kind, message = self.handle_errors(text, response.status_code)
                    if kind == "refresh-token":
                        raise RefreshTokenError(message, message)
                    if kind == "disconnect":
                        raise Disconnect(message, message)
                    raise BadBody(self.identifier, text, "{}", message)

    def post_pending(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        media = first.media or []
        self._check_media(media)
        path = self._first_path(first)
        is_photo = self._is_photo(path)

        video_size = 0
        if not is_photo:
            video_size = media_size(path)
            if video_size <= 0:
                raise BadBody(self.identifier, "{}", "{}", "TikTok could not read the video size, please try again")

        method = self._content_posting_method(first)
        body = {**self._post_info(first), **self._source_info(first, video_size)}
        response = self.get_json(
            f"{API}/v2/post/publish{self._posting_method(method, is_photo)}",
            method="POST",
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "Authorization": f"Bearer {access_token}",
            },
            json=body,
        )
        self._raise_if_error(response)
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        publish_id = str(data.get("publish_id") or "")
        if not publish_id:
            self._raise_rule(str(response), "TikTok did not accept the post")

        if not is_photo:
            upload_url = str(data.get("upload_url") or "")
            if not upload_url:
                raise BadBody(self.identifier, str(response), "{}", "TikTok did not return an upload url")
            try:
                self._upload_video(upload_url, path, video_size)
            except BadBody:
                raise
            except Exception:
                pass

        return [PostResponse(id=first.id, status="pending", pendingData={"publishId": publish_id})]

    def check_post_status(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        publish_id = str(data.get("publishId") or "")
        try:
            raw = self.get_text(
                f"{API}/v2/post/publish/status/fetch/",
                method="POST",
                headers={
                    "Content-Type": "application/json; charset=UTF-8",
                    "Authorization": f"Bearer {access_token}",
                },
                json={"publish_id": publish_id},
            )
            post, public_post_id = parse_publish_status(raw)
        except (RefreshTokenError, Disconnect):
            raise
        except Exception:
            return PendingCheckResponse(status="pending", pendingData=data)

        error = post.get("error")
        status = str(((post.get("data") or {}) if isinstance(post.get("data"), dict) else {}).get("status") or "")
        if status == "SEND_TO_USER_INBOX":
            return PendingCheckResponse(
                status="completed",
                releaseURL="https://www.tiktok.com/messages?lang=en",
                postId="missing",
            )
        if status == "PUBLISH_COMPLETE":
            profile = getattr(integration, "username", "") or getattr(integration, "internalId", "") or ""
            release_url = f"https://www.tiktok.com/@{profile}"
            if public_post_id:
                release_url = f"{release_url}/video/{public_post_id}"
            return PendingCheckResponse(
                status="completed",
                releaseURL=release_url,
                postId=public_post_id or publish_id,
            )
        if status == "FAILED":
            self._raise_rule(raw, "TikTok refused to publish your post")
        if isinstance(error, dict) and error.get("code"):
            rule = self._error_rule(f"{error.get('code')} {error.get('message') or ''}")
            if rule and rule[0] in ("refresh-token", "disconnect"):
                self._raise_rule(str(error.get("code")), rule[1])
        return PendingCheckResponse(status="pending", pendingData=data)

    def finalize_post(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        check = self.check_post_status(access_token, data, integration)
        if check.status == "completed":
            return check
        return PendingCheckResponse(status="pending", pendingData=check.pendingData or data)

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        started = time.time()
        pending = self.post_pending(id, access_token, post_details, integration)[0].pendingData
        for _ in range(27):
            check = self.check_post_status(access_token, pending, integration)
            if check.status == "ready":
                check = self.finalize_post(access_token, check.pendingData or pending, integration)
            if check.status == "completed":
                return [
                    PostResponse(
                        id=post_details[0].id,
                        status="success",
                        postId=check.postId,
                        releaseURL=check.releaseURL,
                    )
                ]
            pending = check.pendingData or pending
            if time.time() - started > 8 * 60:
                break
            time.sleep(20)
        raise BadBody(self.identifier, "{}", "{}", "TikTok refused to publish your post")
