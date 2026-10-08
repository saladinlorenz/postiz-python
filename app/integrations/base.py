import base64
import hashlib
import ipaddress
import unicodedata
from typing import Any
from urllib.parse import quote, urlparse

import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BACKOFF_SECONDS = 5.0


def encode_component(value: str) -> str:
    return quote(str(value), safe="-_.!~*'()")


def pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.b64encode(digest).decode().rstrip("=").replace("+", "-").replace("/", "_")


def is_public_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def slugify(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    out: list[str] = []
    for char in text:
        if char.isalnum():
            out.append(char)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


class BaseIntegrationError(Exception):
    type = "unknown"

    def __init__(self, message: str = "", value: str = "", *extra: str):
        self.value = value
        self.message = extra[-1] if extra else message
        self.identifier = message
        super().__init__(self.message or self.type)


class BadBody(BaseIntegrationError):
    type = "bad-body"


class RefreshTokenError(BaseIntegrationError):
    type = "refresh-token"


class Disconnect(BaseIntegrationError):
    type = "disconnect"


class NotEnoughScopes(BaseIntegrationError):
    type = "not-enough-scopes"

    def __init__(self, message: str = "Not enough scopes", value: str = ""):
        super().__init__(message, value)


class SocialAbstract:
    identifier = ""
    name = ""
    picture = ""
    description = ""
    maxLength = 0
    credentials = False
    refreshCron = False
    stripLinks = False
    convertToJPEG = False
    oneTimeToken = False
    isBetweenSteps = False
    maxConcurrentJob = 1
    postSettings: list[dict[str, Any]] = []

    def handle_errors(self, body: Any, status: int) -> tuple[str, str]:
        if status in (401, 403):
            return "refresh-token", str(body)[:500]
        if status in (400, 422):
            return "bad-body", str(body)[:500]
        if status >= 500:
            return "retry", str(body)[:500]
        return "disconnect", str(body)[:500]

    def options(self, key: str, integration: Any) -> list[dict[str, str]]:
        raise ValueError(f"{self.name} does not support {key} options")

    def check_scopes(self, required: list[str], granted: list[str]) -> None:
        missing = [s for s in required if s not in granted]
        if missing:
            raise NotEnoughScopes(f"Missing scopes: {', '.join(missing)}", ",".join(missing))

    def fetch(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        files: Any = None,
        timeout: float = 30.0,
        auth: httpx.Auth | None = None,
    ) -> httpx.Response:
        last_error: BaseIntegrationError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    response = client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        data=data,
                        json=json,
                        files=files,
                        auth=auth,
                    )
            except httpx.TimeoutException as exc:
                last_error = BadBody(f"Timeout on {url}", str(exc))
                continue
            except httpx.HTTPError as exc:
                last_error = BadBody(f"Network error on {url}", str(exc))
                continue

            if response.status_code in RETRY_STATUS:
                last_error = None
                import time

                if attempt < MAX_RETRIES - 1:
                    time.sleep(BACKOFF_SECONDS)
                    continue

            if response.status_code >= 400:
                kind, value = self.handle_errors(_safe_body(response), response.status_code)
                if kind == "refresh-token":
                    raise RefreshTokenError(value, value)
                if kind == "bad-body":
                    raise BadBody(value, value)
                if kind == "retry":
                    raise BadBody(value, value)
                raise Disconnect(value, value)

            return response

        if last_error:
            raise last_error
        raise BadBody(f"Request failed after {MAX_RETRIES} attempts: {url}")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.fetch(url, **kwargs).json()

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.fetch(url, **kwargs).text


def _safe_body(response: httpx.Response) -> str:
    try:
        return response.text[:2000]
    except Exception:
        return ""
