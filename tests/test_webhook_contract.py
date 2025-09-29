from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from app.infra.positions import PositionState
from app.main import Settings, create_app
from app.store import EventStore


class DummyGMOCoinClient:
    def __init__(self) -> None:
        self.orders: list[Dict[str, Any]] = []

    async def place_market_order(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        self.orders.append({"symbol": symbol, "side": side, "size": size})
        return {"status": "0"}

    async def fetch_open_positions(self, symbol: str) -> Dict[str, Any]:
        return {"data": []}

    async def close(self) -> None:  # pragma: no cover - nothing to cleanup in dummy
        return None


class DummyPositionsService:
    def __init__(self) -> None:
        self.state = PositionState("NONE", 0.0)

    async def fetch_state(self, symbol: str) -> PositionState:
        return PositionState(self.state.side, self.state.size)


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings(
        webhook_token="token",
        gmo_api_key="key",
        gmo_api_secret="secret",
        discord_webhook=None,
        symbol="BTC_JPY",
        entry_policy="ignore",
        max_skew_seconds=60,
        qty_step=0.01,
        timezone="Asia/Tokyo",
    )
    store = EventStore(tmp_path / "bot.db")
    gmo = DummyGMOCoinClient()
    positions = DummyPositionsService()
    notifications: list[Dict[str, Any]] = []

    async def dummy_notify(
        webhook_url: str | None,
        title: str,
        description: str,
        color: str = "gray",
        fields: list[dict] | None = None,
        timeout_sec: float = 10.0,
    ) -> None:
        notifications.append(
            {
                "webhook_url": webhook_url,
                "title": title,
                "description": description,
                "color": color,
                "fields": fields or [],
                "timeout": timeout_sec,
            }
        )

    monkeypatch.setattr("app.domain.notify_discord", dummy_notify)
    monkeypatch.setattr("app.main.notify_discord", dummy_notify)

    app = create_app(
        settings=settings,
        store=store,
        gmocoin_client=gmo,
        positions_service=positions,
    )
    with TestClient(app) as test_client:
        yield test_client, gmo, notifications, positions


def _payload(**overrides: Any) -> Dict[str, Any]:
    base = {
        "token": "token",
        "event_id": "test-event",
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "symbol": "BTC_JPY",
        "size": 0.04,
        "mode": "ENTRY",
        "side": "BUY",
        "entry_price_hint": 16660000,
        "tp1_price": 16662000,
        "tp2_price": None,
        "tp3_price": None,
        "interval": "15",
        "exchange": "GMO-LEVERAGE",
    }
    base.update(overrides)
    return base


def test_entry_success(client):
    test_client, gmo, notifications, positions = client
    positions.state = PositionState("NONE", 0.0)
    response = test_client.post(
        "/webhook",
        json=_payload(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["action"] == "entry"
    assert len(gmo.orders) == 1
    assert notifications[0]["title"] == "ENTRY executed"


def test_duplicate_event_returns_duplicate_status(client):
    test_client, gmo, notifications, positions = client
    payload = _payload(event_id="dup-event")
    response = test_client.post(
        "/webhook", json=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    response = test_client.post(
        "/webhook", json=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "duplicate"


def test_invalid_token_is_rejected(client):
    test_client, *_ = client
    response = test_client.post(
        "/webhook",
        json=_payload(token="wrong"),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_timestamp_skew_rejected(client):
    test_client, *_ = client
    past_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat().replace(
        "+00:00", "Z"
    )
    response = test_client.post(
        "/webhook",
        json=_payload(ts=past_ts),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_close_without_position_returns_noop(client):
    test_client, gmo, notifications, positions = client
    positions.state = PositionState("NONE", 0.0)
    payload = _payload(mode="CLOSE", side="SELL", event_id="close-test")
    response = test_client.post(
        "/webhook", json=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "noop"
    assert notifications[-1]["title"] == "CLOSE skipped"
    assert gmo.orders == []


def test_entry_ignored_when_position_exists(client):
    test_client, gmo, notifications, positions = client
    positions.state = PositionState("BUY", 0.04)
    payload = _payload(event_id="ignore-test")
    response = test_client.post(
        "/webhook", json=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "ignored"
    assert notifications[-1]["title"] == "ENTRY ignored"
    assert gmo.orders == []
