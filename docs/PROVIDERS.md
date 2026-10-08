# Providers

Every social platform is a class in `app/integrations/social/` that inherits from
`SocialAbstract` (`app/integrations/base.py`) and is registered in
`app/integrations/social/__init__.py`.

The framework around them is deliberately tiny: **the scheduler, the HTTP client, retries,
error mapping, token refresh and the UI are already written** — a provider only describes
how *its* platform behaves.

---

## 1. The contract

| Method | Required | Signature | Purpose |
|---|:--:|---|---|
| `identifier` / `name` / `picture` / `description` | ✅ | class attributes | Shown in the picker (description doubles as usage guidance) |
| `maxLength` | ✅ | `int` | Character limit enforced by the composer counter |
| `maxConcurrentJob` | — | `int` (default 1) | Platform rate limit hint |
| `generate_auth_url` | ✅* | `(refresh: bool = False, **kwargs) -> dict` | `{url, codeVerifier, state}` |
| `authenticate` | ✅ | `(code, code_verifier="", refresh="", **kwargs) -> AuthTokenDetails` | Exchange the callback payload for a session |
| `refresh_token` | ✅ | `(refresh_token: str) -> AuthTokenDetails` | New tokens, or empty details to signal "reconnect" |
| `post` | ✅* | `(id, access_token, post_details: list[PostDetails], integration) -> list[PostResponse]` | Publish |
| `post_pending` | async | `(id, access_token, post_details, integration) -> list[PostResponse]` | Start an async upload |
| `check_post_status` | async | `(access_token, pending: dict, integration) -> PendingCheckResponse` | Poll it |
| `finalize_post` | async | `(access_token, pending: dict, integration) -> PendingCheckResponse` | Confirm / promote |
| `comment` | — | `(id, access_token, post_details: PostDetails, parent_post_id, integration) -> PostResponse` | Reply |
| `connections` | — | `(details: AuthTokenDetails) -> list[AuthTokenDetails]` | One login → several channels |
| `channels` | — | `(integration) -> list[dict]` | Values for the composer's channel picker |
| `options` | — | `(key: str, integration) -> list[dict]` | Values for per-post selects (`?key=…`) |
| `custom_fields` | credentials | `() -> list[dict]` | Fields of the "paste your key" modal |
| `handle_errors` | — | `(body, status) -> (kind, message)` | Custom HTTP error classification |
| `postSettings` | — | `list[dict]` | Per-post settings schema (title, tags, cover…) |

\* `post` or `post_pending` must exist (a provider without async support implements `post` only).

### Data shapes (`app/integrations/interfaces.py`)

```python
AuthTokenDetails(id, name, accessToken, refreshToken="", expiresIn=None,
                 picture="", username="", additionalSettings={})

PostDetails(id, message, media=[MediaContent(type, path, alt, thumbnail, ...)],
            poll=None, sensitive=False, quote="", thread=False, settings={})

PostResponse(id, status="success"|"failed"|"pending", postId="", releaseURL="", pendingData=None)

PendingCheckResponse(status="pending"|"ready"|"completed"|"failed",
                     pendingData=None, postId="", releaseURL="", error="")
```

`expiresIn` is a **duration in seconds**; the service stores `tokenExpiration = now + expiresIn`.

---

## 2. Auth styles in the wild

| Style | Providers | What happens |
|---|---|---|
| **OAuth 2.0 authorization code** | LinkedIn, Discord, Reddit, Slack, Mastodon, Pinterest, YouTube, Facebook, Instagram, Threads, Tumblr, TikTok, Twitch | Browser redirect → `code` → token endpoint |
| **OAuth 2.0 + PKCE (S256)** | Kick, VK | `code_verifier` generated at auth-url time, `code_challenge` in the URL, verifier replayed in the token request |
| **OAuth 1.0a (HMAC-SHA1)** | X | `request_token` → `oauth/authenticate` → `access_token` + `oauth_verifier`; every API call is signed (`oauth1_signature`) |
| **Credentials (API key / app password)** | Medium, Bluesky | Frontend modal, values base64-encoded into `code`, `credentials = True` |
| **Bot token** | Telegram | Modal + `poll` endpoint that waits for `/connect <word>` in the chat |
| **Instance OAuth** | Mastodon | User types their instance URL first, the provider builds `/oauth/authorize` on that host |

