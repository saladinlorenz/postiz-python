import os
import sys
import time
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///./test_postiz.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.db.database import SessionLocal, engine, init_db  # noqa: E402
from app.db.models import Integration, Post  # noqa: E402
from app.integrations.interfaces import PostResponse  # noqa: E402
from app.integrations.manager import manager  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)


class FakeMedium:
    identifier = "medium"
    name = "Medium"
    picture = ""
    description = ""
    credentials = True
    maxLength = 100000
    published: list = []

    def custom_fields(self):
        return []

    def post(self, id, token, details, integration):
        FakeMedium.published.append(details[0].message)
        return [PostResponse(id=details[0].id, status="success", postId="medium-123", releaseURL="https://medium.com/@x/post")]


def setup():
    engine.dispose()
    if os.path.exists("test_postiz.db"):
        os.remove("test_postiz.db")
    init_db()


def test_auth_and_providers():
    res = client.post("/auth/register", json={"email": "a@b.com", "password": "password123", "name": "A"})
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.get("/integrations/providers", headers=headers)
    assert res.status_code == 200
    ids = [p["identifier"] for p in res.json()]
    assert "linkedin" in ids and "medium" in ids

    res = client.get("/integrations/social/linkedin", headers=headers)
    assert res.status_code == 200
    assert "linkedin.com/oauth" in res.json()["url"]

    res = client.post("/auth/login", json={"email": "a@b.com", "password": "wrong"})
    assert res.status_code == 401
    print("OK auth + providers + auth url")


def test_connect_and_publish():
    res = client.post("/auth/login", json={"email": "a@b.com", "password": "password123"})
    token = res.json()["token"]
    org_id = res.json()["user"]["organizationId"]
    headers = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    integration = Integration(
        organizationId=org_id,
        providerIdentifier="medium",
        internalId="user-1",
        name="Test Medium",
        token="fake-token",
        tokenExpiration=datetime.utcnow() + timedelta(days=1),
    )
    db.add(integration)
    db.commit()
    db.refresh(integration)

    original = manager.get("medium")
    manager._providers["medium"] = FakeMedium()
    try:
        res = client.post(
            "/posts",
            headers=headers,
            json={
                "posts": [
                    {
                        "integrationId": integration.id,
                        "content": "Hello from Python!",
                        "publishDate": (datetime.utcnow() - timedelta(seconds=1)).isoformat(),
                    }
                ]
            },
        )
        assert res.status_code == 200, res.text
        post_id = res.json()[0]["id"]
        assert res.json()[0]["state"] == "QUEUE"

        from app.services.scheduler import publish_post

        publish_post(post_id)

        db.expire_all()
        post = db.get(Post, post_id)
        assert post.state == "PUBLISHED", post.error
        assert post.releaseId == "medium-123"
        assert FakeMedium.published == ["Hello from Python!"]

        res = client.get("/posts", headers=headers)
        assert res.json()[0]["state"] == "PUBLISHED"
        print("OK publish pipeline (QUEUE -> PUBLISHED)")
    finally:
        manager._providers["medium"] = original
        db.close()


def test_error_state():
    res = client.post("/auth/login", json={"email": "a@b.com", "password": "password123"})
    token = res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    class FailingMedium(FakeMedium):
        def post(self, id, token, details, integration):
            from app.integrations.base import BadBody

            raise BadBody("Medium rejected the post", "bad body")

    original = manager.get("medium")
    manager._providers["medium"] = FailingMedium()
    try:
        res = client.post(
            "/posts",
            headers=headers,
            json={
                "posts": [
                    {
                        "integrationId": res.json() and _first_integration(headers),
                        "content": "fail",
                        "publishDate": datetime.utcnow().isoformat(),
                    }
                ]
            },
        )
        post_id = res.json()[0]["id"]
        from app.services.scheduler import publish_post

        publish_post(post_id)
        db = SessionLocal()
        post = db.get(Post, post_id)
        assert post.state == "ERROR"
        assert "Medium rejected the post" in post.error
        db.close()
        print("OK error classification (BadBody -> ERROR)")
    finally:
        manager._providers["medium"] = original


def _first_integration(headers) -> str:
    return client.get("/integrations/list", headers=headers).json()[0]["id"]


