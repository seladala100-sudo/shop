import os
from decimal import Decimal


def _ids(v: str) -> list[int]:
    return [int(x) for x in v.replace(" ", "").split(",") if x.strip()]


BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID") or (ADMIN_IDS[0] if ADMIN_IDS else 0))
REVIEWS_CHANNEL_ID = int(os.getenv("REVIEWS_CHANNEL_ID") or 0)

_db = os.environ["DATABASE_URL"]
for _p in ("postgres://", "postgresql://"):
    if _db.startswith(_p):
        _db = "postgresql+asyncpg://" + _db[len(_p):]
        break
DATABASE_URL = _db
REDIS_URL = os.getenv("REDIS_URL", "")

CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN", "")
CRYPTO_TESTNET = os.getenv("CRYPTO_TESTNET", "0") == "1"

MARZBAN_URL = os.getenv("MARZBAN_URL", "").rstrip("/")
MARZBAN_USER = os.getenv("MARZBAN_USER", "")
MARZBAN_PASS = os.getenv("MARZBAN_PASS", "")
MARZBAN_PROTOCOL = os.getenv("MARZBAN_PROTOCOL", "vless")

REF_PERCENT = Decimal(os.getenv("REF_PERCENT", "5"))
TOP_DISCOUNT = Decimal(os.getenv("TOP_DISCOUNT", "5"))
MIN_TOPUP = Decimal(os.getenv("MIN_TOPUP", "50"))
