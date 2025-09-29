import asyncio
import hmac
import hashlib

import pytest

from app.infra.gmocoin_client import GMOCoinClient


@pytest.mark.asyncio
async def test_signature_generation_matches_spec():
    api_key = "dummy"
    api_secret = "abcdef1234567890"
    client = GMOCoinClient(api_key, api_secret)
    timestamp = "1700000000000"
    method = "POST"
    path = "/private/v1/orders"
    body = "{\"symbol\":\"BTC_JPY\",\"size\":\"0.01000000\"}"

    expected = hmac.new(
        api_secret.encode(), f"{timestamp}{method}{path}{body}".encode(), hashlib.sha256
    ).hexdigest()

    signature = client._create_signature(timestamp, method, path, body)
    await client.close()
    assert signature == expected
