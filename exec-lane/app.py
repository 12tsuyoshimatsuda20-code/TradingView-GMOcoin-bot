from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import time, os, hmac, hashlib, json
from collections import OrderedDict
import httpx
from typing import Optional
from datetime import datetime, timezone

# ====== Config / Env ======
START = time.time()
TZ = os.getenv("TZ", "UTC")
TOKEN = os.getenv("TOKEN", "")
SYMBOL_WHITELIST = set(os.getenv("SYMBOL_WHITELIST", "BTC_JPY").split(","))
MAX_SIZE = float(os.getenv("MAX_SIZE", "0.01"))
COOLDOWN_MS = int(os.getenv("COOLDOWN_MS", "500"))
KILL_SWITCH = os.getenv("KILL_SWITCH", "0") == "1"

GMO_ENDPOINT = os.getenv("GMO_API_BASE", "https://api.coin.z.com/private")
GMO_KEY = os.getenv("GMO_API_KEY", "")
GMO_SECRET = os.getenv("GMO_API_SECRET", "")

# SLO for /status (p95)
LATENCY_P95_MAX_MS = int(os.getenv("LATENCY_P95_MAX_MS", "0")) or None

# ====== Metrics / State ======
METRICS = {
    "recv_total": 0,
    "exec_attempts": 0,
    "exec_success": 0,
    "exec_failed": 0,
    "idempotent_skipped": 0,
    "latency_ms": []
}
LAST_ERRORS = []
LAST_EVENT_AT = 0.0

class LRUIdem(OrderedDict):
    def __init__(self, cap=2000):
        super().__init__(); self.cap = cap
    def add(self, k):
        if k in self: return True
        self[k] = True; self.move_to_end(k)
        if len(self) > self.cap: self.popitem(last=False)
        return False
IDEM = LRUIdem()

# ====== Utils ======
def _ts_ms() -> str:
    return str(int(time.time() * 1000))

def _sign(ts: str, method: str, path: str, body_dict: Optional[dict]) -> str:
    body_str = json.dumps(body_dict, separators=(',', ':'), ensure_ascii=False) if body_dict is not None else ""
    text = f"{ts}{method}{path}{body_str}"
    return hmac.new(GMO_SECRET.encode(), text.encode(), hashlib.sha256).hexdigest()

async def _post(path: str, body: dict, timeout=3.0) -> dict:
    ts = _ts_ms(); sign = _sign(ts, "POST", path, body)
    headers = {"Content-Type":"application/json","API-KEY":GMO_KEY,"API-TIMESTAMP":ts,"API-SIGN":sign}
    async with httpx.AsyncClient(timeout=timeout) as cli:
        for i in range(2):  # 1回リトライ
            try:
                r = await cli.post(GMO_ENDPOINT + path, headers=headers, json=body)
                data = r.json()
                if r.status_code == 200 and data.get("status") == 0:
                    return data
                if i == 1:
                    raise RuntimeError(f"POST {path} {r.status_code} {data}")
                await asyncio_sleep(0.15)
            except Exception as e:
                if i == 1: raise
                await asyncio_sleep(0.15)

async def _get(path: str, params: str = "", timeout=3.0) -> dict:
    ts = _ts_ms(); sign = _sign(ts, "GET", path + (params or ""), None)
    headers = {"Content-Type":"application/json","API-KEY":GMO_KEY,"API-TIMESTAMP":ts,"API-SIGN":sign}
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.get(GMO_ENDPOINT + path + (params or ""), headers=headers)
        data = r.json()
        if r.status_code == 200 and data.get("status") == 0:
            return data
        raise RuntimeError(f"GET {path} {r.status_code} {data}")

async def asyncio_sleep(sec: float):
    import asyncio; await asyncio.sleep(sec)

# ====== Discord Notify ======
from .notify import send_discord

# ====== GMO Handlers ======
async def entry_market(symbol: str, side: str, size: str) -> str:
    body = {"symbol":symbol, "side":side, "executionType":"MARKET", "size":size}
    data = await _post("/v1/order", body)
    return data.get("data")

async def close_market_all(symbol: str, position_side: str) -> str:
    close_side = "BUY" if position_side.upper()=="SHORT" else "SELL"
    pos = await _get("/v1/openPositions", f"?symbol={symbol}&page=1&count=100")
    lst = pos.get("data", {}).get("list", [])
    target = None
    for p in lst:
        if p.get("side") == ("BUY" if position_side.upper()=="LONG" else "SELL"):
            target = p; break
    if not target:
        raise RuntimeError("No open position to close")
    body = {
        "symbol": symbol,
        "side": close_side,
        "executionType": "MARKET",
        "settlePosition": {
            "positionId": int(target["positionId"]),
            "size": str(target["size"])  # 全量
        }
    }
    data = await _post("/v1/closeOrder", body)
    return data.get("data")

# ====== FastAPI ======
app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/status")
def status():
    lat = METRICS["latency_ms"]
    def pct(p):
        if not lat: return None
        s = sorted(lat); i = int((len(s)-1)*p); return s[i]
    p50, p95, p99 = pct(0.50), pct(0.95), pct(0.99)

    payload = {
        "uptime_sec": int(time.time()-START),
        "events": {k: v for k, v in METRICS.items() if k != "latency_ms"},
        "latency_ms": {"p50": p50, "p95": p95, "p99": p99},
        "last_errors": LAST_ERRORS[-5:],
        "kill_switch": KILL_SWITCH,
        "cooldown_ms": COOLDOWN_MS,
        "symbol_whitelist": list(SYMBOL_WHITELIST),
        "env": {"tz": TZ, "version": "exec-lane-1.0.0"},
        "slo": {"p95_max_ms": LATENCY_P95_MAX_MS, "breach": bool(LATENCY_P95_MAX_MS and p95 and p95 > LATENCY_P95_MAX_MS)},
    }
    if LATENCY_P95_MAX_MS and p95 and p95 > LATENCY_P95_MAX_MS:
        return JSONResponse(payload, status_code=529)
    return JSONResponse(payload, status_code=200)