`state` / `codeVerifier` are stored server-side for one hour
(`_store_state` / `_take_state` in `app/api/integrations.py`).

---

## 3. Platform notes

| Provider | Quirks worth knowing |
|---|---|
| **Bluesky** | Credentials = PDS handle + app password; a full `Agent` (`httpx`) implements `createSession`, `uploadBlob`, `createRecord`, facets (`#tags`, mentions, links by UTF-8 byte offsets), image downscaling to ≤ 976 KB, and video jobs on `video.bsky.app` (`aud = did:web:<pds host>`, `getJobStatus` is public). Arm/witness/publish guard against duplicates. |
| **X (Twitter)** | OAuth 1.0a signing built by hand (`HMAC-SHA1`), chunked `media/upload` (1 MB chunks, `FINALIZE`/`STATUS`), articles via Draft.js JSON → HTML, `stripLinks`/`strip_links_from_x_posts` option. |
| **TikTok** | `FILE_UPLOAD` chunked protocol (`init` → `part` → `complete`), `PULL_FROM_URL` for public links, int64 byte offsets, rule-based error messages (`ERROR_RULES`). |
| **LinkedIn** | Images one-shot, videos chunked (2 MB) with `registerUpload` + `uploadPart` + `finalize`, `ugcPosts` payload, 3-phase handshake. |
| **YouTube** | Resumable upload (`uploads` → chunk PUT → `videos.insert`), thumbnail upload, `channels` for the picker, `comment` via `commentThreads`. |
| **Facebook / Instagram** | Graph API; `connections()` expands one token into Page rows / IG professional accounts; carousels publish children first, then the container; container status polling. |
| **Threads** | Containers (`text` / single media / carousel) → `status` polling, `FINISHED` vs `PUBLISHED` (the latter = already live, don't re-publish), reply via `reply_to_id`. |
| **Tumblr** | Content-blocks payload (heading1, text chunks of 4096 *code points*, link, image with real dimensions, video), JSON or multipart body, one row per blog via `connections()`, shared refresh token synced on refresh. |
| **Reddit** | OAuth + `additionalSettings` JSON (username), `link`/`selftext` switch, `submit` then `edit` for media, comments via `api/comment`. |
| **Pinterest** | Pins need a target `board` (channel picker), `media_source` url/upload, comments via `pins/{id}/comments`. |
| **Slack / Discord / Telegram** | Channel pickers (`channels()`), blocks/embeds, thread replies; Telegram needs a bot token and a chat id captured from a `/connect` command. |
| **VK** | VK answers **HTTP 200 with `{error}`** — `check_api_error()` runs manually after every call (`error_code 5` → refresh, `6/9/29/100` → bad body). |
| **Kick / Twitch** | Text-only chat APIs (500 chars). Kick = PKCE + `reply_to_message_id`; Twitch = 2 s throttle, chat **or** announcement (per-post setting, colour validated against `primary/blue/green/orange/purple`). |
| **Medium** | `api-key` header, `publish=true`, image upload through `/v1/images`. |

---

## 4. Per-post settings

A provider declares fields, the composer renders them automatically:

```python
postSettings = [
    {"key": "title",     "label": "Title",      "type": "text",   "required": True},
    {"key": "type",      "label": "Post type",  "type": "select", "optionsKey": "types", "default": "posts"},
    {"key": "categories", "label": "Categories", "type": "multi",  "optionsKey": "categories"},
    {"key": "main_image", "label": "Cover",      "type": "media"},
]
```

Supported `type`s: `text`, `textarea`, `select` (static `options` or remote `optionsKey`),
`multi` (multiple choice), `tags` (comma separated → `[{label}]` / `[{slug}]`), `media`
(pick one of the attached assets).

`optionsKey` values come from `GET /integrations/{id}/channels?key=<optionsKey>`, which calls
`provider.options(key, integration)`. Errors surface as HTTP 400 with the provider's message.

The values land in `Post.settings` (JSON) and are delivered to `post_details[0].settings`.

---

## 5. Adding a provider in 5 steps

### 1. Create the module

`app/integrations/social/myplatform.py`:

```python
import json
from typing import Any

from app.integrations.base import BadBody, SocialAbstract
from app.integrations.interfaces import AuthTokenDetails, PostDetails, PostResponse

API = "https://api.myplatform.com/v1"


class MyplatformProvider(SocialAbstract):
    identifier = "myplatform"
    name = "My Platform"
    picture = "myplatform.svg"
    description = "Publish posts to My Platform"
    maxLength = 500
    maxConcurrentJob = 5
    scopes = ["posts:write", "profile:read"]

    def generate_auth_url(self, refresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        ...

    def authenticate(self, code, code_verifier="", refresh="", **kwargs) -> AuthTokenDetails:
        ...

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails:
        ...

    def post(self, id, access_token, post_details, integration) -> list[PostResponse]:
        first = post_details[0]
        payload = self.get_json(f"{API}/posts", method="POST",
                                headers={"Authorization": f"Bearer {access_token}"},
                                json={"body": first.message})
        return [PostResponse(id=first.id, status="success",
                             postId=str(payload["id"]),
                             releaseURL=payload["url"])]
```

### 2. Register it

```python
# app/integrations/social/__init__.py
from app.integrations.social.myplatform import MyplatformProvider

PROVIDERS = {
    ...
    "myplatform": MyplatformProvider,
}
```

The API, the picker, the scheduler and the connect flow pick it up automatically.

### 3. Add credentials (if needed)

`app/core/config.py` → `myplatform_client_id: str = ""` (+ `.env.example`), then use
`settings.myplatform_client_id` inside the provider.

### 4. Describe errors

```python
def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
    if "rate_limit" in str(body):
        return "retry", "My Platform is rate limiting, try again later"
    return super().handle_errors(body, status)
```

Kinds: `"retry"` (HTTP 429/5xx → retried by the client), `"bad-body"` (permanent → post ERROR),
`"refresh-token"` (auto refresh + retry), `"disconnect"` (needs reconnection).

### 5. Test it

Append a test to `tests/test_providers.py` (see [DEVELOPMENT.md](DEVELOPMENT.md#testing)):

```python
def test_myplatform_post():
    def fake_get_json(url, method="POST", json=None, **kwargs):
        assert json["body"] == "hello"
        return {"id": 42, "url": "https://myplatform.com/p/42"}

    original = myplatform.get_json
    myplatform.get_json = fake_get_json
    try:
        out = myplatform.post("u1", "tok", [PostDetails(id="p1", message="hello")], None)
        assert out[0].postId == "42"
    finally:
        myplatform.get_json = original
    print("OK myplatform post")
```

Run `python tests/test_providers.py` — your test must print `OK …` and the suite must end with
`ALL PROVIDER TESTS PASSED`.

---

## 6. Checklist before merging a provider

- [ ] `identifier`, `name`, `picture`, `description`, `maxLength` set
- [ ] Auth URL built with `encode_component()` for `redirect_uri` / `scope`
- [ ] `refresh_token()` returns **empty** details when refresh is impossible
- [ ] Media validated (count, size, type) *before* the API call, with a human message
- [ ] Release URL built from real ids (`releaseURL`), never guessed
- [ ] Async providers implement the three-phase handshake + duplicate guard
- [ ] `handle_errors` maps platform-specific messages to the four kinds
- [ ] No secrets in exception messages or logs
- [ ] Tests cover: auth, publish payload, at least one failure path
- [ ] Row added to the [README platform table](../README.md#supported-platforms)
