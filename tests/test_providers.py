import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

from PIL import Image

os.environ["DATABASE_URL"] = "sqlite:///./test_postiz.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.integrations.base import BadBody, NotEnoughScopes, RefreshTokenError  # noqa: E402
from app.integrations.interfaces import AuthTokenDetails, MediaContent, PostDetails  # noqa: E402
from app.integrations.social import bluesky as bluesky_module  # noqa: E402
from app.integrations.social.bluesky import BlueskyProvider  # noqa: E402
from app.integrations.social.discord import DiscordProvider  # noqa: E402
from app.integrations.social.facebook import FacebookProvider, _is_preset_rejection  # noqa: E402
from app.integrations.social.instagram import InstagramProvider  # noqa: E402
from app.integrations.social.kick import KickProvider  # noqa: E402
from app.integrations.social.mastodon import MastodonProvider  # noqa: E402
from app.integrations.social.pinterest import PinterestProvider  # noqa: E402
from app.integrations.social.reddit import RedditProvider  # noqa: E402
from app.integrations.social.slack import SlackProvider  # noqa: E402
from app.integrations.social.telegram import TelegramProvider  # noqa: E402
from app.integrations.social.tumblr import TumblrProvider  # noqa: E402
from app.integrations.social.threads import ThreadsProvider  # noqa: E402
from app.integrations.social.vk import VkProvider  # noqa: E402
from app.integrations.social import vk as vk_module  # noqa: E402
from app.integrations.social.tiktok import TiktokProvider  # noqa: E402
from app.integrations.social.twitch import TwitchProvider  # noqa: E402
from app.integrations.social import twitch as twitch_module  # noqa: E402
from app.integrations.social.x import XProvider, html_to_text, strip_links_text  # noqa: E402
from app.integrations.social.youtube import YoutubeProvider  # noqa: E402

bluesky = BlueskyProvider()
discord = DiscordProvider()
telegram = TelegramProvider()
reddit = RedditProvider()
slack = SlackProvider()
mastodon = MastodonProvider()
pinterest = PinterestProvider()
youtube = YoutubeProvider()
x = XProvider()
facebook = FacebookProvider()
instagram = InstagramProvider()
tiktok = TiktokProvider()
threads = ThreadsProvider()
tumblr = TumblrProvider()
vk = VkProvider()
kick = KickProvider()
twitch = TwitchProvider()


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, raw=None):
        self._payload = payload
        self._raw = raw
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload

    @property
    def text(self):
        if self._raw is not None:
            return self._raw

        import json

        return json.dumps(self._payload)


def test_discord_error_codes():
    assert discord.handle_errors("Missing Access 50001", 403) == ("bad-body", "Bot doesn't have access to this channel")
    assert discord.handle_errors("Missing Permissions 50013", 403) == ("bad-body", "Bot lacks permission to send messages in this channel")
    assert discord.handle_errors("Unknown Channel 10003", 404) == ("bad-body", "Channel no longer exists")
    assert discord.handle_errors("Request entity too large 40005", 413) == ("bad-body", "Attachment exceeds Discord's size limit")
    assert discord.handle_errors("rate limited 20028", 429) == ("retry", "Rate limited by Discord")
    kind, _ = discord.handle_errors("generic", 401)
    assert kind == "refresh-token"
    print("OK discord error classification")


def test_telegram_text_conversion():
    assert "<b>hi</b>" in telegram._convert_text("<strong>hi</strong>")
    assert "<script>alert(1)</script>" not in telegram._convert_text("<script>alert(1)</script>hi")
    assert telegram._convert_text("<p>line</p>") == "line\n"
    assert "<u>u</u>" in telegram._convert_text("<u>u</u>")
    print("OK telegram text conversion (strong->b, script stripped)")


def test_telegram_media_kind():
    assert telegram._media_kind("photo.png") == "photo"
    assert telegram._media_kind("clip.mp4") == "video"
    assert telegram._media_kind("doc.pdf") == "document"
    print("OK telegram media kind detection")


