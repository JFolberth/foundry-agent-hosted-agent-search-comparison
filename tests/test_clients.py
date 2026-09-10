from contextlib import AsyncExitStack

from azure.core.credentials import AccessToken

from comparison.clients import open_clients
from comparison.config import Settings


class Credential:
    async def get_token(self, *scopes, **kwargs):
        return AccessToken("test-only-not-a-real-token", 9999999999)

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


async def test_open_clients_includes_all_configured_sides(settings, monkeypatch):
    extended = Settings.model_validate({
        **settings.model_dump(),
        "prompt_agent_none": "prompt-search-none",
        "hosted_agent_none": "hosted-search-none",
    })
    monkeypatch.setattr("comparison.clients.ManagedIdentityCredential", lambda **kwargs: Credential())
    async with AsyncExitStack() as stack:
        clients = await open_clients(stack, extended)
    assert set(clients) == {"prompt", "hosted", "prompt_none", "hosted_none"}


async def test_open_clients_skips_unconfigured_none_sides(settings, monkeypatch):
    monkeypatch.setattr("comparison.clients.ManagedIdentityCredential", lambda **kwargs: Credential())
    async with AsyncExitStack() as stack:
        clients = await open_clients(stack, settings)
    assert set(clients) == {"prompt", "hosted"}
