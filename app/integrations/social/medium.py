import base64
import json
import secrets
from datetime import datetime, timedelta
from typing import Any

from app.integrations.base import SocialAbstract
from app.integrations.interfaces import AuthTokenDetails, PostDetails, PostResponse


class MediumProvider(SocialAbstract):
    identifier = "medium"
    name = "Medium"
    picture = "medium.svg"
    description = "Publish stories to your Medium profile"
    maxLength = 100000
    credentials = True
    maxConcurrentJob = 3

    def custom_fields(self) -> list[dict[str, Any]]:
        return [{"key": "apiKey", "label": "API key", "validation": "^.{3,}$", "type": "password"}]

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(9)
        return {"url": state, "codeVerifier": secrets.token_urlsafe(10), "state": state}

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        body = json.loads(base64.b64decode(code).decode())
        try:
            data = self.get_json(
                "https://api.medium.com/v1/me",
                headers={"Authorization": f"Bearer {body['apiKey']}"},
            )["data"]
        except Exception:
            return "Invalid credentials"  # type: ignore[return-value]
        return AuthTokenDetails(
            id=str(data.get("id", "")),
            name=data.get("name", ""),
            accessToken=body["apiKey"],
            refreshToken="",
            expiresIn=int((datetime.now() + timedelta(days=36500) - datetime.now()).total_seconds()),
            picture=data.get("imageUrl") or "",
            username=data.get("username", ""),
        )

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        settings = first.settings if isinstance(first.settings, dict) else {}
        url = (
            f"https://api.medium.com/v1/publications/{settings['publication']}/posts"
            if settings.get("publication")
            else f"https://api.medium.com/v1/users/{id}/posts"
        )
        payload: dict[str, Any] = {
            "title": settings.get("title") or first.message.split("\n")[0][:100] or "Untitled",
            "contentFormat": "markdown",
            "content": first.message,
            "publishStatus": "draft" if settings.get("publication") else "public",
        }
        if settings.get("canonical"):
            payload["canonicalUrl"] = settings["canonical"]
        if settings.get("tags"):
            payload["tags"] = [tag.get("value") for tag in settings["tags"]]

        data = self.get_json(
            url,
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload,
        )["data"]
        return [
            PostResponse(id=first.id, status="success", postId=data.get("id", ""), releaseURL=data.get("url", ""))
        ]
