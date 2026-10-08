import base64
import hashlib
import hmac
import io
import mimetypes
import re
import secrets
import time
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from PIL import Image, ImageSequence

from app.core.config import settings
from app.integrations.base import BadBody, RefreshTokenError, SocialAbstract
from app.integrations.interfaces import (
    AuthTokenDetails,
    PendingCheckResponse,
    PostDetails,
    PostResponse,
)
from app.integrations.media import media_chunk, media_size, read_or_fetch

API = "https://api.x.com"
UPLOAD = "https://upload.twitter.com"
CHUNK_SIZE = 1024 * 1024
IMAGE_WIDTH = 1000
SCHEME_URL = re.compile(
    r"https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b"
    r"(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)",
    re.I,
)
BARE_DOMAIN_TLDS = (
    "online|store|cloud|social|world|blog|shop|site|tech|news|live|link|info|app|dev|xyz|"
    "com|net|org|edu|gov|biz|io|co|ai|me|tv|cc|ly|gl|sh|fm|am|be|to|gg|so|is|us|uk|ca|au|"
    "de|fr|es|it|nl|se|no|dk|fi|pl|ch|at|ie|nz|za|in|jp|br|mx|ru|eu"
)
BARE_DOMAIN = re.compile(
    r"(?<![\w@./-])(?:[a-zA-Z0-9-]+\.)+(?:" + BARE_DOMAIN_TLDS + r")\b"
    r"(?:/[-a-zA-Z0-9()@:%_+.~#?&/=]*)?",
    re.I,
)
LINK_PATTERN = re.compile(f"(?:{SCHEME_URL.pattern})|(?:{BARE_DOMAIN.pattern})", re.I)
VOID_TAGS = {"br", "img", "hr", "meta", "input", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


def _pct(value: Any) -> str:
    return quote(str(value), safe="~")


def oauth_header(
    method: str,
    url: str,
    token: str = "",
    token_secret: str = "",
    oauth_extra: dict[str, str] | None = None,
    body_params: dict[str, str] | None = None,
) -> str:
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    oauth: dict[str, str] = {
        "oauth_consumer_key": settings.x_api_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    if token:
        oauth["oauth_token"] = token
    if oauth_extra:
        oauth.update(oauth_extra)

    pairs: list[tuple[str, str]] = []
    pairs.extend(parse_qsl(parsed.query, keep_blank_values=True))
    if body_params:
        pairs.extend((str(key), str(value)) for key, value in body_params.items())
    pairs.extend(oauth.items())

    param_string = "&".join(f"{_pct(key)}={_pct(value)}" for key, value in sorted(pairs))
    base_string = "&".join([method.upper(), _pct(base_url), _pct(param_string)])
    signing_key = f"{_pct(settings.x_api_secret)}&{_pct(token_secret)}"
    digest = hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    return "OAuth " + ", ".join(f'{_pct(key)}="{_pct(value)}"' for key, value in sorted(oauth.items()))


def strip_links_text(text: str) -> str:
    without = LINK_PATTERN.sub("", text or "")
    without = re.sub(r"<a\b[^>]*>\s*</a>", "", without, flags=re.I)
    without = re.sub(r"[ \t]{2,}", " ", without)
    without = re.sub(r" +\n", "\n", without)
    return without.strip()


class _Node:
    __slots__ = ("name", "attrs", "children", "text")

    def __init__(self, name: str, attrs: dict[str, str] | None = None, text: str = ""):
        self.name = name
        self.attrs = attrs or {}
        self.children: list[_Node] = []
        self.text = text


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document")
        self.stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_Node(tag, {key: value or "" for key, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].name == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(_Node("#text", None, data))


def parse_html(html: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(html or "")
    return builder.root


def html_to_text(html: str) -> str:
    if not re.search(r"<\s*/?\s*[a-z]", html or "", re.I):
        return html or ""
    root = parse_html(html)
    parts: list[str] = []

    def walk(node: _Node) -> None:
        if node.name in ("script", "style"):
            return
        if node.name == "#text":
            parts.append(node.text)
            return
        if node.name == "br":
            parts.append("\n")
            return
        block = node.name in (
            "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote", "pre", "section", "article", "ul", "ol"
        )
        if block:
            parts.append("\n")
        for child in node.children:
            walk(child)
        if block:
            parts.append("\n")

    for child in root.children:
        walk(child)
    text = "".join(parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return unescape(text).strip()


def looks_like_html(message: str) -> bool:
    return bool(re.search(r"<\s*/?\s*[a-z]", message or "", re.I))


class XProvider(SocialAbstract):
    identifier = "x"
    name = "X"
    picture = "x.svg"
    description = "Publish tweets, threads and long-form articles"
    maxLength = 280
    maxConcurrentJob = 10
    scopes: list[str] = []
    stripLinks = settings.strip_links_from_x_posts

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        text = str(body)
        rules: list[tuple[str, str, str]] = [
            (
                "You are not permitted to perform this action",
                "bad-body",
                "There is a problem posting, please edit your post and check character count and media attachments",
            ),
            ("Service Unavailable", "retry", "X is currently unavailable, please try again later"),
            ("Too Many Requests", "retry", "X rate limit reached, please try again later"),
            ("maximum of one cashtag", "bad-body", "There can be maximum of one cashtag ($SYMBOL) per post"),
            ("maximum of 4 items", "bad-body", "There must be a maximum of 4 items per post"),
            ("Unsupported Authentication", "refresh-token", "X authentication has expired, please reconnect your account"),
            ("You are not allowed to create a Tweet", "bad-body", "You are not allowed to create a post with duplicate content"),
            ("usage-capped", "bad-body", "Posting failed - capped reached. Please try again later"),
            ("user-suspended", "bad-body", "Your X account has been suspended, please reconnect with another account"),
            ("duplicate-rules", "bad-body", "You have already posted this post, please wait before posting again"),
            ("Your account is not permitted to access this feature", "bad-body", "X blocked your request"),
            ("The Tweet contains an invalid URL.", "bad-body", "The Tweet contains a URL that is not allowed on X"),
            (
                "This user is not allowed to post a video longer than 2 minutes",
                "bad-body",
                "The video you are trying to post is longer than 2 minutes, which is not allowed for this account",
            ),
            (
                "This user is not allowed to post a video longer than 10 minutes",
                "bad-body",
                "The video you are trying to post is longer than 10 minutes, which is not allowed for this account",
            ),
            (
                "Your account is temporarily locked",
                "bad-body",
                "Your X account is temporarily locked, log in to x.com to unlock it and then try again",
            ),
            (
                "Crypto addresses are prohibited",
                "bad-body",
                "X does not allow crypto addresses in posts for the first 7 days after connecting the account",
            ),
            (
                "Your media IDs are invalid",
                "bad-body",
                "X rejected the attached media, please re-upload the media and try again",
            ),
            ("not authorized to create or publish articles", "bad-body", "Publishing articles on X requires an X Premium subscription"),
            (
                "Please include either text or media in your Tweet",
                "bad-body",
                "One of the posts in this thread has no text or media, please add some text or remove it",
            ),
            ('"title":"Unauthorized"', "refresh-token", "X rejected the connected account, please reconnect your account"),
        ]
        for needle, kind, message in rules:
            if needle in text:
                return kind, message
        if status == 429:
            return "retry", "X rate limit reached, please try again later"
        return super().handle_errors(body, status)

    def _redirect_uri(self) -> str:
        return f"{settings.frontend_url}/integrations/social/x"

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        body = self._request(
            "POST",
            f"{API}/1.1/oauth/request_token",
            "",
            "",
            oauth_extra={"oauth_callback": self._redirect_uri()},
            data={"x_auth_access_type": "write"},
            signed_data={"x_auth_access_type": "write"},
        )
        request_token = str(body.get("oauth_token", ""))
        request_secret = str(body.get("oauth_token_secret", ""))
        if not request_token or not request_secret:
            raise BadBody(self.identifier, str(body), "{}", "X did not return a request token")
        return {
            "url": f"{API}/1.1/oauth/authenticate?oauth_token={quote(request_token, safe='')}",
            "codeVerifier": f"{request_token}:{request_secret}",
            "state": request_token,
        }

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        return AuthTokenDetails(id="", name="", accessToken="", refreshToken="", expiresIn=0)

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails:
        request_token, request_secret = (code_verifier.split(":", 1) + [""])[:2]
        body = self._request(
            "POST",
            f"{API}/1.1/oauth/access_token",
            request_token,
            request_secret,
            oauth_extra={"oauth_verifier": code},
        )
        access_token = str(body.get("oauth_token", ""))
        access_secret = str(body.get("oauth_token_secret", ""))
        if not access_token or not access_secret:
            raise BadBody(self.identifier, str(body), "{}", "X did not return an access token")

        me = self._request(
            "GET",
            f"{API}/2/users/me?{urlencode({'user.fields': 'username,verified,verified_type,profile_image_url,name'})}",
            access_token,
            access_secret,
        )
        data = me.get("data") or {}
        return AuthTokenDetails(
            id=str(data.get("id", "")),
            name=data.get("name", ""),
            accessToken=f"{access_token}:{access_secret}",
            refreshToken="",
            expiresIn=999999999,
            picture=data.get("profile_image_url") or "",
            username=data.get("username", ""),
            additionalSettings={"verified": bool(data.get("verified"))},
        )

    def _split(self, access_token: str) -> tuple[str, str]:
        parts = (access_token or "").split(":", 1)
        return parts[0], (parts[1] if len(parts) > 1 else "")

    def _request(
        self,
        method: str,
        url: str,
        token: str = "",
        token_secret: str = "",
        *,
        oauth_extra: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        signed_data: dict[str, str] | None = None,
        files: dict | None = None,
        json_body: Any = None,
    ) -> Any:
        headers = {
            "Authorization": oauth_header(method, url, token, token_secret, oauth_extra, signed_data),
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = self.fetch(url, method=method, headers=headers, data=data, files=files, json=json_body)
        text = response.text
        if not text:
            return {}
        if text.lstrip().startswith("{") or text.lstrip().startswith("["):
            try:
                return response.json()
            except ValueError:
                return {}
        return dict(parse_qsl(text, keep_blank_values=True))

    def _prepare_image(self, path: str, as_article_image: bool) -> tuple[bytes, str, str]:
        raw = read_or_fetch(path)
        lower = path.lower().split("?")[0]
        if lower.endswith(".gif"):
            image = Image.open(io.BytesIO(raw))
            frames = []
            for frame in ImageSequence.Iterator(image):
                current = frame.convert("RGBA")
                if current.width > IMAGE_WIDTH:
                    height = max(1, round(current.height * IMAGE_WIDTH / current.width))
                    current = current.resize((IMAGE_WIDTH, height), Image.LANCZOS)
                frames.append(current.convert("P", palette=Image.ADAPTIVE))
            buffer = io.BytesIO()
            duration = image.info.get("duration", 100)
            frames[0].save(
                buffer,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=duration,
                loop=image.info.get("loop", 0),
            )
            return buffer.getvalue(), "image/gif", "media.gif"

        with Image.open(io.BytesIO(raw)) as opened:
            image = opened.convert("RGBA" if opened.mode in ("RGBA", "LA") and not as_article_image else "RGB")
        if image.width > IMAGE_WIDTH:
            height = max(1, round(image.height * IMAGE_WIDTH / image.width))
            image = image.resize((IMAGE_WIDTH, height), Image.LANCZOS)

        buffer = io.BytesIO()
        if as_article_image or image.mode == "RGB":
            image.convert("RGB").save(buffer, format="JPEG", quality=90)
            return buffer.getvalue(), "image/jpeg", "media.jpg"
        image.save(buffer, format="PNG")
        return buffer.getvalue(), "image/png", "media.png"

    def _upload_image(self, token: str, secret: str, path: str, as_article_image: bool) -> str:
        payload, media_type, filename = self._prepare_image(path, as_article_image)
        category = "tweet_image"
        if media_type == "image/gif" and not as_article_image:
            category = "tweet_gif"
        response = self._request(
            "POST",
            f"{UPLOAD}/1.1/media/upload.json",
            token,
            secret,
            files={"media": (filename, payload, media_type)},
            data={"media_category": category},
        )
        return str(response.get("media_id_string") or response.get("media_id") or "")

    def _upload_video(self, token: str, secret: str, path: str) -> dict[str, Any]:
        total_bytes = media_size(path)
        media_type = mimetypes.guess_type(path)[0] or "video/mp4"
        initialize = self._request(
            "POST",
            f"{API}/2/media/upload/initialize",
            token,
            secret,
            json_body={"media_type": media_type, "total_bytes": total_bytes, "media_category": "tweet_video"},
        )
        media_id = str((initialize.get("data") or {}).get("id", ""))
        if not media_id:
            raise BadBody(self.identifier, str(initialize), "{}", "X could not start the video upload")

        total_chunks = max(1, -(-total_bytes // CHUNK_SIZE))
        for index in range(total_chunks):
            start = index * CHUNK_SIZE
            end = min(start + CHUNK_SIZE, total_bytes) - 1
            if end < start:
                break
            chunk = media_chunk(path, start, end)
            self._request(
                "POST",
                f"{API}/2/media/upload/{media_id}/append",
                token,
                secret,
                data={"segment_index": str(index)},
                files={"media": (mimetypes.guess_type(path)[0] or "video/mp4", chunk, media_type)},
            )

        finalize = self._request(
            "POST",
            f"{API}/2/media/upload/{media_id}/finalize",
            token,
            secret,
            json_body={},
        )
        processing = ((finalize.get("data") or {}).get("processing_info")) or finalize.get("processing_info")
        if (processing or {}).get("state") == "failed":
            message = ((processing or {}).get("error") or {}).get("message", "")
            raise BadBody(
                self.identifier,
                str(processing),
                "{}",
                f"X failed to process the uploaded video{': ' + message if message else ''}",
            )
        return {
            "mediaId": media_id,
            "processing": bool(processing) and processing.get("state") != "succeeded",
        }

    def _media_status(self, token: str, secret: str, media_id: str) -> dict[str, Any] | None:
        url = f"{API}/2/media/upload?{urlencode({'command': 'STATUS', 'media_id': media_id})}"
        body = self._request("GET", url, token, secret)
        return ((body.get("data") or {}).get("processing_info")) or body.get("processing_info")

    def _wait_for_media(self, token: str, secret: str, media_id: str) -> None:
        waited = 0.0
        max_wait = 7 * 60
        processing = self._media_status(token, secret, media_id)
        while processing and processing.get("state") != "succeeded":
            if processing.get("state") == "failed" or waited >= max_wait:
                message = ((processing or {}).get("error") or {}).get("message", "")
                raise BadBody(
                    self.identifier,
                    str(processing),
                    "{}",
                    f"X failed to process the uploaded video{': ' + message if message else ''}",
                )
            wait = float(processing.get("check_after_secs") or 1)
            time.sleep(wait)
            waited += wait
            processing = self._media_status(token, secret, media_id)

    def _upload_entries(
        self, token: str, secret: str, post_details: list[PostDetails], as_article_image: bool = False
    ) -> tuple[dict[str, list[str]], list[str]]:
        media: dict[str, list[str]] = {}
        processing_ids: list[str] = []
        for post in post_details:
            for item in post.media or []:
                if item.path.lower().split("?")[0].endswith(".mp4"):
                    uploaded = self._upload_video(token, secret, item.path)
                else:
                    media_id = self._upload_image(token, secret, item.path, as_article_image)
                    uploaded = {"mediaId": media_id, "processing": False}
                media_id = str(uploaded.get("mediaId") or "")
                if not media_id:
                    continue
                media.setdefault(post.id, []).append(media_id)
                if uploaded.get("processing"):
                    processing_ids.append(media_id)
        return media, processing_ids

    def _tweet_text(self, message: str) -> str:
        text = html_to_text(message) if looks_like_html(message) else (message or "")
        if self.stripLinks:
            text = strip_links_text(text)
        return text

    def _article_images(self, node: _Node) -> list[str]:
        if node.name == "img":
            src = node.attrs.get("src", "")
            if not src or ".." in src or src.startswith("/"):
                return []
            if re.match(r"^https?://", src, re.I) or "://" not in src:
                return [src]
            return []
        found: list[str] = []
        for child in node.children:
            found.extend(self._article_images(child))
        return found

    def post_pending(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        token, secret = self._split(access_token)
        first = post_details[0]
        config = first.settings or {}
        is_article = config.get("post_type") == "article"

        media, processing_ids = self._upload_entries(token, secret, [first], as_article_image=is_article)

        cover_media_id = ""
        cover_path = config.get("article_cover", {}).get("path", "") if is_article else ""
        if cover_path:
            cover_media_id = self._upload_image(token, secret, cover_path, True)

        inline_media_ids: dict[str, str] = {}
        if is_article:
            inline_images = list(dict.fromkeys(self._article_images(parse_html(first.message))))
            if inline_images:
                for index, src in enumerate(inline_images):
                    media_id = self._upload_image(token, secret, src, True)
                    if media_id:
                        inline_media_ids[src] = media_id

        pending: dict[str, Any] = {
            "message": first.message if is_article else self._tweet_text(first.message),
            "settings": {
                "who_can_reply_post": config.get("who_can_reply_post"),
                "community": config.get("community"),
                "made_with_ai": config.get("made_with_ai"),
                "paid_partnership": config.get("paid_partnership"),
                "post_type": config.get("post_type"),
                "article_title": config.get("article_title"),
                "article_status": config.get("article_status"),
            },
            "mediaIds": media.get(first.id, []),
            "processingIds": processing_ids,
        }
        if cover_media_id:
            pending["coverMediaId"] = cover_media_id
        if inline_media_ids:
            pending["inlineMediaIds"] = inline_media_ids
        return [PostResponse(id=first.id, status="pending", pendingData=pending)]

    def check_post_status(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        token, secret = self._split(access_token)
        data = dict(pending)

        if data.get("attempting") and data.get("confirmed"):
            raise BadBody(
                self.identifier,
                "{}",
                "{}",
                "X may have already published this post, please check your account before posting again "
                "to avoid duplicates",
            )

        still_processing: list[str] = []
        for media_id in data.get("processingIds") or []:
            try:
                processing = self._media_status(token, secret, media_id)
            except RefreshTokenError:
                raise
            except BadBody as exc:
                message = str(exc.message or "")
                if any(
                    transient in message
                    for transient in (
                        "Service Unavailable",
                        "Too Many Requests",
                        "Timeout on",
                        "Network error",
                        "Request failed after",
                    )
                ):
                    return PendingCheckResponse(status="pending", pendingData=data)
                raise
            except Exception:
                return PendingCheckResponse(status="pending", pendingData=data)

            if (processing or {}).get("state") == "failed":
                message = ((processing or {}).get("error") or {}).get("message", "")
                raise BadBody(
                    self.identifier,
                    str(processing),
                    "{}",
                    f"X failed to process the uploaded video{': ' + message if message else ''}",
                )
            if processing and processing.get("state") != "succeeded":
                still_processing.append(media_id)

        if still_processing:
            return PendingCheckResponse(status="pending", pendingData={**data, "processingIds": still_processing})

        if data.get("attempting") and not data.get("confirmed"):
            return PendingCheckResponse(
                status="ready", pendingData={**data, "processingIds": [], "confirmed": True}
            )
        return PendingCheckResponse(status="ready", pendingData={**data, "processingIds": []})

    def _article_content_state(
        self, html: str, embedded_media_ids: list[str], inline_media_ids: dict[str, str]
    ) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        entities: list[dict[str, Any]] = []

        def walk_inline(node: _Node, ctx: dict[str, Any]) -> None:
            for child in node.children:
                if child.name == "#text":
                    ctx["text"] += child.text
                    continue
                offset = len(ctx["text"])
                walk_inline(child, ctx)
                length = len(ctx["text"]) - offset
                if not length:
                    continue
                if child.name == "strong":
                    ctx["styles"].append({"offset": offset, "length": length, "style": "bold"})
                url = child.attrs.get("href") if child.name == "a" else ""
                if url:
                    key = len(entities)
                    entities.append(
                        {"key": str(key), "value": {"type": "link", "mutability": "mutable", "data": {"url": url}}}
                    )
                    ctx["entityRanges"].append({"offset": offset, "length": length, "key": key})

        def make_block(
            text: str,
            block_type: str,
            styles: list[dict[str, Any]] | None = None,
            entity_ranges: list[dict[str, Any]] | None = None,
        ) -> dict[str, Any]:
            block: dict[str, Any] = {"key": f"b{len(blocks)}", "text": text, "type": block_type}
            if styles:
                block["inline_style_ranges"] = styles
            if entity_ranges:
                block["entity_ranges"] = entity_ranges
            return block

        def push_image(media_id: str) -> None:
            key = len(entities)
            entities.append(
                {
                    "key": str(key),
                    "value": {
                        "type": "image",
                        "mutability": "immutable",
                        "data": {"media_items": [{"media_category": "tweet_image", "media_id": media_id}]},
                    },
                }
            )
            blocks.append(make_block(" ", "atomic", [], [{"offset": 0, "length": 1, "key": key}]))

        def push_block(node: _Node, block_type: str) -> None:
            ctx: dict[str, Any] = {"text": "", "styles": [], "entityRanges": []}
            walk_inline(node, ctx)
            if ctx["text"].strip():
                blocks.append(make_block(ctx["text"], block_type, ctx["styles"], ctx["entityRanges"]))
            for src in self._article_images(node):
                media_id = inline_media_ids.get(src, "")
                if media_id:
                    push_image(media_id)

        for node in parse_html(html).children:
            if node.name == "h1":
                push_block(node, "header-one")
            elif node.name in ("h2", "h3"):
                push_block(node, "header-two")
            elif node.name in ("ul", "ol"):
                block_type = "ordered-list-item" if node.name == "ol" else "unordered-list-item"
                for child in node.children:
                    if child.name == "li":
                        push_block(child, block_type)
            elif node.name == "#text":
                if node.text.strip():
                    blocks.append(make_block(node.text, "unstyled"))
            else:
                push_block(node, "unstyled")

        if not blocks:
            blocks.append(make_block(html_to_text(html), "unstyled"))

        for media_id in embedded_media_ids:
            push_image(media_id)

        return {"blocks": blocks, "entities": entities}

    def _finalize_article(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        token, secret = self._split(access_token)
        config = pending.get("settings") or {}
        embedded = [media_id for media_id in (pending.get("mediaIds") or []) if media_id]

        payload: dict[str, Any] = {
            "title": config.get("article_title", ""),
            "content_state": self._article_content_state(
                pending.get("message", ""), embedded, pending.get("inlineMediaIds") or {}
            ),
        }
        if pending.get("coverMediaId"):
            payload["cover_media"] = {"media_category": "tweet_image", "media_id": pending["coverMediaId"]}

        draft = self._request("POST", f"{API}/2/articles/draft", token, secret, json_body=payload)
        draft_id = str((draft.get("data") or {}).get("id", ""))
        if not draft_id:
            raise BadBody(self.identifier, str(draft), "{}", "X could not create the article draft")

        if config.get("article_status") != "published":
            return PendingCheckResponse(status="completed", postId=draft_id, releaseURL="https://x.com/i/articles")

        publish = self._request("POST", f"{API}/2/articles/{draft_id}/publish", token, secret, json_body={})
        post_id = str((publish.get("data") or {}).get("post_id", ""))
        if not post_id:
            raise BadBody(
                self.identifier,
                str(publish),
                "{}",
                "X created the article draft but could not publish it, check your drafts on X",
            )
        profile = getattr(integration, "username", "") or ""
        return PendingCheckResponse(
            status="completed",
            postId=post_id,
            releaseURL=f"https://twitter.com/{profile}/status/{post_id}",
        )

    def finalize_post(
        self, access_token: str, pending: dict[str, Any], integration: Any
    ) -> PendingCheckResponse:
        data = dict(pending)
        if not data.get("attempting") or not data.get("confirmed"):
            return PendingCheckResponse(status="pending", pendingData={**data, "attempting": True, "confirmed": False})

        if (data.get("settings") or {}).get("post_type") == "article":
            return self._finalize_article(access_token, data, integration)

        token, secret = self._split(access_token)
        config = data.get("settings") or {}
        media_ids = [media_id for media_id in (data.get("mediaIds") or []) if media_id]

        payload: dict[str, Any] = {"text": self._tweet_text(data.get("message", ""))}
        reply_settings = config.get("who_can_reply_post")
        if reply_settings and reply_settings != "everyone":
            payload["reply_settings"] = reply_settings
        if config.get("community"):
            payload["share_with_followers"] = True
            payload["community_id"] = str(config["community"]).split("/")[-1]
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        payload["made_with_ai"] = self._asset_boolean(config.get("made_with_ai"))
        payload["paid_partnership"] = self._asset_boolean(config.get("paid_partnership"))

        tweet = self._request("POST", f"{API}/2/tweets", token, secret, json_body=payload)
        post_id = str((tweet.get("data") or {}).get("id", ""))
        if not post_id:
            raise BadBody(self.identifier, str(tweet), "{}", "X did not create the post")
        profile = getattr(integration, "username", "") or ""
        return PendingCheckResponse(
            status="completed",
            postId=post_id,
            releaseURL=f"https://twitter.com/{profile}/status/{post_id}",
        )

    @staticmethod
    def _asset_boolean(value: Any) -> bool:
        if isinstance(value, str):
            return value.lower() == "true"
        return bool(value)

    def post(
        self, id: str, access_token: str, post_details: list[PostDetails], integration: Any
    ) -> list[PostResponse]:
        started = time.time()
        pending = self.post_pending(id, access_token, post_details, integration)[0].pendingData
        while True:
            if time.time() - started > 8 * 60:
                raise BadBody(self.identifier, "{}", "{}", "X took too long to process the media, please try again")
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

    def comment(
        self, id: str, access_token: str, post_details: PostDetails, parent_post_id: str, integration: Any
    ) -> PostResponse:
        token, secret = self._split(access_token)
        media, _ = self._upload_entries(token, secret, [post_details])
        media_ids = media.get(post_details.id, [])
        for media_id in media_ids:
            self._wait_for_media(token, secret, media_id)

        payload: dict[str, Any] = {
            "text": self._tweet_text(post_details.message),
            "reply": {"in_reply_to_tweet_id": parent_post_id or id},
        }
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        payload["made_with_ai"] = self._asset_boolean((post_details.settings or {}).get("made_with_ai"))
        payload["paid_partnership"] = self._asset_boolean((post_details.settings or {}).get("paid_partnership"))

        tweet = self._request("POST", f"{API}/2/tweets", token, secret, json_body=payload)
        post_id = str((tweet.get("data") or {}).get("id", ""))
        profile = getattr(integration, "username", "") or ""
        return PostResponse(
            id=post_details.id,
            status="success",
            postId=post_id,
            releaseURL=f"https://twitter.com/{profile}/status/{post_id}",
        )
