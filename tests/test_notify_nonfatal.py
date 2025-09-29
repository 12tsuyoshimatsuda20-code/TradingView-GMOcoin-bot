import asyncio
import types

import pytest

from app.notify import notify_discord


class DummyResp:
    def __init__(self, status_code=429, text="rate"):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        raise Exception("HTTP error")


@pytest.mark.asyncio
async def test_notify_discord_nonfatal(monkeypatch, capsys):
    # httpx.AsyncClient.post を強制的に失敗させる
    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json):
            raise Exception("boom")

    import app.notify as n

    monkeypatch.setattr(n.httpx, "AsyncClient", lambda timeout: DummyClient())

    # 失敗しても例外を外へ投げない（非致命）
    await notify_discord("https://discord.invalid/webhook", "t", "d", "gray")
    # ここまで例外が来なければOK
