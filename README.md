<div align="center">

# Postiz Python

**A complete, dependency-light Python rewrite of [Postiz](https://github.com/gitroomhq/postiz-app)** —
schedule, publish and manage your social media from a single lightweight process.

*No Node.js. No build step. No Redis. No Temporal. No Postgres requirement.
Just Python, FastAPI and SQLite — up and running in under a minute.*

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white&style=flat-square)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white&style=flat-square)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue?style=flat-square)](LICENSE)
[![Providers](https://img.shields.io/badge/platforms-19-ff4500?style=flat-square)](#supported-platforms)
[![Tests](https://img.shields.io/badge/tests-61%20passing-brightgreen?style=flat-square)](#testing)
[![Frontend](https://img.shields.io/badge/frontend-vanilla%20JS%20%C2%B7%20zero%20build-282c34?style=flat-square)](#project-layout)

[Quick start](#quick-start) · [Platforms](#supported-platforms) · [How it works](#how-it-works) · [API](#rest-api) · [Docs](#documentation)

</div>

---

## Why Postiz Python?

The original Postiz is a fantastic open-source social media scheduler — but running it means
operating a **Next.js + NestJS + Prisma + PostgreSQL + Redis + Temporal** monorepo with a
Node toolchain and a multi-service deployment.

**Postiz Py** reimplements the same product — flows, OAuth dances, handshakes and all — in a
single Python codebase you can read end to end:

| | Postiz (upstream) | Postiz Python |
|---|---|---|
| Backend | NestJS (TypeScript) | **FastAPI** (`app/`) |
| Frontend | Next.js / React + build | **Vanilla HTML/CSS/JS**, served by the API |
| Database | PostgreSQL via Prisma | **SQLite** via SQLAlchemy (any SQLAlchemy URL works) |
| Jobs / workflows | Temporal | **In-process scheduler thread** (`app/services/scheduler.py`) |
| OAuth state | Redis | **In-memory store** with TTL |
| Deployment | Docker, many services | **One command**: `uvicorn app.main:app` |
| Runtime deps | Node 20+, pnpm, DB, Redis | **Python 3.11+ only** |

### Highlights

- 🚀 **19 social platforms** connected through a single, uniform provider interface
- 🔐 **Every auth style** the real platforms demand: OAuth 2.0 (+ PKCE S256), OAuth 1.0a HMAC-SHA1, API keys, bot tokens
- 🔁 **Automatic token refresh** with a 5-minute margin, plus sibling synchronization for multi-account providers
- ⏱ **Deterministic scheduler**: publish due posts, poll async handshakes, refresh expiring tokens
- 🤝 **3-phase async publishing** (`post` → `check` → `finalize`) with a duplicate-post guard, mirroring upstream's Temporal workflow
- 🧩 **Multi-account providers**: Facebook Pages, Instagram accounts and Tumblr blogs each become their own channel from one login
- 💬 **Comments & replies** threaded from the scheduler (`parentPostId` + delay)
- 🖼 **Media pipeline**: local uploads, image dimensions, chunked uploads, image downscaling
- 🎛 **Per-post settings** (channel pickers, Twitch message type / announcement color)
- 🧪 **61 tests** that run as plain scripts — no pytest, no fixtures, no mocks library
- 🌐 **Self-contained UI**: accounts, composer, calendar, drafts — no bundler ever touched it

---

## Supported platforms

| Platform | Auth | Media | Comments | Async publish | Max concurrent jobs |
|---|---|:--:|:--:|:--:|:--:|
| **Bluesky** | Credentials (PDS handle + app password) | ✅ images + video | ✅ | ✅ | 6 |
| **Discord** | OAuth 2.0 | ✅ | ✅ | — | 5 |
| **Facebook Page** | OAuth 2.0 (Meta) | ✅ | ✅ | ✅ | 500 |
| **Instagram** | OAuth 2.0 (via Meta) | ✅ carousels + reels | ✅ | ✅ | 400 |
| **Kick** | OAuth 2.0 + PKCE S256 | text only | ✅ replies | — | 3 |
| **LinkedIn** | OAuth 2.0 | ✅ images + video | ✅ | ✅ | 8 |
| **Mastodon** | OAuth 2.0 (any instance) | ✅ | ✅ | ✅ | 10 |
| **Medium** | API key (credentials modal) | ✅ | — | — | 3 |
| **Pinterest** | OAuth 2.0 | ✅ pins | ✅ | ✅ | 10 |
| **Reddit** | OAuth 2.0 | ✅ | ✅ | ✅ | 1 |
| **Slack** | OAuth 2.0 (bot) | ✅ | ✅ | — | 10 |
| **Telegram** | Bot token + chat | ✅ | ✅ | — | 3 |
| **Threads** | OAuth 2.0 (Meta) | ✅ carousel | ✅ | ✅ | 20 |
| **TikTok** | OAuth 2.0 | ✅ video + photo | — | ✅ | 10000 |
| **Tumblr** | OAuth 2.0 (`offline_access`) | ✅ content blocks | — | — | 3 |
| **Twitch** | OAuth 2.0 | text only | ✅ replies | — | 5 |
| **VK** | OAuth 2.0 + PKCE S256 | ✅ photos + video | ✅ | — | 5 |
| **X (Twitter)** | OAuth 1.0a (HMAC-SHA1) | ✅ images + video | ✅ articles | ✅ | 10 |
| **YouTube** | OAuth 2.0 (Google) | ✅ video upload | ✅ | ✅ | 200 |

*Async publish* = the provider needs a handshake (upload is processed remotely), so the post waits in
`PENDING` until the platform confirms — exactly like upstream's Temporal workflow.

---

## Quick start

```bash
git clone https://github.com/saladinlorenz/postiz-python.git
cd postiz-python

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env               # add the keys for the platforms you use

uvicorn app.main:app --port 8000
```

Open **http://localhost:8000**, create an account, then go to **Accounts → Connect**.

That's it — the database (`postiz.db`), the `uploads/` folder and the background scheduler
are all created on first boot.

<details>
<summary><b>Requirements</b></summary>

- Python **3.11+** (tested on 3.12)
- ~40 MB of dependencies (`fastapi`, `uvicorn`, `sqlalchemy`, `httpx`, `pydantic-settings`,
  `pyjwt`, `bcrypt`, `pillow`, `python-multipart`, `email-validator`)
- Optional: [`quickjs`](https://pypi.org/project/QuickJS/) to run the frontend syntax test

</details>

<details>
<summary><b>Linux / macOS one-liner</b></summary>

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt \
  && cp -n .env.example .env && uvicorn app.main:app --host 0.0.0.0 --port 8000
```

</details>

---

## Configuration

Everything lives in `.env` (see [`.env.example`](.env.example)). Only fill in the platforms you enable.

| Variable | Purpose | Default |
|---|---|---|
| `FRONTEND_URL` | Public URL used as OAuth redirect URI | `http://localhost:8000` |
| `BACKEND_URL` | Public URL used to build absolute media links | `http://localhost:8000` |
| `DATABASE_URL` | Any SQLAlchemy URL | `sqlite:///./postiz.db` |
| `JWT_SECRET` | Signing key for API tokens | `change-me` |
| `JWT_EXPIRATION_DAYS` | API token lifetime | `7` |
| `UPLOAD_DIRECTORY` | Where uploaded media is stored | `./uploads` |
| `SCHEDULER_INTERVAL_SECONDS` | Scheduler tick | `10` |
| `PENDING_CHECK_INTERVAL_SECONDS` | Async handshake poll interval | `20` |
| `PENDING_MAX_CHECKS` | Handshake polls before giving up | `90` |
| `REFRESH_MARGIN_SECONDS` | Refresh tokens this long before expiry | `300` |

**Provider credentials** (all optional, one group per platform):

```
LINKEDIN_CLIENT_ID/SECRET      DISCORD_CLIENT_ID/SECRET     DISCORD_BOT_TOKEN
REDDIT_CLIENT_ID/SECRET        SLACK_CLIENT_ID/SECRET       PINTEREST_CLIENT_ID/SECRET
YOUTUBE_CLIENT_ID/SECRET       FACEBOOK_APP_ID/SECRET       X_API_KEY/X_API_SECRET
MASTODON_CLIENT_ID/SECRET      TIKTOK_CLIENT_ID/SECRET      THREADS_APP_ID/SECRET
TUMBLR_CLIENT_ID/SECRET        VK_ID                        KICK_CLIENT_ID/SECRET
TWITCH_CLIENT_ID/SECRET        MEDIUM_API_KEY               TELEGRAM_BOT_TOKEN
```

> **Tip:** `FRONTEND_URL` must be reachable by your browser, because platforms redirect back to it.
> For local development keep the default `http://localhost:8000`.

---

## How it works

```
browser ──► FastAPI (auth · providers · posts · media)
                    │
                    ├── SQLAlchemy ──► SQLite (users, orgs, integrations, posts, media)
                    ├── static SPA ──► app/static (vanilla JS, no build)
                    └── scheduler thread ──► 10s tick
                            ├── publish posts whose date is due
                            ├── resolve pending handshakes (check → finalize)
                            └── refresh tokens about to expire
```

Every provider is a small class implementing the same contract:

```python
class SocialAbstract:
    identifier: str
    name: str
    credentials: bool = False          # "paste your key" modal instead of OAuth
    postSettings: list[dict] = []      # per-post fields exposed in the composer

    def generate_auth_url(...) -> dict   # {url, codeVerifier, state}
    def authenticate(...) -> AuthTokenDetails
    def refresh_token(...) -> AuthTokenDetails
    def post(...) -> list[PostResponse]           # or post_pending/check/finalize
    def comment(...) -> PostResponse              # optional
    def connections(...) -> list[AuthTokenDetails]  # optional, multi-account
    def channels(...) -> list[dict]               # optional, channel picker
    def options(key, integration) -> list[dict]   # optional, per-post option lists
```

Dive deeper in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## REST API

The whole product is scriptable (n8n, Make, curl, agents…):

```bash
# 1. Register and keep the token
TOKEN=$(curl -s -X POST localhost:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"supersecret","name":"Me"}' | jq -r .token)

# 2. List the available platforms
curl -s localhost:8000/integrations/providers -H "Authorization: Bearer $TOKEN" | jq '.[].name'

# 3. Start an OAuth dance
curl -s localhost:8000/integrations/social/x -H "Authorization: Bearer $TOKEN" | jq -r .url

# 4. Schedule a post on two channels at once
curl -s -X POST localhost:8000/posts -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "posts": [
      {"integrationId": "INT_1", "content": "Hello from Postiz Py", "publishDate": "2026-10-10T09:00:00"},
      {"integrationId": "INT_2", "content": "Hello from Postiz Py", "publishDate": "2026-10-10T09:00:00"}
    ]}'
```

Full reference with every endpoint, payload and error shape: **[docs/API.md](docs/API.md)**.

---

## Testing

Tests are plain executable scripts — run them directly, no framework needed:

```bash
python tests/test_providers.py   # 55 tests · auth flows, payloads, handshakes, media rules
python tests/test_e2e.py         #  6 tests · full pipeline, token refresh, JS syntax check
```

```text
ALL PROVIDER TESTS PASSED
ALL TESTS PASSED
```

They exercise real provider classes against stubbed HTTP responses, so they are fast, offline
and deterministic — perfect as a regression net when you add a provider.
Details in **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)**.

---

## Project layout

```text
postiz-python/
├── app/
│   ├── main.py                 # app factory, SPA fallback, health check
│   ├── api/                    # auth · integrations · posts · media routers
│   ├── core/                   # settings (.env) · JWT + password hashing
│   ├── db/                     # SQLAlchemy models + engine
│   ├── integrations/
│   │   ├── base.py             # SocialAbstract, error taxonomy, HTTP client, helpers
│   │   ├── interfaces.py       # AuthTokenDetails, PostDetails, PostResponse, ...
│   │   ├── media.py            # resolve/read/dimensions/public URLs
│   │   └── social/             # ← one file per platform (19 providers)
│   ├── services/
│   │   ├── scheduler.py        # publish · pending · comment · token refresh loop
│   │   ├── integration_service.py  # connect flows + refresh in place
│   │   └── posts_service.py    # create / serialize / state transitions
│   └── static/                 # index.html · app.js · style.css (vanilla)
├── tests/
│   ├── test_providers.py       # 55 provider tests
│   └── test_e2e.py             # 6 end-to-end tests
├── docs/                       # architecture · providers · API · development
├── .env.example
└── requirements.txt
```

~7 500 lines of Python, ~700 lines of frontend, zero build configuration.

---

## Documentation

| Document | What's inside |
|---|---|
| [**ARCHITECTURE**](docs/ARCHITECTURE.md) | Layers, request lifecycles, post state machine, scheduler, token refresh, data model |
| [**PROVIDERS**](docs/PROVIDERS.md) | The provider contract, auth flows per platform, how to add a provider in 5 steps |
| [**API**](docs/API.md) | Every REST endpoint with `curl` examples, payloads, error model |
| [**DEVELOPMENT**](docs/DEVELOPMENT.md) | Setup, testing strategy, conventions, debugging, contributing |

---

## Roadmap

- [x] Core: auth, scheduling, media, comments, token refresh, pending handshakes
- [x] 19 platforms across 4 auth styles
- [x] Multi-account connections (Facebook Pages, Instagram, Tumblr blogs)
- [x] Per-post settings (channel pickers, Twitch post type)
- [ ] **WordPress** (Application Password + content blocks) — in progress
- [ ] **Dev.to** and **Hashnode** (API key publishing)
- [ ] Google Business Profile
- [ ] LinkedIn Pages, TikTok Business accounts
- [ ] Analytics endpoints and per-post insights
- [ ] Recurring posts / post series
- [ ] Docker image and Compose file

---

## Credits

This project is an independent **Python port of [Postiz](https://github.com/gitroomhq/postiz-app)**
by [Gitroom](https://postiz.com) — same concepts, same platform behaviours, rewritten from
scratch in Python with a vanilla frontend. All credit for the original design and platform
integrations belongs to the Postiz team.

Issues and pull requests are welcome.

## License

Distributed under the [Apache License 2.0](LICENSE).