def test_telegram_api_error_mapping():
    calls = []

    def fake_fetch(url, method="GET", **kwargs):
        calls.append(url)
        if "sendMessage" in url:
            return FakeResponse({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
        if "getMe" in url:
            return FakeResponse({"ok": True, "result": {"id": 1, "username": "mybot"}})
        return FakeResponse({"ok": True, "result": {}})

    telegram.fetch = fake_fetch
    try:
        try:
            telegram._call("TOKEN", "sendMessage", {"chat_id": "1", "text": "x"})
            raise AssertionError("should have raised")
        except BadBody as exc:
            assert "chat not found" in exc.message

        try:
            telegram._call("TOKEN", "sendMessage", {"chat_id": "1", "text": "x"})
        except BadBody as exc:
            assert exc.message.startswith("Telegram rejected the post")

        bot = telegram._call("TOKEN", "getMe")
        assert bot["username"] == "mybot"
        print("OK telegram API error mapping (400 -> BadBody)")
    finally:
        del telegram.fetch


def test_telegram_release_url():
    assert telegram._release_url("mychannel", "-100123", 5) == "https://t.me/mychannel/5"
    assert telegram._release_url("undefined", "-100123456", 9) == "https://t.me/c/123456/9"
    print("OK telegram release url (public + private chat)")


def test_discord_content_conversion():
    import re

    result = re.sub(r"\[\[\[(@.*?)]]]", r"<\1>", "hello [[[@user]]] bye")
    assert result == "hello <@user> bye"
    assert re.sub(r"\[\[\[(@.*?)]]]", r"<\1>", "no mentions here") == "no mentions here"
    print("OK discord mention conversion")


def test_reddit_auth_url_and_subreddit():
    data = reddit.generate_auth_url()
    assert "reddit.com/api/v1/authorize" in data["url"]
    assert "redirect_uri=" in data["url"]
    assert "state=" in data["url"]
    assert reddit._subreddit("r/Python/") == "python"
    assert reddit._subreddit("/r/AskReddit") == "askreddit"
    assert reddit.handle_errors("RATELIMIT exceeded", 403) == ("retry", "Reddit rate limited the submission")
    print("OK reddit auth url + subreddit normalization + rate limit")


def test_reddit_pending_handshake():
    import time

    pending = {
        "subreddits": [{"value": {"subreddit": "r/Python", "title": "Hello", "type": "self"}}],
        "message": "body",
        "cursor": 0,
        "results": [],
    }
    check = reddit.check_post_status("token", pending, None)
    assert check.status == "ready"
    assert check.pendingData["armed"]["sr"] == "python"

    reddit._submit = lambda *a, **k: {"postId": "abc123", "releaseURL": "https://www.reddit.com/r/python/1"}
    try:
        final = reddit.finalize_post("token", check.pendingData, None)
        assert final.status == "completed"
        assert final.postId == "abc123"
        assert final.releaseURL.endswith("/1")

        armed = {
            **pending,
            "armed": {
                "sr": "python",
                "title": "Hello",
                "armedAt": time.time() * 1000,
                "media": False,
                "submitted": True,
                "lookups": 1,
            },
        }

        class Integration:
            additionalSettings = '{"username": "bob"}'

        reddit._find_submitted = lambda *a, **k: {"id": "xyz", "url": "https://www.reddit.com/r/python/2"}
        found = reddit.check_post_status("token", armed, Integration())
        assert found.status == "completed"
        assert found.postId == "xyz"
    finally:
        del reddit._submit
        del reddit._find_submitted
    print("OK reddit pending handshake (arm -> submit -> lookup -> completed)")


def test_slack_oauth_blocks_errors():
    data = slack.generate_auth_url()
    assert "slack.com/oauth/v2/authorize" in data["url"]
    assert "chat:write" in data["url"]
    assert "redirectmeto.com" in slack._redirect_uri()

    blocks = slack._blocks(PostDetails(id="1", message="hello"))
    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["text"] == "hello"

    try:
        slack._check_api_error({"ok": False, "error": "ratelimited"})
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "ratelimited" in exc.message

    try:
        slack._check_api_error({"ok": False, "error": "token_revoked"})
        raise AssertionError("expected RefreshTokenError")
    except RefreshTokenError:
        pass
    print("OK slack oauth url + block kit + error mapping")


def test_mastodon_instance_and_pending():
    data = mastodon.generate_auth_url(instance="https://m.example.org/")
    assert data["url"].startswith("https://m.example.org/oauth/authorize")
    assert "client_id=" in data["url"]

    pending = {
        "url": "https://m.example.org",
        "message": "hi",
        "mediaIds": ["m1"],
        "idempotencyKey": "k-1",
    }

    mastodon._media_status = lambda instance, media_id, token: 206
    assert mastodon.check_post_status("t", pending, None).status == "pending"

    mastodon._media_status = lambda instance, media_id, token: 200
    assert mastodon.check_post_status("t", pending, None).status == "ready"

    calls = {}

    def fake_fetch(url, method="GET", **kwargs):
        calls["url"] = url
        calls["headers"] = kwargs.get("headers")
        calls["data"] = kwargs.get("data")
        return FakeResponse({"id": "99"})

    mastodon.fetch = fake_fetch
    try:
        done = mastodon.finalize_post("t", pending, None)
        assert done.status == "completed"
        assert done.releaseURL == "https://m.example.org/statuses/99"
        assert calls["url"].endswith("/api/v1/statuses")
        assert calls["data"]["status"] == "hi"
        assert calls["headers"]["Idempotency-Key"] == "k-1"
    finally:
        del mastodon.fetch
    print("OK mastodon instance auth url + media readiness + finalize idempotency")


def test_pinterest_auth_url_and_errors():
    data = pinterest.generate_auth_url()
    assert "pinterest.com/oauth/" in data["url"]
    assert "pins:write" in data["url"]
    assert pinterest.handle_errors("Board not found", 400)[1].startswith("The specified board")
    assert "maximum of 5 images" in pinterest.handle_errors("constraint: maxItems=5", 400)[1]
    assert "cover image" in pinterest.handle_errors("cover_image_url or cover_image_content_type", 400)[1]
    kind, _ = pinterest.handle_errors("generic failure", 401)
    assert kind == "refresh-token"
    print("OK pinterest auth url + error mapping")


def test_pinterest_pending_handshake():
    image = PostDetails(
        id="p1",
        message="my pin",
        media=[MediaContent(type="image", path="https://cdn.example.com/pin.png")],
        settings={"channel": "12345", "title": "Pin title"},
    )
    pending = pinterest.post_pending("p1", "token", [image], None)[0]
    assert pending.status == "pending"
    assert pending.pendingData["mediaId"] == ""
    assert pending.pendingData["settings"]["board"] == "12345"

    armed = pinterest.finalize_post("token", pending.pendingData, None)
    assert armed.status == "pending"
    assert armed.pendingData["attempting"] is True

    witnessed = pinterest.check_post_status("token", armed.pendingData, None)
    assert witnessed.status == "ready"
    assert witnessed.pendingData["confirmed"] is True

    pinterest.fetch = lambda url, **kwargs: FakeResponse({"id": "pin42"})
    try:
        done = pinterest.finalize_post("token", witnessed.pendingData, None)
        assert done.status == "completed"
        assert done.releaseURL == "https://www.pinterest.com/pin/pin42"
    finally:
        del pinterest.fetch

    duplicate = {**witnessed.pendingData, "attempting": True, "confirmed": True}
    try:
        pinterest.check_post_status("token", duplicate, None)
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "avoid duplicates" in exc.message
    print("OK pinterest arm -> witness -> publish handshake + duplicate guard")


def test_pinterest_media_rules():
    try:
        pinterest.post_pending("p1", "token", [PostDetails(id="p1", message="x", media=[])], None)
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "at least one media" in exc.message

    video_only = PostDetails(
        id="p1",
        message="x",
        media=[MediaContent(type="video", path="https://cdn.example.com/clip.mp4")],
    )
    try:
        pinterest.post_pending("p1", "token", [video_only], None)
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "cover image" in exc.message
    print("OK pinterest media rules (min one, video needs cover)")


def test_youtube_auth_url_and_errors():
    data = youtube.generate_auth_url()
    assert "accounts.google.com/o/oauth2/v2/auth" in data["url"]
    assert "access_type=offline" in data["url"]
    assert "youtube.upload" in data["url"]
    assert youtube.handle_errors("invalidTitle", 400)[1].startswith("We have uploaded your video")
    assert "daily upload limit" in youtube.handle_errors("uploadLimitExceeded", 400)[1]
    assert youtube.handle_errors("invalid_grant", 400)[0] == "refresh-token"
    assert youtube.handle_errors("generic", 401)[0] == "refresh-token"
    print("OK youtube auth url + error mapping")


def test_youtube_resumable_flow():
    video = PostDetails(
        id="y1",
        message="hello world",
        media=[MediaContent(type="video", path="https://cdn.example.com/clip.mp4")],
        settings={"title": "My video", "type": "public"},
    )

    youtube._media_size = lambda path: 1024
    youtube.fetch = lambda url, **kwargs: FakeResponse(
        {}, headers={"location": "https://upload.example.com/session1"}
    )
    try:
        started = youtube.post_pending("y1", "token", [video], None)[0]
        assert started.status == "pending"
        assert started.pendingData["uploadUri"] == "https://upload.example.com/session1"
        assert started.pendingData["videoSize"] == 1024

        youtube._probe = lambda access_token, uri, size: {"uploadedBytes": 0}
        ready = youtube.check_post_status("token", started.pendingData, None)
        assert ready.status == "ready"
        assert ready.pendingData["uploadedBytes"] == 0

        youtube._probe = lambda access_token, uri, size: {"videoId": "vid9"}
        completed = youtube.check_post_status("token", ready.pendingData, None)
        assert completed.status == "completed"
        assert completed.releaseURL == "https://www.youtube.com/watch?v=vid9"

        finalized = youtube.finalize_post("token", {**ready.pendingData, "videoId": "vid9"}, None)
        assert finalized.status == "completed"
        assert finalized.postId == "vid9"
    finally:
        del youtube.fetch
        del youtube._media_size
        del youtube._probe

    image = PostDetails(id="y1", message="x", media=[MediaContent(type="image", path="https://cdn.example.com/a.png")])
    try:
        youtube.post_pending("y1", "token", [image], None)
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "must be a video" in exc.message
    print("OK youtube resumable session (start -> probe -> complete)")


def test_facebook_auth_and_errors():
    data = facebook.generate_auth_url()
    assert "facebook.com/v25.0/dialog/oauth" in data["url"]
    assert "pages_manage_posts" in data["url"]
    assert facebook.handle_errors("Error validating access token", 400)[0] == "refresh-token"
    assert "missing permissions" in facebook.handle_errors("(#200) Permission denied", 400)[1]
    assert "4 MB" in facebook.handle_errors("1366046", 400)[1]
    assert "security check" in facebook.handle_errors('{"error_subcode":459}', 400)[1]
    assert facebook.handle_errors("anything", 401)[0] == "bad-body"

    assert not _is_preset_rejection('{"error":{"message":"Error validating access token"}}')
    assert _is_preset_rejection('unknown parameter text_format_preset_id')
    assert _is_preset_rejection("Unknown Error")
    print("OK facebook auth url + error mapping + preset rejection")


def test_facebook_pages_connections():
    details = AuthTokenDetails(
        id="user1", name="User", accessToken="usertoken", additionalSettings={"userToken": "usertoken"}
    )
    facebook._pages = lambda token: [
        {"id": "111", "name": "My Page", "access_token": "pagetoken", "username": "mypage"}
    ]
    try:
        pages = facebook.connections(details)
    finally:
        del facebook._pages
    assert len(pages) == 1
    assert pages[0].id == "111"
    assert pages[0].accessToken == "pagetoken"
    assert pages[0].additionalSettings["userToken"] == "usertoken"
    print("OK facebook pages -> one connection per page")


def test_facebook_story_handshake():
    post = PostDetails(
        id="f1",
        message="story time",
        media=[MediaContent(type="image", path="pic.png")],
        settings={"post_type": "story"},
    )

    class Integration:
        internalId = "111"

    facebook.get_json = lambda url, **kwargs: {"id": "photo1"} if url.endswith("/photos") else {"post_id": "story9"}
    try:
        started = facebook.post_pending("111", "pagetoken", [post], Integration())[0]
        assert started.status == "pending"
        assert started.pendingData["items"] == [{"kind": "photo", "mediaId": "photo1"}]

        ready = facebook.check_post_status("pagetoken", started.pendingData, Integration())
        assert ready.status == "ready"

        armed = facebook.finalize_post("pagetoken", ready.pendingData, Integration())
        assert armed.status == "pending"
        assert armed.pendingData["attempting"] == 0

        witnessed = facebook.check_post_status("pagetoken", armed.pendingData, Integration())
        assert witnessed.status == "ready"
        assert witnessed.pendingData["confirmed"] is True

        done = facebook.finalize_post("pagetoken", witnessed.pendingData, Integration())
        assert done.status == "completed"
        assert done.releaseURL == "https://www.facebook.com/stories/story9"

        try:
            facebook.check_post_status("pagetoken", {**witnessed.pendingData, "confirmed": True}, Integration())
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "avoid duplicates" in exc.message
    finally:
        del facebook.get_json
    print("OK facebook story arm -> witness -> publish + duplicate guard")


def test_facebook_feed_post():
    post = PostDetails(id="f1", message="hello", media=[MediaContent(type="image", path="pic.png")])

    def fake_get_json(url, **kwargs):
        if url.endswith("/photos"):
            return {"id": "photo1"}
        if url.endswith("/feed"):
            return {"id": "111_222", "permalink_url": "https://www.facebook.com/111_222/posts/222"}
        raise AssertionError(f"unexpected {url}")

    facebook.get_json = fake_get_json
    try:
        result = facebook.post_pending("111", "pagetoken", [post], None)[0]
        assert result.status == "success"
        assert result.postId == "111_222"
        assert result.releaseURL.endswith("/posts/222")
    finally:
        del facebook.get_json
    print("OK facebook feed post (photo upload -> feed publish)")


def test_instagram_auth_and_errors():
    data = instagram.generate_auth_url()
    assert "facebook.com/v25.0/dialog/oauth" in data["url"]
    assert "instagram_content_publish" in data["url"]
    assert instagram.handle_errors("2207003", 400)[1] == "Timeout downloading media, please try again"
    assert instagram.handle_errors("REVOKED_ACCESS_TOKEN", 400)[0] == "refresh-token"
    assert "business account" in instagram.handle_errors("the user is not an Instagram Business", 400)[1]
    assert instagram.handle_errors("Session key is malformed", 400)[0] == "disconnect"
    assert instagram._tokens("page___user") == ("page", "user")
    assert instagram._tokens("only") == ("only", "")
    print("OK instagram auth url + error mapping + token split")


def test_instagram_carousel_flow():
    post = PostDetails(
        id="i1",
        message="carousel",
        media=[
            MediaContent(type="image", path="one.png"),
            MediaContent(type="image", path="two.png"),
        ],
    )

    class Integration:
        internalId = "ig1"
        additionalSettings = '{"profile": "myhandle"}'

    counter = {"n": 0}

    def fake_get_json(url, **kwargs):
        params = kwargs.get("params") or {}
        fields = params.get("fields", "")
        if fields == "status_code,status":
            return {"status_code": "READY"}
        if fields == "permalink":
            return {"permalink": "https://www.instagram.com/p/xyz"}
        if url.endswith("/media"):
            if params.get("media_type") == "CAROUSEL":
                return {"id": "carousel1"}
            counter["n"] += 1
            return {"id": f"child{counter['n']}"}
        if url.endswith("/media_publish"):
            return {"id": "published1"}
        raise AssertionError(f"unexpected {url}")

    instagram.get_json = fake_get_json
    try:
        started = instagram.post_pending("ig1", "page___user", [post], Integration())[0]
        assert started.status == "pending"
        assert started.pendingData["postType"] == "carousel"
        assert started.pendingData["containers"] == ["child1", "child2"]

        ready = instagram.check_post_status("page___user", started.pendingData, Integration())
        assert ready.status == "ready"

        armed = instagram.finalize_post("page___user", ready.pendingData, Integration())
        assert armed.status == "pending"
        assert armed.pendingData["carouselId"] == "carousel1"

        ready2 = instagram.check_post_status("page___user", armed.pendingData, Integration())
        assert ready2.status == "ready"

        done = instagram.finalize_post("page___user", ready2.pendingData, Integration())
        assert done.status == "completed"
        assert done.postId == "published1"
        assert done.releaseURL == "https://www.instagram.com/p/xyz"
    finally:
        del instagram.get_json

    empty = PostDetails(id="i1", message="x", media=[])
    try:
        instagram.post_pending("ig1", "page___user", [empty], Integration())
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "at least one media" in exc.message
    print("OK instagram carousel (children -> container -> publish)")


def test_instagram_connections():
    details = AuthTokenDetails(
        id="user1", name="User", accessToken="usertoken", additionalSettings={"userToken": "usertoken"}
    )
    instagram._pages = lambda token: [
        {"id": "page1", "access_token": "pagetoken", "instagram_business_account": {"id": "ig9"}}
    ]
    instagram.get_json = lambda url, **kwargs: {
        "username": "myhandle",
        "name": "My IG",
        "profile_picture_url": "https://cdn/ig.jpg",
    }
    try:
        accounts = instagram.connections(details)
    finally:
        del instagram._pages
        del instagram.get_json
    assert len(accounts) == 1
    assert accounts[0].id == "ig9"
    assert accounts[0].accessToken == "pagetoken___usertoken"
    assert accounts[0].username == "myhandle"
    assert accounts[0].additionalSettings["profile"] == "myhandle"
    print("OK instagram connections (page with IG business account)")


def test_x_oauth1_signature():
    import time as time_module

    from app.core.config import settings
    from app.integrations.social import x as x_module

    previous = (settings.x_api_key, settings.x_api_secret, x_module.secrets.token_hex, time_module.time)
    try:
        settings.x_api_key = "xvz1evFS4wEEPTGEFPHBog"
        settings.x_api_secret = "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw"
        x_module.secrets.token_hex = lambda size=16: "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg"
        time_module.time = lambda: 1318622958
        header = x_module.oauth_header(
            "POST",
            "https://api.twitter.com/1.1/statuses/update.json?include_entities=true",
            "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
            "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
            body_params={"status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
        )
    finally:
        settings.x_api_key, settings.x_api_secret = previous[0], previous[1]
        x_module.secrets.token_hex = previous[2]
        time_module.time = previous[3]

    signature = unquote(header.split('oauth_signature="')[1].split('"')[0])
    assert signature == "hCtSmYh+iHYCEqBWrE7C7hYmtUk="
    assert header.startswith("OAuth ")
    print("OK x oauth1 hmac-sha1 signature (Twitter docs vector)")


def test_x_auth_flow():
    calls = {}

    def fake_request(method, url, token="", token_secret="", **kwargs):
        calls[method + url] = kwargs
        if url.endswith("/oauth/request_token"):
            return {"oauth_token": "reqtok", "oauth_token_secret": "reqsec"}
        if url.endswith("/oauth/access_token"):
            return {
                "oauth_token": "acctok",
                "oauth_token_secret": "accsec",
                "user_id": "42",
                "screen_name": "handle",
            }
        return {"data": {"id": "42", "name": "Name", "username": "handle", "verified": True, "profile_image_url": "https://cdn/p.jpg"}}

    original = x._request
    x._request = fake_request
    try:
        auth = x.generate_auth_url()
        details = x.authenticate(code="verifier", code_verifier="reqtok:reqsec")
    finally:
        x._request = original

    assert "oauth/authenticate?oauth_token=reqtok" in auth["url"]
    assert auth["codeVerifier"] == "reqtok:reqsec"
    assert auth["state"] == "reqtok"
    assert details.accessToken == "acctok:accsec"
    assert details.username == "handle"
    assert details.additionalSettings["verified"] is True
    assert calls["POST" + "https://api.x.com/1.1/oauth/request_token"]["data"] == {"x_auth_access_type": "write"}
    assert x._split(details.accessToken) == ("acctok", "accsec")
    print("OK x oauth1 request token -> access token -> profile")


def test_x_errors_and_text():
    assert x.handle_errors("Too Many Requests", 429) == ("retry", "X rate limit reached, please try again later")
    assert x.handle_errors("Service Unavailable", 503)[0] == "retry"
    assert x.handle_errors("Unsupported Authentication", 401)[0] == "refresh-token"
    assert x.handle_errors("duplicate-rules", 400)[0] == "bad-body"
    assert x.handle_errors("Your media IDs are invalid", 400)[1].startswith("X rejected the attached media")
    assert x.handle_errors("something else", 401)[0] == "refresh-token"

    assert html_to_text("<p>Hello <strong>world</strong></p><p>next</p>") == "Hello world\n\nnext"
    assert html_to_text("plain text") == "plain text"
    assert strip_links_text("read https://example.com/post now") == "read now"
    assert strip_links_text("go to postiz.com today") == "go to today"
    print("OK x error mapping + html/text helpers")


def test_x_pending_handshake():
    post = PostDetails(
        id="p1",
        message="<p>hello</p>",
        media=[MediaContent(type="image", path="pic.png")],
        settings={"who_can_reply_post": "following", "community": "MySpace/1234", "made_with_ai": "true"},
    )

    class Integration:
        username = "handle"

    x._upload_entries = lambda token, secret, details, as_article_image=False: ({"p1": ["m1"]}, [])
    captured = {}

    def fake_request(method, url, token="", token_secret="", **kwargs):
        captured[url] = kwargs.get("json_body")
        return {"data": {"id": "999"}}

    original = x._request
    try:
        started = x.post_pending("42", "tok:sec", [post], Integration())[0]
        assert started.status == "pending"
        assert started.pendingData["mediaIds"] == ["m1"]
        assert started.pendingData["message"] == "hello"

        ready = x.check_post_status("tok:sec", started.pendingData, Integration())
        assert ready.status == "ready"

        armed = x.finalize_post("tok:sec", ready.pendingData, Integration())
        assert armed.status == "pending"
        assert armed.pendingData["attempting"] is True

        witnessed = x.check_post_status("tok:sec", armed.pendingData, Integration())
        assert witnessed.status == "ready"
        assert witnessed.pendingData["confirmed"] is True

        x._request = fake_request
        done = x.finalize_post("tok:sec", witnessed.pendingData, Integration())
        assert done.status == "completed"
        assert done.postId == "999"
        assert done.releaseURL == "https://twitter.com/handle/status/999"

        body = next(iter(captured.values()))
        assert body["reply_settings"] == "following"
        assert body["community_id"] == "1234"
        assert body["media"] == {"media_ids": ["m1"]}
        assert body["made_with_ai"] is True
        assert body["paid_partnership"] is False

        try:
            x.check_post_status("tok:sec", {**witnessed.pendingData, "confirmed": True}, Integration())
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "avoid duplicates" in exc.message
    finally:
        x._request = original
        del x._upload_entries
    print("OK x arm -> witness -> publish handshake + tweet payload")


def test_x_media_upload_and_status():
    import app.integrations.social.x as x_module

    uploaded = {}

    def fake_request(method, url, token="", token_secret="", **kwargs):
        if url.endswith("/media/upload.json"):
            uploaded["files"] = kwargs.get("files")
            uploaded["data"] = kwargs.get("data")
            return {"media_id_string": "img1"}
        if url.endswith("/initialize"):
            return {"data": {"id": "vid1"}}
        if url.endswith("/finalize"):
            return {"data": {"id": "vid1", "processing_info": {"state": "in_progress", "check_after_secs": 1}}}
        if "command=STATUS" in url:
            uploaded["status"] = url
            return {"data": {"processing_info": {"state": "succeeded"}}}
        if url.endswith("/append"):
            uploaded["append"] = kwargs
            return {}
        raise AssertionError(f"unexpected {url}")

    original = x._request
    original_size = x_module.media_size
    original_chunk = x_module.media_chunk
    original_prepare = x._prepare_image
    chunks = []

    def fake_chunk(path, start, end):
        chunks.append((start, end))
        return b"x" * (end - start + 1)

    x._request = fake_request
    x._prepare_image = lambda path, as_article_image: (b"PNGDATA", "image/png", "media.png")
    x_module.media_size = lambda path: 1024
    x_module.media_chunk = fake_chunk
    try:
        media_id = x._upload_image("tok", "sec", "pic.png", False)
        assert media_id == "img1"
        assert uploaded["data"] == {"media_category": "tweet_image"}
        assert uploaded["files"]["media"][1]

        video = x._upload_video("tok", "sec", "clip.mp4")
        assert video["mediaId"] == "vid1"
        assert video["processing"] is True
        assert chunks == [(0, 1023)]

        x._wait_for_media("tok", "sec", "vid1")
        assert "command=STATUS" in uploaded["status"]
    finally:
        x._request = original
        x_module.media_size = original_size
        x_module.media_chunk = original_chunk
        x._prepare_image = original_prepare

    from app.core.config import settings as app_settings

    target = Path(app_settings.upload_directory) / "x_test.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2400, 1200), "red").save(target)
    try:
        payload, media_type, filename = x._prepare_image(f"x_test.png", False)
        with Image.open(io.BytesIO(payload)) as resized:
            assert resized.width == 1000
        assert media_type == "image/jpeg"
        assert filename == "media.jpg"
    finally:
        target.unlink(missing_ok=True)
    print("OK x image upload + chunked video upload + status wait")


def test_x_article_content_state():
    pending = {
        "message": "<h1>Title</h1><p>Hello <strong>bold</strong> and <a href='https://x.com'>link</a></p>"
        "<ul><li>one</li><li>two</li></ul><img src='inline.png'/>",
        "settings": {"post_type": "article", "article_title": "My article", "article_status": "draft"},
        "mediaIds": ["cover1"],
        "inlineMediaIds": {"inline.png": "inline1"},
    }

    class Integration:
        username = "handle"

    captured = {}

    def fake_request(method, url, token="", token_secret="", **kwargs):
        captured[url] = kwargs.get("json_body")
        return {"data": {"id": "draft1"}} if url.endswith("/draft") else {"data": {"post_id": "art1"}}

    original = x._request
    x._request = fake_request
    try:
        draft = x.finalize_post("tok:sec", {**pending, "attempting": True, "confirmed": True}, Integration())
        assert draft.status == "completed"
        assert draft.postId == "draft1"
        assert draft.releaseURL == "https://x.com/i/articles"

        state = captured["https://api.x.com/2/articles/draft"]["content_state"]
        types = [block["type"] for block in state["blocks"]]
        assert types[0] == "header-one"
        assert "unordered-list-item" in types
        assert any(block["type"] == "atomic" for block in state["blocks"])
        assert captured["https://api.x.com/2/articles/draft"]["title"] == "My article"

        published_pending = {**pending, "settings": {**pending["settings"], "article_status": "published"}}
        published = x.finalize_post("tok:sec", {**published_pending, "attempting": True, "confirmed": True}, Integration())
        assert published.status == "completed"
        assert published.releaseURL == "https://twitter.com/handle/status/art1"
    finally:
        x._request = original
    print("OK x article draft -> publish (draft.js content state)")


def test_tiktok_auth_and_errors():
    import app.integrations.social.tiktok as tiktok_module

    data = tiktok.generate_auth_url()
    assert "tiktok.com/v2/auth/authorize/" in data["url"]
    assert "video.publish" in data["url"]
    assert data["codeVerifier"] == data["state"]
    assert tiktok._redirect_uri().endswith("/integrations/social/tiktok")
    assert tiktok._redirect_uri("int-1").endswith("/integrations/social/tiktok?refresh=int-1")
    assert tiktok._redirect_uri().startswith("https://redirectmeto.com/")

    assert tiktok.handle_errors("access_token_invalid", 401)[0] == "refresh-token"
    assert "daily post limit" in tiktok.handle_errors("spam_risk_too_many_posts", 400)[1]
    assert "1080px" in tiktok.handle_errors("picture_size_check_failed", 400)[1]
    assert tiktok.handle_errors("reached_active_user_cap", 400)[0] == "disconnect"
    assert tiktok.handle_errors("Too Many Requests", 429)[0] == "retry"
    assert tiktok.handle_errors("generic failure", 401)[0] == "refresh-token"

    try:
        tiktok._raise_if_error({"error": {"code": "access_token_invalid", "message": "expired"}})
        raise AssertionError("expected RefreshTokenError")
    except RefreshTokenError as exc:
        assert "re-authenticate" in exc.message

    try:
        tiktok._raise_if_error({"error": {"code": "10000", "message": "something went wrong"}})
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "something went wrong" in exc.message

    assert tiktok_module.parse_publish_status('{"data": {"status": "FAILED"}}')[1] == ""
    print("OK tiktok auth url + error mapping")


def test_tiktok_auth_exchange():
    import app.integrations.social.tiktok as tiktok_module

    calls = {}

    def fake_fetch(url, method="GET", **kwargs):
        calls[url] = (method, kwargs)
        if "oauth/token" in url:
            return FakeResponse({"access_token": "tok", "refresh_token": "ref", "scope": ",".join(tiktok.scopes)})
        if "user/info" in url:
            return FakeResponse(
                {"data": {"user": {"open_id": "a-b-c", "display_name": "Ann", "avatar_url": "p.png", "username": "ann"}}}
            )
        raise AssertionError(url)

    tiktok.fetch = fake_fetch
    try:
        details = tiktok.authenticate("CODE", code_verifier="verifier", refresh="int-1")
        assert details.id == "abc"
        assert details.accessToken == "tok"
        assert details.refreshToken == "ref"
        assert details.expiresIn == 23 * 3600
        assert details.username == "ann"

        _, token_kwargs = calls[tiktok_module.TOKEN_ENDPOINT]
        assert token_kwargs["data"]["grant_type"] == "authorization_code"
        assert token_kwargs["data"]["code_verifier"] == "verifier"
        assert token_kwargs["data"]["redirect_uri"].endswith("/integrations/social/tiktok?refresh=int-1")

        refreshed = tiktok.refresh_token("ref")
        assert refreshed.accessToken == "tok"
        assert calls[tiktok_module.TOKEN_ENDPOINT][1]["data"]["grant_type"] == "refresh_token"
    finally:
        del tiktok.fetch

    def narrow_fetch(url, **kwargs):
        if "oauth/token" in url:
            return FakeResponse({"access_token": "tok", "refresh_token": "r", "scope": "user.info.basic"})
        raise AssertionError(url)

    tiktok.fetch = narrow_fetch
    try:
        try:
            tiktok.authenticate("CODE", code_verifier="verifier")
            raise AssertionError("expected NotEnoughScopes")
        except NotEnoughScopes as exc:
            assert "video.publish" in exc.message
    finally:
        del tiktok.fetch
    print("OK tiktok auth exchange + refresh + scopes")


def test_tiktok_post_bodies():
    video = PostDetails(
        id="t1",
        message="my clip",
        media=[MediaContent(type="video", path="https://cdn.example.com/clip.mp4")],
        settings={
            "privacy_level": "SELF_ONLY",
            "duet": "true",
            "comment": "false",
            "stitch": "false",
            "video_made_with_ai": "true",
            "content_posting_method": "DIRECT_POST",
        },
    )
    info = tiktok._post_info(video)["post_info"]
    assert info["title"] == "my clip"
    assert info["privacy_level"] == "SELF_ONLY"
    assert info["disable_duet"] is False
    assert info["disable_comment"] is True
    assert info["disable_stitch"] is True
    assert info["is_aigc"] is True
    assert "auto_add_music" not in info

    photo = PostDetails(
        id="t2",
        message="hello",
        media=[MediaContent(type="image", path="https://cdn.example.com/a.png")],
        settings={"title": "T" * 120, "autoAddMusic": "yes", "content_posting_method": "DIRECT_POST"},
    )
    info = tiktok._post_info(photo)["post_info"]
    assert len(info["title"]) == 90
    assert info["description"] == "hello"
    assert info["auto_add_music"] is True
    assert info["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert "disable_duet" not in info

    draft = tiktok._post_info(
        PostDetails(
            id="t3",
            message="m",
            media=[MediaContent(type="image", path="a.png")],
            settings={"title": "draft", "content_posting_method": "UPLOAD"},
        )
    )["post_info"]
    assert sorted(draft) == ["description", "title"]

    assert tiktok._posting_method("DIRECT_POST", False) == "/video/init/"
    assert tiktok._posting_method("UPLOAD", False) == "/inbox/video/init/"
    assert tiktok._posting_method("DIRECT_POST", True) == "/content/init/"
    assert tiktok._chunk_plan(64 * 1024 * 1024) == (64 * 1024 * 1024, 1)
    assert tiktok._chunk_plan(65 * 1024 * 1024) == (10 * 1024 * 1024, 6)

    source = tiktok._source_info(video, 25 * 1024 * 1024)
    assert source["source_info"] == {
        "source": "FILE_UPLOAD",
        "video_size": 25 * 1024 * 1024,
        "chunk_size": 25 * 1024 * 1024,
        "total_chunk_count": 1,
    }

    photo_source = tiktok._source_info(photo)
    assert photo_source["post_mode"] == "DIRECT_POST"
    assert photo_source["media_type"] == "PHOTO"
    assert photo_source["source_info"]["photo_images"] == ["https://cdn.example.com/a.png"]

    inbox_source = tiktok._source_info(
        PostDetails(
            id="t4",
            message="m",
            media=[MediaContent(type="image", path="a.png")],
            settings={"content_posting_method": "UPLOAD"},
        )
    )
    assert inbox_source["post_mode"] == "MEDIA_UPLOAD"
    print("OK tiktok post info + source info + chunk plan")


def test_tiktok_media_rules():
    try:
        tiktok.post_pending("t1", "token", [PostDetails(id="t1", message="x", media=[])], None)
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "No video / images selected" in exc.message

    mixed = PostDetails(
        id="t1",
        message="x",
        media=[MediaContent(type="image", path="a.png"), MediaContent(type="video", path="b.mp4")],
    )
    try:
        tiktok.post_pending("t1", "token", [mixed], None)
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "Only pictures" in exc.message

    from app.core.config import settings as app_settings

    target = Path(app_settings.upload_directory) / "tiktok_test.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2000, 2000), "red").save(target)
    try:
        tiktok.post_pending(
            "t1",
            "token",
            [PostDetails(id="t1", message="x", media=[MediaContent(type="image", path=target.name)])],
            None,
        )
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "1080px" in exc.message
    finally:
        target.unlink(missing_ok=True)
    print("OK tiktok media rules (attachment required + 1080px shorter side)")


def test_tiktok_pending_handshake():
    import app.integrations.social.tiktok as tiktok_module

    video = PostDetails(
        id="t1",
        message="clip",
        media=[MediaContent(type="video", path="https://cdn.example.com/clip.mp4")],
        settings={"content_posting_method": "DIRECT_POST"},
    )
    profile = type("Integration", (), {"username": "ann", "internalId": "open1"})()
    calls = []
    responses = [
        FakeResponse({"data": {"publish_id": "v_pub_file~1", "upload_url": "https://upload.example.com/u"}}),
        FakeResponse({"data": {"status": "SEND_IN_PROGRESS"}}),
    ]

    def fake_fetch(url, method="GET", **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/video/init/"):
            return responses[0]
        if url.endswith("/status/fetch/"):
            return responses[1]
        raise AssertionError(url)

    uploads = []
    original_size = tiktok_module.media_size
    tiktok.fetch = fake_fetch
    tiktok._upload_video = lambda url, path, size: uploads.append((url, path, size))
    tiktok_module.media_size = lambda path: 25 * 1024 * 1024
    try:
        pending = tiktok.post_pending("t1", "TOKEN", [video], profile)[0]
        assert pending.status == "pending"
        assert pending.pendingData == {"publishId": "v_pub_file~1"}
        assert uploads == [("https://upload.example.com/u", video.media[0].path, 25 * 1024 * 1024)]

        init = [c for c in calls if c[0].endswith("/video/init/")][0]
        assert init[1]["headers"]["Authorization"] == "Bearer TOKEN"
        assert init[1]["json"]["source_info"]["source"] == "FILE_UPLOAD"
        assert init[1]["json"]["source_info"]["total_chunk_count"] == 1
        assert init[1]["json"]["post_info"]["title"] == "clip"
        init_count = len([c for c in calls if c[0].endswith("/video/init/")])

        progressing = tiktok.check_post_status("TOKEN", pending.pendingData, profile)
        assert progressing.status == "pending"
        assert progressing.pendingData == pending.pendingData

        responses[1] = FakeResponse(
            {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7051111111111111111]}}
        )
        done = tiktok.check_post_status("TOKEN", pending.pendingData, profile)
        assert done.status == "completed"
        assert done.postId == "7051111111111111111"
        assert done.releaseURL == "https://www.tiktok.com/@ann/video/7051111111111111111"

        finalized = tiktok.finalize_post("TOKEN", pending.pendingData, profile)
        assert finalized.status == "completed"
        assert len([c for c in calls if c[0].endswith("/video/init/")]) == init_count

        responses[1] = FakeResponse({"data": {"status": "SEND_TO_USER_INBOX"}})
        inbox = tiktok.check_post_status("TOKEN", pending.pendingData, profile)
        assert inbox.status == "completed"
        assert inbox.postId == "missing"
        assert inbox.releaseURL == "https://www.tiktok.com/messages?lang=en"

        responses[1] = FakeResponse(
            {"data": {"status": "FAILED"}},
            raw='{"data": {"status": "FAILED", "error": {"code": "picture_size_check_failed"}}}',
        )
        try:
            tiktok.check_post_status("TOKEN", pending.pendingData, profile)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "1080px" in exc.message

        responses[1] = FakeResponse(
            {"data": {}},
            raw='{"error": {"code": "access_token_invalid", "message": "expired"}}',
        )
        try:
            tiktok.check_post_status("TOKEN", pending.pendingData, profile)
            raise AssertionError("expected RefreshTokenError")
        except RefreshTokenError as exc:
            assert "re-authenticate" in exc.message
    finally:
        del tiktok.fetch
        del tiktok._upload_video
        tiktok_module.media_size = original_size
    print("OK tiktok init -> upload -> status poll -> int64 id -> inbox")


def test_tiktok_photo_publish_and_upload_chunks():
    import app.integrations.social.tiktok as tiktok_module

    photo = PostDetails(
        id="p1",
        message="caption",
        media=[MediaContent(type="image", path="https://cdn.example.com/pic.png")],
        settings={"content_posting_method": "DIRECT_POST"},
    )
    calls = []

    def fake_fetch(url, method="GET", **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"data": {"publish_id": "p_pub_url~9"}})

    original_dimensions = tiktok_module.image_dimensions
    tiktok.fetch = fake_fetch
    tiktok_module.image_dimensions = lambda path: (800, 600)
    try:
        pending = tiktok.post_pending("p1", "TOKEN", [photo], None)[0]
        assert pending.status == "pending"
        assert pending.pendingData == {"publishId": "p_pub_url~9"}
        init = calls[0]
        assert init[0].endswith("/content/init/")
        assert init[1]["json"]["source_info"]["source"] == "PULL_FROM_URL"
        assert init[1]["json"]["source_info"]["photo_images"] == ["https://cdn.example.com/pic.png"]
        assert init[1]["json"]["post_info"]["description"] == "caption"
    finally:
        del tiktok.fetch
        tiktok_module.image_dimensions = original_dimensions

    puts = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def put(self, url, content=None, headers=None):
            puts.append((url, len(content), headers["Content-Range"]))
            return FakeResponse({}, status_code=200)

    fake_httpx = type("FakeHttpx", (), {"Client": FakeClient})
    original_chunk = tiktok_module.media_chunk
    original_httpx = tiktok_module.httpx
    tiktok_module.media_chunk = lambda path, start, end: b"x" * (end - start + 1)
    tiktok_module.httpx = fake_httpx
    try:
        tiktok._upload_video("https://upload.example.com/u", "clip.mp4", 65 * 1024 * 1024)
        assert [range_header for _, _, range_header in puts] == [
            "bytes 0-10485759/68157440",
            "bytes 10485760-20971519/68157440",
            "bytes 20971520-31457279/68157440",
            "bytes 31457280-41943039/68157440",
            "bytes 41943040-52428799/68157440",
            "bytes 52428800-68157439/68157440",
        ]

        puts.clear()
        tiktok._upload_video("https://upload.example.com/u", "clip.mp4", 1024)
        assert puts == [("https://upload.example.com/u", 1024, "bytes 0-1023/1024")]

        tiktok_module.media_chunk = lambda path, start, end: b"short"
        try:
            tiktok._upload_video("https://upload.example.com/u", "clip.mp4", 1024)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "byte range" in exc.message
    finally:
        tiktok_module.media_chunk = original_chunk
        tiktok_module.httpx = original_httpx
    print("OK tiktok photo PULL_FROM_URL publish + chunked PUT ranges")


class FakeBlueskyAgent:
    instances = []
    create_calls = []
    video_calls = []
    job_state = {"state": "JOB_STATE_PROCESSING"}
    upload_error = None

    def __init__(self, service, provider=None):
        self.service = service
        self.provider = provider
        self.access_jwt = "session-jwt"
        self.refresh_jwt = "refresh-jwt"
        self.did = "did:plc:abc123"
        self.handle = "ann.bsky.social"
        type(self).instances.append(self)

    def login(self, identifier, password):
        if password == "bad":
            raise bluesky_module.XrpcError(401, {"message": "Invalid identifier or password"}, "Invalid credentials")

    def get_profile(self, actor):
        return {"displayName": "Ann", "avatar": "https://cdn.example.com/a.jpg", "handle": self.handle}

    def resolve_handle(self, handle):
        return f"did:plc:{handle.replace('.', '-')}"

    def upload_blob(self, content, content_type):
        if FakeBlueskyAgent.upload_error:
            raise FakeBlueskyAgent.upload_error
        return {"$type": "blob", "ref": {"$link": "bafyblob"}, "mimeType": content_type, "size": len(content)}

    def create_record(self, repo, record):
        FakeBlueskyAgent.create_calls.append((repo, record))
        return {"uri": f"at://{repo}/app.bsky.feed.post/3abc", "cid": "bafycid"}

    def get_job_status(self, job_id, host="https://video.bsky.app"):
        return dict(FakeBlueskyAgent.job_state)

    def start_video_upload(self, path):
        FakeBlueskyAgent.video_calls.append(path)
        return {"jobId": "job-1", "state": "JOB_STATE_PROCESSING"}

    def get_post_thread(self, uri):
        return {"thread": {"post": {"cid": "parent-cid", "record": {}}}}

    def service_auth(self, aud, lxm):
        assert aud.startswith("did:web:")
        return "service-jwt"


def _bluesky_integration(password="pw"):
    import json

    return type(
        "Integration",
        (),
        {
            "internalId": "did:plc:abc123",
            "username": "ann.bsky.social",
            "token": "session-jwt",
            "additionalSettings": json.dumps(
                {"service": "https://bsky.social", "identifier": "ann.test", "password": password}
            ),
        },
    )()


def test_bluesky_credentials_auth():
    import base64
    import json

    import app.integrations.social.bluesky as bluesky_module

    assert bluesky.credentials is True
    assert [field["key"] for field in bluesky.custom_fields()] == ["service", "identifier", "password"]
    assert bluesky.custom_fields()[2]["type"] == "password"

    url = bluesky.generate_auth_url()
    assert url["url"] and url["state"] and url["codeVerifier"]

    assert bluesky_module.is_safe_service("https://bsky.social") is True
    assert bluesky_module.is_safe_service("http://bsky.social") is False
    assert bluesky_module.is_safe_service("https://localhost:3000") is False
    assert bluesky_module.is_safe_service("https://127.0.0.1") is False
    assert bluesky_module.is_safe_service("not-a-url") is False

    assert bluesky.authenticate("") == "Invalid credentials"

    unsafe = base64.b64encode(
        json.dumps({"service": "http://bsky.social", "identifier": "ann.test", "password": "pw"}).encode()
    ).decode()
    assert "HTTPS" in bluesky.authenticate(unsafe)

    original = bluesky_module.Agent
    bluesky_module.Agent = FakeBlueskyAgent
    try:
        payload = base64.b64encode(
            json.dumps({"service": "https://bsky.social", "identifier": "ann.test", "password": "pw"}).encode()
        ).decode()
        details = bluesky.authenticate(payload)
        assert isinstance(details, AuthTokenDetails)
        assert details.id == "did:plc:abc123"
        assert details.name == "Ann"
        assert details.username == "ann.bsky.social"
        assert details.picture == "https://cdn.example.com/a.jpg"
        assert details.accessToken == "session-jwt"
        assert details.refreshToken == ""
        assert details.expiresIn is None
        assert details.additionalSettings["identifier"] == "ann.test"

        failing = base64.b64encode(
            json.dumps({"service": "https://bsky.social", "identifier": "ann.test", "password": "bad"}).encode()
        ).decode()
        assert bluesky.authenticate(failing) == "Invalid credentials"

        credentials = bluesky._credentials(_bluesky_integration())
        assert credentials["service"] == "https://bsky.social"
        agent = bluesky._agent(_bluesky_integration())
        assert agent.did == "did:plc:abc123"
        try:
            bluesky._agent(_bluesky_integration(password="bad"))
            raise AssertionError("expected RefreshTokenError")
        except RefreshTokenError as exc:
            assert "reconnect" in exc.value
    finally:
        bluesky_module.Agent = original
    print("OK bluesky credentials auth + service validation")


def test_bluesky_facets_and_media_rules():
    import app.integrations.social.bluesky as bluesky_module

    text = "café @alice.test #salut https://example.com/page."
    facets = bluesky_module.build_facets(text, {"alice.test": "did:plc:alice"})
    assert [facet["features"][0]["$type"] for facet in facets] == [
        "app.richtext.facet.mention",
        "app.richtext.facet.tag",
        "app.richtext.facet.link",
    ]
    mention = facets[0]
    assert text.encode("utf-8")[mention["index"]["byteStart"] : mention["index"]["byteEnd"]] == b"@alice.test"
    assert mention["features"][0]["did"] == "did:plc:alice"
    assert facets[1]["features"][0]["tag"] == "salut"
    link = facets[2]
    assert link["features"][0]["uri"] == "https://example.com/page"
    assert text.encode("utf-8")[link["index"]["byteStart"] : link["index"]["byteEnd"]] == b"https://example.com/page"

    assert bluesky_module.build_facets("hi @bob.test", {}) == []
    assert bluesky_module.build_facets("#" + "a" * 100, {}) == []

    too_many = PostDetails(
        id="b1",
        message="many",
        media=[MediaContent(type="image", path=f"p{index}.png") for index in range(5)],
    )
    two_videos = PostDetails(
        id="b2",
        message="two",
        media=[
            MediaContent(type="video", path="https://cdn.example.com/a.mp4"),
            MediaContent(type="video", path="https://cdn.example.com/b.mp4"),
        ],
    )
    video_and_image = PostDetails(
        id="b3",
        message="mix",
        media=[
            MediaContent(type="video", path="https://cdn.example.com/a.mp4"),
            MediaContent(type="image", path="p0.png"),
        ],
    )
    try:
        bluesky.post_pending("did:plc:abc123", "", [too_many], _bluesky_integration())
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "maximum 4 pictures" in exc.message
    try:
        bluesky.post_pending("did:plc:abc123", "", [two_videos], _bluesky_integration())
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "one video per post" in exc.message
    try:
        bluesky.post_pending("did:plc:abc123", "", [video_and_image], _bluesky_integration())
        raise AssertionError("expected BadBody")
    except BadBody as exc:
        assert "one video per post" in exc.message
    print("OK bluesky facets byte offsets + media rules")


def test_bluesky_pending_handshake():
    import os
    import tempfile

    import app.integrations.social.bluesky as bluesky_module

    directory = tempfile.mkdtemp()
    image_path = os.path.join(directory, "pic.png")
    Image.new("RGB", (64, 64), (200, 40, 40)).save(image_path)

    post = PostDetails(id="b1", message="hello bluesky", media=[MediaContent(type="image", path=image_path, alt="alt text")])
    integration = _bluesky_integration()
    original = bluesky_module.Agent
    bluesky_module.Agent = FakeBlueskyAgent
    FakeBlueskyAgent.create_calls = []
    try:
        started = bluesky.post_pending("did:plc:abc123", "", [post], integration)[0]
        assert started.status == "pending"
        assert started.pendingData["jobId"] == ""
        assert started.pendingData["attempting"] is False
        assert started.pendingData["confirmed"] is False

        ready = bluesky.check_post_status("", started.pendingData, integration)
        assert ready.status == "ready"
        assert ready.pendingData["attempting"] is False

        armed = bluesky.finalize_post("", ready.pendingData, integration)
        assert armed.status == "pending"
        assert armed.pendingData["attempting"] is True
        assert armed.pendingData["confirmed"] is False

        witnessed = bluesky.check_post_status("", armed.pendingData, integration)
        assert witnessed.status == "ready"
        assert witnessed.pendingData["confirmed"] is True

        done = bluesky.finalize_post("", witnessed.pendingData, integration)
        assert done.status == "completed"
        assert done.postId == "at://did:plc:abc123/app.bsky.feed.post/3abc"
        assert done.releaseURL == "https://bsky.app/profile/did:plc:abc123/post/3abc"

        assert len(FakeBlueskyAgent.create_calls) == 1
        repo, record = FakeBlueskyAgent.create_calls[0]
        assert repo == "did:plc:abc123"
        assert record["$type"] == "app.bsky.feed.post"
        assert record["text"] == "hello bluesky"
        assert record["embed"]["$type"] == "app.bsky.embed.images"
        assert record["embed"]["images"][0]["alt"] == "alt text"
        assert record["embed"]["images"][0]["image"]["ref"]["$link"] == "bafyblob"
        assert record["embed"]["images"][0]["aspectRatio"] == {"width": 64, "height": 64}
        assert record["createdAt"].endswith("Z")

        try:
            bluesky.check_post_status("", witnessed.pendingData, integration)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "avoid duplicates" in exc.message

        reply = PostDetails(id="b2", message="nice post", media=[])
        comment = bluesky.comment("did:plc:abc123", "", reply, done.postId, integration)
        assert comment.status == "success"
        assert comment.releaseURL == done.releaseURL
        _, reply_record = FakeBlueskyAgent.create_calls[-1]
        assert reply_record["reply"]["parent"]["uri"] == done.postId
        assert reply_record["reply"]["parent"]["cid"] == "parent-cid"
        assert reply_record["reply"]["root"]["uri"] == done.postId
        assert reply_record["reply"]["root"]["cid"] == "parent-cid"
    finally:
        bluesky_module.Agent = original
    print("OK bluesky arm -> witness -> publish + duplicate guard + reply")


def test_bluesky_video_job():
    import app.integrations.social.bluesky as bluesky_module

    video = PostDetails(
        id="b3",
        message="clip",
        media=[MediaContent(type="video", path="https://cdn.example.com/clip.mp4", alt="a clip")],
    )
    integration = _bluesky_integration()
    original = bluesky_module.Agent
    bluesky_module.Agent = FakeBlueskyAgent
    FakeBlueskyAgent.create_calls = []
    FakeBlueskyAgent.video_calls = []
    FakeBlueskyAgent.job_state = {"state": "JOB_STATE_PROCESSING"}
    try:
        started = bluesky.post_pending("did:plc:abc123", "", [video], integration)[0]
        assert started.pendingData["jobId"] == "job-1"
        assert FakeBlueskyAgent.video_calls == ["https://cdn.example.com/clip.mp4"]

        progressing = bluesky.check_post_status("", started.pendingData, integration)
        assert progressing.status == "pending"

        FakeBlueskyAgent.job_state = {"state": "JOB_STATE_FAILED", "error": "too big"}
        try:
            bluesky.check_post_status("", started.pendingData, integration)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "job failed" in exc.message

        FakeBlueskyAgent.job_state = {"state": "JOB_STATE_DONE", "blob": {"$type": "blob", "ref": {"$link": "bafyvideo"}}}
        ready = bluesky.check_post_status("", started.pendingData, integration)
        assert ready.status == "ready"
        assert ready.pendingData["attempting"] is False

        armed = bluesky.finalize_post("", ready.pendingData, integration)
        assert armed.status == "pending"
        assert armed.pendingData["attempting"] is True

        witnessed = bluesky.check_post_status("", armed.pendingData, integration)
        assert witnessed.status == "ready"
        assert witnessed.pendingData["confirmed"] is True

        done = bluesky.finalize_post("", witnessed.pendingData, integration)
        assert done.status == "completed"
        _, record = FakeBlueskyAgent.create_calls[-1]
        assert record["embed"]["$type"] == "app.bsky.embed.video"
        assert record["embed"]["video"]["ref"]["$link"] == "bafyvideo"

        try:
            bluesky.check_post_status("", witnessed.pendingData, integration)
            raise AssertionError("expected duplicate guard")
        except BadBody as exc:
            assert "avoid duplicates" in exc.message
    finally:
        bluesky_module.Agent = original
    print("OK bluesky video job handshake + failed job guard")


def test_bluesky_failure_paths():
    import os
    import tempfile

    import app.integrations.social.bluesky as bluesky_module

    directory = tempfile.mkdtemp()
    image_path = os.path.join(directory, "pic.png")
    Image.new("RGB", (64, 64), (10, 10, 250)).save(image_path)

    integration = _bluesky_integration()
    original = bluesky_module.Agent
    bluesky_module.Agent = FakeBlueskyAgent
    FakeBlueskyAgent.upload_error = None
    original_create = FakeBlueskyAgent.create_record
    try:
        pending = {
            "jobId": "",
            "message": "boom",
            "media": [{"path": image_path, "alt": ""}],
            "attempting": True,
            "confirmed": True,
            "prepFailures": 0,
        }

        FakeBlueskyAgent.upload_error = RuntimeError("network down")
        for attempt in range(4):
            result = bluesky.finalize_post("", {**pending, "attempting": True, "confirmed": True}, integration)
            assert result.status == "pending"
            assert result.pendingData["prepFailures"] == attempt + 1
            assert result.pendingData["attempting"] is False
            pending = result.pendingData
        try:
            bluesky.finalize_post("", {**pending, "attempting": True, "confirmed": True}, integration)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "nothing was published" in exc.message

        FakeBlueskyAgent.upload_error = None

        def rejecting_create(self, repo, record):
            raise bluesky_module.XrpcError(400, {"message": "Invalid facet"}, '{"message":"Invalid facet"}')

        FakeBlueskyAgent.create_record = rejecting_create
        try:
            bluesky.finalize_post("", {**pending, "attempting": True, "confirmed": True, "prepFailures": 0}, integration)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "nothing was published" in exc.message

        def flaky_create(self, repo, record):
            raise bluesky_module.XrpcError(500, {}, "gateway timeout")

        FakeBlueskyAgent.create_record = flaky_create
        ambiguous = bluesky.finalize_post("", {**pending, "attempting": True, "confirmed": True, "prepFailures": 0}, integration)
        assert ambiguous.status == "pending"
        assert ambiguous.pendingData["attempting"] is True
        assert ambiguous.pendingData["confirmed"] is True
    finally:
        FakeBlueskyAgent.create_record = original_create
        FakeBlueskyAgent.upload_error = None
        bluesky_module.Agent = original
    print("OK bluesky prep failures + rejection + ambiguous create")


def test_bluesky_reduce_image():
    import os
    import tempfile

    directory = tempfile.mkdtemp()
    noisy = os.path.join(directory, "noise.png")
    Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3)).save(noisy)
    buffer, width, height, mime = bluesky.reduce_image(noisy)
    assert len(buffer) / 1024 <= 976
    assert mime == "image/png"
    assert width < 900 and height < 900

    small = os.path.join(directory, "small.jpg")
    Image.new("RGB", (40, 40), (10, 200, 60)).save(small, format="JPEG")
    buffer, width, height, mime = bluesky.reduce_image(small)
    assert mime == "image/jpeg"
    assert (width, height) == (40, 40)
    print("OK bluesky reduce image under 976KB")





