import json
import secrets
import time
from typing import Any

from app.core.config import settings
from app.integrations.base import BadBody, SocialAbstract, encode_component
from app.integrations.interfaces import AuthTokenDetails, PostDetails, PostResponse

TWITCH_ID = "https://id.twitch.tv"
TWITCH_API = "https://api.twitch.tv/helix"
POST_DELAY_SECONDS = 2.0
MESSAGE_COLORS = ("primary", "blue", "green", "orange", "purple")


def random_id() -> str:
    return f"{secrets.randbelow(10**10):010d}"


class TwitchProvider(SocialAbstract):
    identifier = "twitch"
    name = "Twitch"
    picture = "twitch.svg"
    description = "Send a chat message or announcement to your Twitch channel (max 500 characters)"
    maxLength = 500
    maxConcurrentJob = 5
    scopes = ["user:write:chat", "user:read:chat", "moderator:manage:announcements"]

    def _redirect_uri(self, refresh: str = "") -> str:
        base = f"{settings.frontend_url}/integrations/social/twitch"
        return f"{base}?refresh={refresh}" if refresh else base

    @staticmethod
    def _form(fields: dict[str, Any]) -> dict[str, Any]:
        return {key: str(value) for key, value in fields.items() if value is not None}

    @staticmethod
    def _headers(access_token: str, content_type: str = "") -> dict[str, str]:
        headers = {"Authorization": f"Bearer {access_token}", "Client-Id": settings.twitch_client_id}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(32)
        url = (
            f"{TWITCH_ID}/oauth2/authorize"
            "?response_type=code"
            f"&client_id={settings.twitch_client_id}"
            f"&redirect_uri={encode_component(self._redirect_uri())}"
            f"&scope={encode_component(' '.join(self.scopes))}"
            f"&state={state}"
        )
        return {"url": url, "codeVerifier": secrets.token_urlsafe(10), "state": state}

    def _token(self, fields: dict[str, Any]) -> dict[str, Any]:
        data = self.get_json(
            f"{TWITCH_ID}/oauth2/token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=self._form(fields),
        )
        return data if isinstance(data, dict) else {}

    def _user(self, access_token: str) -> dict[str, Any]:
        data = self.get_json(f"{TWITCH_API}/users", headers=self._headers(access_token))
        if not isinstance(data, dict):
            return {}
        raw = data.get("data")
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        return raw if isinstance(raw, dict) else {}

    def _details(self, token: dict[str, Any]) -> AuthTokenDetails:
        user = self._user(str(token.get("access_token") or ""))
        return AuthTokenDetails(
            id=str(user.get("id") or ""),
            name=str(user.get("display_name") or ""),
            accessToken=str(token.get("access_token") or ""),
            refreshToken=str(token.get("refresh_token") or ""),
            expiresIn=int(token.get("expires_in") or 0) or None,
            picture=str(user.get("profile_image_url") or ""),
            username=str(user.get("login") or ""),
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        token = self._token(
            {
                "grant_type": "authorization_code",
                "client_id": settings.twitch_client_id,
                "client_secret": settings.twitch_client_secret,
                "redirect_uri": self._redirect_uri(refresh),
                "code": code,
            }
        )
        if not token.get("access_token"):
            raise BadBody("twitch", json.dumps(token), "{}", "Twitch did not return an access token")
        return self._details(token)

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        token = self._token(
            {
                "grant_type": "refresh_token",
                "client_id": settings.twitch_client_id,
                "client_secret": settings.twitch_client_secret,
                "refresh_token": refresh_token,
            }
        )
        if not token.get("access_token"):
            return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)
        return self._details(token)

    @staticmethod
    def _release_url(integration: Any) -> str:
        profile = str(getattr(integration, "username", "") or "")
        return f"https://twitch.tv/{profile or 'twitch'}"

    @staticmethod
    def _message(settings_map: dict[str, Any] | None) -> tuple[str, str]:
        data = settings_map or {}
        kind = str(data.get("messageType") or "message")
        color = str(data.get("announcementColor") or "primary")
        if color not in MESSAGE_COLORS:
            color = "primary"
        return kind, color

    def _announcement(self, id: str, access_token: str, message: str, color: str) -> None:
        self.fetch(
            f"{TWITCH_API}/chat/announcements?broadcaster_id={id}&moderator_id={id}",
            method="POST",
            headers=self._headers(access_token, "application/json"),
            json={"message": message[: self.maxLength], "color": color},
        )

    def _chat(self, id: str, access_token: str, message: str, reply_to: str = "") -> dict[str, Any]:
        fields: dict[str, Any] = {
            "broadcaster_id": id,
            "sender_id": id,
            "message": message[: self.maxLength],
        }
        if reply_to:
            fields["reply_parent_message_id"] = reply_to
        payload = self.get_json(
            f"{TWITCH_API}/chat/messages",
            method="POST",
            headers=self._headers(access_token, "application/json"),
            json=fields,
        )
        if not isinstance(payload, dict):
            raise BadBody("twitch", json.dumps(payload), "{}", "Twitch did not return a chat response")
        raw = payload.get("data")
        entry = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else {})
        return entry

    @staticmethod
    def _publish(integration: Any, entry: dict[str, Any], raw_payload: Any) -> PostResponse:
        if not entry.get("is_sent"):
            raise BadBody("twitch", json.dumps(raw_payload), "{}", "Twitch did not send the chat message")
        message_id = str(entry.get("message_id") or "") or random_id()
        return PostResponse(
            id="", status="success", postId=message_id, releaseURL=TwitchProvider._release_url(integration)
        )

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        if not post_details:
            return []
        first = post_details[0]
        if first.media:
            raise BadBody("twitch", "{}", "{}", "Twitch chat does not support media attachments")
        time.sleep(POST_DELAY_SECONDS)
        kind, color = self._message(first.settings)
        if kind == "announcement":
            self._announcement(id, access_token, first.message, color)
            result = PostResponse(
                id=first.id, status="success", postId=random_id(), releaseURL=self._release_url(integration)
            )
            return [result]
        entry = self._chat(id, access_token, first.message)
        result = self._publish(integration, entry, {"data": [entry]})
        result.id = first.id
        return [result]

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        if post_details.media:
            raise BadBody("twitch", "{}", "{}", "Twitch chat does not support media attachments")
        time.sleep(POST_DELAY_SECONDS)
        kind, color = self._message(post_details.settings)
        if kind == "announcement":
            self._announcement(id, access_token, post_details.message, color)
            return PostResponse(
                id=post_details.id, status="success", postId=random_id(), releaseURL=self._release_url(integration)
            )
        entry = self._chat(id, access_token, post_details.message, reply_to=parent_post_id or "")
        result = self._publish(integration, entry, {"data": [entry]})
        result.id = post_details.id
        return result
