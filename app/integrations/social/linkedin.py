import secrets
from typing import Any
from urllib.parse import quote

from app.core.config import settings
from app.integrations.base import BadBody, RefreshTokenError, SocialAbstract
from app.integrations.interfaces import (
    AuthTokenDetails,
    MediaContent,
    PendingCheckResponse,
    PostDetails,
    PostResponse,
)
from app.integrations import media as media_io

LINKEDIN_API = "https://api.linkedin.com"
LINKEDIN_VERSION = "202601"
LINKEDIN_REST_HEADERS = {
    "X-Restli-Protocol-Version": "2.0.0",
    "LinkedIn-Version": LINKEDIN_VERSION,
}


def fix_text(text: str) -> str:
    import re

    pattern = re.compile(r"@\[.+?]\(urn:li:organization.+?\)")
    matches = pattern.findall(text)
    parts = pattern.split(text)
    escaped: list[str] = []
    for part in parts:
        for char in ("\\", "<", ">", "#", "~", "_", "|", "[", "]", "*", "(", ")", "{", "}", "@"):
            part = part.replace(char, "\\" + char)
        escaped.append(part)
    out: list[str] = []
    for index, part in enumerate(escaped):
        out.append(part)
        if index < len(matches):
            out.append(matches[index])
    return "".join(out)