def test_threads_auth_and_errors():
    import app.integrations.social.threads as threads_module

    url = threads.generate_auth_url()
    assert "threads.net/oauth/authorize" in url["url"]
    assert "threads_content_publish" in url["url"]
    assert "redirectmeto.com" in url["url"]
    assert "client_id=" in url["url"]
    assert url["state"] and url["codeVerifier"]

    assert threads.handle_errors("Error validating access token", 400)[0] == "refresh-token"
    assert "restrict certain activity" in threads.handle_errors("2207051", 400)[1]
    assert threads.handle_errors("4279013", 400)[1] == "User restricted"
    assert "inaccessible" in threads.handle_errors(
        "The media could not be fetched from this URI", 400
    )[1]
    assert threads.handle_errors("4279009", 400)[0] == "retry"
    assert "500 characters" in threads.handle_errors("text must be at most 500 characters", 400)[1]
    assert threads.handle_errors("unknown", 401)[0] == "refresh-token"

    calls = []

    def fake_get_json(url, method="GET", params=None, **kwargs):
        params = dict(params or {})
        calls.append((method, url, params))
        if url.endswith("/oauth/access_token"):
            assert params["grant_type"] == "authorization_code"
            assert params["client_secret"] == ""
            return {"access_token": "short-token"}
        if url.endswith("/access_token"):
            assert params["grant_type"] == "th_exchange_token"
            return {"access_token": "long-token"}
        if url.endswith("/refresh_access_token"):
            assert params["grant_type"] == "th_refresh_token"
            return {"access_token": "refreshed-token"}
        if url.endswith("/v1.0/me"):
            return {
                "id": "user-1",
                "username": "ann.threads",
                "threads_profile_picture_url": "https://cdn.example.com/a.jpg",
            }
        raise AssertionError(url)

    original = threads.get_json
    threads.get_json = fake_get_json
    try:
        details = threads.authenticate("auth-code")
        assert details.id == "user-1"
        assert details.name == "ann.threads"
        assert details.username == "ann.threads"
        assert details.accessToken == "long-token"
        assert details.refreshToken == "long-token"
        assert details.picture == "https://cdn.example.com/a.jpg"
        assert details.expiresIn == 58 * 24 * 60 * 60

        refreshed = threads.refresh_token("long-token")
        assert refreshed.accessToken == "refreshed-token"
        assert refreshed.refreshToken == "refreshed-token"
        assert refreshed.id == "user-1"
    finally:
        threads.get_json = original
    assert threads_module.SETTLE_SECONDS == 2
    print("OK threads auth url + exchange + refresh + error mapping")


