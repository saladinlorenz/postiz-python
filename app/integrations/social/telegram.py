import mimetypes
import os
import secrets
from typing import Any
from urllib.parse import quote

from app.core.config import settings
from app.integrations.base import BadBody, SocialAbstract
from app.integrations.interfaces import AuthTokenDetails, PostDetails, PostResponse
from app.integrations.media import is_url, resolve_local

TELEGRAM_API = "https://api.telegram.org"


def _mimetypes(path: str) -> str:
    guess = mimetypes.guess_type(path.split("?")[0])[0]
    return guess or "application/octet-stream"


class TelegramProvider(SocialAbstract):
    identifier = "telegram"
    name = "Telegram"
    picture = "telegram.svg"
    description = "Post to Telegram groups and channels via bot"
    maxLength = 4096
    credentials = True
    maxConcurrentJob = 3

    def custom_fields(self) -> list[dict[str, Any]]:
        return [{"key": "botToken", "label": "Bot token (from @BotFather)", "type": "password", "validation": "^.{8,}$"}]

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(12)
        return {"url": state, "codeVerifier": secrets.token_urlsafe(10), "state": state}

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)

    def _bot_token(self, kwargs: dict[str, Any]) -> str:
        details = kwargs.get("details") or {}
        token = details.get("botToken") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise BadBody("telegram", "{}", "{}", "Telegram bot token is required")
        return token

    def _call(self, bot_token: str, method: str, payload: dict | None = None, files: dict | None = None) -> Any:
        url = f"{TELEGRAM_API}/bot{bot_token}/{method}"
        if files:
            form = {key: str(value) for key, value in (payload or {}).items()}
            response = self.fetch(url, method="POST", data=form, files=files)
        else:
            response = self.fetch(url, method="POST", json=payload or {})
        body = response.json()
        if not body.get("ok"):
            result = body.get("result", {})
            code = body.get("error_code", 400)
            description = str(result.get("description", body.get("description", ""))).replace("Bad Request: ", "")
            if code == 400:
                if description == "failed to get HTTP URL content":
                    raise BadBody(
                        "telegram",
                        str(body),
                        "{}",
                        "Telegram could not download your media file, please check the file and try again",
                    )
                raise BadBody("telegram", str(body), "{}", f"Telegram rejected the post: {description}")
            if code in (401, 403):
                from app.integrations.base import RefreshTokenError

                raise RefreshTokenError(description, description)
            raise BadBody("telegram", str(body), "{}", description)
        return body.get("result")

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        bot_token = self._bot_token(kwargs)
        chat = self._call(bot_token, "getChat", {"chat_id": code})
        if not chat or not chat.get("id"):
            return "No chat found"
        picture = ""
        big_file_id = (chat.get("photo") or {}).get("big_file_id")
        if big_file_id:
            picture = self._call(bot_token, "getFile", {"file_id": big_file_id}).get("file_path", "")
            if picture:
                picture = f"{TELEGRAM_API}/file/bot{bot_token}/{picture}"
        return AuthTokenDetails(
            id=str(chat.get("username") or chat.get("id")),
            name=chat.get("title") or chat.get("first_name") or "",
            accessToken=str(chat.get("id")),
            refreshToken="",
            expiresIn=200 * 365 * 24 * 3600,
            picture=picture,
            username=chat.get("username") or "",
            additionalSettings={"botToken": bot_token},
        )

    def get_bot_id(self, bot_token: str, word: str, offset: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"allowed_updates": ["message", "channel_post"]}
        if offset:
            params["offset"] = offset
        updates = self._call(bot_token, "getUpdates", params)
        match = None
        for update in updates:
            message = update.get("message") or update.get("channel_post") or {}
            if message.get("text") == f"/connect {word}" and message.get("chat", {}).get("id"):
                match = message
                break
        if not match:
            if updates:
                return {"lastChatId": updates[-1]["update_id"] + 1}
            return {}
        chat_id = match["chat"]["id"]
        bot = self._call(bot_token, "getMe")
        is_admin = self._bot_is_admin(bot_token, chat_id, bot["id"])
        try:
            self._call(bot_token, "deleteMessage", {"chat_id": chat_id, "message_id": match["message_id"]})
            sent = self._call(
                bot_token,
                "sendMessage",
                {"chat_id": chat_id, "text": "Connection successful. This message will be deleted in 10 seconds."},
            )
            if is_admin:
                import threading

                threading.Timer(
                    10,
                    lambda: self._safe_delete(bot_token, chat_id, sent.get("message_id")),
                ).start()
        except BadBody:
            pass
        return {"chatId": str(chat_id)}

    def _safe_delete(self, bot_token: str, chat_id: Any, message_id: Any) -> None:
        try:
            self._call(bot_token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except Exception:
            pass

    def _bot_is_admin(self, bot_token: str, chat_id: Any, bot_id: Any) -> bool:
        try:
            member = self._call(bot_token, "getChatMember", {"chat_id": chat_id, "user_id": bot_id})
        except Exception:
            return False
        if member.get("status") in ("administrator", "creator"):
            return bool(member.get("can_delete_messages"))
        return False

    def _convert_text(self, text: str) -> str:
        import re

        text = re.sub(r"<(?!/?(?:u|strong|p|b|i|em|code|pre|a\b))[^>]*>", "", text)
        text = text.replace("<strong>", "<b>").replace("</strong>", "</b>")
        text = re.sub(r"<p>(.*?)</p>", r"\1\n", text, flags=re.S)
        return text

    def _media_kind(self, path: str) -> str:
        mime = _mimetypes(path)
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("video/"):
            return "video"
        return "document"

    def _send_media(self, bot_token: str, chat_id: str, kind: str, path: str, caption: str, reply_to: int | None) -> dict:
        method = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}[kind]
        payload: dict[str, Any] = {"chat_id": chat_id, "parse_mode": "HTML"}
        if caption:
            payload["caption"] = caption
        if reply_to:
            payload["reply_to_message_id"] = reply_to

        if is_url(path):
            payload[kind] = path
            return self._call(bot_token, method, payload)

        local = resolve_local(path)
        filename = os.path.basename(local)
        with open(local, "rb") as handle:
            files = {kind: (filename, handle.read(), _mimetypes(filename))}
            return self._call(bot_token, method, payload, files=files)

    def _send_message(self, bot_token: str, chat_id: str, message: PostDetails, reply_to: int | None = None) -> int:
        text = self._convert_text(message.message or "")
        media = message.media or []

        if not media:
            payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            if reply_to:
                payload["reply_to_message_id"] = reply_to
            return self._call(bot_token, "sendMessage", payload)["message_id"]

        if len(media) == 1:
            item = media[0]
            response = self._send_media(bot_token, chat_id, self._media_kind(item.path), item.path, text, reply_to)
            return response["message_id"]

        first_id = None
        for index in range(0, len(media), 10):
            batch = media[index : index + 10]
            media_group = []
            files = {}
            for i, item in enumerate(batch):
                kind = self._media_kind(item.path)
                entry: dict[str, Any] = {"type": kind, "media": f"attach://{kind}{i}"}
                if index == 0 and i == 0:
                    entry["caption"] = text
                    entry["parse_mode"] = "HTML"
                media_group.append(entry)
                if not is_url(item.path):
                    local = resolve_local(item.path)
                    with open(local, "rb") as handle:
                        files[f"{kind}{i}"] = (os.path.basename(local), handle.read(), _mimetypes(local))
            payload = {"chat_id": chat_id, "media": __import__("json").dumps(media_group)}
            if reply_to and index == 0:
                payload["reply_to_message_id"] = str(reply_to)
            if files:
                result = self._call(bot_token, "sendMediaGroup", payload, files=files)
            else:
                for entry, item in zip(media_group, batch):
                    entry["media"] = item.path
                payload["media"] = __import__("json").dumps(media_group)
                result = self._call(bot_token, "sendMediaGroup", payload)
            if index == 0 and result:
                first_id = result[0]["message_id"]
        return first_id or 0

    def _release_url(self, internal_id: str, chat_id: str, message_id: int) -> str:
        if internal_id and internal_id != "undefined":
            return f"https://t.me/{internal_id}/{message_id}"
        return f"https://t.me/c/{str(chat_id).replace('-100', '')}/{message_id}"

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        bot_token = self._integration_bot_token(integration)
        message_id = self._send_message(bot_token, str(access_token), first)
        if not message_id:
            return []
        return [
            PostResponse(
                id=first.id,
                status="success",
                postId=str(message_id),
                releaseURL=self._release_url(id, access_token, message_id),
            )
        ]

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        bot_token = self._integration_bot_token(integration)
        reply_to = int(parent_post_id) if str(parent_post_id).lstrip("-").isdigit() else None
        message_id = self._send_message(bot_token, str(access_token), post_details, reply_to=reply_to)
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=str(message_id),
            releaseURL=self._release_url(id, access_token, message_id),
        )

    def _integration_bot_token(self, integration: Any) -> str:
        import json

        if integration is not None:
            extra = json.loads(getattr(integration, "additionalSettings", "{}") or "{}")
            if extra.get("botToken"):
                return extra["botToken"]
        return os.environ.get("TELEGRAM_BOT_TOKEN", "")
