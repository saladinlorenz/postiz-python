import json
import mimetypes
import secrets
from typing import Any

from app.core.config import settings
from app.integrations.base import BadBody, RefreshTokenError, SocialAbstract, encode_component, pkce_challenge
from app.integrations.interfaces import AuthTokenDetails, PostDetails, PostResponse
from app.integrations.media import public_url, read_or_fetch

VK_API = "https://api.vk.com/method"
VK_ID = "https://id.vk.com"
VK_VERSION = "5.251"
REFRESH_SEPARATOR = "&&&&"


class VkProvider(SocialAbstract):
    identifier = "vk"
    name = "VK"
    picture = "vk.svg"
    description = "Publish posts to your VK wall"
    maxLength = 2048
    maxConcurrentJob = 5
    scopes = [
        "vkid.personal_info",
        "email",
        "wall",
        "status",
        "docs",
        "photos",
        "video",
    ]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        if "User authorization failed" in text or "Access token has expired" in text:
            return "refresh-token", "VK access token expired, please re-authenticate your account"
        return super().handle_errors(body, status)

    def _redirect_uri(self) -> str:
        base = settings.frontend_url
        if not base.startswith("https"):
            base = f"https://redirectmeto.com/{base}"
        return f"{base}/integrations/social/vk"

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        challenge = pkce_challenge(code_verifier)
        url = (
            f"{VK_ID}/authorize"
            "?response_type=code"
            f"&client_id={settings.vk_id}"
            f"&code_challenge_method=S256"
            f"&code_challenge={challenge}"
            f"&redirect_uri={encode_component(self._redirect_uri())}"
            f"&state={state}"
            f"&scope={encode_component(' '.join(self.scopes))}"
        )
        return {"url": url, "codeVerifier": code_verifier, "state": state}

    @staticmethod
    def _form(fields: dict[str, Any]) -> dict[str, Any]:
        return {key: str(value) for key, value in fields.items() if value is not None}

    def _oauth(self, fields: dict[str, Any]) -> dict[str, Any]:
        data = self.get_json(
            f"{VK_ID}/oauth2/auth",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=self._form(fields),
        )
        return data if isinstance(data, dict) else {}

    def _user_info(self, access_token: str) -> dict[str, Any]:
        data = self.get_json(
            f"{VK_ID}/oauth2/user_info",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=self._form({"client_id": settings.vk_id, "access_token": access_token}),
        )
        if not isinstance(data, dict):
            return {}
        return data.get("user") or {}

    @staticmethod
    def _split_refresh(refresh: str) -> tuple[str, str]:
        parts = str(refresh or "").split(REFRESH_SEPARATOR)
        return parts[0], (parts[1] if len(parts) > 1 else "")

    def _details(self, token: dict[str, Any], device_id: str) -> AuthTokenDetails:
        user = self._user_info(str(token.get("access_token") or ""))
        first_name = str(user.get("first_name") or "")
        last_name = str(user.get("last_name") or "")
        return AuthTokenDetails(
            id=str(user.get("user_id") or ""),
            name=f"{first_name} {last_name}".strip(),
            accessToken=str(token.get("access_token") or ""),
            refreshToken=f"{token.get('refresh_token') or ''}{REFRESH_SEPARATOR}{device_id}",
            expiresIn=int(token.get("expires_in") or 0) or None,
            picture=str(user.get("avatar") or ""),
            username=first_name.lower(),
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        real_code, device_id = self._split_refresh(code)
        token = self._oauth(
            {
                "client_id": settings.vk_id,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "device_id": device_id,
                "code": real_code,
                "redirect_uri": self._redirect_uri(),
            }
        )
        if not token.get("access_token"):
            raise BadBody("vk", json.dumps(token), "{}", "VK did not return an access token")
        return self._details(token, device_id)

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        old_refresh, device_id = self._split_refresh(refresh_token)
        token = self._oauth(
            {
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
                "client_id": settings.vk_id,
                "device_id": device_id,
                "state": secrets.token_urlsafe(32),
                "scope": " ".join(self.scopes),
            }
        )
        if not token.get("access_token"):
            return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)
        return self._details(token, device_id)

    @staticmethod
    def _is_video(path: str) -> bool:
        return ".mp4" in (path or "").lower()

    @staticmethod
    def _check_api_error(payload: Any) -> None:
        if not isinstance(payload, dict) or not payload.get("error"):
            return
        error = payload.get("error") or {}
        message = str(error.get("error_msg") or "VK rejected the request")
        code = error.get("error_code")
        if code == 5:
            raise RefreshTokenError(message, message)
        raise BadBody("vk", json.dumps(payload), "{}", message)

    @staticmethod
    def _mime(path: str) -> str:
        guessed, _ = mimetypes.guess_type(str(path or "").split("?")[0])
        return guessed or "application/octet-stream"

    def _upload_media(self, user_id: str, access_token: str, post: PostDetails) -> list[dict[str, str]]:
        uploaded: list[dict[str, str]] = []
        for item in post.media or []:
            is_video = self._is_video(item.path)
            if is_video:
                endpoint = f"{VK_API}/video.save?access_token={access_token}&v={VK_VERSION}"
            else:
                endpoint = (
                    f"{VK_API}/photos.getWallUploadServer"
                    f"?owner_id={user_id}&access_token={access_token}&v={VK_VERSION}"
                )
            server = self.get_json(endpoint)
            self._check_api_error(server)
            upload_url = str((server.get("response") or {}).get("upload_url") or "")
            if not upload_url:
                raise BadBody("vk", json.dumps(server), "{}", "VK did not return an upload URL")

            raw = read_or_fetch(public_url(item.path))
            filename = str(item.path).split("?")[0].split("/")[-1] or "media"
            value = self.fetch(
                upload_url,
                method="POST",
                files={"photo": (filename, raw, self._mime(item.path))},
            )
            body: Any
            try:
                body = value.json()
            except ValueError:
                body = {}
            if is_video:
                self._check_api_error(body)
                video_id = str((server.get("response") or {}).get("video_id") or "")
                uploaded.append({"id": video_id, "type": "video"})
                continue

            self._check_api_error(body)
            saved = self.fetch(
                f"{VK_API}/photos.saveWallPhoto?access_token={access_token}&v={VK_VERSION}",
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=self._form(
                    {
                        "photo": body.get("photo"),
                        "server": body.get("server"),
                        "hash": body.get("hash"),
                    }
                ),
            )
            try:
                saved_payload = saved.json()
            except ValueError:
                saved_payload = {}
            self._check_api_error(saved_payload)
            photo_list = (saved_payload.get("response") or []) or []
            if not photo_list:
                raise BadBody("vk", json.dumps(saved_payload), "{}", "VK did not save the photo")
            uploaded.append({"id": str(photo_list[0].get("id") or ""), "type": "photo"})
        return uploaded

    @staticmethod
    def _attachments(user_id: str, uploaded: list[dict[str, str]]) -> str:
        return ",".join(f"{entry['type']}{user_id}_{entry['id']}" for entry in uploaded)

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        if not post_details:
            return []
        first = post_details[0]
        user_id = str(getattr(integration, "internalId", "") or id)
        uploaded = self._upload_media(user_id, access_token, first)

        fields: dict[str, Any] = {"message": first.message}
        if uploaded:
            fields["attachments"] = self._attachments(user_id, uploaded)
        payload = self.fetch(
            f"{VK_API}/wall.post?v={VK_VERSION}&access_token={access_token}&client_id={settings.vk_id}",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=self._form(fields),
        )
        try:
            body = payload.json()
        except ValueError:
            body = {}
        self._check_api_error(body)
        post_id = str((body.get("response") or {}).get("post_id") or "")
        return [
            PostResponse(
                id=first.id,
                status="success",
                postId=post_id,
                releaseURL=f"https://vk.com/feed?w=wall{user_id}_{post_id}",
            )
        ]

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        user_id = str(getattr(integration, "internalId", "") or id)
        uploaded = self._upload_media(user_id, access_token, post_details)

        fields: dict[str, Any] = {"message": post_details.message, "post_id": parent_post_id or id}
        if uploaded:
            fields["attachments"] = self._attachments(user_id, uploaded)
        payload = self.fetch(
            f"{VK_API}/wall.createComment?v={VK_VERSION}&access_token={access_token}&client_id={settings.vk_id}",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=self._form(fields),
        )
        try:
            body = payload.json()
        except ValueError:
            body = {}
        self._check_api_error(body)
        comment_id = str((body.get("response") or {}).get("comment_id") or "")
        target = parent_post_id or id
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=comment_id,
            releaseURL=f"https://vk.com/feed?w=wall{user_id}_{target}",
        )