def test_threads_text_handshake():
    import app.integrations.social.threads as threads_module

    integration = type("Integration", (), {"internalId": "user-1", "username": "ann.threads"})()
    calls = []
    statuses = []

    def fake_get_json(url, method="GET", params=None, **kwargs):
        params = dict(params or {})
        calls.append((method, url, params))
        if url.endswith("/threads") and method == "POST":
            return {"id": "container-1"}
        if url.endswith("/threads_publish"):
            return {"id": "thread-1"}
        if params.get("fields") == "status,error_message":
            return statuses.pop(0) if statuses else {"status": "FINISHED"}
        if params.get("fields") == "id,permalink":
            return {"id": "thread-1", "permalink": "https://www.threads.net/@ann.threads/post/thread-1"}
        raise AssertionError(url)

    original = threads.get_json
    threads.get_json = fake_get_json
    try:
        post = PostDetails(id="p1", message="hello threads")
        started = threads.post_pending("user-1", "long-token", [post], integration)[0]
        assert started.status == "pending"
        assert started.pendingData == {"step": "container", "containerId": "container-1"}
        create = calls[-1]
        assert create[0] == "POST" and create[1].endswith("/v1.0/user-1/threads")
        assert create[2]["media_type"] == "TEXT"
        assert create[2]["text"] == "hello threads"
        assert create[2]["access_token"] == "long-token"

        statuses[:] = [{"status": "IN_PROGRESS"}]
        progressing = threads.check_post_status("long-token", started.pendingData, integration)
        assert progressing.status == "pending"

        statuses[:] = [{"status": "FINISHED"}]
        ready = threads.check_post_status("long-token", started.pendingData, integration)
        assert ready.status == "ready"

        done = threads.finalize_post("long-token", ready.pendingData, integration)
        assert done.status == "completed"
        assert done.postId == "thread-1"
        assert done.releaseURL == "https://www.threads.net/@ann.threads/post/thread-1"
        publish = [call for call in calls if call[1].endswith("/threads_publish")][0]
        assert publish[2] == {"creation_id": "container-1", "access_token": "long-token"}

        statuses[:] = [{"status": "PUBLISHED"}]
        again = threads.check_post_status("long-token", started.pendingData, integration)
        assert again.status == "completed"
        assert again.postId == "container-1"
        assert again.releaseURL == "https://www.threads.net/@ann.threads"

        statuses[:] = [{"status": "ERROR", "error_message": "media too large"}]
        try:
            threads.check_post_status("long-token", started.pendingData, integration)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "media too large" in exc.message

        statuses[:] = [{"status": "EXPIRED", "error_message": "UNKNOWN"}]
        try:
            threads.check_post_status("long-token", started.pendingData, integration)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "check the media format" in exc.message

        statuses[:] = []
        result = threads.post("user-1", "long-token", [PostDetails(id="p2", message="again")], integration)
        assert result[0].status == "success"
        assert result[0].postId == "thread-1"
    finally:
        threads.get_json = original
    assert threads_module.POLL_SECONDS >= 0
    print("OK threads container -> status -> publish + duplicate guard")


