# REST API

Base URL: `http://localhost:8000` (configurable through `FRONTEND_URL` / `BACKEND_URL`).

Every endpoint except registration, login, public media and `/api/health` requires:

```http
Authorization: Bearer <token>
Content-Type: application/json
```

Interactive documentation is served by FastAPI at **`/docs`** (Swagger UI) and **`/redoc`**.

---

## 1. Authentication

### `POST /auth/register`

| Field | Type | Required | Notes |
|---|---|:--:|---|
| `email` | string | ✅ | must be a valid address |
| `password` | string | ✅ | **min 8 characters** |
| `name` | string | — | display name |
| `organization` | string | — | defaults to `My organization` |

```bash
curl -s -X POST localhost:8000/auth/register -H 'Content-Type: application/json' -d '{
  "email": "me@example.com", "password": "supersecret", "name": "Me", "organization": "Acme"
}'
```

```json
{ "token": "eyJhbGciOi…", "user": { "id": "…", "email": "me@example.com",
  "name": "Me", "organizationId": "…" } }
```

Errors: `400` password too short · `409` email already registered.

### `POST /auth/login`

```bash
curl -s -X POST localhost:8000/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"supersecret"}'
```

Same shape as register. Error: `401 Invalid credentials`.

### `GET /auth/me`

```json
{ "id": "…", "email": "me@example.com", "name": "Me", "organizationId": "…" }
```

Error: `401` missing/invalid/expired token (JWT lifetime = `JWT_EXPIRATION_DAYS`, default 7).

---

## 2. Platforms

### `GET /integrations/providers`

The catalogue consumed by the connect picker:

```json
[
  {
    "identifier": "bluesky",
    "name": "Bluesky",
    "picture": "bluesky.svg",
    "description": "Publish to Bluesky (max 4 pictures or 1 video, …)",
    "credentials": true,
    "customFields": [
      { "key": "service", "label": "Service", "validation": "^https?://.+$", "type": "text" },
      { "key": "identifier", "label": "Identifier", "validation": "^.+$", "type": "text" },
      { "key": "password", "label": "Password", "validation": "^.{3,}$", "type": "password" }
    ],
    "maxLength": 300,
    "postSettingsFields": []
  }
]
```

