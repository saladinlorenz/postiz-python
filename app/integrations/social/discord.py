import base64
import os
from typing import Any

from app.core.config import settings
from app.integrations.base import BadBody, SocialAbstract
from app.integrations.interfaces import AuthTokenDetails, PostDetails, PostResponse

DISCORD_API = "https://discord.com/api"
DISCORD_PERMISSIONS = "377957124096"


class DiscordProvider(SocialAbstract):
    identifier = "discord"
    name = "Discord"
    picture = "discord.svg"
    description = "Send messages to your Discord server channels"
    maxLength = 1980
    maxConcurrentJob = 5
    scopes = ["identify", "guilds"]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        if "50001" in text:
            return "bad-body", "Bot doesn't have access to this channel"
        if "50013" in text:
            return "bad-body", "Bot lacks permission to send messages in this channel"
        if "10003" in text:
            return "bad-body", "Channel no longer exists"
        if "40005" in text:
            return "bad-body", "Attachment exceeds Discord's size limit"
        if "20028" in text:
            return "retry", "Rate limited by Discord"
        return super().handle_errors(body, status)

    def _redirect_uri(self) -> str:
        return f"{settings.frontend_url}/integrations/social/discord"

    def _basic_auth(self) -> dict[str, str]:
        raw = f"{settings.discord_client_id}:{settings.discord_client_secret}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        import secrets

        state = secrets.token_urlsafe(9)
        url = (
            f"{DISCORD_API}/oauth2/authorize?client_id={settings.discord_client_id}"
            f"&permissions={DISCORD_PERMISSIONS}&response_type=code"
            f"&redirect_uri={self._redirect_uri()}&integration_type=0"
            f"&scope=bot+identify+guilds&state={state}"
        )
        return {"url": url, "codeVerifier": secrets.token_urlsafe(10), "state": state}

    def _token_request(self, params: dict[str, str]) -> dict[str, Any]:
        return self.fetch(
            f"{DISCORD_API}/oauth2/token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", **self._basic_auth()},
            data=params,
        ).json()

    def _me(self, access_token: str) -> dict[str, Any]:
        return self.get_json(f"{DISCORD_API}/oauth2/@me", headers={"Authorization": f"Bearer {access_token}"})

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        payload = self._token_request(
            {"code": code, "grant_type": "authorization_code", "redirect_uri": self._redirect_uri()}
        )
        scope = payload.get("scope", "")
        if scope:
            self.check_scopes(self.scopes, scope.split(" "))
        me = self._me(payload["access_token"])
        application = me.get("application", {})
        guild = me.get("guild") or payload.get("guild") or {}
        bot = application.get("bot") or {}
        picture = ""
        if bot.get("id") and bot.get("avatar"):
            picture = f"https://cdn.discordapp.com/avatars/{bot['id']}/{bot['avatar']}.png"
        return AuthTokenDetails(
            id=str(guild.get("id", "")),
            name=application.get("name", ""),
            accessToken=payload["access_token"],
            refreshToken=payload.get("refresh_token", ""),
            expiresIn=payload.get("expires_in"),
            picture=picture,
            username=bot.get("username", ""),
        )

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        payload = self._token_request(
            {"refresh_token": refresh_token, "grant_type": "refresh_token"}
        )
        me = self._me(payload["access_token"])
        application = me.get("application", {})
        bot = application.get("bot") or {}
        return AuthTokenDetails(
            id="",
            name=application.get("name", ""),
            accessToken=payload["access_token"],
            refreshToken=payload.get("refresh_token", refresh_token),
            expiresIn=payload.get("expires_in"),
            username=bot.get("username", ""),
        )

    def _bot_headers(self) -> dict[str, str]:
        token = settings.discord_bot_token
        if not token:
            raise BadBody("discord", "{}", "{}", "DISCORD_BOT_TOKEN is not configured")
        return {"Authorization": f"Bot {token}"}

    def channels(self, integration: Any) -> list[dict[str, str]]:
        raw = self.get_json(
            f"{DISCORD_API}/guilds/{integration.internalId}/channels", headers=self._bot_headers()
        )
        return [
            {"id": str(channel["id"]), "name": channel["name"]}
            for channel in raw
            if channel.get("type") in (0, 5, 15)
        ]

    def _send_message(
        self, channel: str, message: str, media: list, guild_id: str
    ) -> dict[str, Any]:
        import json as jsonlib
        import re

        from app.integrations.media import resolve_local

        content = re.sub(r"\[\[\[(@.*?)]]]", r"<\1>", message or "")

        if not media:
            return self.fetch(
                f"{DISCORD_API}/channels/{channel}/messages",
                method="POST",
                headers={**self._bot_headers(), "Content-Type": "application/json"},
                json={"content": content},
            ).json()

        files = {}
        attachments = []
        for index, item in enumerate(media):
            filename = item.path.split("/")[-1].split("?")[0]
            attachments.append({"id": index, "description": f"Picture {index}", "filename": filename})
            if item.path.startswith(("http://", "https://")):
                files[f"files[{index}]"] = (filename, self.fetch(item.path).content)
            else:
                local = resolve_local(item.path)
                with open(local, "rb") as handle:
                    files[f"files[{index}]"] = (os.path.basename(local), handle.read())

        form = {"payload_json": jsonlib.dumps({"content": content, "attachments": attachments})}
        return self.fetch(
            f"{DISCORD_API}/channels/{channel}/messages",
            method="POST",
            headers=self._bot_headers(),
            data=form,
            files=files,
        ).json()

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        channel = (first.settings or {}).get("channel", "")
        if not channel:
            raise BadBody("discord", "{}", "{}", "No Discord channel selected for this post")
        data = self._send_message(channel, first.message, first.media or [], id)
        return [
            PostResponse(
                id=first.id,
                status="success",
                postId=str(data.get("id", "")),
                releaseURL=f"https://discord.com/channels/{id}/{channel}/{data.get('id', '')}",
            )
        ]

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        first = post_details
        channel = (first.settings or {}).get("channel", "")
        thread_channel = channel
        if channel and parent_post_id:
            thread = self.get_json(
                f"{DISCORD_API}/channels/{channel}/messages/{parent_post_id}/threads",
                method="POST",
                headers={**self._bot_headers(), "Content-Type": "application/json"},
                json={"name": "Thread", "auto_archive_duration": 1440},
            )
            thread_channel = str(thread.get("id", channel))
        data = self._send_message(thread_channel, first.message, first.media or [], id)
        return PostResponse(
            id=first.id,
            status="success",
            postId=str(data.get("id", "")),
            releaseURL=f"https://discord.com/channels/{id}/{thread_channel}/{data.get('id', '')}",
        )