def test_threads_carousel_and_comment():
    import app.integrations.social.threads as threads_module

    integration = type("Integration", (), {"internalId": "user-1", "username": "ann.threads"})()
    calls = []
    created = []
    statuses = []

    def fake_get_json(url, method="GET", params=None, **kwargs):
        params = dict(params or {})
        calls.append((method, url, params))
        if url.endswith("/threads") and method == "POST":
            created.append(params)
            return {"id": f"container-{len(created)}"}
        if url.endswith("/threads_publish"):
            return {"id": "thread-9"}
        if params.get("fields") == "status,error_message":
            return statuses.pop(0) if statuses else {"status": "FINISHED"}
        if params.get("fields") == "id,permalink":
            return {"id": "thread-9", "permalink": "https://www.threads.net/@ann.threads/post/thread-9"}
        raise AssertionError(url)

    original = threads.get_json
    original_poll = threads_module.POLL_SECONDS
    original_settle = threads_module.SETTLE_SECONDS
    threads.get_json = fake_get_json
    threads_module.POLL_SECONDS = 0
    threads_module.SETTLE_SECONDS = 0
    try:
        media = [
            MediaContent(type="image", path="https://cdn.example.com/one.png", alt="first"),
            MediaContent(type="image", path="https://cdn.example.com/two.png", alt="second"),
        ]
        started = threads.post_pending("user-1", "long-token", [PostDetails(id="p1", message="c", media=media)], integration)[0]
        assert started.pendingData["step"] == "children"
        assert started.pendingData["childIds"] == ["container-1", "container-2"]
        assert created[0]["is_carousel_item"] == "true"
        assert created[0]["image_url"] == "https://cdn.example.com/one.png"
        assert created[1]["image_url"] == "https://cdn.example.com/two.png"

        statuses[:] = [{"status": "IN_PROGRESS"}, {"status": "FINISHED"}]
        progressing = threads.check_post_status("long-token", started.pendingData, integration)
        assert progressing.status == "pending"

        statuses[:] = [{"status": "FINISHED"}, {"status": "FINISHED"}]
        ready = threads.check_post_status("long-token", started.pendingData, integration)
        assert ready.status == "ready"

        carved = threads.finalize_post("long-token", ready.pendingData, integration)
        assert carved.status == "pending"
        assert carved.pendingData == {"step": "container", "containerId": "container-3"}
        assert created[-1]["media_type"] == "CAROUSEL"
        assert created[-1]["children"] == "container-1,container-2"
        assert created[-1]["text"] == "c"

        statuses[:] = [{"status": "FINISHED"}]
        second = threads.check_post_status("long-token", carved.pendingData, integration)
        assert second.status == "ready"
        done = threads.finalize_post("long-token", second.pendingData, integration)
        assert done.status == "completed"
        assert done.postId == "thread-9"

        created.clear()
        reply = threads.comment(
            "user-1",
            "long-token",
            PostDetails(id="p2", message="thanks for sharing"),
            done.postId,
            integration,
        )
        assert reply.status == "success"
        assert reply.postId == "thread-9"
        assert reply.releaseURL == "https://www.threads.net/@ann.threads/post/thread-9"
        assert created[0]["reply_to_id"] == "thread-9"
        assert created[0]["media_type"] == "TEXT"

        created.clear()
        statuses[:] = []
        video_reply = threads.comment(
            "user-1",
            "long-token",
            PostDetails(
                id="p3",
                message="clip reply",
                media=[MediaContent(type="video", path="https://cdn.example.com/clip.mp4")],
            ),
            "",
            integration,
        )
        assert video_reply.status == "success"
        assert created[0]["media_type"] == "VIDEO"
        assert created[0]["video_url"] == "https://cdn.example.com/clip.mp4"
        assert created[0]["reply_to_id"] == "user-1"
    finally:
        threads.get_json = original
        threads_module.POLL_SECONDS = original_poll
        threads_module.SETTLE_SECONDS = original_settle
    print("OK threads carousel children -> container + reply publish")



