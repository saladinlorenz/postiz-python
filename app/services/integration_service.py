import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Integration
from app.integrations.interfaces import AuthTokenDetails
from app.integrations.manager import manager


def create_or_update_integration(
    db: Session,
    organization_id: str,
    provider_identifier: str,
    details: AuthTokenDetails,
    disabled: bool = False,
) -> Integration:
    provider = manager.get(provider_identifier)
    expires_in = details.expiresIn
    expiration = datetime.utcnow() + timedelta(seconds=expires_in) if expires_in else None

    existing = (
        db.query(Integration)
        .filter(
            Integration.organizationId == organization_id,
            Integration.providerIdentifier == provider_identifier,
            Integration.internalId == details.id,
        )
        .first()
    )
    integration = existing or Integration(
        organizationId=organization_id, providerIdentifier=provider_identifier, internalId=details.id
    )
    integration.name = details.name or integration.name
    integration.picture = details.picture or integration.picture
    integration.username = details.username or integration.username
    integration.token = details.accessToken
    integration.refreshToken = details.refreshToken
    integration.tokenExpiration = expiration
    integration.additionalSettings = json.dumps(details.additionalSettings or {})
    integration.disabled = disabled
    integration.refreshNeeded = False
    if not existing:
        db.add(integration)
    db.commit()
    db.refresh(integration)
    return integration


def needs_refresh(integration: Integration) -> bool:
    if not integration.refreshToken:
        return False
    if not integration.tokenExpiration:
        return False
    margin = timedelta(seconds=settings.refresh_margin_seconds)
    return integration.tokenExpiration - margin <= datetime.utcnow()


def refresh_integration(db: Session, integration: Integration) -> bool:
    provider = manager.get(integration.providerIdentifier)
    previous_refresh = integration.refreshToken
    try:
        details = provider.refresh_token(integration.refreshToken)
    except Exception:
        integration.refreshNeeded = True
        db.commit()
        return False
    if not details.accessToken:
        integration.refreshNeeded = True
        db.commit()
        return False

    expiration = datetime.utcnow() + timedelta(seconds=details.expiresIn) if details.expiresIn else None
    targets = [integration]
    if previous_refresh:
        siblings = (
            db.query(Integration)
            .filter(
                Integration.id != integration.id,
                Integration.organizationId == integration.organizationId,
                Integration.providerIdentifier == integration.providerIdentifier,
                Integration.refreshToken == previous_refresh,
            )
            .all()
        )
        targets.extend(siblings)

    for target in targets:
        target.token = details.accessToken
        target.refreshToken = details.refreshToken or previous_refresh
        target.tokenExpiration = expiration
        target.refreshNeeded = False
    db.commit()
    return True


def list_integrations(db: Session, organization_id: str) -> list[Integration]:
    return (
        db.query(Integration)
        .filter(Integration.organizationId == organization_id)
        .order_by(Integration.createdAt.desc())
        .all()
    )


def serialize_integration(integration: Integration) -> dict:
    provider = manager.get(integration.providerIdentifier)
    return {
        "id": integration.id,
        "providerIdentifier": integration.providerIdentifier,
        "name": integration.name,
        "picture": integration.picture,
        "username": integration.username,
        "disabled": integration.disabled,
        "refreshNeeded": integration.refreshNeeded,
        "inBetweenSteps": integration.inBetweenSteps,
        "tokenExpiration": integration.tokenExpiration.isoformat() if integration.tokenExpiration else None,
        "provider": {
            "name": provider.name,
            "picture": provider.picture,
            "description": provider.description,
            "credentials": provider.credentials,
            "customFields": provider.custom_fields() if provider.credentials else [],
            "maxLength": provider.maxLength,
        },
    }
