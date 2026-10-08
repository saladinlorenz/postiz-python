import base64
import io
import ipaddress
import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image

from app.integrations.base import BadBody, RefreshTokenError, SocialAbstract
from app.integrations.interfaces import (
    AuthTokenDetails,
    PendingCheckResponse,
    PostDetails,
    PostResponse,
)
from app.integrations.media import is_url, read_or_fetch, resolve_local

PREP_MAX_FAILURES = 4
IMAGE_MAX_KB = 976
POST_COLLECTION = "app.bsky.feed.post"
URL_PATTERN = re.compile(r"(?:^|\s)((?:https?://)[^\s]+)", re.I)
MENTION_PATTERN = re.compile(r"(?<![\w@.])@([a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,})")
TAG_PATTERN = re.compile(r"(?<!\w)#(\w+)")
PUNCTUATION = ".,:;!?'\")]}"


class XrpcError(Exception):
    def __init__(self, status: int, body: Any = None, text: str = ""):
        self.status = status
        self.body = body if body is not None else {}
        self.text = text
        super().__init__(text or f"Bluesky request failed with {status}")


def is_safe_service(url: str) -> bool:
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def trim_url(url: str) -> str:
    trimmed = url.rstrip(PUNCTUATION)
    if trimmed.endswith(")") and trimmed.count("(") < trimmed.count(")"):
        trimmed = trimmed[:-1]
    return trimmed


def build_facets(text: str, mentions: dict[str, str] | None = None) -> list[dict[str, Any]]:
    mentions = mentions or {}
    facets: list[dict[str, Any]] = []

    def byte_offset(index: int) -> int:
        return len(text[:index].encode("utf-8"))

    for match in URL_PATTERN.finditer(text):
        uri = trim_url(match.group(1))
        if not uri:
            continue
        start = byte_offset(match.start(1))
        facets.append(
            {
                "index": {"byteStart": start, "byteEnd": start + len(uri.encode("utf-8"))},
                "features": [{"$type": "app.richtext.facet.link", "uri": uri}],
            }
        )

    for match in MENTION_PATTERN.finditer(text):
        did = mentions.get(match.group(1).lower(), "")
        if not did:
            continue
        start = byte_offset(match.start())
        facets.append(
            {
                "index": {"byteStart": start, "byteEnd": byte_offset(match.end())},
                "features": [{"$type": "app.richtext.facet.mention", "did": did}],
            }
        )

    for match in TAG_PATTERN.finditer(text):
        tag = match.group(1)
        if not tag or len(tag) > 64:
            continue
        start = byte_offset(match.start())
        facets.append(
            {
                "index": {"byteStart": start, "byteEnd": byte_offset(match.end())},
                "features": [{"$type": "app.richtext.facet.tag", "tag": tag}],
            }
        )

    return sorted(facets, key=lambda facet: facet["index"]["byteStart"])