def test_tumblr_auth_and_errors():
    url = tumblr.generate_auth_url()
    assert "tumblr.com/oauth2/authorize" in url["url"]
    assert "scope=write+offline_access" in url["url"]
    assert "response_type=code" in url["url"]
    assert "redirect_uri=" in url["url"]
    assert url["state"] and url["codeVerifier"]

    assert tumblr.handle_errors("Unauthorized", 400)[0] == "refresh-token"
    assert tumblr.handle_errors("invalid_grant", 400)[0] == "refresh-token"
    assert tumblr.handle_errors("nope", 401)[0] == "refresh-token"
    assert "daily posting limit" in tumblr.handle_errors("daily posting limit reached", 400)[1]
    assert tumblr.handle_errors('{"code":8023}', 400)[0] == "bad-body"
    assert "media upload error" in tumblr.handle_errors("meta 8006.", 400)[1]
    assert tumblr.handle_errors("meta 8006.", 400)[0] == "retry"
    assert "transcoding" in tumblr.handle_errors("code=8010", 400)[1]
    assert tumblr.handle_errors("429 hit", 429)[0] == "retry"
    assert tumblr.handle_errors("down", 503)[0] == "retry"
    assert tumblr.handle_errors("nope", 400)[0] == "bad-body"

    def fake_get_json(url, method="GET", headers=None, params=None, data=None, **kwargs):
        if url.endswith("/oauth2/token"):
            assert method == "POST"
            assert data["grant_type"] in ("authorization_code", "refresh_token")
            if data["grant_type"] == "authorization_code":
                return {"access_token": "access", "refresh_token": "refresh", "expires_in": 31536000, "scope": "write offline_access"}
            return {"access_token": "access2", "refresh_token": "refresh2", "expires_in": 31536000, "scope": "write offline_access"}
        if url.endswith("/user/info"):
            assert headers["Authorization"] == "Bearer access" or headers["Authorization"] == "Bearer access2"
            return {
                "response": {
                    "user": {
                        "name": "ann",
                        "blogs": [
                            {"name": "myblog", "title": "My Blog", "primary": True},
                            {"name": "second", "title": "Second"},
                        ],
                    }
                }
            }
        raise AssertionError(url)

    original = tumblr.get_json
    tumblr.get_json = fake_get_json
    try:
        details = tumblr.authenticate("code")
        assert details.id == "ann"
        assert details.name == "ann"
        assert details.accessToken == "access"
        assert details.refreshToken == "refresh"
        assert details.expiresIn == 31536000
        assert details.picture.endswith("/myblog.tumblr.com/avatar/128")

        refreshed = tumblr.refresh_token("refresh")
        assert refreshed.accessToken == "access2"
        assert refreshed.refreshToken == "refresh2"

        try:
            tumblr._request_token = lambda params: {"access_token": "access", "refresh_token": "r", "expires_in": 1, "scope": "read"}
            tumblr.authenticate("code")
            raise AssertionError("expected NotEnoughScopes")
        except NotEnoughScopes:
            pass
    finally:
        tumblr.get_json = original
        if hasattr(tumblr, "_request_token"):
            del tumblr._request_token
    print("OK tumblr auth url + token exchange + refresh + error mapping")


def test_tumblr_content_blocks():
    import app.integrations.social.tumblr as tumblr_module

    payload = tumblr._payload(
        PostDetails(
            id="p1",
            message="para one\n\npara two",
            settings={
                "title": "My Title",
                "link": "example.com",
                "tags": ["alpha", "beta"],
                "sourceUrl": "source.example.com",
            },
        )
    )
    content = payload["content"]
    assert payload["state"] == "published"
    assert payload["tags"] == "alpha,beta"
    assert payload["source_url"] == "https://source.example.com"
    assert content[0] == {"type": "text", "subtype": "heading1", "text": "My Title"}
    assert content[1] == {"type": "text", "text": "para one"}
    assert content[2] == {"type": "text", "text": "para two"}
    assert content[3] == {"type": "link", "url": "https://example.com"}

    long_message = tumblr._content_blocks(PostDetails(id="p2", message="x" * 5000))
    assert [len(block["text"]) for block in long_message] == [4096, 904]

    assert tumblr._content_blocks(PostDetails(id="p3", message="")) == [{"type": "text", "text": ""}]

    original = tumblr_module.image_dimensions
    tumblr_module.image_dimensions = lambda path: (640, 480)
    try:
        blocks = tumblr._content_blocks(
            PostDetails(
                id="p4",
                message="",
                media=[MediaContent(type="image", path="https://cdn.example.com/pic.png", alt="alt text")],
            )
        )
        assert blocks == [
            {
                "type": "image",
                "media": [
                    {"type": "image/png", "identifier": "media-0", "width": 640, "height": 480}
                ],
                "alt_text": "alt text",
            }
        ]

        video = tumblr._content_blocks(
            PostDetails(
                id="p5",
                message="",
                media=[MediaContent(type="video", path="https://cdn.example.com/clip.mp4")],
            )
        )
        assert video[0]["type"] == "video"
        assert video[0]["provider"] == "tumblr"
        assert video[0]["media"] == {
            "type": "video/mp4",
            "identifier": "media-0",
            "width": 540,
            "height": 405,
        }

        too_many = [MediaContent(type="image", path=f"p{index}.png") for index in range(31)]
        try:
            tumblr._check_media(too_many)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "30 images" in exc.message

        two_videos = [
            MediaContent(type="video", path="https://cdn.example.com/a.mp4"),
            MediaContent(type="video", path="https://cdn.example.com/b.mp4"),
        ]
        try:
            tumblr._check_media(two_videos)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "one uploaded video" in exc.message
    finally:
        tumblr_module.image_dimensions = original
    print("OK tumblr content blocks (title/link/chunks/media) + media rules")


