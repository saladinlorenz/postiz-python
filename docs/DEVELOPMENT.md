# Development

Everything in this repository can be understood and modified with a text editor and a Python
interpreter — there is no toolchain to install, no bundler to configure and no container
required to start hacking.

---

## 1. Prerequisites

- **Python 3.11+** (developed and tested on 3.12)
- `pip` (and optionally `virtualenv`)
- A browser
- Optional: [`quickjs`](https://pypi.org/project/QuickJS/) to run the frontend syntax test

---

## 2. Local setup

```bash
git clone https://github.com/saladinlorenz/postiz-python.git
cd postiz-python

python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env                 # then edit it
```

`.env` is optional for a smoke test (sensible defaults exist), but you need the credentials for
the platforms you want to connect. See the
[configuration table](../README.md#configuration).

---

## 3. Running

```bash
uvicorn app.main:app --port 8000 --reload
```

- UI: <http://localhost:8000>
- Swagger: <http://localhost:8000/docs>
- Health: <http://localhost:8000/api/health>

On boot the app creates `postiz.db`, the `uploads/` folder and starts the scheduler thread.
Logs go to stdout (`logging.basicConfig(level=logging.INFO)` in `app/main.py`).

**Reset everything** (dev only): stop the server and delete `postiz.db` (+ `uploads/` if you
want a clean media library). There are no migrations — `Base.metadata.create_all()` runs at
startup, so schema changes mean dropping the file (or adding Alembic yourself for production).

---

## 4. Testing

Tests are **plain executable scripts**. No pytest, no fixtures, no mocking library:

```bash
python tests/test_providers.py   # 55 provider tests → "ALL PROVIDER TESTS PASSED"
python tests/test_e2e.py         #  6 end-to-end tests → "ALL TESTS PASSED"
```

Redirect output when you want a file to inspect:

```bash
python tests/test_providers.py > tests/out_providers.txt 2>&1
```

(`tests/out_*.txt` is git-ignored.)

### How `test_providers.py` is organised

```text
imports  +  provider instances (bluesky = BlueskyProvider(), …)
FakeResponse            # minimal httpx.Response stand-in
test_discord_error_codes()   → prints "OK …"
test_x_oauth1_signature()
…
if __name__ == "__main__":
    test_discord_error_codes()
    …
    print("\nALL PROVIDER TESTS PASSED")
```

Conventions:

1. One `def test_<provider>_<aspect>()` per behaviour.
2. Stub the network by **patching the instance**, always inside `try/finally`:

   ```python
   original = provider.get_json
   provider.get_json = fake_get_json
   try:
       ...
   finally:
       provider.get_json = original
   ```

3. Patch module-level helpers through the module object
   (`bluesky_module.read_or_fetch = ...`) — never through the instance.
4. Assert on **payloads** (what would be sent to the platform), not on internals.
5. Always `print("OK …")` at the end: the suite's success is detected by the final banner.
6. Never touch real credentials — tests must pass offline.

### How `test_e2e.py` is organised

It boots the real FastAPI app with `fastapi.testclient.TestClient` against a dedicated database
(`DATABASE_URL=sqlite:///./test_postiz.db`, deleted at the start of each run) and swaps fake
providers into the manager:

```python
original = manager.get("medium")
manager._providers["medium"] = FakeMedium()
try:
    ...
finally:
    manager._providers["medium"] = original
```

The six scenarios cover: registration + provider catalogue + auth URL, the publish pipeline
(`QUEUE → PUBLISHED`), error classification (`BadBody → ERROR`), the three-phase pending
handshake, token refresh on expiry, and a **syntax check of `app/static/app.js`** executed in a
`quickjs` context (the test is skipped with a warning if `quickjs` is missing).

---

## 5. Code conventions

- **No code comments.** The code is written to be self-explanatory; documentation lives in
  `README.md` and `docs/`.
- Type hints everywhere on public functions; dataclasses for payloads
  (`app/integrations/interfaces.py`).
- Exceptions only from `app/integrations/base.py`
  (`BadBody`, `RefreshTokenError`, `Disconnect`, `NotEnoughScopes`, `BaseIntegrationError`).
- Secrets always read from `app.core.config.settings`, never `os.environ` directly.
- HTTP access always through `provider.fetch()` / `get_json()` so retries and error mapping apply.
- Strings returned to users are actionable ("…reconnect your account"), never raw dumps of tokens.
- Frontend: template literals + `esc()` for every dynamic value, no `innerHTML` with raw input.

---

## 6. Where to change what

| Goal | File(s) |
|---|---|
| New platform | `app/integrations/social/<id>.py` + register in `social/__init__.py` → [PROVIDERS.md §5](PROVIDERS.md#5-adding-a-provider-in-5-steps) |
| New OAuth app / credential | `app/core/config.py` + `.env.example` |
| Per-post fields (title, tags, cover…) | `postSettings` on the provider + rendering in `renderChannelPickers()` (`app/static/app.js`) |
| Scheduler behaviour (intervals, retries) | `app/services/scheduler.py`, knobs in `app/core/config.py` |
| Post state transitions | `app/services/posts_service.py` |
| Connect / refresh flows | `app/services/integration_service.py` |
| REST endpoint | `app/api/*.py` |
| Composer / calendar / accounts UI | `app/static/app.js`, styles in `app/static/style.css` |
| Media limits (types, size) | `app/api/media.py`, `app/integrations/media.py` |
| Provider payload rules | the provider module itself |

---

## 7. Debugging checklist

1. **A post stays in `QUEUE`** — check `pendingData` (async upload in flight) and the scheduler
   logs; pending posts are polled every `PENDING_CHECK_INTERVAL_SECONDS`.
2. **A post is in `ERROR`** — read `Post.error`; the first 2 000 characters of the provider
   message are stored there.
3. **"reconnect required"** — `Integration.refreshNeeded = True`: the refresh token was rejected.
   Re-connect from the Accounts view (it reuses the same state machinery with `?refresh=1`).
4. **OAuth redirect fails** — `FRONTEND_URL` must be the URL you browse to (default
   `http://localhost:8000`). The SPA fallback deliberately serves `index.html` for
   `/integrations/social/<provider>` so the callback can run in the browser.
5. **Media 404 for platforms** — absolute URLs are built from `BACKEND_URL`; make sure it is
   reachable by the platform (it must be public for Facebook/Instagram/TikTok/YouTube).
6. **Inspect the database**:

   ```bash
   python -c "import sqlite3;c=sqlite3.connect('postiz.db');print(c.execute('select state,error,releaseURL from post').fetchall())"
   ```

---

## 8. Contributing

1. Fork and create a focused branch (`provider-twitch`, `fix-pending-timeout`, …).
2. Keep the parity mindset: check how upstream Postiz
   ([gitroomhq/postiz-app](https://github.com/gitroomhq/postiz-app)) solves the same problem and
   reuse its semantics — payload shapes, status names, error messages.
3. Add or extend tests (see §4). Both suites must end with their success banner.
4. Update the [README platform table](../README.md#supported-platforms) and, if the behaviour is
   new, `docs/PROVIDERS.md` / `docs/ARCHITECTURE.md`.
5. Keep the diff small and readable; no unrelated reformatting.

---

## 9. Repository hygiene

`.gitignore` already excludes `.venv/`, `__pycache__/`, `*.db`, `uploads/`, `server_*.txt`,
`tests/out_*.txt` and `.env`. **Never commit `.env`** — it contains client secrets for every
connected platform.
