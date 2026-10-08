import secrets
from datetime import datetime, timedelta
from typing import Any

from app.core.config import settings
from app.integrations.base import BadBody, RefreshTokenError, SocialAbstract
from app.integrations.interfaces import AuthTokenDetails, PostDetails, PostResponse

SLACK_API = "https://slack.com/api"


class SlackProvider(SocialAbstract):
    identifier = "slack"
    name = "Slack"
    picture = "slack.svg"
    description = "Post messages to Slack channels"
    maxLength = 400000
    maxConcurrentJob = 10
    scopes = [
        "channels:read",
        "chat:write",
        "users:read",
        "groups:read",
        "channels:join",
        "chat:write.customize",
    ]

    def _redirect_uri(self) -> str:
        base = settings.frontend_url
        if not base.startswith("https"):
            base = f"https://redirectmeto.com/{base}"
        return f"{base}/integrations/social/slack"

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(9)
        url = (
            "https://slack.com/oauth/v2/authorize"
            f"?client_id={settings.slack_client_id}&redirect_uri={self._redirect_uri()}"
            f"&scope={','.join(self.scopes)}&state={state}"
        )
        return {"url": url, "codeVerifier": secrets.token_urlsafe(10), "state": state}

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        return AuthTokenDetails(
            id="", name="", accessToken="", refreshToken="", expiresIn=1000000
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        payload = self.fetch(
            f"{SLACK_API}/oauth.v2.access",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "code": code,
                "redirect_uri": self._redirect_uri(),
            },
        ).json()
        scope = payload.get("scope", "")
        if scope:
            self.check_scopes(self.scopes, scope.split(","))
        if not payload.get("ok", True):
            raise BadBody("slack", str(payload), "{}", payload.get("error", "Slack auth failed"))

        user = self.get_json(
            f"{SLACK_API}/users.info?user={payload['bot_user_id']}",
            headers={"Authorization": f"Bearer {payload['access_token']}"},
        ).get("user", {})
        return AuthTokenDetails(
            id=str((payload.get("team") or {}).get("id", "")),
            name=user.get("real_name", ""),
            accessToken=payload["access_token"],
            refreshToken="",
            expiresIn=int((datetime.now() + timedelta(days=36500) - datetime.now()).total_seconds()),
            picture=(user.get("profile") or {}).get("image_original") or "",
            username=user.get("name", ""),
            additionalSettings={"team": (payload.get("team") or {}).get("name", "")},
        )

    def _check_api_error(self, body: Any) -> None:
        if not isinstance(body, dict) or body.get("ok") is not False:
            return
        errors = body.get("errors") or []
        message = ": ".join([str(body.get("error", ""))] + [str(e) for e in errors if e]).strip(": ") or "Slack rejected the request"
        if body.get("error") in ("invalid_auth", "token_revoked", "token_expired", "account_inactive"):
            raise RefreshTokenError(message, message)
        if body.get("error") == "ratelimited":
            raise BadBody("slack", str(body), "{}", message)
        raise BadBody("slack", str(body), "{}", message)

    def channels(self, integration: Any) -> list[dict[str, str]]:
        access_token = integration.token if hasattr(integration, "token") else str(integration)
        raw = self.get_json(
            f"{SLACK_API}/conversations.list?types=public_channel,private_channel",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return [{"id": c["id"], "name": c["name"]} for c in raw.get("channels", [])]

    def _blocks(self, message: PostDetails) -> list[dict]:
        blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": message.message}}]
        for item in message.media or []:
            blocks.append({"type": "image", "image_url": item.path, "alt_text": item.alt or ""})
        return blocks

    def _post_message(
        self, access_token: str, channel: str, message: PostDetails, integration: Any, thread_ts: str = ""
    ) -> dict[str, Any]:
        self.fetch(
            f"{SLACK_API}/conversations.join",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"channel": channel},
        )
        payload: dict[str, Any] = {
            "channel": channel,
            "username": getattr(integration, "name", "") if integration else "",
            "icon_url": getattr(integration, "picture", "") if integration else "",
            "blocks": self._blocks(message),
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        posted = self.fetch(
            f"{SLACK_API}/chat.postMessage",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload,
        ).json()
        self._check_api_error(posted)
        permalink = ""
        if posted.get("ts") and posted.get("channel"):
            link = self.get_json(
                f"{SLACK_API}/chat.getPermalink?channel={posted['channel']}&message_ts={posted['ts']}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            permalink = link.get("permalink") or ""
        return {"ts": posted.get("ts", ""), "permalink": permalink}

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        channel = (first.settings or {}).get("channel", "")
        if not channel:
            raise BadBody("slack", "{}", "{}", "No Slack channel selected for this post")
        result = self._post_message(access_token, channel, first, integration)
        return [
            PostResponse(id=first.id, status="success", postId=result["ts"], releaseURL=result["permalink"])
        ]

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        channel = (post_details.settings or {}).get("channel", "")
        thread_ts = parent_post_id
        result = self._post_message(access_token, channel, post_details, integration, thread_ts=thread_ts)
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=result["ts"],
            releaseURL=result["permalink"],
        )