def test_tumblr_post_and_connections():
    import app.integrations.social.tumblr as tumblr_module

    integration = type(
        "Integration", (), {"internalId": "myblog", "username": "https://myblog.tumblr.com/"}
    )()
    calls = []

    class FakeResp:
        def __init__(self, payload=None, text=""):
            self._payload = payload
            self._text = text

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

        @property
        def text(self):
            return self._text

    def fake_fetch(url, method="GET", headers=None, params=None, json=None, files=None, data=None, **kwargs):
        calls.append(
            {
                "url": url,
                "method": method,
                "headers": headers,
                "json": json,
                "files": files,
                "data": data,
            }
        )
        if files is not None:
            return FakeResp({"response": {"id_string": "777"}})
        return FakeResp({"response": {"id_string": "123"}})

    original_fetch = tumblr.fetch
    original_read = tumblr_module.read_or_fetch
    original_dimensions = tumblr_module.image_dimensions
    tumblr.fetch = fake_fetch
    tumblr_module.read_or_fetch = lambda path: b"binary-bytes"
    tumblr_module.image_dimensions = lambda path: (320, 240)
    try:
        text_post = tumblr.post("myblog", "tok", [PostDetails(id="p1", message="hello tumblr")], integration)[0]
        assert text_post.status == "success"
        assert text_post.postId == "123"
        assert text_post.releaseURL == "https://myblog.tumblr.com/post/123"
        first = calls[0]
        assert first["url"] == "https://api.tumblr.com/v2/blog/myblog/posts"
        assert first["method"] == "POST"
        assert first["headers"]["Authorization"] == "Bearer tok"
        assert first["headers"]["User-Agent"].startswith("Postiz/")
        assert first["files"] is None
        assert first["json"]["content"] == [{"type": "text", "text": "hello tumblr"}]
        assert first["json"]["state"] == "published"

        media_post = tumblr.post(
            "myblog",
            "tok",
            [
                PostDetails(
                    id="p2",
                    message="with media",
                    media=[MediaContent(type="image", path="pic.png", alt="pic")],
                )
            ],
            integration,
        )[0]
        assert media_post.postId == "777"
        second = calls[1]
        assert second["json"] is None
        assert set(second["files"]) == {"media-0"}
        assert second["files"]["media-0"][1] == b"binary-bytes"
        assert second["files"]["media-0"][2] == "image/png"
        blocks = json.loads(second["data"]["json"])["content"]
        image_block = [block for block in blocks if block["type"] == "image"][0]
        assert image_block["alt_text"] == "pic"

        tumblr.fetch = lambda *args, **kwargs: FakeResp(None, "<html>proxy page</html>")
        try:
            tumblr.post("myblog", "tok", [PostDetails(id="p3", message="nope")], integration)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "valid post response" in exc.message

        def fake_get_json(url, method="GET", headers=None, **kwargs):
            assert url.endswith("/user/info")
            return {
                "response": {
                    "user": {
                        "name": "ann",
                        "blogs": [
                            {"name": "myblog", "title": "My Blog", "url": "https://myblog.tumblr.com/", "primary": True},
                            {"name": "second", "title": "Second", "url": "https://second.tumblr.com/"},
                        ],
                    }
                }
            }

        original_get_json = tumblr.get_json
        tumblr.get_json = fake_get_json
        try:
            links = tumblr.connections(
                AuthTokenDetails(id="ann", name="ann", accessToken="tok", refreshToken="ref", expiresIn=99)
            )
        finally:
            tumblr.get_json = original_get_json

        assert [link.id for link in links] == ["myblog", "second"]
        assert links[0].name == "My Blog"
        assert links[0].accessToken == "tok"
        assert links[0].refreshToken == "ref"
        assert links[0].username == "https://myblog.tumblr.com/"
        assert links[1].id == "second"
    finally:
        tumblr.fetch = original_fetch
        tumblr_module.read_or_fetch = original_read
        tumblr_module.image_dimensions = original_dimensions
    print("OK tumblr json/multipart publish + one connection per blog")



def test_vk_auth_and_refresh():
    import hashlib
    import base64 as _b64

    result = vk.generate_auth_url()
    url = result["url"]
    assert "https://id.vk.com/authorize" in url
    assert "code_challenge_method=S256" in url
    assert f"&state={result['state']}" in url
    digest = hashlib.sha256(result["codeVerifier"].encode()).digest()
    expected = _b64.b64encode(digest).decode().rstrip("=").replace("+", "-").replace("/", "_")
    assert f"&code_challenge={expected}" in url
    assert "redirectmeto.com" in url
    assert "vkid.personal_info" in url

    def fake_get_json(url, method="GET", headers=None, params=None, data=None, **kwargs):
        assert method == "POST"
        if url.endswith("/oauth2/auth"):
            if data["grant_type"] == "authorization_code":
                assert data["code"] == "abcdef"
                assert data["device_id"] == "dev1"
                assert data["code_verifier"] == "verif"
                return {"access_token": "tok1", "refresh_token": "ref1", "expires_in": 3600}
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "ref1"
            assert data["device_id"] == "dev1"
            assert data["state"] and data["scope"].startswith("vkid.personal_info")
            return {"access_token": "tok2", "refresh_token": "ref2", "expires_in": 7200}
        if url.endswith("/oauth2/user_info"):
            assert data["access_token"] in ("tok1", "tok2")
            return {"user": {"user_id": 42, "first_name": "Ann", "last_name": "Lee", "avatar": "https://img/a.jpg"}}
        raise AssertionError(url)

    original = vk.get_json
    vk.get_json = fake_get_json
    try:
        details = vk.authenticate("abcdef&&&&dev1", code_verifier="verif")
        assert details.id == "42"
        assert details.name == "Ann Lee"
        assert details.accessToken == "tok1"
        assert details.refreshToken == "ref1&&&&dev1"
        assert details.expiresIn == 3600
        assert details.picture == "https://img/a.jpg"
        assert details.username == "ann"

        refreshed = vk.refresh_token("ref1&&&&dev1")
        assert refreshed.accessToken == "tok2"
        assert refreshed.refreshToken == "ref2&&&&dev1"
        assert refreshed.expiresIn == 7200

        vk.get_json = lambda url, **kwargs: {"error": "invalid_grant"} if url.endswith("/oauth2/auth") else (_ for _ in ()).throw(AssertionError(url))
        try:
            vk.authenticate("x&&&&d", code_verifier="v")
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "access token" in exc.message

        vk.get_json = lambda url, **kwargs: {"user": {}} if url.endswith("/oauth2/user_info") else {"access_token": "tok3", "refresh_token": "ref3", "expires_in": 60}
        refreshed = vk.refresh_token("ref3")
        assert refreshed.accessToken == "tok3"
        assert refreshed.refreshToken == "ref3&&&&"
    finally:
        vk.get_json = original
    print("OK vk auth url (PKCE) + exchange + refresh")


def test_vk_upload_and_post():
    events = []

    def fake_get_json(url, method="GET", **kwargs):
        if "photos.getWallUploadServer" in url:
            assert "owner_id=42" in url
            assert "access_token=tok" in url
            assert "v=5.251" in url
            return {"response": {"upload_url": "https://up.example/photo"}}
        if "video.save" in url:
            return {"response": {"upload_url": "https://up.example/video", "video_id": 77}}
        raise AssertionError(url)

    def fake_fetch(url, method="GET", headers=None, params=None, data=None, files=None, **kwargs):
        events.append((url, method, data, files))
        if url == "https://up.example/photo":
            assert method == "POST"
            assert files["photo"][0] == "pic.png"
            assert files["photo"][1] == b"pngbytes"
            return FakeResponse({"photo": "ph", "server": "1", "hash": "h"})
        if url == "https://up.example/video":
            return FakeResponse({})
        if "photos.saveWallPhoto" in url:
            assert data["photo"] == "ph"
            assert data["server"] == "1"
            assert data["hash"] == "h"
            return FakeResponse({"response": [{"id": 55}]})
        if "wall.post" in url:
            if data["message"] == "hello":
                assert data["attachments"] == "photo42_55,video42_77"
            else:
                assert data["message"] == "no media"
                assert "attachments" not in data
            return FakeResponse({"response": {"post_id": 9}})
        raise AssertionError(url)

    originals = (vk.get_json, vk.fetch, vk_module.read_or_fetch, vk_module.public_url)
    vk.get_json = fake_get_json
    vk.fetch = fake_fetch
    vk_module.read_or_fetch = lambda path: b"pngbytes" if path.endswith(".png") else b"mp4bytes"
    vk_module.public_url = lambda path: path
    try:
        integration = SimpleNamespace(internalId="42")
        media = [
            MediaContent(type="image", path="https://cdn.example.com/pic.png"),
            MediaContent(type="video", path="https://cdn.example.com/clip.mp4"),
        ]
        out = vk.post("42", "tok", [PostDetails(id="p1", message="hello", media=media)], integration)
        assert len(out) == 1
        assert out[0].id == "p1"
        assert out[0].status == "success"
        assert out[0].postId == "9"
        assert out[0].releaseURL == "https://vk.com/feed?w=wall42_9"

        events.clear()
        out = vk.post("42", "tok", [PostDetails(id="p2", message="no media")], integration)
        assert out[0].postId == "9"
        assert not [entry for entry in events if "photos" in entry[0]]
    finally:
        vk.get_json, vk.fetch, vk_module.read_or_fetch, vk_module.public_url = originals
    print("OK vk photo/video upload + wall.post attachments")


def test_vk_comment_and_errors():
    vk._check_api_error({"response": {"post_id": 1}})
    try:
        vk._check_api_error({"error": {"error_code": 5, "error_msg": "access token expired"}})
        raise AssertionError("expected RefreshTokenError")
    except RefreshTokenError:
        pass
    for code in (6, 9, 29, 100):
        try:
            vk._check_api_error({"error": {"error_code": code, "error_msg": "boom"}})
            raise AssertionError("expected BadBody")
        except BadBody:
            pass

    assert vk.handle_errors("User authorization failed: invalid token", 400)[0] == "refresh-token"

    def fake_get_json(url, method="GET", **kwargs):
        assert "photos.getWallUploadServer" in url
        return {"response": {"upload_url": "https://up.example/photo"}}

    def fake_fetch(url, method="GET", data=None, files=None, **kwargs):
        if url == "https://up.example/photo":
            return FakeResponse({"photo": "ph", "server": "1", "hash": "h"})
        if "photos.saveWallPhoto" in url:
            return FakeResponse({"response": [{"id": 77}]})
        if "wall.createComment" in url:
            assert data["post_id"] == "9"
            assert data["message"] == "reply text"
            assert data["attachments"] == "photo42_77"
            return FakeResponse({"response": {"comment_id": 501}})
        raise AssertionError(url)

    originals = (vk.get_json, vk.fetch, vk_module.read_or_fetch, vk_module.public_url)
    vk.get_json = fake_get_json
    vk.fetch = fake_fetch
    vk_module.read_or_fetch = lambda path: b"pngbytes"
    vk_module.public_url = lambda path: path
    try:
        integration = SimpleNamespace(internalId="42")
        media = [MediaContent(type="image", path="https://cdn.example.com/pic.png")]
        comment = vk.comment(
            "42", "tok", PostDetails(id="p1", message="reply text", media=media), "9", integration
        )
        assert comment.status == "success"
        assert comment.postId == "501"
        assert comment.releaseURL == "https://vk.com/feed?w=wall42_9"
    finally:
        vk.get_json, vk.fetch, vk_module.read_or_fetch, vk_module.public_url = originals
    print("OK vk createComment + api error classification")




def test_kick_auth_and_refresh():
    import base64 as _b64
    import hashlib as _hashlib

    result = kick.generate_auth_url()
    url = result["url"]
    assert "https://id.kick.com/oauth/authorize" in url
    assert "code_challenge_method=S256" in url
    assert f"&state={result['state']}" in url
    digest = _hashlib.sha256(result["codeVerifier"].encode()).digest()
    expected = _b64.b64encode(digest).decode().rstrip("=").replace("+", "-").replace("/", "_")
    assert f"&code_challenge={expected}" in url
    assert "redirectmeto.com" not in url
    assert "chat%3Awrite" in url or "chat:write" in url

    from app.core.config import settings as cfg

    def fake_get_json(url, method="GET", headers=None, params=None, data=None, json=None, **kwargs):
        if url.endswith("/oauth/token"):
            assert method == "POST"
            assert data["client_id"] == cfg.kick_client_id
            assert data["client_secret"] == cfg.kick_client_secret
            if data["grant_type"] == "authorization_code":
                assert data["code"] == "abc"
                assert data["code_verifier"] == "verif"
                assert data["redirect_uri"].endswith("/integrations/social/kick")
                return {"access_token": "tok1", "refresh_token": "ref1", "expires_in": 3600}
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "ref1"
            return {"access_token": "tok2", "refresh_token": "ref2", "expires_in": 7200}
        if url.endswith("/public/v1/users"):
            assert headers["Authorization"] == "Bearer tok1" or headers["Authorization"] == "Bearer tok2"
            return {"data": [{"user_id": 99, "name": "Ann", "profile_picture": "https://img/a.png"}]}
        raise AssertionError(url)

    original = kick.get_json
    kick.get_json = fake_get_json
    try:
        details = kick.authenticate("abc", code_verifier="verif")
        assert details.id == "99"
        assert details.name == "Ann"
        assert details.username == "Ann"
        assert details.accessToken == "tok1"
        assert details.refreshToken == "ref1"
        assert details.expiresIn == 3600
        assert details.picture == "https://img/a.png"

        refreshed = kick.refresh_token("ref1")
        assert refreshed.accessToken == "tok2"
        assert refreshed.refreshToken == "ref2"
        assert refreshed.expiresIn == 7200

        kick.get_json = lambda url, **kwargs: {"error": "invalid_grant"} if url.endswith("/oauth/token") else (_ for _ in ()).throw(AssertionError(url))
        try:
            kick.authenticate("abc", code_verifier="v")
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "access token" in exc.message

        refreshed = kick.refresh_token("ref1")
        assert refreshed.accessToken == ""
        assert refreshed.refreshToken == ""
    finally:
        kick.get_json = original
    print("OK kick auth url (PKCE) + exchange + refresh")


