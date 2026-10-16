import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from aiogram import Bot
from sqlalchemy import select, update

from ..config import REF_PERCENT
from ..db import Payment, Session, User
from ..utils import money
from . import cryptopay

log = logging.getLogger(__name__)


async def credit(s, p: Payment, bot: Bot) -> bool:
    """Идемпотентно зачисляет оплаченный платёж."""
    r = await s.execute(update(Payment).where(Payment.id == p.id, Payment.status == "pending").values(status="paid"))
    if r.rowcount == 0:
        await s.rollback()
        return False
    await s.execute(update(User).where(User.id == p.user_id).values(balance=User.balance + p.amount))
    ref_id, bonus = None, Decimal(0)
    user = await s.get(User, p.user_id)
    if user and user.referrer_id and REF_PERCENT > 0:
        bonus = (Decimal(p.amount) * REF_PERCENT / 100).quantize(Decimal("0.01"))
        ref_id = user.referrer_id
        await s.execute(update(User).where(User.id == ref_id).values(balance=User.balance + bonus))
    await s.commit()
    try:
        await bot.send_message(p.user_id, f"✅ Баланс пополнен на <b>{money(p.amount)}</b>")
        if ref_id:
            await bot.send_message(ref_id, f"🎉 Реферальный бонус: <b>{money(bonus)}</b>")
    except Exception:
        pass
    return True


async def check_pending(bot: Bot, only: int | None = None):
    async with Session() as s:
        q = select(Payment).where(Payment.status == "pending")
        if only:
            q = q.where(Payment.id == only)
        rows = (await s.scalars(q)).all()
        if not rows:
            return
        items = await cryptopay.get_invoices([p.invoice_id for p in rows])
        paid = {i["invoice_id"] for i in items if i.get("status") == "paid"}
        for p in rows:
            if p.invoice_id in paid:
                await credit(s, p, bot)
            elif p.created < datetime.utcnow() - timedelta(hours=2):
                p.status = "expired"
                await s.commit()


async def watcher(bot: Bot):
    while True:
        try:
            if cryptopay.enabled():
                await check_pending(bot)
        except Exception:
            log.exception("payment watcher")
        await asyncio.sleep(20)