class LinkedinProvider(SocialAbstract):
    identifier = "linkedin"
    name = "LinkedIn"
    picture = "linkedin.svg"
    description = "Share posts and media on your LinkedIn profile"
    maxLength = 3000
    oneTimeToken = True
    isBetweenSteps = False
    maxConcurrentJob = 8
    refreshWait = True
    scopes = [
        "openid",
        "profile",
        "w_member_social",
        "r_basicprofile",
        "rw_organization_admin",
        "w_organization_social",
        "r_organization_social",
    ]

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        if "Unable to obtain activity" in text:
            return "retry", "Unable to obtain activity"
        if "resource is forbidden" in text or "Service Unavailable" in text:
            return "retry", "Resource is forbidden"
        return super().handle_errors(body, status)

    def _redirect_uri(self) -> str:
        return f"{settings.frontend_url}/integrations/social/linkedin"

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        state = secrets.token_urlsafe(9)
        code_verifier = secrets.token_urlsafe(30)
        redirect_uri = self._redirect_uri() + (f"?refresh={refresh}" if refresh else "")
        url = (
            "https://www.linkedin.com/oauth/v2/authorization"
            f"?response_type=code&client_id={settings.linkedin_client_id}"
            f"&prompt=none&redirect_uri={quote(redirect_uri, safe='')}"
            f"&state={state}&scope={quote(' '.join(self.scopes))}"
        )
        return {"url": url, "codeVerifier": code_verifier, "state": state}

    def _token_request(self, params: dict[str, str]) -> dict[str, Any]:
        body = {
            "client_id": settings.linkedin_client_id,
            "client_secret": settings.linkedin_client_secret,
            **params,
        }
        return self.fetch(
            "https://www.linkedin.com/oauth/v2/accessToken",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        ).json()

    def _profile(self, access_token: str) -> AuthTokenDetails:
        headers = {"Authorization": f"Bearer {access_token}"}
        userinfo = self.get_json(f"{LINKEDIN_API}/v2/userinfo", headers=headers)
        me = self.get_json(f"{LINKEDIN_API}/v2/me", headers=headers)
        return AuthTokenDetails(
            id=userinfo.get("sub", ""),
            name=userinfo.get("name", ""),
            accessToken=access_token,
            picture=userinfo.get("picture") or "",
            username=me.get("vanityName", ""),
        )

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri() + (f"?refresh={refresh}" if refresh else ""),
            }
        )
        scope = payload.get("scope", "")
        if scope:
            self.check_scopes(self.scopes, scope.split(" "))
        details = self._profile(payload["access_token"])
        details.refreshToken = payload.get("refresh_token", "")
        details.expiresIn = payload.get("expires_in")
        return details

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        payload = self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        details = self._profile(payload["access_token"])
        details.refreshToken = payload.get("refresh_token", refresh_token)
        details.expiresIn = payload.get("expires_in")
        return details

    def _upload_picture(
        self,
        file_name: str,
        access_token: str,
        person_id: str,
        payload: bytes | dict[str, str],
        post_type: str = "personal",
    ) -> dict[str, Any]:
        lower = file_name.lower().split("?")[0]
        is_video = lower.endswith(".mp4")
        is_pdf = lower.endswith(".pdf")
        endpoint = "videos" if is_video else "documents" if is_pdf else "images"
        file_size = len(payload) if isinstance(payload, bytes) else media_io.media_size(payload["path"])

        headers = {
            "Content-Type": "application/json",
            **LINKEDIN_REST_HEADERS,
            "Authorization": f"Bearer {access_token}",
        }
        owner = (
            f"urn:li:person:{person_id}"
            if post_type == "personal"
            else f"urn:li:organization:{person_id}"
        )
        request: dict[str, Any] = {"owner": owner}
        if is_video:
            request.update({"fileSizeBytes": file_size, "uploadCaptions": False, "uploadThumbnail": False})

        response = self.get_json(
            f"{LINKEDIN_API}/rest/{endpoint}?action=initializeUpload",
            method="POST",
            headers=headers,
            json={"initializeUploadRequest": request},
        )
        value = response.get("value", {})
        upload_url = (value.get("uploadInstructions") or [{}])[0].get("uploadUrl") or value.get("uploadUrl")
        final_output = value.get("video") or value.get("image") or value.get("document")
        if not upload_url or not final_output:
            raise BadBody(self.identifier, str(response), "{}", "LinkedIn initializeUpload failed")

        etags: list[str] = []
        if is_video:
            chunk_size = 2 * 1024 * 1024
            for offset in range(0, file_size, chunk_size):
                end = min(offset + chunk_size, file_size) - 1
                if isinstance(payload, bytes):
                    chunk = payload[offset : end + 1]
                else:
                    chunk = media_io.media_chunk(payload["path"], offset, end)
                upload_headers = {
                    **LINKEDIN_REST_HEADERS,
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/octet-stream",
                }
                upload = self.fetch(upload_url, method="PUT", headers=upload_headers, data=chunk)
                etags.append(upload.headers.get("etag", "").strip('"'))
            self.fetch(
                f"{LINKEDIN_API}/rest/videos?action=finalizeUpload",
                method="POST",
                headers={**LINKEDIN_REST_HEADERS, "Content-Type": "application/json", "Authorization": f"Bearer {access_token}"},
                json={"finalizeUploadRequest": {"video": final_output, "uploadToken": "", "uploadedPartIds": etags}},
            )
        else:
            body = payload if isinstance(payload, bytes) else media_io.read_or_fetch(payload["path"])
            put_headers = {**LINKEDIN_REST_HEADERS, "Authorization": f"Bearer {access_token}"}
            if is_pdf:
                put_headers["Content-Type"] = "application/pdf"
            self.fetch(upload_url, method="PUT", headers=put_headers, data=body)

        if is_video:
            return {"id": final_output, "poll": {"urn": final_output, "endpoint": "videos"}}
        if post_type == "company":
            return {
                "id": final_output,
                "poll": {"urn": final_output, "endpoint": "documents" if is_pdf else "images"},
            }
        return {"id": final_output, "grace": True}

    def _media_status(self, access_token: str, urn: str, endpoint: str) -> dict[str, Any]:
        return self.get_json(
            f"{LINKEDIN_API}/rest/{endpoint}/{quote(urn, safe='')}",
            headers={**LINKEDIN_REST_HEADERS, "Content-Type": "application/json", "Authorization": f"Bearer {access_token}"},
        )

    def _build_post_content(self, is_pdf: bool, media_ids: list[str], pdf_title: str = "") -> dict[str, Any]:
        if not media_ids:
            return {}
        if len(media_ids) == 1:
            media: dict[str, Any] = {"id": media_ids[0]}
            if is_pdf:
                media["title"] = pdf_title or "slides"
            return {"content": {"media": media}}
        return {"content": {"multiImage": {"images": [{"id": mid} for mid in media_ids]}}}

    def _create_main_post(
        self,
        author_id: str,
        access_token: str,
        message: str,
        media_ids: list[str],
        post_type: str,
        is_pdf: bool = False,
        pdf_title: str = "",
    ) -> str:
        author = f"urn:li:person:{author_id}" if post_type == "personal" else f"urn:li:organization:{author_id}"
        payload = {
            "author": author,
            "commentary": fix_text(message),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            **self._build_post_content(is_pdf, media_ids, pdf_title),
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        response = self.fetch(
            f"{LINKEDIN_API}/rest/posts",
            method="POST",
            headers={**LINKEDIN_REST_HEADERS, "Content-Type": "application/json", "Authorization": f"Bearer {access_token}"},
            json=payload,
        )
        post_id = response.headers.get("x-restli-id", "")
        if not post_id:
            raise BadBody(self.identifier, "{}", str(payload), "Error posting to LinkedIn")
        return post_id

    def _create_comment(self, author_id: str, access_token: str, message: str, parent_id: str, post_type: str) -> str:
        actor = f"urn:li:person:{author_id}" if post_type == "personal" else f"urn:li:organization:{author_id}"
        response = self.get_json(
            f"{LINKEDIN_API}/rest/socialActions/{quote(parent_id, safe='')}/comments",
            method="POST",
            headers={**LINKEDIN_REST_HEADERS, "Content-Type": "application/json", "Authorization": f"Bearer {access_token}"},
            json={"actor": actor, "object": parent_id, "message": {"text": fix_text(message)}},
        )
        return response.get("object", "")

    def _media_for_post(
        self, post: PostDetails, access_token: str, person_id: str, post_type: str
    ) -> dict[str, Any]:
        media_uploads: list[str] = []
        poll: list[dict[str, str]] = []
        grace = False
        for item in post.media or []:
            lower = item.path.lower().split("?")[0]
            if lower.endswith(".mp4"):
                payload: bytes | dict[str, str] = {"path": item.path}
            else:
                payload = media_io.prepare_image_buffer(item.path)
            uploaded = self._upload_picture(item.path, access_token, person_id, payload, post_type)
            if not uploaded.get("id"):
                continue
            media_uploads.append(uploaded["id"])
            if uploaded.get("poll"):
                poll.append(uploaded["poll"])
            if uploaded.get("grace"):
                grace = True
        return {"mediaIds": media_uploads, "poll": poll, "grace": grace}

    def post_pending(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any, post_type: str = "personal"
    ) -> list[PostResponse]:
        first = post_details[0]
        settings_dict = first.settings if isinstance(first.settings, dict) else {}
        is_pdf = bool(settings_dict.get("post_as_images_carousel"))
        processed = self._media_for_post(first, access_token, id, post_type)
        pending = {
            "authorId": id,
            "postType": post_type,
            "message": first.message,
            "isPdf": is_pdf,
            "mediaIds": processed["mediaIds"],
            "poll": processed["poll"],
            "graceChecks": 1 if processed["grace"] else 0,
            "statusStalls": 0,
            "attempting": False,
            "confirmed": False,
        }
        if is_pdf:
            pending["pdfTitle"] = settings_dict.get("carousel_name") or "slides"
        return [
            PostResponse(id=first.id, status="pending", postId="", releaseURL="", pendingData=pending)
        ]

    def check_post_status(
        self, access_token: str, pending_data: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        if pending_data.get("attempting") and pending_data.get("confirmed"):
            raise BadBody(
                self.identifier,
                "{}",
                "{}",
                "LinkedIn may have already published this post, please check your account before posting again to avoid duplicates",
            )

        still_processing: list[dict[str, str]] = []
        for item in pending_data.get("poll") or []:
            try:
                status = self._media_status(access_token, item["urn"], item["endpoint"])
                if not status.get("status"):
                    raise BadBody(self.identifier, str(status), "{}", "LinkedIn answered without a media status")
            except RefreshTokenError:
                raise
            except Exception:
                if int(pending_data.get("statusStalls") or 0) >= 10:
                    raise BadBody(
                        self.identifier,
                        "{}",
                        "{}",
                        "LinkedIn kept failing the media status check, nothing was published, please try again",
                    )
                pending_data = {**pending_data, "statusStalls": int(pending_data.get("statusStalls") or 0) + 1}
                return PendingCheckResponse(status="pending", pendingData=pending_data)

            if status.get("status") == "PROCESSING_FAILED":
                label = {"videos": "video", "documents": "document"}.get(item["endpoint"], "image")
                reason = status.get("processingFailureReason", "")
                raise BadBody(self.identifier, str(status), "{}", f"LinkedIn {label} processing failed" + (f": {reason}" if reason else ""))
            if status.get("status") != "AVAILABLE":
                still_processing.append(item)

        if still_processing:
            return PendingCheckResponse(status="pending", pendingData={**pending_data, "poll": still_processing, "statusStalls": 0})

        if int(pending_data.get("graceChecks") or 0) > 0:
            return PendingCheckResponse(
                status="pending",
                pendingData={**pending_data, "poll": [], "graceChecks": pending_data["graceChecks"] - 1},
            )

        if pending_data.get("attempting") and not pending_data.get("confirmed"):
            return PendingCheckResponse(status="ready", pendingData={**pending_data, "poll": [], "confirmed": True})
        return PendingCheckResponse(status="ready", pendingData={**pending_data, "poll": []})

    def finalize_post(
        self, access_token: str, pending_data: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        if not pending_data.get("attempting") or not pending_data.get("confirmed"):
            return PendingCheckResponse(status="pending", pendingData={**pending_data, "attempting": True, "confirmed": False})

        main_post_id = self._create_main_post(
            pending_data["authorId"],
            access_token,
            pending_data["message"],
            [mid for mid in pending_data.get("mediaIds") or [] if mid],
            pending_data.get("postType", "personal"),
            pending_data.get("isPdf", False),
            pending_data.get("pdfTitle", ""),
        )
        return PendingCheckResponse(
            status="completed",
            postId=main_post_id,
            releaseURL=f"https://www.linkedin.com/feed/update/{main_post_id}",
        )

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any, post_type: str = "personal"
    ) -> list[PostResponse]:
        first = post_details[0]
        processed = self._media_for_post(first, access_token, id, post_type)
        main_post_id = self._create_main_post(
            id, access_token, first.message, processed["mediaIds"], post_type
        )
        return [
            PostResponse(
                id=first.id,
                status="success",
                postId=main_post_id,
                releaseURL=f"https://www.linkedin.com/feed/update/{main_post_id}",
            )
        ]

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any, post_type: str = "personal"
    ) -> PostResponse:
        comment_id = self._create_comment(id, access_token, post_details.message, parent_post_id, post_type)
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=comment_id,
            releaseURL=f"https://www.linkedin.com/embed/feed/update/{comment_id}",
        )
