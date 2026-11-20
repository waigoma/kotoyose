"""In-memory client API keys for HTTP tests using synthetic credentials."""

import asyncio
from unittest.mock import patch

from kotoyose.api_key_store import ApiKeyStore


async def fixture_key_store(**bindings):
    """Issue deterministic keys named by client ID through the real store."""
    store = ApiKeyStore()
    for client, suffix in bindings.items():
        with patch("kotoyose.api_key_store.secrets.token_urlsafe", return_value=suffix):
            await store.create(client, client)
    return store


def sync_fixture_key_store(**bindings):
    return asyncio.run(fixture_key_store(**bindings))


TEST_API_HEADERS = {"Authorization": "Bearer kys_test-api-key"}
