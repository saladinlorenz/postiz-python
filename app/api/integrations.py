import base64
import json
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.db.database import get_db
from app.db.models import Integration, User
from app.integrations.base import BaseIntegrationError, NotEnoughScopes
from app.integrations.manager import manager
from app.services import integration_service

router = APIRouter(prefix="/integrations", tags=["integrations"])

_state_store: dict[str, dict] = {}
STATE_TTL = 3600


class ConnectBody(BaseModel):
    code: str = ""
    state: str = ""
    refresh: str = ""
    details: dict = {}


def _store_state(state: str, code_verifier: str, extra: dict | None = None) -> None:
    now = time.time()
    for key in [k for k, v in _state_store.items() if v["expires"] < now]:
        del _state_store[key]
    _state_store[state] = {"codeVerifier": code_verifier, "extra": extra or {}, "expires": now + STATE_TTL}


def _take_state(state: str) -> dict:
    entry = _state_store.pop(state, None)
    if not entry or entry["expires"] < time.time():
        raise HTTPException(400, "Invalid or expired state")
    return entry


@router.get("/providers")
def providers(user: User = Depends(get_current_user)):
    return [
        {
            "identifier": provider.identifier,
            "name": provider.name,
            "picture": provider.picture,
            "description": provider.description,
            "credentials": provider.credentials,
            "customFields": provider.custom_fields() if provider.credentials else [],
            "maxLength": provider.maxLength,
            "postSettingsFields": provider.postSettings,
        }
        for provider in manager.all()
    ]


@router.get("/list")
def list_integrations(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [
        integration_service.serialize_integration(integration)
        for integration in integration_service.list_integrations(db, user.organizationId)
    ]


@router.delete("/{integration_id}")
def disconnect(integration_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    integration = db.get(Integration, integration_id)
    if not integration or integration.organizationId != user.organizationId:
        raise HTTPException(404, "Integration not found")
    db.delete(integration)
    db.commit()
    return {"ok": True}


@router.get("/social/{provider}")
def auth_url(provider: str, refresh: str = "", instance: str = "", user: User = Depends(get_current_user)):
    try:
        provider_instance = manager.get(provider)
    except KeyError:
        raise HTTPException(404, "Unknown provider")
    result = provider_instance.generate_auth_url(refresh=refresh, instance=instance)
    _store_state(
        result["state"],
        result.get("codeVerifier", ""),
        {"instanceUrl": instance} if instance else {},
    )
    if provider_instance.credentials:
        return {"state": result["state"], "credentials": True}
    return {"url": result["url"], "state": result["state"]}


@router.post("/social-connect/{provider}")
def social_connect(provider: str, body: ConnectBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        provider_instance = manager.get(provider)
    except KeyError:
        raise HTTPException(404, "Unknown provider")

    code_verifier = ""
    extra: dict = {}
    if not provider_instance.credentials:
        entry = _take_state(body.state)
        code_verifier = entry["codeVerifier"]
        extra = dict(entry.get("extra") or {})

    kwargs = dict(extra)
    if body.details:
        kwargs["details"] = body.details

    try:
        details = provider_instance.authenticate(
            code=body.code, code_verifier=code_verifier, refresh=body.refresh, **kwargs
        )
    except NotEnoughScopes as exc:
        raise HTTPException(409, f"Missing scopes: {exc.value}")
    except BaseIntegrationError as exc:
        raise HTTPException(400, exc.message or "Authentication failed")
    except Exception:
        raise HTTPException(400, "Authentication failed")

    if isinstance(details, str):
        raise HTTPException(400, details)

    builder = getattr(provider_instance, "connections", None)
    try:
        details_list = builder(details) if builder else [details]
    except NotEnoughScopes as exc:
        raise HTTPException(409, f"Missing scopes: {exc.value}")
    except Exception as exc:
        raise HTTPException(400, str(exc))

    if not details_list:
        raise HTTPException(400, "No channels found on this account")

    integrations = [
        integration_service.create_or_update_integration(db, user.organizationId, provider, detail)
        for detail in details_list
    ]
    return [integration_service.serialize_integration(integration) for integration in integrations]


class TelegramPollBody(BaseModel):
    word: str
    botToken: str
    offset: int | None = None


@router.post("/telegram/poll")
def telegram_poll(body: TelegramPollBody, user: User = Depends(get_current_user)):
    from app.integrations.social.telegram import TelegramProvider

    provider = TelegramProvider()
    try:
        return provider.get_bot_id(body.botToken, body.word, body.offset)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@router.get("/{integration_id}/channels")
def list_channels(
    integration_id: str,
    key: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    integration = db.get(Integration, integration_id)
    if not integration or integration.organizationId != user.organizationId:
        raise HTTPException(404, "Integration not found")
    provider = manager.get(integration.providerIdentifier)
    if key:
        try:
            return provider.options(key, integration)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, str(exc))
    if not hasattr(provider, "channels"):
        raise HTTPException(400, f"{provider.name} does not support listing channels")
    try:
        return provider.channels(integration)
    except Exception as exc:
        raise HTTPException(400, str(exc))