def test_pending_handshake():
    res = client.post("/auth/login", json={"email": "a@b.com", "password": "password123"})
    token = res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    class PendingMedium(FakeMedium):
        def post_pending(self, id, token, details, integration):
            return [
                PostResponse(
                    id=details[0].id,
                    status="pending",
                    pendingData={"authorId": id, "message": details[0].message, "attempting": False, "confirmed": False},
                )
            ]

        def check_post_status(self, token, pending_data, integration):
            from app.integrations.interfaces import PendingCheckResponse

            if pending_data.get("attempting") and not pending_data.get("confirmed"):
                return PendingCheckResponse(status="ready", pendingData={**pending_data, "confirmed": True})
            return PendingCheckResponse(status="ready", pendingData=pending_data)

        def finalize_post(self, token, pending_data, integration):
            from app.integrations.interfaces import PendingCheckResponse

            if not pending_data.get("attempting") or not pending_data.get("confirmed"):
                return PendingCheckResponse(status="pending", pendingData={**pending_data, "attempting": True, "confirmed": False})
            return PendingCheckResponse(status="completed", postId="pending-42", releaseURL="https://example.com/42")

    original = manager.get("medium")
    manager._providers["medium"] = PendingMedium()
    try:
        res = client.post(
            "/posts",
            headers=headers,
            json={
                "posts": [
                    {
                        "integrationId": _first_integration(headers),
                        "content": "pending post",
                        "publishDate": datetime.utcnow().isoformat(),
                    }
                ]
            },
        )
        post_id = res.json()[0]["id"]
        from app.services.scheduler import publish_post, resolve_pending

        publish_post(post_id)
        db = SessionLocal()
        post = db.get(Post, post_id)
        assert post.pendingData, "expected pendingData after postPending"
        assert post.state == "QUEUE"

        for _ in range(4):
            resolve_pending(post_id)
            db.expire_all()
            post = db.get(Post, post_id)
            if post.state == "PUBLISHED":
                break

        assert post.state == "PUBLISHED", f"state={post.state} error={post.error}"
        assert post.releaseId == "pending-42"
        assert post.pendingData is None
        db.close()
        print("OK pending handshake (postPending -> check -> finalize -> PUBLISHED)")
    finally:
        manager._providers["medium"] = original


def test_refresh_token_on_expiry():
    res = client.post("/auth/login", json={"email": "a@b.com", "password": "password123"})
    token = res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    from app.db.models import Integration as IntegrationModel
    from app.integrations.interfaces import AuthTokenDetails

    db = SessionLocal()
    integration = db.query(IntegrationModel).first()
    integration.refreshToken = "old-refresh"
    integration.tokenExpiration = datetime.utcnow() - timedelta(minutes=5)
    db.commit()
    integration_id = integration.id
    db.close()

    calls = []

    class RefreshingMedium(FakeMedium):
        def refresh_token(self, refresh_token):
            calls.append(refresh_token)
            return AuthTokenDetails(
                id="user-1", name="Test Medium", accessToken="new-token", refreshToken="new-refresh", expiresIn=3600
            )

    original = manager.get("medium")
    manager._providers["medium"] = RefreshingMedium()
    try:
        from app.services.integration_service import refresh_integration

        db = SessionLocal()
        integration = db.get(IntegrationModel, integration_id)
        assert refresh_integration(db, integration) is True
        db.expire_all()
        integration = db.get(IntegrationModel, integration_id)
        assert integration.token == "new-token"
        assert integration.refreshToken == "new-refresh"
        assert not integration.refreshNeeded
        db.close()
        assert calls == ["old-refresh"]
        print("OK token refresh (expired -> refresh_token -> new token persisted)")
    finally:
        manager._providers["medium"] = original


def test_frontend_syntax():
    try:
        import quickjs
    except ImportError:
        print("SKIP frontend syntax (quickjs not installed)")
        return

    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    for path in sorted(static.glob("*.js")):
        context = quickjs.Context()
        try:
            context.eval(path.read_text(encoding="utf-8"))
        except quickjs.JSException as exc:
            assert "SyntaxError" not in str(exc), f"{path.name}: {exc}"
    print("OK frontend javascript syntax")


if __name__ == "__main__":
    setup()
    test_auth_and_providers()
    test_connect_and_publish()
    test_error_state()
    test_pending_handshake()
    test_refresh_token_on_expiry()
    test_frontend_syntax()
    print("\nALL TESTS PASSED")
