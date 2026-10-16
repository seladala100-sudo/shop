import time
from datetime import datetime

import aiohttp

from ..config import MARZBAN_PASS, MARZBAN_PROTOCOL, MARZBAN_URL, MARZBAN_USER


def enabled() -> bool:
    return bool(MARZBAN_URL and MARZBAN_USER and MARZBAN_PASS)


async def _token(http) -> str:
    async with http.post(f"{MARZBAN_URL}/api/admin/token",
                         data={"username": MARZBAN_USER, "password": MARZBAN_PASS}) as r:
        r.raise_for_status()
        return (await r.json())["access_token"]


def _full(url: str) -> str:
    return url if url.startswith("http") else MARZBAN_URL + url


async def issue(username: str, days: int, gb: int) -> tuple[str, datetime]:
    """Создаёт пользователя в Marzban или продлевает существующего. Возвращает (ссылка подписки, дата окончания)."""
    limit = gb * 1024 ** 3 if gb else 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as http:
        http.headers.update({"Authorization": f"Bearer {await _token(http)}"})
        now = int(time.time())
        body = {"username": username, "proxies": {MARZBAN_PROTOCOL: {}}, "inbounds": {},
                "expire": now + days * 86400, "data_limit": limit,
                "data_limit_reset_strategy": "no_reset", "status": "active", "note": "telegram-bot"}
        async with http.post(f"{MARZBAN_URL}/api/user", json=body) as r:
            if r.status == 409:  # уже есть — продлеваем
                async with http.get(f"{MARZBAN_URL}/api/user/{username}") as g:
                    g.raise_for_status()
                    cur = await g.json()
                base = max(now, cur.get("expire") or 0)
                body = {"expire": base + days * 86400, "data_limit": limit, "status": "active"}
                async with http.put(f"{MARZBAN_URL}/api/user/{username}", json=body) as p:
                    p.raise_for_status()
                    res = await p.json()
            else:
                r.raise_for_status()
                res = await r.json()
    return _full(res["subscription_url"]), datetime.utcfromtimestamp(res["expire"])
