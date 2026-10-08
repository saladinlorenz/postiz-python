from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class AuthTokenDetails:
    id: str
    name: str
    accessToken: str
    refreshToken: str = ""
    expiresIn: int | None = None
    picture: str = ""
    username: str = ""
    additionalSettings: dict[str, Any] = field(default_factory=dict)


@dataclass
class MediaContent:
    type: Literal["image", "video"]
    path: str
    alt: str = ""
    thumbnail: str = ""
    thumbnailTimestamp: int = 0


@dataclass
class PollDetails:
    choices: list[str]
    duration: int = 0


@dataclass
class PostDetails:
    id: str
    message: str
    media: list[MediaContent] = field(default_factory=list)
    poll: PollDetails | None = None
    sensitive: bool = False
    quote: str = ""
    thread: bool = False
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class PostResponse:
    id: str
    status: Literal["success", "failed", "pending"]
    postId: str = ""
    releaseURL: str = ""
    pendingData: Any = None


@dataclass
class PendingCheckResponse:
    status: Literal["pending", "ready", "completed", "failed"]
    pendingData: Any = None
    postId: str = ""
    releaseURL: str = ""
    error: str = ""


class Authenticator(Protocol):
    identifier: str
    name: str
    picture: str
    description: str
    maxLength: int
    credentials: bool

    def generate_auth_url(self, refresh: bool = False) -> dict[str, Any]: ...

    def authenticate(self, code: str, code_verifier: str = "", refresh: str = "", **kwargs: Any) -> AuthTokenDetails: ...

    def refresh_token(self, refresh_token: str) -> AuthTokenDetails: ...