def test_kick_chat_post():
    calls = []

    def fake_get_json(url, method="GET", headers=None, data=None, json=None, **kwargs):
        calls.append((url, json))
        if url.endswith("/public/v1/chat"):
            assert headers["Authorization"] == "Bearer tok"
            assert json["type"] == "user"
            assert json["broadcaster_user_id"] == 99
            assert "reply_to_message_id" not in json
            assert json["content"] == ("x" * 500)
            return {"data": {"message_id": 777, "is_sent": True}}
        raise AssertionError(url)

    original = kick.get_json
    kick.get_json = fake_get_json
    try:
        integration = SimpleNamespace(username="Ann")
        out = kick.post("99", "tok", [PostDetails(id="p1", message="x" * 600)], integration)
        assert len(out) == 1
        assert out[0].status == "success"
        assert out[0].postId == "777"
        assert out[0].releaseURL == "https://kick.com/Ann"

        try:
            kick.post(
                "99",
                "tok",
                [PostDetails(id="p2", message="hi", media=[MediaContent(type="image", path="https://cdn/a.png")])],
                integration,
            )
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "media" in exc.message

        kick.get_json = lambda url, **kwargs: {"data": {"message_id": 888, "is_sent": False}}
        try:
            kick.post("99", "tok", [PostDetails(id="p3", message="hi")], integration)
            raise AssertionError("expected BadBody")
        except BadBody:
            pass

        kick.get_json = lambda url, **kwargs: {"data": {}}
        try:
            kick.post("99", "tok", [PostDetails(id="p4", message="hi")], integration)
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "message id" in exc.message

        kick.get_json = lambda url, **kwargs: {"message_id": 999}
        out = kick.post("99", "tok", [PostDetails(id="p5", message="hi")], integration)
        assert out[0].postId == "999"
    finally:
        kick.get_json = original
    print("OK kick chat post truncation + guards")


def test_kick_comment_reply():
    captured = {}

    def fake_get_json(url, method="GET", json=None, headers=None, **kwargs):
        captured.update(json)
        return {"data": {"message_id": 1001, "is_sent": True}}

    original = kick.get_json
    kick.get_json = fake_get_json
    try:
        integration = SimpleNamespace(username="Ann")
        comment = kick.comment("99", "tok", PostDetails(id="p1", message="reply!"), "1000", integration)
        assert comment.status == "success"
        assert comment.postId == "1001"
        assert comment.releaseURL == "https://kick.com/Ann"
        assert captured["reply_to_message_id"] == "1000"
        assert captured["broadcaster_user_id"] == 99
        assert captured["content"] == "reply!"

        comment = kick.comment("99", "tok", PostDetails(id="p2", message="hi"), "", integration)
        assert comment.postId == "1001"

        try:
            kick.comment(
                "99",
                "tok",
                PostDetails(id="p3", message="hi", media=[MediaContent(type="video", path="https://cdn/a.mp4")]),
                "1000",
                integration,
            )
            raise AssertionError("expected BadBody")
        except BadBody:
            pass
    finally:
        kick.get_json = original
    print("OK kick comment reply_to + media guard")




def test_twitch_auth_and_refresh():
    from app.core.config import settings as cfg

    result = twitch.generate_auth_url()
    url = result["url"]
    assert "https://id.twitch.tv/oauth2/authorize" in url
    assert f"&state={result['state']}" in url
    assert "redirectmeto.com" not in url
    assert "user%3Awrite%3Achat" in url
    assert "code_challenge" not in url
    assert result["codeVerifier"]

    def fake_get_json(url, method="GET", headers=None, params=None, data=None, json=None, **kwargs):
        if url.endswith("/oauth2/token"):
            assert method == "POST"
            assert data["client_id"] == cfg.twitch_client_id
            assert data["client_secret"] == cfg.twitch_client_secret
            assert "code_verifier" not in data
            if data["grant_type"] == "authorization_code":
                assert data["code"] == "abc"
                assert data["redirect_uri"].endswith("/integrations/social/twitch")
                return {"access_token": "tok1", "refresh_token": "ref1", "expires_in": 3600}
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "ref1"
            return {"access_token": "tok2", "refresh_token": "ref2", "expires_in": 7200}
        if url.endswith("/helix/users"):
            assert headers["Authorization"] == "Bearer tok1" or headers["Authorization"] == "Bearer tok2"
            assert headers["Client-Id"] == cfg.twitch_client_id
            return {
                "data": [
                    {
                        "id": 555,
                        "display_name": "Ann",
                        "login": "ann",
                        "profile_image_url": "https://img/a.png",
                    }
                ]
            }
        raise AssertionError(url)

    original = twitch.get_json
    twitch.get_json = fake_get_json
    try:
        details = twitch.authenticate("abc")
        assert details.id == "555"
        assert details.name == "Ann"
        assert details.username == "ann"
        assert details.accessToken == "tok1"
        assert details.refreshToken == "ref1"
        assert details.expiresIn == 3600
        assert details.picture == "https://img/a.png"

        refreshed = twitch.refresh_token("ref1")
        assert refreshed.accessToken == "tok2"
        assert refreshed.refreshToken == "ref2"
        assert refreshed.expiresIn == 7200

        twitch.get_json = lambda url, **kwargs: {"error": "invalid_grant"} if url.endswith("/oauth2/token") else (_ for _ in ()).throw(AssertionError(url))
        try:
            twitch.authenticate("abc")
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "access token" in exc.message

        refreshed = twitch.refresh_token("ref1")
        assert refreshed.accessToken == ""
        assert refreshed.refreshToken == ""
    finally:
        twitch.get_json = original
    print("OK twitch auth url + exchange + refresh")


def test_twitch_chat_and_reply():
    delay = twitch_module.POST_DELAY_SECONDS
    twitch_module.POST_DELAY_SECONDS = 0
    captured = {}

    def fake_get_json(url, method="GET", headers=None, json=None, **kwargs):
        captured.clear()
        captured.update(json)
        return {"data": [{"message_id": "m1", "is_sent": True}]}

    original = twitch.get_json
    twitch.get_json = fake_get_json
    try:
        integration = SimpleNamespace(username="ann", providerIdentifier="twitch")
        out = twitch.post("555", "tok", [PostDetails(id="p1", message="x" * 600)], integration)
        assert out[0].status == "success"
        assert out[0].postId == "m1"
        assert out[0].releaseURL == "https://twitch.tv/ann"
        assert captured["broadcaster_id"] == "555"
        assert captured["sender_id"] == "555"
        assert captured["message"] == "x" * 500
        assert "reply_parent_message_id" not in captured

        comment = twitch.comment("555", "tok", PostDetails(id="p2", message="hi"), "m0", integration)
        assert comment.postId == "m1"
        assert comment.releaseURL == "https://twitch.tv/ann"
        assert captured["reply_parent_message_id"] == "m0"

        twitch.get_json = lambda url, **kwargs: {"data": [{"message_id": "m2", "is_sent": False}]}
        try:
            twitch.post("555", "tok", [PostDetails(id="p3", message="hi")], integration)
            raise AssertionError("expected BadBody")
        except BadBody:
            pass

        twitch.get_json = lambda url, **kwargs: {"data": [{"is_sent": True}]}
        out = twitch.post("555", "tok", [PostDetails(id="p4", message="hi")], integration)
        assert len(out[0].postId) == 10

        try:
            twitch.post(
                "555",
                "tok",
                [PostDetails(id="p5", message="hi", media=[MediaContent(type="image", path="https://cdn/a.png")])],
                integration,
            )
            raise AssertionError("expected BadBody")
        except BadBody as exc:
            assert "media" in exc.message

        integration.username = ""
        twitch.get_json = lambda url, **kwargs: {"data": [{"message_id": "m3", "is_sent": True}]}
        out = twitch.post("555", "tok", [PostDetails(id="p6", message="hi")], integration)
        assert out[0].releaseURL == "https://twitch.tv/twitch"
    finally:
        twitch.get_json = original
        twitch_module.POST_DELAY_SECONDS = delay
    print("OK twitch chat message + reply + guards")


def test_twitch_announcement():
    delay = twitch_module.POST_DELAY_SECONDS
    twitch_module.POST_DELAY_SECONDS = 0
    calls = []

    def fake_fetch(url, method="GET", headers=None, json=None, **kwargs):
        calls.append((url, method, headers, json))
        if "chat/announcements" in url:
            assert method == "POST"
            assert headers["Content-Type"] == "application/json"
            return FakeResponse({})
        raise AssertionError(url)

    original = twitch.fetch
    twitch.fetch = fake_fetch
    try:
        integration = SimpleNamespace(username="ann", providerIdentifier="twitch")
        out = twitch.post(
            "555",
            "tok",
            [PostDetails(id="p1", message="big news", settings={"messageType": "announcement", "announcementColor": "green"})],
            integration,
        )
        assert out[0].status == "success"
        assert len(out[0].postId) == 10
        assert out[0].releaseURL == "https://twitch.tv/ann"
        url, method, headers, body = calls[0]
        assert "broadcaster_id=555" in url
        assert "moderator_id=555" in url
        assert body == {"message": "big news", "color": "green"}

        twitch.post(
            "555",
            "tok",
            [PostDetails(id="p2", message="hi", settings={"messageType": "announcement", "announcementColor": "nope"})],
            integration,
        )
        assert calls[1][3] == {"message": "hi", "color": "primary"}

        comment = twitch.comment(
            "555",
            "tok",
            PostDetails(id="p3", message="bye", settings={"messageType": "announcement"}),
            "m1",
            integration,
        )
        assert comment.status == "success"
        assert calls[2][3]["color"] == "primary"
    finally:
        twitch.fetch = original
        twitch_module.POST_DELAY_SECONDS = delay
    print("OK twitch announcement endpoint + colors")


if __name__ == "__main__":
    test_discord_error_codes()
    test_telegram_text_conversion()
    test_telegram_media_kind()
    test_telegram_api_error_mapping()
    test_telegram_release_url()
    test_discord_content_conversion()
    test_reddit_auth_url_and_subreddit()
    test_reddit_pending_handshake()
    test_slack_oauth_blocks_errors()
    test_mastodon_instance_and_pending()
    test_pinterest_auth_url_and_errors()
    test_pinterest_pending_handshake()
    test_pinterest_media_rules()
    test_youtube_auth_url_and_errors()
    test_youtube_resumable_flow()
    test_facebook_auth_and_errors()
    test_facebook_pages_connections()
    test_facebook_story_handshake()
    test_facebook_feed_post()
    test_instagram_auth_and_errors()
    test_instagram_carousel_flow()
    test_instagram_connections()
    test_x_oauth1_signature()
    test_x_auth_flow()
    test_x_errors_and_text()
    test_x_pending_handshake()
    test_x_media_upload_and_status()
    test_x_article_content_state()
    test_tiktok_auth_and_errors()
    test_tiktok_auth_exchange()
    test_tiktok_post_bodies()
    test_tiktok_media_rules()
    test_tiktok_pending_handshake()
    test_tiktok_photo_publish_and_upload_chunks()
    test_bluesky_credentials_auth()
    test_bluesky_facets_and_media_rules()
    test_bluesky_pending_handshake()
    test_bluesky_video_job()
    test_bluesky_failure_paths()
    test_bluesky_reduce_image()
    test_threads_auth_and_errors()
    test_threads_text_handshake()
    test_threads_carousel_and_comment()
    test_tumblr_auth_and_errors()
    test_tumblr_content_blocks()
    test_tumblr_post_and_connections()
    test_vk_auth_and_refresh()
    test_vk_upload_and_post()
    test_vk_comment_and_errors()
    test_kick_auth_and_refresh()
    test_kick_chat_post()
    test_kick_comment_reply()
    test_twitch_auth_and_refresh()
    test_twitch_chat_and_reply()
    test_twitch_announcement()
    print("\nALL PROVIDER TESTS PASSED")
