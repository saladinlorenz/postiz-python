from app.integrations.social import PROVIDERS
from app.integrations.base import SocialAbstract


class IntegrationManager:
    def __init__(self) -> None:
        self._providers: dict[str, SocialAbstract] = {
            identifier: cls() for identifier, cls in PROVIDERS.items()
        }

    def get(self, identifier: str) -> SocialAbstract:
        provider = self._providers.get(identifier)
        if not provider:
            raise KeyError(f"Unknown provider: {identifier}")
        return provider

    def all(self) -> list[SocialAbstract]:
        return list(self._providers.values())


manager = IntegrationManager()
