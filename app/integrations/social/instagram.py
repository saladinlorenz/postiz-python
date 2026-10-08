import json
import secrets
from typing import Any
from urllib.parse import urlencode

from app.core.config import settings
from app.integrations.base import BadBody, Disconnect, SocialAbstract
from app.integrations.interfaces import (
    AuthTokenDetails,
    PendingCheckResponse,
    PostDetails,
    PostResponse,
)
from app.integrations.media import public_url

META_GRAPH_API_VERSION = "v25.0"
DEFAULT_HOST = "graph.facebook.com"


class InstagramProvider(SocialAbstract):
    identifier = "instagram"
    name = "Instagram (Facebook Business)"
    picture = "instagram.svg"
    description = "Publish photos, carousels and reels to Instagram"
    maxLength = 2200
    maxConcurrentJob = 400
    scopes = [
        "instagram_basic",
        "pages_show_list",
        "pages_read_engagement",
        "business_management",
        "instagram_content_publish",
        "instagram_manage_comments",
        "instagram_manage_insights",
    ]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        rules: list[tuple[str, str, str]] = [
            ("An unknown error occurred", "bad-body", "An unknown error occurred, please try again later"),
            ("2207081", "bad-body", "This account doesn't support Trial Reels"),
            ("REVOKED_ACCESS_TOKEN", "refresh-token", "Something is wrong with your connected user, please re-authenticate"),
            ('"error_subcode":33', "refresh-token", "Something is wrong with your connected user, please re-authenticate"),
            (
                "the user is not an instagram business",
                "refresh-token",
                "Your Instagram account is not a business account, please convert it to a business account",
            ),
            (
                "session has been invalidated",
                "refresh-token",
                "You session has been invalidated, this can usually happen from frequent posting, please "
                "re-authenticate, and wait 1-2 days before posting again",
            ),
            ("2207050", "bad-body", "Instagram user is restricted"),
            ("2207003", "bad-body", "Timeout downloading media, please try again"),
            ("2207020", "bad-body", "Media expired, please upload again"),
            ("2207032", "bad-body", "Failed to create media, please try again"),
            ("2207053", "bad-body", "Unknown upload error, please try again"),
            ("2207052", "bad-body", "Media fetch failed, please try again"),
            ("2207057", "bad-body", "Invalid thumbnail offset for video"),
            ("2207026", "bad-body", "Unsupported video format"),
            ("2207023", "bad-body", "Unknown media type"),
            ("2207006", "bad-body", "Media not found, please upload again"),
            ("2207008", "bad-body", "Media builder expired, please try again"),
            ("2207028", "bad-body", "Carousel validation failed"),
            ("2207010", "bad-body", "Caption is too long"),
            ("2207035", "bad-body", "Product tag positions not supported for videos"),
            ("2207036", "bad-body", "Product tag positions required for photos"),
            ("2207037", "bad-body", "Product tag validation failed"),
            ("2207040", "bad-body", "Too many product tags"),
            ("2207004", "bad-body", "Image is too large"),
            ("2207005", "bad-body", "Unsupported image format"),
            ("2207009", "bad-body", "Aspect ratio not supported, must be between 4:5 to 1.91:1"),
            ("Page request limit reached", "bad-body", "Page posting for today is limited, please try again tomorrow"),
            ("2207042", "bad-body", "You have reached the maximum of 25 posts per day, allowed for your account"),
            (
                "(#200)",
                "bad-body",
                "Facebook rejected the post due to missing permissions. Make sure your Facebook account has full "
                "content access to the Page linked to this Instagram account, then reconnect the channel.",
            ),
            ("Not enough permissions to post", "bad-body", "Not enough permissions to post"),
            ("36003", "bad-body", "Aspect ratio not supported, must be between 4:5 to 1.91:1"),
            (
                "You cannot access the app till you log in to",
                "disconnect",
                "Instagram requires you to log in at instagram.com and follow its instructions before posting "
                "can resume. After that, please reconnect this channel.",
            ),
            (
                "Session key is malformed",
                "disconnect",
                "Instagram requires you to log in at instagram.com and follow its instructions before posting "
                "can resume. After that, please reconnect this channel.",
            ),
            (
                "190,",
                "bad-body",
                "The account is missing some permissions to perform this action, please re-add the account and allow all permissions",
            ),
            ("36001", "bad-body", "Invalid Instagram image resolution max: 1920x1080px"),
            ("2207051", "bad-body", "Instagram blocked your request"),
            ("2207001", "bad-body", "Instagram detected that your post is spam, please try again with different content"),
            (
                "2207082",
                "bad-body",
                "Instagram could not process this video. If you attached audio to a video that has no sound track, "
                "set the original video volume to 0 and try again",
            ),
            (
                "2207085",
                "bad-body",
                "Instagram could not process the video, please check the video format, duration and resolution and try again",
            ),
            ("2207077", "bad-body", "Instagram Video download failed"),
            ("too little or too many attachments", "bad-body", "Instagram carousel should have between 2 and 10 media attachments"),
            ("2207027", "bad-body", "Unknown error, please try again later or contact support"),
            ("param collaborators is not allowed", "bad-body", "Collaborators are not allowed for carousel"),
        ]
        lowered = text.lower()
        for needle, kind, message in rules:
            if needle.lower() in lowered:
                return kind, message
        return super().handle_errors(body, status)

    def _redirect_uri(self, refresh: str = "") -> str:
        return f"{settings.frontend_url}/integrations/social/instagram" + (f"?refresh={refresh}" if refresh else "")

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
        return self.get_json(f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/oauth/access_token", params=params)

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
            }
        )
        access_token = long_lived["access_token"]
        graph = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"

        permissions = self.get_json(f"{graph}/me/permissions", params={"access_token": access_token}).get("data", [])
        granted = [p["permission"] for p in permissions if p.get("status") == "granted"]
        self.check_scopes(self.scopes, granted)

        me = self.get_json(f"{graph}/me", params={"fields": "id,name,picture", "access_token": access_token})
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
        graph = f"https://{DEFAULT_HOST}/{META_GRAPH_API_VERSION}"
        pages: list[dict[str, Any]] = []
        seen: set[str] = set()
        query = urlencode(
            {
                "fields": "id,instagram_business_account,username,name,picture.type(large),access_token",
                "limit": 100,
                "access_token": user_token,
            }
        )
        self._walk(f"{graph}/me/accounts?{query}", pages, seen)

        try:
            business_url = f"{graph}/me/businesses?access_token={user_token}"
            while business_url:
                response = self.get_json(business_url)
                for business in response.get("data") or []:
                    for edge in ("owned_pages", "client_pages"):
                        try:
                            self._walk(f"{graph}/{business['id']}/{edge}?{query}", pages, seen)
                        except Exception:
                            continue
                business_url = (response.get("paging") or {}).get("next", "")
        except Exception:
            pass
        return pages

    def connections(self, details: AuthTokenDetails) -> list[AuthTokenDetails]:
        graph = f"https://{DEFAULT_HOST}/{META_GRAPH_API_VERSION}"
        user_token = details.additionalSettings.get("userToken") or details.accessToken
        results: list[AuthTokenDetails] = []
        for page in self._pages(user_token):
            page_token = page.get("access_token")
            ig = page.get("instagram_business_account")
            if not ig or not page_token:
                continue
            profile = self.get_json(
                f"{graph}/{ig['id']}",
                params={
                    "fields": "username,name,profile_picture_url",
                    "access_token": user_token,
                },
            )
            results.append(
                AuthTokenDetails(
                    id=str(ig["id"]),
                    name=profile.get("name") or ig.get("name") or page.get("name", ""),
                    accessToken=f"{page_token}___{user_token}",
                    refreshToken="",
                    expiresIn=None,
                    picture=profile.get("profile_picture_url", ""),
                    username=profile.get("username", ""),
                    additionalSettings={
                        "userToken": user_token,
                        "pageId": str(page.get("id", "")),
                        "profile": profile.get("username", ""),
                    },
                )
            )
        return results

    def _tokens(self, token: str) -> tuple[str, str]:
        parts = token.split("___", 1)
        return parts[0], (parts[1] if len(parts) > 1 else "")

    def _profile(self, integration: Any) -> str:
        try:
            return (json.loads(getattr(integration, "additionalSettings", "{}") or "{}") or {}).get("profile", "")
        except (json.JSONDecodeError, TypeError):
            return ""

    def _is_video(self, path: str) -> bool:
        return path.lower().split("?")[0].endswith(".mp4")

    def post_pending(
        self, id: str, token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        access_token, _ = self._tokens(token)
        first = post_details[0]
        config = first.settings or {}
        is_story = config.get("post_type") == "story"
        media = first.media or []
        if not media:
            raise BadBody(self.identifier, "{}", "{}", "Should have at least one media")
        if len(media) > 10:
            raise BadBody(
                self.identifier, "{}", "{}", "Instagram carousel only supports up to 10 media attachments"
            )

        collaborators: list[str] = []
        if config.get("collaborators") and not is_story:
            for item in config["collaborators"]:
                label = str(item.get("label", "")) if isinstance(item, dict) else str(item)
                handle = _strip_handle(label).strip()
                if handle:
                    collaborators.append(handle)

        containers: list[str] = []
        for item in media:
            single = len(media) == 1
            is_carousel = not single and not is_story
            params: dict[str, Any] = {"access_token": access_token}
            if self._is_video(item.path):
                params["video_url"] = public_url(item.path)
                if single:
                    params["media_type"] = "STORIES" if is_story else "REELS"
                    if not is_story:
                        if item.thumbnail:
                            params["cover_url"] = public_url(item.thumbnail)
                        else:
                            params["thumb_offset"] = item.thumbnailTimestamp or 0
                else:
                    params["media_type"] = "STORIES" if is_story else "VIDEO"
                    params["thumb_offset"] = item.thumbnailTimestamp or 0
            else:
                params["image_url"] = public_url(item.path)
                if is_story:
                    params["media_type"] = "STORIES"
            if is_carousel:
                params["is_carousel_item"] = "true"
            if single:
                params["caption"] = first.message
                if collaborators:
                    params["collaborators"] = json.dumps(collaborators)

            created = self.get_json(
                f"https://{DEFAULT_HOST}/{META_GRAPH_API_VERSION}/{id}/media",
                params=params,
                method="POST",
            )
            containers.append(str(created.get("id", "")))

        post_type = "stories" if is_story and len(containers) > 1 else ("single" if len(containers) == 1 else "carousel")
        pending: dict[str, Any] = {
            "type": DEFAULT_HOST,
            "postType": post_type,
            "containers": containers,
            "message": first.message or "",
        }
        if collaborators:
            pending["collaborators"] = collaborators
        return [PostResponse(id=first.id, status="pending", pendingData=pending)]

    def _container_status(self, container_id: str, check_token: str, host: str) -> str:
        body = self.get_json(
            f"https://{host}/{META_GRAPH_API_VERSION}/{container_id}",
            params={"access_token": check_token, "fields": "status_code,status"},
        )
        status_code = body.get("status_code", "")
        if status_code in ("ERROR", "EXPIRED"):
            status_text = str(body.get("status", ""))
            kind, message = self.handle_errors(status_text, 200)
            if kind == "disconnect":
                raise Disconnect(self.identifier, str(body), "{}", message)
            raise BadBody(self.identifier, str(body), "{}", message or status_text or "Instagram could not process the media")
        return status_code

    def _permalink(self, media_id: str, check_token: str, host: str, integration: Any) -> str:
        try:
            body = self.get_json(
                f"https://{host}/{META_GRAPH_API_VERSION}/{media_id}",
                params={"access_token": check_token, "fields": "permalink"},
            )
            return body.get("permalink", "")
        except Exception:
            return f"https://www.instagram.com/{self._profile(integration)}"

    def check_post_status(
        self, token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        access_token, user_token = self._tokens(token)
        check_token = user_token or access_token
        data = dict(pending)
        host = data.get("type", DEFAULT_HOST)
        profile_url = f"https://www.instagram.com/{self._profile(integration)}"

        if data.get("carouselId"):
            status = self._container_status(data["carouselId"], check_token, host)
            if status == "IN_PROGRESS":
                return PendingCheckResponse(status="pending", pendingData=data)
            if status == "PUBLISHED":
                return PendingCheckResponse(
                    status="completed", postId=data["carouselId"], releaseURL=profile_url
                )
            return PendingCheckResponse(status="ready", pendingData=data)

        for container_id in data.get("containers") or []:
            status = self._container_status(container_id, check_token, host)
            if status == "IN_PROGRESS":
                return PendingCheckResponse(status="pending", pendingData=data)
            if status == "PUBLISHED" and data.get("postType") == "single":
                return PendingCheckResponse(status="completed", postId=container_id, releaseURL=profile_url)

        return PendingCheckResponse(status="ready", pendingData=data)

    def finalize_post(
        self, token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        access_token, user_token = self._tokens(token)
        check_token = user_token or access_token
        data = dict(pending)
        host = data.get("type", DEFAULT_HOST)
        ig_id = getattr(integration, "internalId", "")
        containers = data.get("containers") or []
        graph = f"https://{host}/{META_GRAPH_API_VERSION}"

        if data.get("postType") == "stories":
            last_media_id = ""
            for creation_id in containers:
                if self._container_status(creation_id, check_token, host) == "PUBLISHED":
                    continue
                published = self.get_json(
                    f"{graph}/{ig_id}/media_publish",
                    params={"creation_id": creation_id, "access_token": access_token, "field": "id"},
                    method="POST",
                )
                last_media_id = str(published.get("id", ""))
            return PendingCheckResponse(
                status="completed",
                postId=last_media_id or str(containers[-1]),
                releaseURL=self._permalink(last_media_id, check_token, host, integration)
                if last_media_id
                else f"https://www.instagram.com/{self._profile(integration)}",
            )

        if data.get("postType") == "carousel" and not data.get("carouselId"):
            params: dict[str, Any] = {
                "caption": data.get("message", ""),
                "media_type": "CAROUSEL",
                "children": ",".join(containers),
                "access_token": access_token,
            }
            if data.get("collaborators"):
                params["collaborators"] = json.dumps(data["collaborators"])
            created = self.get_json(f"{graph}/{ig_id}/media", params=params, method="POST")
            return PendingCheckResponse(
                status="pending", pendingData={**data, "carouselId": str(created.get("id", ""))}
            )

        creation_id = data["carouselId"] if data.get("postType") == "carousel" else containers[0]
        published = self.get_json(
            f"{graph}/{ig_id}/media_publish",
            params={"creation_id": creation_id, "access_token": access_token, "field": "id"},
            method="POST",
        )
        media_id = str(published.get("id", ""))
        return PendingCheckResponse(
            status="completed",
            postId=media_id,
            releaseURL=self._permalink(media_id, check_token, host, integration),
        )

    def post(
        self, id: str, token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        import time

        pending = self.post_pending(id, token, post_details, integration)[0].pendingData
        started = time.time()
        while True:
            if time.time() - started > 8 * 60:
                raise BadBody(self.identifier, "{}", "{}", "Media processing timed out")
            check = self.check_post_status(token, pending, integration)
            if check.status == "pending":
                pending = check.pendingData
                time.sleep(30)
                continue
            final = self.finalize_post(token, check.pendingData, integration)
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
            time.sleep(30)

    def comment(
        self, id: str, token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        access_token, user_token = self._tokens(token)
        host = DEFAULT_HOST
        graph = f"https://{host}/{META_GRAPH_API_VERSION}"
        created = self.get_json(
            f"{graph}/{parent_post_id}/comments",
            params={"message": post_details.message, "access_token": access_token},
            method="POST",
        )
        permalink = self._permalink(parent_post_id, user_token or access_token, host, integration)
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=str(created.get("id", "")),
            releaseURL=permalink,
        )


def _strip_handle(handle: str) -> str:
    return handle.strip().lstrip("@").rstrip(",")
