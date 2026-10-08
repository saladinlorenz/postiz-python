import base64
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import quote

from app.core.config import settings
from app.integrations.base import BadBody, RefreshTokenError, SocialAbstract
from app.integrations.interfaces import (
    AuthTokenDetails,
    PendingCheckResponse,
    PostDetails,
    PostResponse,
)
from app.integrations.media import is_url, media_size, read_or_fetch, resolve_local

REDDIT_API = "https://oauth.reddit.com"


class RedditProvider(SocialAbstract):
    identifier = "reddit"
    name = "Reddit"
    picture = "reddit.svg"
    description = "Submit posts to subreddits"
    maxLength = 10000
    maxConcurrentJob = 1
    scopes = ["read", "identity", "submit", "flair"]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        if "RATELIMIT" in text:
            return "retry", "Reddit rate limited the submission"
        return super().handle_errors(body, status)

    def _redirect_uri(self) -> str:
        return f"{settings.frontend_url}/integrations/social/reddit"

    def _basic_auth(self) -> dict[str, str]:
        raw = f"{settings.reddit_client_id}:{settings.reddit_client_secret}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(9)
        url = (
            "https://www.reddit.com/api/v1/authorize"
            f"?client_id={settings.reddit_client_id}&response_type=code"
            f"&state={state}&redirect_uri={self._redirect_uri()}"
            f"&duration=permanent&scope={quote(' '.join(self.scopes))}"
        )
        return {"url": url, "codeVerifier": secrets.token_urlsafe(30), "state": state}

    def _token_request(self, params: dict[str, str]) -> dict[str, Any]:
        return self.fetch(
            "https://www.reddit.com/api/v1/access_token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", **self._basic_auth()},
            data=params,
        ).json()

    def _me(self, access_token: str) -> dict[str, Any]:
        return self.get_json(f"{REDDIT_API}/api/v1/me", headers={"Authorization": f"Bearer {access_token}"})

    def _details(self, token_payload: dict[str, Any], me: dict[str, Any]) -> AuthTokenDetails:
        icon = (me.get("icon_img") or "").split("?")[0]
        return AuthTokenDetails(
            id=str(me.get("id", "")),
            name=me.get("name", ""),
            accessToken=token_payload["access_token"],
            refreshToken=token_payload.get("refresh_token", ""),
            expiresIn=token_payload.get("expires_in"),
            picture=icon,
            username=me.get("name", ""),
            additionalSettings={"username": me.get("name", "")},
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        payload = self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri()}
        )
        scope = payload.get("scope", "")
        if scope:
            self.check_scopes(self.scopes, scope.split(" "))
        return self._details(payload, self._me(payload["access_token"]))

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        payload = self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        details = self._details(payload, self._me(payload["access_token"]))
        details.refreshToken = refresh_token
        return details

    def _subreddit(self, name: str) -> str:
        return re.sub(r"^/?r/", "", name).strip("/").lower()

    def _upload_media(self, access_token: str, path: str) -> str:
        filename = path.split("/")[-1].split("?")[0]
        local = resolve_local(path) if not is_url(path) else path
        import mimetypes

        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if is_url(path):
            content = self.fetch(path).content
        else:
            content = read_or_fetch(path)

        args = self.fetch(
            f"{REDDIT_API}/api/media/asset",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}"},
            data={"filepath": filename, "mimetype": mime},
            files={"file": (filename, content, mime)},
        ).json().get("args", {})

        action = args.get("action", "")
        fields = args.get("fields", [])
        if not action:
            raise BadBody(self.identifier, str(args), "{}", "Reddit media upload did not return an action")

        form = {field["name"]: field["value"] for field in fields}
        response = self.fetch(
            action if action.startswith("http") else f"https:{action}",
            method="POST",
            data=form,
            files={"file": (filename, content, mime)},
        )
        match = re.search(r"<Location>(.*?)</Location>", response.text or "")
        if not match:
            raise BadBody(self.identifier, response.text[:500], "{}", "Reddit media upload did not return a location")
        return match.group(1)

    def _find_submitted(
        self, access_token: str, username: str, sr: str, title: str, armed_at: float
    ) -> dict[str, Any] | None:
        data = self.get_json(
            f"{REDDIT_API}/user/{username}/submitted?limit=25&sort=new",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        cutoff = armed_at - 60 if armed_at else time.time() - 3600
        for child in (data.get("data") or {}).get("children", []):
            item = child.get("data") or {}
            if (
                (item.get("subreddit") or "").lower() == sr
                and (not title or item.get("title") == title)
                and (item.get("created_utc") or 0) > cutoff
            ):
                if item.get("id"):
                    return {
                        "id": item["id"],
                        "url": f"https://www.reddit.com{item['permalink']}" if item.get("permalink") else f"https://www.reddit.com/r/{sr}",
                    }
        return None

    def _submit(
        self, access_token: str, pending: dict[str, Any], entry: dict[str, Any], username: str
    ) -> dict[str, Any]:
        value = entry.get("value", {})
        media_path = pending.get("mediaPath") or ""
        media_thumb = pending.get("mediaThumbnail") or ""
        kind = value.get("type", "self")
        if kind == "media" and media_path:
            kind = "video" if media_path.lower().endswith(".mp4") else "image"

        payload: dict[str, str] = {
            "api_type": "json",
            "title": value.get("title", ""),
            "kind": kind if kind in ("link", "self", "image", "video", "videogif") else "self",
            "text": pending.get("message", ""),
            "sr": self._subreddit(value.get("subreddit", "")),
        }
        if value.get("flair"):
            payload["flair_id"] = value["flair"].get("id", "")
        if value.get("type") == "link":
            payload["url"] = value.get("url", "")
        if value.get("type") == "media" and media_path:
            payload["url"] = self._upload_media(access_token, media_path)
            if media_path.lower().endswith(".mp4") and media_thumb:
                payload["video_poster_url"] = self._upload_media(access_token, media_thumb)

        result = self.fetch(
            f"{REDDIT_API}/api/submit",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/x-www-form-urlencoded"},
            data=payload,
        ).json()

        errors = ((result.get("json") or {}).get("errors")) or []
        if errors:
            if all(err[0] == "RATELIMIT" for err in errors if err):
                return {"rateLimited": True}
            messages = ", ".join((err[1] or err[0]) for err in errors if err)
            raise BadBody(
                self.identifier,
                json.dumps(result),
                "{}",
                f"Reddit rejected the post to r/{payload['sr']}: {messages}",
            )

        data = (result.get("json") or {}).get("data") or {}
        if data.get("id"):
            return {
                "postId": data["id"],
                "releaseURL": data.get("url") or f"https://www.reddit.com/r/{payload['sr']}",
            }
        return {"accepted": True}

    def _completed(self, results: list) -> PendingCheckResponse:
        return PendingCheckResponse(
            status="completed",
            postId=",".join(r["postId"] for r in results if r.get("postId")),
            releaseURL=",".join(r["releaseURL"] for r in results if r.get("releaseURL")),
        )

    def post_pending(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        subreddit_setting = (first.settings or {}).get("subreddit", [])
        return [
            PostResponse(
                id=first.id,
                status="pending",
                pendingData={
                    "subreddits": subreddit_setting,
                    "message": first.message,
                    "mediaPath": first.media[0].path if first.media else None,
                    "mediaThumbnail": first.media[0].thumbnail if first.media else None,
                    "cursor": 0,
                    "results": [],
                },
            )
        ]

    def check_post_status(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        username = ""
        if integration is not None:
            try:
                username = (json.loads(getattr(integration, "additionalSettings", "{}") or "{}") or {}).get("username", "")
            except (json.JSONDecodeError, TypeError):
                username = ""

        armed = data.get("armed")
        if armed:
            found = None
            if username:
                try:
                    found = self._find_submitted(access_token, username, armed["sr"], armed["title"], armed["armedAt"])
                except RefreshTokenError:
                    raise
                except Exception:
                    pass
            if found:
                data["results"] = data.get("results", []) + [{"postId": found["id"], "releaseURL": found["url"]}]
                data["cursor"] = data.get("cursor", 0) + 1
                data["armed"] = None
            elif armed.get("submitted"):
                if armed.get("lookups", 0) < 9:
                    data["armed"] = {**armed, "lookups": armed.get("lookups", 0) + 1}
                    return PendingCheckResponse(status="pending", pendingData=data)
                data["results"] = data.get("results", []) + [
                    {"postId": "", "releaseURL": f"https://www.reddit.com/r/{armed['sr']}"}
                ]
                data["cursor"] = data.get("cursor", 0) + 1
                data["armed"] = None
            elif armed.get("media") and (
                time.time() * 1000 - armed["armedAt"] < 10 * 60 * 1000 or armed.get("lookups", 0) < 9
            ):
                data["armed"] = {**armed, "lookups": armed.get("lookups", 0) + 1}
                return PendingCheckResponse(status="pending", pendingData=data)
            elif not armed.get("media") and armed.get("lookups", 0) < 1:
                data["armed"] = {**armed, "lookups": armed.get("lookups", 0) + 1}
                return PendingCheckResponse(status="pending", pendingData=data)
            else:
                data["armed"] = None

        subreddits = data.get("subreddits") or []
        if data.get("cursor", 0) >= len(subreddits):
            return self._completed(data.get("results") or [])

        value = subreddits[data["cursor"]].get("value", {})
        data["armed"] = {
            "sr": self._subreddit(value.get("subreddit", "")),
            "title": value.get("title", ""),
            "armedAt": time.time() * 1000,
            "media": value.get("type") == "media",
            "submitted": False,
            "lookups": 0,
        }
        return PendingCheckResponse(status="ready", pendingData=data)

    def finalize_post(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        subreddits = data.get("subreddits") or []
        cursor = data.get("cursor", 0)
        entry = subreddits[cursor] if cursor < len(subreddits) else None

        if not entry or not data.get("armed"):
            return PendingCheckResponse(status="pending", pendingData=data)

        username = ""
        if integration is not None:
            try:
                username = (json.loads(getattr(integration, "additionalSettings", "{}") or "{}") or {}).get("username", "")
            except (json.JSONDecodeError, TypeError):
                username = ""

        outcome = self._submit(access_token, data, entry, username)
        if outcome.get("rateLimited"):
            data["armed"] = None
            return PendingCheckResponse(status="pending", pendingData=data)

        if outcome.get("postId"):
            data["results"] = data.get("results", []) + [
                {"postId": outcome["postId"], "releaseURL": outcome["releaseURL"]}
            ]
            data["cursor"] = cursor + 1
            data["armed"] = None
            if data["cursor"] >= len(subreddits):
                return self._completed(data["results"])
            return PendingCheckResponse(status="pending", pendingData=data)

        data["armed"] = {**data["armed"], "submitted": True}
        return PendingCheckResponse(status="pending", pendingData=data)

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        started = time.time()
        pending = self.post_pending(id, access_token, post_details, integration)[0].pendingData
        while True:
            check = self.check_post_status(access_token, pending, integration)
            if check.status == "completed":
                return [
                    PostResponse(
                        id=post_details[0].id,
                        status="success",
                        postId=check.postId,
                        releaseURL=check.releaseURL,
                    )
                ]
            if check.status == "ready":
                finalize = self.finalize_post(access_token, check.pendingData, integration)
                if finalize.status == "completed":
                    return [
                        PostResponse(
                            id=post_details[0].id,
                            status="success",
                            postId=finalize.postId,
                            releaseURL=finalize.releaseURL,
                        )
                    ]
                pending = finalize.pendingData
            else:
                pending = check.pendingData

            if time.time() - started > 8 * 60:
                raise BadBody(
                    self.identifier,
                    "{}",
                    "{}",
                    "Reddit took too long to answer, please check your account before posting again",
                )
            time.sleep(5)

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        thing_id = parent_post_id if parent_post_id.startswith("t3_") else f"t3_{parent_post_id}"
        result = self.fetch(
            f"{REDDIT_API}/api/comment",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"text": post_details.message, "thing_id": thing_id, "api_type": "json"},
        ).json()
        things = (((result.get("json") or {}).get("data") or {}).get("things")) or []
        comment = (things[0].get("data") or {}) if things else {}
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=comment.get("id", ""),
            releaseURL="https://www.reddit.com" + (comment.get("permalink") or ""),
        )
