import io
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image

from app.core.config import settings
from app.integrations.base import BadBody

MEDIA_READ_TIMEOUT = 120.0
LINKEDIN_MAX_PIXELS = 6000


def is_url(path: str) -> bool:
    return urlparse(path).scheme in ("http", "https")


def resolve_local(path: str) -> str:
    if os.path.isabs(path):
        return path
    return str(Path(settings.upload_directory) / path)


def public_url(path: str) -> str:
    if is_url(path):
        return path
    return f"{settings.backend_url.rstrip('/')}/media/public/{path.lstrip('/')}"


def read_or_fetch(path: str) -> bytes:
    if is_url(path):
        try:
            with httpx.Client(timeout=MEDIA_READ_TIMEOUT, follow_redirects=True) as client:
                response = client.get(path)
                response.raise_for_status()
                return response.content
        except httpx.HTTPError as exc:
            raise BadBody(f"Failed to fetch media {path}", str(exc))
    try:
        with open(resolve_local(path), "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise BadBody(f"Failed to read media {path}", str(exc))


def media_size(path: str) -> int:
    if is_url(path):
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                response = client.head(path)
                return int(response.headers.get("content-length", 0))
        except (httpx.HTTPError, ValueError):
            return 0
    try:
        return os.path.getsize(resolve_local(path))
    except OSError:
        return 0


def media_chunk(path: str, start: int, end: int) -> bytes:
    if is_url(path):
        try:
            with httpx.Client(timeout=MEDIA_READ_TIMEOUT, follow_redirects=True) as client:
                response = client.get(path, headers={"Range": f"bytes={start}-{end}"})
                if response.status_code not in (200, 206):
                    raise BadBody(f"Range read failed for {path}", str(response.status_code))
                return response.content
        except httpx.HTTPError as exc:
            raise BadBody(f"Range read failed for {path}", str(exc))
    with open(resolve_local(path), "rb") as handle:
        handle.seek(start)
        return handle.read(end - start + 1)


def image_dimensions(path: str) -> tuple[int, int]:
    raw = read_or_fetch(path)
    with Image.open(io.BytesIO(raw)) as image:
        return image.size


def prepare_image_buffer(path: str) -> bytes:
    raw = read_or_fetch(path)
    lower = path.lower().split("?")[0]
    is_gif = lower.endswith(".gif")
    keep_format = lower.endswith(".png") or lower.endswith(".jpg") or lower.endswith(".jpeg")

    if is_gif:
        return raw

    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGB") if not keep_format else image
        image.thumbnail((LINKEDIN_MAX_PIXELS, LINKEDIN_MAX_PIXELS), Image.LANCZOS)
        buffer = io.BytesIO()
        if keep_format and image.format in ("PNG", "JPEG", "MPO"):
            image.save(buffer, format=image.format)
        else:
            image.convert("RGB").save(buffer, format="JPEG", quality=90)
        return buffer.getvalue()
