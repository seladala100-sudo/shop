import aiohttp

from ..config import CRYPTO_TESTNET, CRYPTO_TOKEN

BASE = "https://testnet-pay.crypt.bot/api" if CRYPTO_TESTNET else "https://pay.crypt.bot/api"


def enabled() -> bool:
    return bool(CRYPTO_TOKEN)


async def _call(method: str, **params):
    async with aiohttp.ClientSession(headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN}) as http:
        async with http.post(f"{BASE}/{method}", json=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(f"CryptoPay error: {data}")
    return data["result"]


async def create_invoice(amount_rub, description: str, payload: str) -> dict:
    return await _call("createInvoice", currency_type="fiat", fiat="RUB", amount=str(amount_rub),
                       description=description[:1000], payload=payload, expires_in=3600)


async def get_invoices(ids: list[int]) -> list[dict]:
    res = await _call("getInvoices", invoice_ids=",".join(map(str, ids)))
    return res["items"] if isinstance(res, dict) else res