class Agent:
    def __init__(self, service: str, provider: "BlueskyProvider"):
        self.service = service.rstrip("/")
        self.provider = provider
        self.access_jwt = ""
        self.refresh_jwt = ""
        self.did = ""
        self.handle = ""

    def call(
        self,
        method: str,
        path: str,
        *,
        host: str = "",
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: Any = None,
        content_type: str = "",
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> Any:
        url = f"{(host or self.service).rstrip('/')}/xrpc/{path}"
        request_headers = dict(headers or {})
        if self.access_jwt:
            request_headers["Authorization"] = f"Bearer {self.access_jwt}"
        if content_type:
            request_headers["Content-Type"] = content_type
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.request(
                    method, url, params=params, json=json_body, content=content, headers=request_headers
                )
        except httpx.HTTPError as exc:
            raise XrpcError(0, {}, str(exc))
        if response.status_code >= 400:
            text = response.text[:2000] if response.text else ""
            try:
                body = response.json()
            except ValueError:
                body = {"message": text}
            raise XrpcError(response.status_code, body, text)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def login(self, identifier: str, password: str) -> None:
        data = self.call(
            "POST",
            "com.atproto.server.createSession",
            json_body={"identifier": identifier, "password": password},
        )
        self.access_jwt = str(data.get("accessJwt") or "")
        self.refresh_jwt = str(data.get("refreshJwt") or "")
        self.did = str(data.get("did") or "")
        self.handle = str(data.get("handle") or "")
        if not self.access_jwt or not self.did:
            raise XrpcError(400, data, "Bluesky did not return a session")

    def get_profile(self, actor: str) -> dict[str, Any]:
        return self.call("GET", "app.bsky.actor.getProfile", params={"actor": actor})

    def resolve_handle(self, handle: str) -> str:
        data = self.call("GET", "com.atproto.identity.resolveHandle", params={"handle": handle})
        return str(data.get("did") or "")

    def upload_blob(self, content: bytes, content_type: str) -> dict[str, Any]:
        data = self.call(
            "POST",
            "com.atproto.repo.uploadBlob",
            content=content,
            content_type=content_type,
            timeout=120,
        )
        blob = data.get("blob")
        if not isinstance(blob, dict) or not blob.get("ref"):
            raise XrpcError(400, data, "Bluesky did not accept the media")
        return blob

    def create_record(self, repo: str, record: dict[str, Any]) -> dict[str, Any]:
        return self.call(
            "POST",
            "com.atproto.repo.createRecord",
            json_body={"repo": repo, "collection": POST_COLLECTION, "record": record},
            timeout=120,
        )

    def get_post_thread(self, uri: str) -> dict[str, Any]:
        return self.call(
            "GET", "app.bsky.feed.getPostThread", params={"uri": uri, "depth": 0}
        )

    def service_auth(self, aud: str, lxm: str) -> str:
        data = self.call(
            "POST",
            "com.atproto.server.getServiceAuth",
            json_body={"aud": aud, "lxm": lxm, "exp": int(time.time()) + 30 * 60},
        )
        return str(data.get("token") or "")

    def start_video_upload(self, path: str) -> dict[str, Any]:
        size = self.provider.video_size(path)
        if size <= 0:
            raise BadBody("bluesky", "{}", "{}", "Could not determine the video size for Bluesky upload")
        token = self.service_auth(f"did:web:{urlparse(self.service).netloc}", "com.atproto.repo.uploadBlob")
        name = path.split("?")[0].split("/")[-1] or "video.mp4"
        params = {"did": self.did, "name": name}
        content = self.provider.video_content(path)
        try:
            with httpx.Client(timeout=600, follow_redirects=True) as client:
                response = client.post(
                    "https://video.bsky.app/xrpc/app.bsky.video.uploadVideo",
                    params=params,
                    content=content,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "video/mp4",
                        "Content-Length": str(size),
                    },
                )
        except httpx.HTTPError as exc:
            raise XrpcError(0, {}, str(exc))
        finally:
            close = getattr(content, "close", None)
            if callable(close):
                close()
        if response.status_code >= 400:
            raise XrpcError(response.status_code, {}, response.text[:2000])
        job = response.json() if response.content else {}
        if str(job.get("state") or "") == "JOB_STATE_FAILED":
            raise BadBody("bluesky", json.dumps(job), "{}", "Could not upload video, job failed")
        if not job.get("jobId"):
            raise XrpcError(400, job, "Bluesky did not start the video upload")
        return job

    def get_job_status(self, job_id: str, host: str = "https://video.bsky.app") -> dict[str, Any]:
        data = self.call(
            "GET",
            "app.bsky.video.getJobStatus",
            host=host,
            params={"jobId": job_id},
        )
        status = data.get("jobStatus")
        return status if isinstance(status, dict) else {}


