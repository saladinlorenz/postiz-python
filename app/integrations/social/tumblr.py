import json
import mimetypes
import re
import secrets
from typing import Any
from urllib.parse import quote, urlencode

from app.core.config import settings
from app.integrations.base import BadBody, SocialAbstract
from app.integrations.interfaces import AuthTokenDetails, PostDetails, PostResponse
from app.integrations.media import image_dimensions, public_url, read_or_fetch

TUMBLR_API = "https://api.tumblr.com/v2"
TUMBLR_USER_AGENT = "Postiz/1.0 (+https://postiz.com)"
TEXT_BLOCK_LIMIT = 4096
DEFAULT_VIDEO_WIDTH = 540
DEFAULT_VIDEO_HEIGHT = 405
MAX_IMAGES = 30
MAX_VIDEOS = 1


def has_error_code(body: Any, code: int) -> bool:
    return re.search(f"(?:\\b|\\.){code}\\b", str(body)) is not None


class TumblrProvider(SocialAbstract):
    identifier = "tumblr"
    name = "Tumblr"
    picture = "tumblr.svg"
    description = "Publish posts to your Tumblr blogs"
    maxLength = 32768
    maxConcurrentJob = 3
    scopes = ["write", "offline_access"]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        if status == 401 or "Unauthorized" in text or "invalid_grant" in text or "invalid_token" in text:
            return "refresh-token", "Please re-authenticate your Tumblr account."

        rules: list[tuple[int, str, str]] = [
            (8023, "bad-body", "Tumblr daily posting limit reached."),
            (8001, "bad-body", "Tumblr rejected the post content format."),
            (8002, "bad-body", "Tumblr rejected the reblog parent post information."),
            (8004, "bad-body", "Tumblr daily media upload limit reached."),
            (8005, "bad-body", "Tumblr rejected one of the uploaded media files."),
            (8006, "retry", "Tumblr had a media upload error."),
            (8008, "bad-body", "Tumblr does not allow uploaded videos in reblog content."),
            (
                8010,
                "bad-body",
                "Tumblr is still transcoding a video upload for this blog. Please try again later.",
            ),
            (8011, "bad-body", "Tumblr daily video upload limit reached."),
            (8016, "bad-body", "Tumblr rejected the ask content or layout."),
            (8022, "bad-body", "Tumblr blog queue limit reached."),
            (8009, "retry", "Tumblr had a video upload error."),
        ]
        if "daily posting limit" in text:
            return "bad-body", "Tumblr daily posting limit reached."
        for code, kind, message in rules:
            if has_error_code(text, code):
                return kind, message
        if status == 429:
            return "retry", "Tumblr API rate limit reached."
        if status == 503:
            return "retry", "Tumblr posting via the API is temporarily unavailable."
        return super().handle_errors(body, status)

    def _redirect_uri(self) -> str:
        return f"{settings.frontend_url}/integrations/social/tumblr"

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(9)
        params = urlencode(
            {
                "client_id": settings.tumblr_client_id,
                "response_type": "code",
                "scope": " ".join(self.scopes),
                "state": state,
                "redirect_uri": self._redirect_uri(),
            }
        )
        return {
            "url": f"https://www.tumblr.com/oauth2/authorize?{params}",
            "codeVerifier": secrets.token_urlsafe(10),
            "state": state,
        }

    @staticmethod
    def _headers(access_token: str = "") -> dict[str, str]:
        headers = {"User-Agent": TUMBLR_USER_AGENT}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _request_token(self, params: dict[str, Any]) -> dict[str, Any]:
        data = self.get_json(
            f"{TUMBLR_API}/oauth2/token",
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": TUMBLR_USER_AGENT,
            },
            data=params,
        )
        return data if isinstance(data, dict) else {}

    def _user_info(self, access_token: str) -> dict[str, Any]:
        data = self.get_json(f"{TUMBLR_API}/user/info", headers=self._headers(access_token))
        if not isinstance(data, dict):
            return {}
        return data.get("response") or {}

    @staticmethod
    def _blogs(info: dict[str, Any]) -> list[dict[str, Any]]:
        user = info.get("user") or {}
        blogs = user.get("blogs") or []
        return [blog for blog in blogs if isinstance(blog, dict)]

    @staticmethod
    def _primary(blogs: list[dict[str, Any]]) -> dict[str, Any]:
        for blog in blogs:
            if blog.get("primary"):
                return blog
        return blogs[0] if blogs else {}

    @staticmethod
    def _avatar_url(blog_name: str) -> str:
        return f"{TUMBLR_API}/blog/{quote(f'{blog_name}.tumblr.com', safe='')}/avatar/128"

    @staticmethod
    def _normalize_url(url: str) -> str:
        value = str(url or "")
        return value if value.startswith("http") else f"https://{value}"

    def _details(
        self, token: dict[str, Any], previous_refresh: str = ""
    ) -> AuthTokenDetails:
        access_token = str(token.get("access_token") or "")
        info = self._user_info(access_token)
        user = info.get("user") or {}
        blogs = self._blogs(info)
        primary = self._primary(blogs)
        name = str(user.get("name") or primary.get("title") or primary.get("name") or "Tumblr")
        username = str(user.get("name") or "")
        return AuthTokenDetails(
            id=str(user.get("name") or primary.get("name") or ""),
            name=name,
            accessToken=access_token,
            refreshToken=str(token.get("refresh_token") or previous_refresh),
            expiresIn=int(token.get("expires_in") or 0) or None,
            picture=self._avatar_url(str(primary["name"])) if primary else "",
            username=username,
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        token = self._request_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.tumblr_client_id,
                "client_secret": settings.tumblr_client_secret,
                "redirect_uri": self._redirect_uri(),
            }
        )
        scope = str(token.get("scope") or "")
        if scope:
            self.check_scopes(self.scopes, scope.split())
        return self._details(token)

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        token = self._request_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.tumblr_client_id,
                "client_secret": settings.tumblr_client_secret,
            }
        )
        if not token.get("access_token"):
            return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)
        return self._details(token, refresh_token)

    def connections(self, details: AuthTokenDetails) -> list[AuthTokenDetails]:
        blogs = self._blogs(self._user_info(details.accessToken))
        if not blogs:
            return [details]
        connections: list[AuthTokenDetails] = []
        for blog in blogs:
            blog_name = str(blog.get("name") or "")
            if not blog_name:
                continue
            connections.append(
                AuthTokenDetails(
                    id=blog_name,
                    name=str(blog.get("title") or blog_name),
                    accessToken=details.accessToken,
                    refreshToken=details.refreshToken,
                    expiresIn=details.expiresIn,
                    picture=self._avatar_url(blog_name),
                    username=str(blog.get("url") or f"https://{blog_name}.tumblr.com/"),
                    additionalSettings=dict(details.additionalSettings),
                )
            )
        return connections or [details]

    @staticmethod
    def _is_video(path: str) -> bool:
        return ".mp4" in (path or "").lower()

    @staticmethod
    def _mime(path: str) -> str:
        guessed, _ = mimetypes.guess_type(str(path or "").split("?")[0])
        return guessed or "application/octet-stream"

    @staticmethod
    def _chunk_text(text: str) -> list[str]:
        return [text[index : index + TEXT_BLOCK_LIMIT] for index in range(0, len(text), TEXT_BLOCK_LIMIT)]

    def _text_blocks(self, message: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for part in re.split(r"\n{2,}", message or ""):
            for chunk in self._chunk_text(part.strip()):
                if chunk:
                    blocks.append({"type": "text", "text": chunk})
        return blocks

    @staticmethod
    def _video_dimensions(media: Any) -> tuple[int, int]:
        thumbnail = getattr(media, "thumbnail", "") or ""
        if thumbnail:
            try:
                return image_dimensions(thumbnail)
            except Exception:
                pass
        return DEFAULT_VIDEO_WIDTH, DEFAULT_VIDEO_HEIGHT

    def _content_blocks(self, post: PostDetails) -> list[dict[str, Any]]:
        post_settings = post.settings or {}
        blocks: list[dict[str, Any]] = []
        if post_settings.get("title"):
            blocks.append({"type": "text", "subtype": "heading1", "text": str(post_settings["title"])})
        blocks.extend(self._text_blocks(post.message or ""))
        if post_settings.get("link"):
            blocks.append({"type": "link", "url": self._normalize_url(str(post_settings["link"]))})

        for index, item in enumerate(post.media or []):
            identifier = f"media-{index}"
            if self._is_video(item.path):
                width, height = self._video_dimensions(item)
                blocks.append(
                    {
                        "type": "video",
                        "provider": "tumblr",
                        "media": {
                            "type": self._mime(item.path),
                            "identifier": identifier,
                            "width": width,
                            "height": height,
                        },
                    }
                )
                continue
            width, height = image_dimensions(item.path)
            image_block: dict[str, Any] = {
                "type": "image",
                "media": [
                    {
                        "type": self._mime(item.path),
                        "identifier": identifier,
                        "width": width,
                        "height": height,
                    }
                ],
            }
            if item.alt:
                image_block["alt_text"] = item.alt
            blocks.append(image_block)

        if not blocks:
            blocks.append({"type": "text", "text": ""})
        return blocks

    @staticmethod
    def _check_media(media: list[Any]) -> None:
        images = [item for item in media if not TumblrProvider._is_video(item.path)]
        videos = [item for item in media if TumblrProvider._is_video(item.path)]
        if len(images) > MAX_IMAGES:
            raise BadBody("tumblr", "{}", "{}", "Tumblr supports up to 30 images in one post.")
        if len(videos) > MAX_VIDEOS:
            raise BadBody("tumblr", "{}", "{}", "Tumblr supports one uploaded video in one post.")

    def _payload(self, post: PostDetails) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": self._content_blocks(post), "state": "published"}
        post_settings = post.settings or {}
        if post_settings.get("tags"):
            tags = post_settings["tags"]
            payload["tags"] = tags if isinstance(tags, str) else ",".join(str(tag) for tag in tags)
        if post_settings.get("sourceUrl"):
            payload["source_url"] = self._normalize_url(str(post_settings["sourceUrl"]))
        return payload

    @staticmethod
    def _decode(response: Any) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text

    def _create_json_post(self, blog_name: str, access_token: str, payload: dict[str, Any]) -> Any:
        response = self.fetch(
            f"{TUMBLR_API}/blog/{quote(str(blog_name), safe='')}/posts",
            method="POST",
            headers=self._headers(access_token),
            json=payload,
        )
        return self._decode(response)

    def _create_multipart_post(
        self, blog_name: str, access_token: str, payload: dict[str, Any], media: list[Any]
    ) -> Any:
        files: dict[str, Any] = {}
        for index, item in enumerate(media):
            raw = read_or_fetch(public_url(item.path))
            filename = str(item.path).split("?")[0].split("/")[-1] or f"media-{index}"
            files[f"media-{index}"] = (filename, raw, self._mime(item.path))
        response = self.fetch(
            f"{TUMBLR_API}/blog/{quote(str(blog_name), safe='')}/posts",
            method="POST",
            headers=self._headers(access_token),
            data={"json": json.dumps(payload)},
            files=files,
        )
        return self._decode(response)

    @staticmethod
    def _blog_url(integration: Any, blog_name: str) -> str:
        profile = str(getattr(integration, "username", "") or "")
        base = TumblrProvider._normalize_url(profile or f"https://www.tumblr.com/{blog_name}")
        return base.rstrip("/")

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        if not post_details:
            return []
        first = post_details[0]
        media = first.media or []
        self._check_media(media)
        payload = self._payload(first)

        body = (
            self._create_multipart_post(id, access_token, payload, media)
            if media
            else self._create_json_post(id, access_token, payload)
        )
        if not isinstance(body, dict) or not body.get("response"):
            raise BadBody(
                "tumblr",
                str(body) if body is not None else "{}",
                "{}",
                "Tumblr did not return a valid post response",
            )

        response = body.get("response") or {}
        post_id = str(response.get("id_string") or response.get("post_id") or response.get("id") or "")
        return [
            PostResponse(
                id=first.id,
                status="success",
                postId=post_id,
                releaseURL=f"{self._blog_url(integration, id)}/post/{post_id}",
            )
        ]
