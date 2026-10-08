import re
import secrets
from typing import Any

from app.core.config import settings
from app.integrations.base import BadBody, SocialAbstract
from app.integrations.interfaces import (
    AuthTokenDetails,
    PendingCheckResponse,
    PostDetails,
    PostResponse,
)
from app.integrations.media import public_url

META_GRAPH_API_VERSION = "v25.0"
GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
PRESET_MAX_CHARS = 130


class FacebookProvider(SocialAbstract):
    identifier = "facebook"
    name = "Facebook Page"
    picture = "facebook.svg"
    description = "Publish to your Facebook Pages"
    maxLength = 63206
    maxConcurrentJob = 500
    scopes = [
        "pages_show_list",
        "business_management",
        "pages_manage_posts",
        "pages_manage_engagement",
        "pages_read_engagement",
        "read_insights",
    ]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        rules: list[tuple[str, str, str]] = [
            ("Error validating access token", "refresh-token", "Please re-authenticate your Facebook account"),
            ("REVOKED_ACCESS_TOKEN", "refresh-token", "Access token has been revoked, please re-authenticate"),
            (
                "Unpublished posts must be posted to a page as the page itself",
                "refresh-token",
                "Postiz is not authorized to publish as this page, please reconnect the channel",
            ),
            (
                "(#200)",
                "bad-body",
                "Facebook rejected the post due to missing permissions. Make sure your Facebook account has "
                "full content access to the Page, then reconnect the channel.",
            ),
            ("1366046", "bad-body", "Photos should be smaller than 4 MB and saved as JPG, PNG"),
            ("1390008", "bad-body", "You are posting too fast, please slow down"),
            ("1346003", "bad-body", "Content flagged as abusive by Facebook"),
            ("1404006", "bad-body", "We couldn't post your comment, A security check in facebook required to proceed."),
            ("2069019", "bad-body", "Invalid file"),
            ("1404102", "bad-body", "Content violates Facebook Community Standards"),
            ("1404078", "refresh-token", "Page publishing authorization required, please re-authenticate"),
            ("1366051", "bad-body", "These photos were already posted."),
            ("1609008", "bad-body", "Cannot post Facebook.com links"),
            ("2061006", "bad-body", "Invalid URL format in post content"),
            ("1349125", "bad-body", "Invalid content format"),
            (
                "1404112",
                "bad-body",
                "For security reasons, your account has limited access to the site for a few days",
            ),
            ("Name parameter too long", "bad-body", "Post content is too long"),
            ("1363047", "bad-body", "Facebook service temporarily unavailable"),
            ("1609010", "bad-body", "Facebook service temporarily unavailable"),
            (
                "4854002",
                "bad-body",
                "Confirm your identity before you can publish as this Page. Open the Facebook app on your phone "
                "and follow the instructions",
            ),
            ("(#100) No permission to publish the video", "bad-body", "Facebook return: No permission to publish the video"),
            ("must be granted before impersonating", "refresh-token", "Facebook Page permissions are missing, please reconnect the channel and allow all permissions"),
            ("Sorry, something went wrong", "bad-body", "Facebook is temporarily unavailable, please try again later"),
            ("490", "refresh-token", "Access token expired, please re-authenticate"),
        ]
        for needle, kind, message in rules:
            if needle in text:
                return kind, message

        if '"error_subcode":459' in text:
            return (
                "bad-body",
                "Facebook is asking you to resolve a security check. Log in at facebook.com, complete it, then try again",
            )
        if '"error_subcode":492' in text:
            return (
                "bad-body",
                "Your Facebook user no longer has a role on this Page. Ask a Page admin to grant you a role, then reconnect the channel",
            )
        if '"error_subcode":33' in text and "does not exist" in text:
            return (
                "bad-body",
                "The Facebook Page or post this was targeting no longer exists, please reconnect the channel and schedule again",
            )
        if status == 401:
            return "bad-body", "An unknown error occurred, please try again later or contact support"
        return super().handle_errors(body, status)

    def _redirect_uri(self, refresh: str = "") -> str:
        return f"{settings.frontend_url}/integrations/social/facebook" + (f"?refresh={refresh}" if refresh else "")

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(9)
        url = (
            f"https://www.facebook.com/{META_GRAPH_API_VERSION}/dialog/oauth"
            f"?client_id={settings.facebook_app_id}"
            f"&redirect_uri={self._redirect_uri(str(refresh) if refresh else '')}"
            f"&state={state}"
            f"&scope={','.join(self.scopes)}"
        )
        return {"url": url, "codeVerifier": secrets.token_urlsafe(10), "state": state}

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)

    def _exchange(self, params: dict[str, str]) -> dict[str, Any]:
        return self.get_json(f"{GRAPH}/oauth/access_token", params=params)

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        short = self._exchange(
            {
                "client_id": settings.facebook_app_id,
                "redirect_uri": self._redirect_uri(refresh),
                "client_secret": settings.facebook_app_secret,
                "code": code,
            }
        )
        long_lived = self._exchange(
            {
                "grant_type": "fb_exchange_token",
                "client_id": settings.facebook_app_id,
                "client_secret": settings.facebook_app_secret,
                "fb_exchange_token": short["access_token"],
                "fields": "access_token,expires_in",
            }
        )
        access_token = long_lived["access_token"]

        permissions = self.get_json(
            f"{GRAPH}/me/permissions", params={"access_token": access_token}
        ).get("data", [])
        granted = [p["permission"] for p in permissions if p.get("status") == "granted"]
        self.check_scopes(self.scopes, granted)

        me = self.get_json(
            f"{GRAPH}/me", params={"fields": "id,name,picture", "access_token": access_token}
        )
        return AuthTokenDetails(
            id=str(me.get("id", "")),
            name=me.get("name", ""),
            accessToken=access_token,
            refreshToken="",
            expiresIn=None,
            picture=(me.get("picture") or {}).get("data", {}).get("url", ""),
            username="",
            additionalSettings={"userToken": access_token},
        )

    def _walk(self, url: str, pages: list[dict[str, Any]], seen: set[str]) -> None:
        while url:
            response = self.get_json(url)
            for page in response.get("data") or []:
                if page.get("id") and page["id"] not in seen:
                    seen.add(page["id"])
                    pages.append(page)
            url = (response.get("paging") or {}).get("next", "")

    def _pages(self, user_token: str) -> list[dict[str, Any]]:
        from urllib.parse import urlencode

        pages: list[dict[str, Any]] = []
        seen: set[str] = set()
        query = urlencode(
            {
                "fields": "id,username,name,access_token,picture.type(large)",
                "limit": 100,
                "access_token": user_token,
            }
        )
        self._walk(f"{GRAPH}/me/accounts?{query}", pages, seen)

        try:
            business_url = f"{GRAPH}/me/businesses?access_token={user_token}"
            while business_url:
                response = self.get_json(business_url)
                for business in response.get("data") or []:
                    for edge in ("owned_pages", "client_pages"):
                        try:
                            self._walk(f"{GRAPH}/{business['id']}/{edge}?{query}", pages, seen)
                        except Exception:
                            continue
                business_url = (response.get("paging") or {}).get("next", "")
        except Exception:
            pass
        return pages

    def connections(self, details: AuthTokenDetails) -> list[AuthTokenDetails]:
        user_token = details.additionalSettings.get("userToken") or details.accessToken
        results: list[AuthTokenDetails] = []
        for page in self._pages(user_token):
            page_token = page.get("access_token")
            if not page_token:
                continue
            results.append(
                AuthTokenDetails(
                    id=str(page["id"]),
                    name=page.get("name", ""),
                    accessToken=page_token,
                    refreshToken="",
                    expiresIn=None,
                    picture=(page.get("picture") or {}).get("data", {}).get("url", ""),
                    username=page.get("username", ""),
                    additionalSettings={"userToken": user_token},
                )
            )
        return results

    def _is_video(self, path: str) -> bool:
        return path.lower().split("?")[0].endswith(".mp4")

    def _video_ready(self, video_id: str, access_token: str) -> bool:
        body = self.get_json(
            f"{GRAPH}/{video_id}",
            params={"fields": "status", "access_token": access_token},
        )
        video_status = (body.get("status") or {}).get("video_status", "in_progress")
        if video_status == "error":
            raise BadBody(self.identifier, str(body), "{}", "Video processing failed")
        return video_status in ("upload_complete", "ready")

    def post_pending(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        is_story = (first.settings or {}).get("post_type") == "story"

        if is_story:
            items: list[dict[str, str]] = []
            for media in first.media or []:
                if self._is_video(media.path):
                    session = self.get_json(
                        f"{GRAPH}/{id}/video_stories",
                        params={"upload_phase": "start", "access_token": access_token},
                        method="POST",
                    )
                    self.fetch(
                        session["upload_url"],
                        method="POST",
                        headers={"Authorization": f"OAuth {access_token}", "file_url": public_url(media.path)},
                    )
                    items.append({"kind": "video", "mediaId": str(session.get("video_id", ""))})
                else:
                    photo = self.get_json(
                        f"{GRAPH}/{id}/photos",
                        params={"access_token": access_token},
                        method="POST",
                        json={"url": public_url(media.path), "published": False},
                    )
                    items.append({"kind": "photo", "mediaId": str(photo.get("id", ""))})
            return [
                PostResponse(
                    id=first.id,
                    status="pending",
                    pendingData={
                        "postType": "story",
                        "items": items,
                        "publishedCount": 0,
                        "lastPostId": "",
                    },
                )
            ]

        return self._post_feed(id, access_token, first)

    def _post_feed(self, id: str, access_token: str, first: PostDetails) -> list[PostResponse]:
        media = first.media or []
        config = first.settings or {}

        if media and self._is_video(media[0].path):
            payload: dict[str, Any] = {
                "file_url": public_url(media[0].path),
                "description": first.message,
                "published": True,
            }
            if config.get("title"):
                payload["title"] = config["title"]
            video = self.get_json(
                f"{GRAPH}/{id}/videos",
                params={"access_token": access_token, "fields": "id,permalink_url"},
                method="POST",
                json=payload,
            )
            video_id = str(video.get("id", ""))
            return [
                PostResponse(
                    id=first.id,
                    status="success",
                    postId=video_id,
                    releaseURL=f"https://www.facebook.com/reel/{video_id}",
                )
            ]

        attached = [
            {
                "media_fbid": str(
                    self.get_json(
                        f"{GRAPH}/{id}/photos",
                        params={"access_token": access_token},
                        method="POST",
                        json={"url": public_url(item.path), "published": False},
                    ).get("id", "")
                )
            }
            for item in media
        ]

        preset_id = ""
        if not attached and config.get("text_format_preset_id") and len(first.message) <= PRESET_MAX_CHARS:
            preset_id = str(config["text_format_preset_id"])

        def publish(with_preset: bool) -> dict[str, Any]:
            payload = {"message": first.message, "published": True}
            if attached:
                payload["attached_media"] = attached
            if config.get("url"):
                payload["link"] = config["url"]
            if with_preset and preset_id:
                payload["text_format_preset_id"] = preset_id
            return self.get_json(
                f"{GRAPH}/{id}/feed",
                params={"access_token": access_token, "fields": "id,permalink_url"},
                method="POST",
                json=payload,
            )

        try:
            feed = publish(bool(preset_id))
        except BadBody as exc:
            if not preset_id or not _is_preset_rejection(str(exc.value) or str(exc)):
                raise
            feed = publish(False)

        return [
            PostResponse(
                id=first.id,
                status="success",
                postId=str(feed.get("id", "")),
                releaseURL=feed.get("permalink_url", ""),
            )
        ]

    def check_post_status(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        if data.get("attempting") is not None and data.get("confirmed"):
            raise BadBody(
                self.identifier,
                "{}",
                "{}",
                "Facebook may have already published part of the story, please check your page before "
                "posting again to avoid duplicates",
            )

        for item in (data.get("items") or [])[int(data.get("publishedCount") or 0) :]:
            if item.get("kind") != "video":
                continue
            if not self._video_ready(item.get("mediaId", ""), access_token):
                return PendingCheckResponse(status="pending", pendingData=data)

        if data.get("attempting") is not None and not data.get("confirmed"):
            return PendingCheckResponse(status="ready", pendingData={**data, "confirmed": True})
        return PendingCheckResponse(status="ready", pendingData=data)

    def finalize_post(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        if data.get("attempting") is None or not data.get("confirmed"):
            return PendingCheckResponse(
                status="pending",
                pendingData={**data, "attempting": data.get("publishedCount", 0), "confirmed": False},
            )

        items = data.get("items") or []
        index = int(data.get("publishedCount") or 0)
        item = items[index]
        page_id = getattr(integration, "internalId", "")
        if item.get("kind") == "video":
            published = self.get_json(
                f"{GRAPH}/{page_id}/video_stories",
                params={
                    "upload_phase": "finish",
                    "video_id": item.get("mediaId", ""),
                    "access_token": access_token,
                },
                method="POST",
            )
        else:
            published = self.get_json(
                f"{GRAPH}/{page_id}/photo_stories",
                params={"photo_id": item.get("mediaId", ""), "access_token": access_token},
                method="POST",
            )

        story_id = str(published.get("post_id", ""))
        published_count = index + 1
        if published_count < len(items):
            return PendingCheckResponse(
                status="pending",
                pendingData={
                    **data,
                    "publishedCount": published_count,
                    "lastPostId": story_id,
                    "attempting": None,
                    "confirmed": False,
                },
            )
        return PendingCheckResponse(
            status="completed",
            postId=story_id,
            releaseURL=f"https://www.facebook.com/stories/{story_id}",
        )

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        import time

        response = self.post_pending(id, access_token, post_details, integration)[0]
        if response.status != "pending":
            return [response]

        pending = response.pendingData
        started = time.time()
        while True:
            if time.time() - started > 8 * 60:
                raise BadBody(self.identifier, "{}", "{}", "Video processing timed out")
            check = self.check_post_status(access_token, pending, integration)
            if check.status == "pending":
                pending = check.pendingData
                time.sleep(10)
                continue
            final = self.finalize_post(access_token, check.pendingData, integration)
            if final.status == "completed":
                return [
                    PostResponse(
                        id=post_details[0].id,
                        status="success",
                        postId=final.postId,
                        releaseURL=final.releaseURL,
                    )
                ]
            pending = final.pendingData

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        reply_to = parent_post_id or id
        payload: dict[str, Any] = {"message": post_details.message}
        if post_details.media:
            payload["attachment_url"] = public_url(post_details.media[0].path)
        data = self.get_json(
            f"{GRAPH}/{reply_to}/comments",
            params={"access_token": access_token, "fields": "id,permalink_url"},
            method="POST",
            json=payload,
        )
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=str(data.get("id", "")),
            releaseURL=data.get("permalink_url", ""),
        )


def _is_preset_rejection(body: str) -> bool:
    if re.search(r"access token|re-authenticate|revoked|\"code\":\s*190\b", body, re.I):
        return False
    return bool(re.search(r"text_format_preset_id|\"code\":\s*1\b", body)) or body.strip() == "Unknown Error"