- `credentials: true` → the frontend opens a modal built from `customFields` instead of an OAuth redirect.
- `postSettingsFields` → per-post fields rendered in the composer (see [PROVIDERS.md](PROVIDERS.md#4-per-post-settings)).
- `maxLength` → composer counter limit.

---

## 3. Integrations (channels)

### `GET /integrations/list`

```json
[
  {
    "id": "int_1",
    "providerIdentifier": "x",
    "name": "Ann",
    "picture": "…",
    "username": "ann",
    "disabled": false,
    "refreshNeeded": false,
    "inBetweenSteps": false,
    "tokenExpiration": "2026-11-01T12:00:00",
    "provider": { "name": "X", "picture": "x.svg", "description": "…",
                  "credentials": false, "customFields": [], "maxLength": 280 }
  }
]
```

Tokens are **never** exposed.

### `GET /integrations/social/{provider}`

Start an OAuth dance. Optional query: `refresh=1` (re-connect), `instance=…` (Mastodon).

```bash
curl -s localhost:8000/integrations/social/x -H "Authorization: Bearer $TOKEN"
# {"url":"https://api.x.com/1.1/oauth/authenticate?oauth_token=…","state":"…"}
```

Credentials providers answer `{"state":"…","credentials":true}` — the frontend then shows the modal.

The `state` is stored server-side for **1 hour** and is single-use.

### `POST /integrations/social-connect/{provider}`

Complete the connection (this is where the browser lands after the platform redirects back).

| Field | Type | Notes |
|---|---|---|
| `code` | string | OAuth `code`, or base64 of the credentials JSON, or a provider-specific payload |
| `state` | string | must match the stored state (empty for credentials providers) |
| `refresh` | string | set when re-connecting |
| `details` | object | extra values (Telegram bot token, Mastodon instance…) |

```bash
# credentials-style connect (Bluesky)
curl -s -X POST localhost:8000/integrations/social-connect/bluesky \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"code\":\"$(printf '%s' '{"service":"https://bsky.social","identifier":"me.bsky.social","password":"xxx"}' | base64 -w0)\",\"state\":\"\"}"
```

Response — **an array**, because one login can create several channels
(Facebook Pages, Instagram accounts, Tumblr blogs):

```json
[ { "id": "…", "providerIdentifier": "bluesky", "name": "Ann", … } ]
```

Errors: `404 Unknown provider` · `400 Authentication failed` (or the provider's own message) ·
`409 Missing scopes: …` · `400 No channels found on this account` · `400 Invalid or expired state`.

### `DELETE /integrations/{integration_id}`

Disconnect. Returns `{"ok": true}`. `404` if it does not belong to your organization.

### `GET /integrations/{integration_id}/channels`

Channel picker values (Discord channels, Slack channels, Pinterest boards, YouTube channels):

```json
[ { "id": "C0123", "name": "general" } ]
```

With `?key=<optionsKey>` it returns per-post option lists instead
(`types`, `categories`, `tags`, `organizations`, `publications`…), served by
`provider.options(key, integration)`. Unknown keys → `400 <provider> does not support … options`.

### `POST /integrations/telegram/poll`

```json
{ "word": "connect-abc123", "botToken": "123456:ABC…", "offset": 123456 }
```

Long-polls Telegram for `/connect <word>` and answers
`{"lastChatId": …}` while waiting. `400` on an invalid token.

---

## 4. Posts

### `POST /posts`

Create one row per channel (a `group` ties them together):

```json
{
  "posts": [
    {
      "integrationId": "int_1",
      "content": "Hello world",
      "publishDate": "2026-10-10T09:00:00",
      "group": "g1",
      "image": [ { "path": "8f2c….png", "type": "image", "alt": "screenshot" } ],
      "settings": { "channel": "C0123", "messageType": "announcement" },
      "delay": 0,
      "draft": false,
      "parentPostId": null
    }
  ]
}
```

| Field | Notes |
|---|---|
| `integrationId` | target channel (from `/integrations/list`) |
| `publishDate` | ISO 8601; **required** (`400 publishDate is required`) |
| `image` | media objects as returned by `/media/upload` |
| `settings` | free-form JSON delivered to the provider (channel id, title, tags…) |
| `parentPostId` | set it to a parent post id to schedule a **comment** |
| `delay` | seconds offset used by comment chains |
| `draft` | `true` → stored as `DRAFT`, not picked by the scheduler |

Returns the serialized posts (see below).

### `GET /posts`

Up to 500 rows, ordered by `publishDate`, joined with their integration.

```json
[
  {
    "id": "post_1",
    "group": "g1",
    "state": "QUEUE",
    "publishDate": "2026-10-10T09:00:00",
    "content": "Hello world",
    "image": [ { "path": "8f2c….png", "type": "image" } ],
    "settings": {},
    "delay": 0,
    "releaseId": null,
    "releaseURL": null,
    "error": null,
    "parentPostId": null,
    "integration": { "id": "int_1", "name": "Ann", "picture": "…", "providerIdentifier": "x" }
  }
]
```

`state` ∈ `DRAFT` · `QUEUE` · `PUBLISHED` · `ERROR`.
While an asynchronous upload is being processed, `state` stays `QUEUE` and the row carries a
`pendingData` payload internally.

### `GET /posts/group/{group}`

All rows of one group (used when editing a multi-channel post).

### `POST /posts/{post_id}/now`

Re-queues the post for **immediate** publishing and runs `publish_post()` synchronously —
the response already contains the final `state` (unless the provider went async).

### `PUT /posts/{post_id}/date`

```json
{ "publishDate": "2026-10-11T10:00:00", "action": "schedule" }
```

`action: "schedule"` clears `error`, `releaseId` and `releaseURL` — that is how an `ERROR`
post is retried.

### `DELETE /posts/{post_id}`

Returns `{"ok": true}`.

---

## 5. Media

### `POST /media/upload`

`multipart/form-data`, field name `file`.

```bash
curl -s -X POST localhost:8000/media/upload -H "Authorization: Bearer $TOKEN" \
  -F "file=@cover.png"
```

```json
{ "id": "…", "path": "a1b2….png", "type": "image", "originalName": "cover.png" }
```

- Images: `png · jpg · jpeg · gif · webp`
- Videos: `mp4 · mov · webm`
- Max size **100 MB**; anything else → `400` / `413`.

### `GET /media/list`

100 most recent assets of your organization (id, path, type, originalName, fileSize).

### `GET /media/file/{path}`

Authenticated download of an asset.

### `GET /media/public/{path}`

Public download — this is the URL handed to platforms (`{BACKEND_URL}/media/public/<file>`).
Only files registered in the `Media` table are served; unknown names → `404`.

---

## 6. Operations

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | `{"status":"ok"}` — use it for container liveness probes |
| `GET /docs`, `GET /redoc` | OpenAPI UI |
| `GET /` | The SPA (any unknown path falls back to `index.html`, which is what makes OAuth redirects work) |

---

## 7. Error model

Errors are standard FastAPI responses with a human-readable `detail`:

```json
{ "detail": "Publishing failed: media too large" }
```

| Status | Meaning |
|---|---|
| `400` | Validation, provider rejection (the message comes from the provider) |
| `401` | Missing / invalid / expired JWT, or bad credentials |
| `404` | Unknown provider, unknown post, foreign resource |
| `409` | Duplicate email, missing OAuth scopes |
| `413` | Upload too large |
| `422` | Malformed JSON body |

Publishing errors are **not** returned by `POST /posts` — they are recorded on the row
(`state = "ERROR"`, `error = "…"`), because platforms answer asynchronously.
Poll `GET /posts` (or the UI) for the outcome.

---

## 8. Complete workflow example

```bash
BASE=http://localhost:8000
TOKEN=$(curl -s -X POST $BASE/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"supersecret"}' | python -c "import sys,json;print(json.load(sys.stdin)['token'])")

AUTH="Authorization: Bearer $TOKEN"

# connect a platform (browser flow: open the returned url, then POST the callback code)
curl -s $BASE/integrations/social/bluesky -H "$AUTH"

# list channels
curl -s $BASE/integrations/list -H "$AUTH"

# upload an image
MEDIA=$(curl -s -X POST $BASE/media/upload -H "$AUTH" -F "file=@cover.png")
PATH=$(echo $MEDIA | python -c "import sys,json;print(json.load(sys.stdin)['path'])")

# schedule on two channels
curl -s -X POST $BASE/posts -H "$AUTH" -H 'Content-Type: application/json' -d "{
  \"posts\": [
    {\"integrationId\":\"$CH1\",\"content\":\"Hello from Postiz Py\",\"publishDate\":\"2026-10-10T09:00:00\",
     \"image\":[{\"path\":\"$PATH\",\"type\":\"image\"}]},
    {\"integrationId\":\"$CH2\",\"content\":\"Hello from Postiz Py\",\"publishDate\":\"2026-10-10T09:00:00\",
     \"image\":[{\"path\":\"$PATH\",\"type\":\"image\"}]}
  ]}"

# watch the outcome
watch -n5 "curl -s $BASE/posts -H '$AUTH' | python -m json.tool"
```