# ====== Validation ======
ALLOWED_MODES = {"ENTRY","CLOSE"}
ALLOWED_SIDES = {"BUY","SELL"}
ALLOWED_POS_SIDES = {"LONG","SHORT"}

@app.post("/webhook")
async def webhook(req: Request):
    global LAST_EVENT_AT
    if KILL_SWITCH:
        await send_discord("warn", "EXEC ⚠️ Kill-switch 有効", [{"name":"note","value":"受付拒否"}], "exec-lane 1.0.0")
        raise HTTPException(423, "kill-switch enabled")

    body = await req.json()
    METRICS["recv_total"] += 1

    token = body.get("token"); event_id = body.get("event_id"); ts_iso = body.get("ts")
    symbol = body.get("symbol"); size = body.get("size"); mode = body.get("mode")

    if token != TOKEN:
        METRICS["exec_failed"] += 1
        await send_discord("warn", "EXEC ⚠️ 検証NG", [{"name":"reason","value":"bad token"}], "exec-lane 1.0.0")
        raise HTTPException(401, "bad token")

    if not event_id or IDEM.add(event_id):
        METRICS["idempotent_skipped"] += 1
        return {"accepted": False, "reason": "idempotent"}

    if not ts_iso:
        METRICS["exec_failed"] += 1
        raise HTTPException(422, "missing ts")
    try:
        ts = datetime.fromisoformat(ts_iso.replace('Z','+00:00'))
        now = datetime.now(timezone.utc)
        if abs((now - ts).total_seconds()) > 60:
            METRICS["exec_failed"] += 1
            await send_discord("warn", "EXEC ⚠️ 検証NG", [{"name":"reason","value":"stale ts"}], "exec-lane 1.0.0")
            raise HTTPException(422, "stale ts")
    except Exception:
        METRICS["exec_failed"] += 1
        raise HTTPException(422, "bad ts")

    if symbol not in SYMBOL_WHITELIST:
        METRICS["exec_failed"] += 1
        raise HTTPException(422, "symbol not allowed")

    try:
        fsize = float(size)
        if not (fsize > 0.0 and fsize <= MAX_SIZE):
            raise ValueError
    except Exception:
        METRICS["exec_failed"] += 1
        raise HTTPException(422, "bad size")

    if mode not in ALLOWED_MODES:
        METRICS["exec_failed"] += 1
        raise HTTPException(422, "bad mode")

    # 冷却
    now_s = time.time()
    if (now_s - LAST_EVENT_AT) * 1000 < COOLDOWN_MS:
        METRICS["exec_failed"] += 1
        await send_discord("warn", "EXEC ⚠️ 検証NG", [{"name":"reason","value":"cooldown"}], "exec-lane 1.0.0")
        raise HTTPException(429, "cooldown")
    LAST_EVENT_AT = now_s

    # 実行
    t0 = time.time()
    METRICS["exec_attempts"] += 1
    try:
        if mode == "ENTRY":
            side = (body.get("side") or "").upper()
            if side not in ALLOWED_SIDES:
                raise HTTPException(422, "bad side")
            order_id = await entry_market(symbol, side, str(fsize))
            title = f"EXEC ✅ ENTRY {side} 成行"
        else:
            reason = body.get("reason", "-")
            pos_side = (body.get("position_side") or "").upper()
            if pos_side not in ALLOWED_POS_SIDES:
                raise HTTPException(422, "bad position_side")
            order_id = await close_market_all(symbol, pos_side)
            title = f"EXEC ✅ CLOSE {pos_side} 成行（全量）"

        METRICS["exec_success"] += 1
        METRICS["latency_ms"].append(int((time.time()-t0)*1000))
        fields = [
            {"name":"event_id","value": event_id},
            {"name":"mode","value": mode},
            {"name":"symbol","value": symbol},
            {"name":"size","value": str(fsize)},
            {"name":"order_id","value": str(order_id)},
        ]
        await send_discord("info", title, fields, "exec-lane 1.0.0")
        return {"accepted": True, "order_id": order_id}

    except HTTPException:
        raise
    except Exception as e:
        METRICS["exec_failed"] += 1
        LAST_ERRORS.append({"ts": time.time(), "event_id": event_id, "api_status": "EXCEPTION", "msg": str(e)})
        fields = [
            {"name":"mode","value": mode},
            {"name":"reason","value": body.get("reason", "-")},
            {"name":"position_side","value": body.get("position_side", "-")},
            {"name":"event_id","value": event_id},
            {"name":"api_status","value": "EXCEPTION"},
            {"name":"message","value": str(e)[:512]},
        ]
        footer = "オペレーション：口座の建玉・履歴を確認し、必要なら手動でクローズしてください。"
        await send_discord("error", "EXEC ❌ 失敗（要手動CLOSE）", fields, footer)
        raise HTTPException(500, "exec failed")
