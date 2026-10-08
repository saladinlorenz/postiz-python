import json
import secrets
from typing import Any

from app.core.config import settings
from app.integrations.base import BadBody, SocialAbstract, encode_component, pkce_challenge
from app.integrations.interfaces import AuthTokenDetails, PostDetails, PostResponse

KICK_API = "https://api.kick.com/public/v1"
KICK_ID = "https://id.kick.com"
KICK_CONTENT_LIMIT = 500


class KickProvider(SocialAbstract):
    identifier = "kick"
    name = "Kick"
    picture = "kick.svg"
    description = "Publish a chat message to your Kick channel (text only, max 500 characters)"
    maxLength = KICK_CONTENT_LIMIT
    maxConcurrentJob = 3
    scopes = ["chat:write", "user:read", "channel:read"]

    def _redirect_uri(self, refresh: str = "") -> str:
        base = f"{settings.frontend_url}/integrations/social/kick"
        return f"{base}?refresh={refresh}" if refresh else base

    @staticmethod
    def _form(fields: dict[str, Any]) -> dict[str, Any]:
        return {key: str(value) for key, value in fields.items() if value is not None}

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        url = (
            f"{KICK_ID}/oauth/authorize"
            "?response_type=code"
            f"&client_id={settings.kick_client_id}"
            f"&redirect_uri={encode_component(self._redirect_uri())}"
            f"&scope={encode_component(' '.join(self.scopes))}"
            f"&state={state}"
            f"&code_challenge={pkce_challenge(code_verifier)}"
            f"&code_challenge_method=S256"
        )
        return {"url": url, "codeVerifier": code_verifier, "state": state}

    def _token(self, fields: dict[str, Any]) -> dict[str, Any]:
        data = self.get_json(
            f"{KICK_ID}/oauth/token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=self._form(fields),
        )
        return data if isinstance(data, dict) else {}

    def _user(self, access_token: str) -> dict[str, Any]:
        data = self.get_json(f"{KICK_API}/users", headers={"Authorization": f"Bearer {access_token}"})
        if not isinstance(data, dict):
            return {}
        raw = data.get("data")
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        return raw if isinstance(raw, dict) else {}

    def _details(self, token: dict[str, Any]) -> AuthTokenDetails:
        user = self._user(str(token.get("access_token") or ""))
        name = str(user.get("name") or "")
        raw_id = user.get("user_id") or user.get("id") or ""
        return AuthTokenDetails(
            id=str(raw_id),
            name=name,
            accessToken=str(token.get("access_token") or ""),
            refreshToken=str(token.get("refresh_token") or ""),
            expiresIn=int(token.get("expires_in") or 0) or None,
            picture=str(user.get("profile_picture") or ""),
            username=name,
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        token = self._token(
            {
                "grant_type": "authorization_code",
                "client_id": settings.kick_client_id,
                "client_secret": settings.kick_client_secret,
                "redirect_uri": self._redirect_uri(refresh),
                "code": code,
                "code_verifier": code_verifier,
            }
        )
        if not token.get("access_token"):
            raise BadBody("kick", json.dumps(token), "{}", "Kick did not return an access token")
        return self._details(token)

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        token = self._token(
            {
                "grant_type": "refresh_token",
                "client_id": settings.kick_client_id,
                "client_secret": settings.kick_client_secret,
                "refresh_token": refresh_token,
            }
        )
        if not token.get("access_token"):
            return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)
        return self._details(token)

    @staticmethod
    def _broadcaster_id(user_id: str) -> Any:
        try:
            return int(str(user_id))
        except ValueError:
            return user_id

    @staticmethod
    def _release_url(integration: Any) -> str:
        profile = str(getattr(integration, "username", "") or "")
        return f"https://kick.com/{profile or 'channel'}"

    @staticmethod
    def _sent_state(payload: dict[str, Any]) -> Any:
        nested = (payload.get("data") or {}).get("is_sent")
        if nested is not None:
            return nested
        return payload.get("is_sent")

    def _chat(
        self, id: str, access_token: str, message: str, integration: Any, reply_to: str = ""
    ) -> tuple[dict[str, Any], str]:
        fields: dict[str, Any] = {
            "type": "user",
            "content": message[:KICK_CONTENT_LIMIT],
            "broadcaster_user_id": self._broadcaster_id(id),
        }
        if reply_to:
            fields["reply_to_message_id"] = reply_to
        payload = self.get_json(
            f"{KICK_API}/chat",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=fields,
        )
        if not isinstance(payload, dict):
            raise BadBody("kick", json.dumps(payload), "{}", "Kick did not return a chat response")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        message_id = str(data.get("message_id") or "")
        if not message_id:
            raise BadBody("kick", json.dumps(payload), "{}", "Kick did not return a message id")
        if self._sent_state(payload) is False:
            raise BadBody("kick", json.dumps(payload), "{}", "Kick did not send the chat message")
        return data, message_id

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        if not post_details:
            return []
        first = post_details[0]
        if first.media:
            raise BadBody("kick", "{}", "{}", "Kick chat does not support media attachments")
        _, message_id = self._chat(id, access_token, first.message, integration)
        return [
            PostResponse(
                id=first.id,
                status="success",
                postId=message_id,
                releaseURL=self._release_url(integration),
            )
        ]

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        if post_details.media:
            raise BadBody("kick", "{}", "{}", "Kick chat does not support media attachments")
        _, message_id = self._chat(id, access_token, post_details.message, integration, reply_to=parent_post_id or "")
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=message_id,
            releaseURL=self._release_url(integration),
        )
