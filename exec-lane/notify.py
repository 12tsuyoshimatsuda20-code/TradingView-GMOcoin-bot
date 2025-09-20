import os, time
import httpx
from typing import List, Dict, Optional

COLORS = {"info": 5763719, "warn": 16776960, "error": 15548997}
LEVEL_ORDER = {"info":0, "warn":1, "error":2}

WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
ROLE_ID = os.getenv("DISCORD_MENTION_ROLE_ID", "").strip()
MIN_LEVEL = os.getenv("DISCORD_NOTIFY_LEVEL", "info")

async def send_discord(level: str, title: str, fields: List[Dict], footer: str = "", ts_iso: Optional[str] = None):
    if not WEBHOOK:
        return
    if LEVEL_ORDER.get(level,0) < LEVEL_ORDER.get(MIN_LEVEL,0):
        return

    color = COLORS.get(level, COLORS["info"])
    ts = ts_iso or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    content = f"<@&{ROLE_ID}> 要確認" if (level=="error" and ROLE_ID) else None

    payload = {
        "username": "exec-lane",
        "content": content,
        "embeds": [{
            "title": title,
            "color": color,
            "fields": fields[:20],
            "footer": {"text": footer[:2048]},
            "timestamp": ts
        }]
    }

    async with httpx.AsyncClient(timeout=3.0) as cli:
        r = await cli.post(WEBHOOK, json=payload)
        if r.status_code >= 300:
            fb = {"content": (content+"\n" if content else "") + title}
            await cli.post(WEBHOOK, json=fb)