class BlueskyProvider(SocialAbstract):
    identifier = "bluesky"
    name = "Bluesky"
    picture = "bluesky.svg"
    description = "Publish to Bluesky (max 4 pictures or 1 video, two-factor authentication must be disabled)"
    maxLength = 300
    maxConcurrentJob = 6
    credentials = True

    def custom_fields(self) -> list[dict[str, Any]]:
        return [
            {"key": "service", "label": "Service", "validation": "^https?://.+$", "type": "text"},
            {"key": "identifier", "label": "Identifier", "validation": "^.+$", "type": "text"},
            {"key": "password", "label": "Password", "validation": "^.{3,}$", "type": "password"},
        ]

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        import secrets

        state = secrets.token_urlsafe(9)
        return {"url": state, "codeVerifier": secrets.token_urlsafe(10), "state": state}

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> Any:
        try:
            body = json.loads(base64.b64decode(code).decode())
        except Exception:
            return "Invalid credentials"
        service = str(body.get("service") or "").strip() or "https://bsky.social"
        identifier = str(body.get("identifier") or "").strip()
        password = str(body.get("password") or "")
        if not is_safe_service(service):
            return "Invalid service URL: must be a public HTTPS address"
        try:
            agent = Agent(service, self)
            agent.login(identifier, password)
            profile = agent.get_profile(agent.did)
        except Exception:
            return "Invalid credentials"
        return AuthTokenDetails(
            id=agent.did,
            name=str(profile.get("displayName") or identifier),
            accessToken=agent.access_jwt,
            refreshToken="",
            expiresIn=None,
            picture=str(profile.get("avatar") or ""),
            username=str(profile.get("handle") or identifier),
            additionalSettings={"service": service, "identifier": identifier, "password": password},
        )

    def _credentials(self, integration: Any) -> dict[str, Any]:
        raw = getattr(integration, "additionalSettings", "") or ""
        if isinstance(raw, dict):
            return raw
        try:
            body = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return body if isinstance(body, dict) else {}

    def _agent(self, integration: Any) -> Agent:
        credentials = self._credentials(integration)
        service = str(credentials.get("service") or "https://bsky.social")
        agent = Agent(service, self)
        try:
            agent.login(str(credentials.get("identifier") or ""), str(credentials.get("password") or ""))
        except XrpcError as exc:
            if exc.status and 400 <= exc.status < 500 and exc.status != 429:
                raise RefreshTokenError(
                    "Bluesky credentials are invalid, please reconnect your account",
                    "Bluesky credentials are invalid, please reconnect your account",
                )
            raise
        return agent

    @staticmethod
    def video_size(path: str) -> int:
        from app.integrations.media import media_size

        return media_size(path)

    @staticmethod
    def video_content(path: str) -> Any:
        if is_url(path):
            return read_or_fetch(path)
        handle = open(resolve_local(path), "rb")
        return handle

    @staticmethod
    def _mime_for(image_format: str) -> str:
        return {
            "JPEG": "image/jpeg",
            "JPG": "image/jpeg",
            "MPO": "image/jpeg",
            "PNG": "image/png",
            "GIF": "image/gif",
            "WEBP": "image/webp",
        }.get((image_format or "").upper(), "image/jpeg")

    def reduce_image(self, path: str) -> tuple[bytes, int, int, str]:
        raw = read_or_fetch(path)
        with Image.open(io.BytesIO(raw)) as opened:
            image_format = (opened.format or "JPEG").upper()
            width, height = opened.size
            image = opened.copy()

        buffer = raw
        while len(buffer) / 1024 > IMAGE_MAX_KB:
            width = max(1, int(width * 0.9))
            height = max(1, int(height * 0.9))
            resized = image.resize((width, height), Image.LANCZOS)
            output = io.BytesIO()
            fmt = image_format if image_format in ("JPEG", "PNG", "GIF", "WEBP") else "JPEG"
            if fmt == "JPEG" and resized.mode not in ("RGB", "L"):
                resized = resized.convert("RGB")
            resized.save(output, format=fmt)
            image = resized
            buffer = output.getvalue()
            if width < 10 or height < 10:
                break
        mime = self._mime_for(image_format)
        if mime == "image/jpeg" and image.mode not in ("RGB", "L"):
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG")
            buffer = output.getvalue()
        return buffer, width, height, mime

    @staticmethod
    def _is_video(path: str) -> bool:
        return ".mp4" in (path or "").lower()

    def _split_media(self, media: list[Any]) -> tuple[list[Any], list[Any]]:
        images = [item for item in media if not self._is_video(item.path)]
        videos = [item for item in media if self._is_video(item.path)]
        if videos and len(media) > 1:
            raise BadBody("bluesky", "{}", "{}", "You can only upload one video per post.")
        if len(media) > 4:
            raise BadBody("bluesky", "{}", "{}", "There can be maximum 4 pictures in a post.")
        return images, videos

    def _upload_media(self, agent: Agent, media: list[dict[str, Any]]) -> dict[str, Any]:
        images = [item for item in media if not self._is_video(item.get("path", ""))]
        embed: dict[str, Any] = {}
        uploaded: list[dict[str, Any]] = []
        for item in images:
            buffer, width, height, mime = self.reduce_image(item.get("path", ""))
            blob = agent.upload_blob(buffer, mime)
            uploaded.append({"blob": blob, "width": width, "height": height, "alt": item.get("alt", "")})
        if uploaded:
            embed = {
                "$type": "app.bsky.embed.images",
                "images": [
                    {
                        "alt": entry["alt"],
                        "image": entry["blob"],
                        "aspectRatio": {"width": entry["width"], "height": entry["height"]},
                    }
                    for entry in uploaded
                ],
            }
        return embed

    def _facets(self, agent: Agent, text: str) -> list[dict[str, Any]]:
        mentions: dict[str, str] = {}
        for match in MENTION_PATTERN.finditer(text or ""):
            handle = match.group(1).lower()
            if handle in mentions:
                continue
            try:
                mentions[handle] = agent.resolve_handle(handle)
            except XrpcError:
                mentions[handle] = ""
        return build_facets(text or "", mentions)

    def _release_url(self, integration: Any, uri: str) -> str:
        profile = getattr(integration, "internalId", "") or getattr(integration, "username", "") or ""
        return f"https://bsky.app/profile/{profile}/post/{uri.split('/')[-1]}"

    @staticmethod
    def _record(message: str, facets: list[dict[str, Any]], embed: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "$type": POST_COLLECTION,
            "text": message,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if facets:
            record["facets"] = facets
        if embed:
            record["embed"] = embed
        return record

    def post_pending(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        first = post_details[0]
        media = first.media or []
        _, videos = self._split_media(media)

        job_id = ""
        if videos:
            agent = self._agent(integration)
            job = agent.start_video_upload(videos[0].path)
            job_id = str(job.get("jobId") or "")

        return [
            PostResponse(
                id=first.id,
                status="pending",
                pendingData={
                    "jobId": job_id,
                    "message": first.message,
                    "media": [{"path": item.path, "alt": item.alt or ""} for item in media],
                    "attempting": False,
                    "confirmed": False,
                    "prepFailures": 0,
                },
            )
        ]

    def check_post_status(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        if data.get("attempting") and data.get("confirmed"):
            raise BadBody(
                "bluesky",
                "{}",
                "{}",
                "Bluesky may have already published this post, please check your account before posting again "
                "to avoid duplicates",
            )

        def witness() -> PendingCheckResponse:
            if data.get("attempting") and not data.get("confirmed"):
                return PendingCheckResponse(status="ready", pendingData={**data, "confirmed": True})
            return PendingCheckResponse(status="ready", pendingData=data)

        if not data.get("jobId"):
            return witness()

        try:
            status = Agent("https://video.bsky.app", self).get_job_status(str(data["jobId"]))
        except XrpcError:
            return PendingCheckResponse(status="pending", pendingData=data)

        if str(status.get("state") or "") == "JOB_STATE_FAILED":
            raise BadBody("bluesky", json.dumps(status), "{}", "Could not upload video, job failed")
        if status.get("blob"):
            return witness()
        return PendingCheckResponse(status="pending", pendingData=data)

    def finalize_post(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        if not data.get("attempting") or not data.get("confirmed"):
            return PendingCheckResponse(
                status="pending", pendingData={**data, "attempting": True, "confirmed": False}
            )

        try:
            agent = self._agent(integration)
            if data.get("jobId"):
                status = Agent("https://video.bsky.app", self).get_job_status(str(data["jobId"]))
                if str(status.get("state") or "") == "JOB_STATE_FAILED":
                    raise BadBody("bluesky", json.dumps(status), "{}", "Could not upload video, job failed")
                if not status.get("blob"):
                    return PendingCheckResponse(
                        status="pending",
                        pendingData={**data, "attempting": False, "confirmed": False},
                    )
                embed = {"$type": "app.bsky.embed.video", "video": status["blob"]}
            else:
                embed = self._upload_media(agent, data.get("media") or [])
            facets = self._facets(agent, str(data.get("message") or ""))
        except (BadBody, RefreshTokenError):
            raise
        except Exception as exc:
            if int(data.get("prepFailures") or 0) >= PREP_MAX_FAILURES:
                raise BadBody(
                    "bluesky",
                    json.dumps({"message": str(exc)}),
                    "{}",
                    f"Could not prepare the post for Bluesky, nothing was published: {exc}",
                )
            return PendingCheckResponse(
                status="pending",
                pendingData={
                    **data,
                    "attempting": False,
                    "confirmed": False,
                    "prepFailures": int(data.get("prepFailures") or 0) + 1,
                },
            )

        record = self._record(str(data.get("message") or ""), facets, embed)
        try:
            created = agent.create_record(agent.did, record)
        except XrpcError as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                raise BadBody(
                    "bluesky",
                    exc.text,
                    "{}",
                    "Bluesky rejected the post, nothing was published, please check the content and try again",
                )
            return PendingCheckResponse(status="pending", pendingData=data)

        uri = str(created.get("uri") or "")
        if not uri:
            raise BadBody("bluesky", json.dumps(created), "{}", "Bluesky did not create the post")
        return PendingCheckResponse(
            status="completed",
            postId=uri,
            releaseURL=self._release_url(integration, uri),
        )

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        started = time.time()
        pending = self.post_pending(id, access_token, post_details, integration)[0].pendingData
        while True:
            if time.time() - started > 8 * 60:
                raise BadBody(
                    "bluesky", "{}", "{}", "Video upload timed out, job did not complete"
                )
            check = self.check_post_status(access_token, pending, integration)
            if check.status == "pending":
                pending = check.pendingData
                time.sleep(20)
                continue
            result = (
                self.finalize_post(access_token, check.pendingData, integration)
                if check.status == "ready"
                else check
            )
            if result.status == "completed":
                return [
                    PostResponse(
                        id=post_details[0].id,
                        status="success",
                        postId=result.postId,
                        releaseURL=result.releaseURL,
                    )
                ]
            pending = result.pendingData
            if not (pending or {}).get("attempting"):
                time.sleep(20)

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        agent = self._agent(integration)
        comment_post = post_details
        embed = self._upload_media(agent, [{"path": item.path, "alt": item.alt or ""} for item in comment_post.media or []])
        facets = self._facets(agent, comment_post.message)

        parent_uri = parent_post_id or id
        try:
            thread = agent.get_post_thread(parent_uri)
        except XrpcError:
            thread = {}
        post = ((thread.get("thread") or {}).get("post") or {}) if isinstance(thread, dict) else {}
        parent_cid = str(post.get("cid") or "")
        root = (((post.get("record") or {}).get("reply") or {}).get("root")) or {}
        record = self._record(
            comment_post.message,
            facets,
            embed,
        )
        record["reply"] = {
            "root": {"uri": root.get("uri") or parent_uri, "cid": root.get("cid") or parent_cid},
            "parent": {"uri": parent_uri, "cid": parent_cid},
        }
        created = agent.create_record(agent.did, record)
        uri = str(created.get("uri") or "")
        return PostResponse(
            id=comment_post.id,
            status="success",
            postId=uri,
            releaseURL=self._release_url(integration, uri),
        )
