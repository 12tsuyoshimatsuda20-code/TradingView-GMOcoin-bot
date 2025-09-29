import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("WEBHOOK_TOKEN", "SECRETxxyyzz")
os.environ.setdefault("GMO_API_KEY", "A" * 32)
os.environ.setdefault("GMO_API_SECRET", "B" * 64)
os.environ.setdefault("NOTIFY_DISCORD_WEBHOOK_URL", "")
os.environ.setdefault("DRY_RUN", "1")


import importlib

app_module = importlib.import_module("app.app")


class DummyResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status = status_code
        self._payload = payload

    async def json(self):
        return deepcopy(self._payload)


def make_dummy_client(responses):
    class DummyClient:
        def __init__(self, *args, **kwargs):
            self._responses_ref = responses
            self._queue = None
            self.requests = []

        async def request(self, method, path, params=None, json=None):
            if self._queue is None:
                self._queue = [deepcopy(item) for item in self._responses_ref]
            if not self._queue:
                raise AssertionError(f"no stub response for {method} {path}")
            expected = self._queue.pop(0)
            assert expected["method"] == method
            assert expected["path"] == path
            self.requests.append({"method": method, "path": path, "params": params, "json": json})
            return DummyResponse(expected["status"], expected["body"])

        async def close(self):
            return None

    return DummyClient


@pytest.fixture
def client(monkeypatch):
    responses: list[dict] = []

    def set_responses(items):
        responses.clear()
        responses.extend(items)

    monkeypatch.setattr(
        app_module,
        "pybotters",
        SimpleNamespace(Client=lambda *args, **kwargs: make_dummy_client(responses)(*args, **kwargs)),
    )

    test_client = TestClient(app_module.app)
    test_client.set_stub_responses = set_responses  # type: ignore[attr-defined]
    try:
        yield test_client
    finally:
        test_client.close()


def _timestamp(seconds_delta: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_entry_success(client):
    client.set_stub_responses(
        [
            {
                "method": "GET",
                "path": "/private/v1/openPositions",
                "status": 200,
                "body": {"status": 0, "data": {"list": []}},
            },
            {
                "method": "POST",
                "path": "/private/v1/order",
                "status": 200,
                "body": {"status": 0, "data": {"orderId": "123"}},
            },
        ]
    )

    payload = {
        "token": "SECRETxxyyzz",
        "event_id": "test-entry-1",
        "ts": _timestamp(),
        "mode": "ENTRY",
        "symbol": "BTC_JPY",
        "side": "BUY",
        "size": 0.04,
    }
    response = client.post("/webhook", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "executed"
    assert data["event_id"] == "test-entry-1"


def test_close_no_position(client):
    client.set_stub_responses(
        [
            {
                "method": "GET",
                "path": "/private/v1/openPositions",
                "status": 200,
                "body": {"status": 0, "data": {"list": []}},
            }
        ]
    )

    payload = {
        "token": "SECRETxxyyzz",
        "event_id": "test-close-1",
        "ts": _timestamp(),
        "mode": "CLOSE",
        "symbol": "BTC_JPY",
    }
    response = client.post("/webhook", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "noop"
    assert data["detail"] == "already flat"


def test_duplicate_event(client):
    client.set_stub_responses(
        [
            {
                "method": "GET",
                "path": "/private/v1/openPositions",
                "status": 200,
                "body": {"status": 0, "data": {"list": []}},
            },
            {
                "method": "POST",
                "path": "/private/v1/order",
                "status": 200,
                "body": {"status": 0, "data": {"orderId": "123"}},
            },
        ]
    )

    payload = {
        "token": "SECRETxxyyzz",
        "event_id": "duplicate-1",
        "ts": _timestamp(),
        "mode": "ENTRY",
        "symbol": "BTC_JPY",
        "side": "BUY",
        "size": 0.04,
    }
    first = client.post("/webhook", json=payload)
    assert first.status_code == 200
    second = client.post("/webhook", json=payload)
    assert second.status_code == 200
    data = second.json()
    assert data["status"] == "duplicate"
    assert data["duplicate"] is True
