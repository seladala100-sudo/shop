import asyncio
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select, update

from ..config import ADMIN_CHAT_ID, ADMIN_IDS
from ..db import Category, Order, Payment, Product, User
from ..utils import esc, ikb, money, show
from .user import ask_review

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class AddCat(StatesGroup):
    title = State()


class AddProd(StatesGroup):
    title = State(); desc = State(); price = State(); kind = State()
    days = State(); gb = State(); prompt = State(); stock = State()


class EditPrice(StatesGroup):
    price = State()


class Broadcast(StatesGroup):
    msg = State()


def admin_kb():
    return ikb([("📊 Статистика", "a_stats"), ("⏳ Заказы в работе", "a_pending")],
               [("➕ Категория", "a_addcat"), ("➕ Товар", "a_addprod")],
               [("📦 Товары", "a_prods"), ("🗂 Категории", "a_cats")],
               [("📢 Рассылка", "a_bc")],
               [("⬅️ В меню", "menu")])


HELP = ("⚙️ <b>Админ-панель</b>\n\nКоманды:\n/addbalance <code>id сумма</code>\n/ban <code>id</code>\n/unban <code>id</code>")


@router.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(HELP, reply_markup=admin_kb())


@router.callback_query(F.data == "admin")
async def admin_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, HELP, admin_kb())


# ── Заказы: выполнить / отказ ──
@router.callback_query(F.data.startswith("od:"))
async def order_done(c: CallbackQuery, session):
    oid = int(c.data[3:])
    r = await session.execute(update(Order).where(Order.id == oid, Order.status == "processing").values(status="done"))
    await session.commit()
    if r.rowcount == 0:
        return await c.answer("Уже обработан", show_alert=True)
    o = await session.get(Order, oid)
    await session.refresh(o)
    try:
        await c.bot.send_message(o.user_id, f"✅ Ваш заказ <b>#{o.id}</b> ({esc(o.title)}) выполнен!")
    except Exception:
        pass
    await ask_review(c.bot, o)
    await c.message.edit_text((c.message.html_text or "") + "\n\n✅ <b>Выполнен</b>")
    await c.answer()


@router.callback_query(F.data.startswith("or:"))
async def order_refund(c: CallbackQuery, session):
    oid = int(c.data[3:])
    r = await session.execute(update(Order).where(Order.id == oid, Order.status == "processing").values(status="refunded"))
    if r.rowcount == 0:
        await session.rollback()
        return await c.answer("Уже обработан", show_alert=True)
    o = await session.get(Order, oid)
    await session.execute(update(User).where(User.id == o.user_id)
                          .values(balance=User.balance + o.price, spent=User.spent - o.price))
    if o.product_id:
        await session.execute(update(Product).where(Product.id == o.product_id, Product.stock.is_not(None))
                              .values(stock=Product.stock + 1))
    await session.commit()
    try:
        await c.bot.send_message(o.user_id, f"↩️ Заказ <b>#{o.id}</b> отменён, {money(o.price)} возвращены на баланс.")
    except Exception:
        pass
    await c.message.edit_text((c.message.html_text or "") + "\n\n↩️ <b>Возврат выполнен</b>")
    await c.answer()


@router.callback_query(F.data == "a_pending")
async def pending(c: CallbackQuery, session):
    os_ = (await session.scalars(select(Order).where(Order.status == "processing").order_by(Order.id).limit(15))).all()
    if not os_:
        return await show(c, "Нет заказов в работе 🎉", ikb([("⬅️ Назад", "admin")]))
    await c.answer()
    for o in os_:
        await c.message.answer(f"<b>Заказ #{o.id}</b>\n{esc(o.title)} · {money(o.price)}\n"
                               f"Данные: <code>{esc(o.input_data) or '—'}</code>\nUser: <code>{o.user_id}</code>",
                               reply_markup=ikb([("✅ Выполнено", f"od:{o.id}"), ("❌ Возврат", f"or:{o.id}")]))


# ── Статистика ──
@router.callback_query(F.data == "a_stats")
async def stats(c: CallbackQuery, session):
    day = datetime.utcnow() - timedelta(days=1)
    users = await session.scalar(select(func.count(User.id)))
    new = await session.scalar(select(func.count(User.id)).where(User.created > day))
    done = await session.scalar(select(func.count(Order.id)).where(Order.status == "done"))
    proc = await session.scalar(select(func.count(Order.id)).where(Order.status == "processing"))
    rev = await session.scalar(select(func.coalesce(func.sum(Order.price), 0)).where(Order.status == "done"))
    top = await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid"))
    bal = await session.scalar(select(func.coalesce(func.sum(User.balance), 0)))
    await show(c, f"📊 <b>Статистика</b>\n\n👥 Пользователей: {users} (+{new} за 24ч)\n"
                  f"✅ Выполнено: {done} · ⏳ В работе: {proc}\n💰 Выручка: {money(rev)}\n"
                  f"💳 Пополнений: {money(top)}\n🏦 Остатки на балансах: {money(bal)}", ikb([("⬅️ Назад", "admin")]))


# ── Категории ──
@router.callback_query(F.data == "a_addcat")
async def addcat(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddCat.title)
    await show(c, "Название категории (можно с эмодзи):", ikb([("❌ Отмена", "admin")]))


@router.message(AddCat.title, F.text)
async def addcat_t(m: Message, session, state: FSMContext):
    n = await session.scalar(select(func.count(Category.id)))
    session.add(Category(title=m.text.strip()[:100], sort=n))
    await session.commit()
    await state.clear()
    await m.answer("✅ Категория добавлена", reply_markup=admin_kb())


@router.callback_query(F.data == "a_cats")
async def cats(c: CallbackQuery, session):
    cs = (await session.scalars(select(Category).order_by(Category.sort, Category.id))).all()
    rows = [[(f"🗑 {x.title}", f"acd:{x.id}")] for x in cs] + [[("⬅️ Назад", "admin")]]
    await show(c, "🗂 Нажмите, чтобы удалить категорию <b>вместе с товарами</b>:", ikb(*rows))


@router.callback_query(F.data.startswith("acd:"))
async def cat_del(c: CallbackQuery, session):
    cid = int(c.data[4:])
    await session.execute(delete(Product).where(Product.category_id == cid))
    await session.execute(delete(Category).where(Category.id == cid))
    await session.commit()
    await cats(c, session)


# ── Добавление товара ──
@router.callback_query(F.data == "a_addprod")
async def addprod(c: CallbackQuery, session):
    cs = (await session.scalars(select(Category).order_by(Category.sort, Category.id))).all()
    rows = [[(x.title, f"apc:{x.id}")] for x in cs] + [[("❌ Отмена", "admin")]]
    await show(c, "Выберите категорию:", ikb(*rows))


@router.callback_query(F.data.startswith("apc:"))
async def addprod_cat(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddProd.title)
    await state.update_data(cid=int(c.data[4:]))
    await show(c, "Название товара:", ikb([("❌ Отмена", "admin")]))


@router.message(AddProd.title, F.text)
async def ap_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:150])
    await state.set_state(AddProd.desc)
    await m.answer("Описание (или «-»):")


@router.message(AddProd.desc, F.text)
async def ap_desc(m: Message, state: FSMContext):
    await state.update_data(desc="" if m.text == "-" else m.text)
    await state.set_state(AddProd.price)
    await m.answer("Цена в ₽ (например 149.90):")


@router.message(AddProd.price, F.text)
async def ap_price(m: Message, state: FSMContext):
    try:
        price = Decimal(m.text.replace(",", "."))
        assert price > 0
    except Exception:
        return await m.answer("Введите положительное число")
    await state.update_data(price=str(price))
    await state.set_state(AddProd.kind)
    await m.answer("Тип товара:", reply_markup=ikb([("🖐 Ручная выдача", "pk:manual")],
                                                    [("🔐 VPN (авто через Marzban)", "pk:vpn")]))


@router.callback_query(AddProd.kind, F.data.startswith("pk:"))
async def ap_kind(c: CallbackQuery, state: FSMContext):
    kind = c.data[3:]
    await state.update_data(kind=kind)
    await c.answer()
    if kind == "vpn":
        await state.set_state(AddProd.days)
        await c.message.answer("Срок в днях:")
    else:
        await state.set_state(AddProd.prompt)
        await c.message.answer("Что спросить у покупателя при заказе? Например «Введите @username для получения звёзд». «-» если не нужно:")


@router.message(AddProd.days, F.text)
async def ap_days(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("Число дней")
    await state.update_data(days=int(m.text))
    await state.set_state(AddProd.gb)
    await m.answer("Лимит трафика в ГБ (0 = безлимит):")


@router.message(AddProd.gb, F.text)
async def ap_gb(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("Число ГБ")
    await state.update_data(gb=int(m.text), prompt=None)
    await state.set_state(AddProd.stock)
    await m.answer("Остаток на складе (число) или «-» без лимита:")


@router.message(AddProd.prompt, F.text)
async def ap_prompt(m: Message, state: FSMContext):
    await state.update_data(prompt=None if m.text == "-" else m.text[:200], days=0, gb=0)
    await state.set_state(AddProd.stock)
    await m.answer("Остаток на складе (число) или «-» без лимита:")


@router.message(AddProd.stock, F.text)
async def ap_stock(m: Message, session, state: FSMContext):
    if m.text != "-" and not m.text.isdigit():
        return await m.answer("Число или «-»")
    d = await state.get_data()
    session.add(Product(category_id=d["cid"], title=d["title"], description=d["desc"], price=Decimal(d["price"]),
                        kind=d["kind"], vpn_days=d["days"], vpn_gb=d["gb"], input_prompt=d["prompt"],
                        stock=None if m.text == "-" else int(m.text)))
    await session.commit()
    await state.clear()
    await m.answer("✅ Товар добавлен", reply_markup=admin_kb())


# ── Управление товарами ──
@router.callback_query(F.data == "a_prods")
async def prods(c: CallbackQuery, session):
    ps = (await session.scalars(select(Product).order_by(Product.category_id, Product.id).limit(60))).all()
    rows = [[(f"{'✅' if p.active else '🚫'} {p.title} · {money(p.price)}", f"ap:{p.id}")] for p in ps]
    await show(c, "📦 Товары:", ikb(*rows, [("⬅️ Назад", "admin")]))


@router.callback_query(F.data.startswith("ap:"))
async def prod_card(c: CallbackQuery, session):
    p = await session.get(Product, int(c.data[3:]))
    if not p:
        return await c.answer("Не найден")
    await show(c, f"<b>{esc(p.title)}</b>\n{money(p.price)} · {p.kind} · остаток: {p.stock if p.stock is not None else '∞'}"
                  f"\nСтатус: {'активен' if p.active else 'скрыт'}",
               ikb([("👁 Показать/скрыть", f"apt:{p.id}"), ("💲 Цена", f"app:{p.id}")],
                   [("➕ Остаток +10", f"aps:{p.id}"), ("🗑 Удалить", f"apd:{p.id}")], [("⬅️ Назад", "a_prods")]))


@router.callback_query(F.data.startswith("apt:"))
async def prod_toggle(c: CallbackQuery, session):
    p = await session.get(Product, int(c.data[4:]))
    p.active = not p.active
    await session.commit()
    await prod_card(c, session)


@router.callback_query(F.data.startswith("aps:"))
async def prod_stock(c: CallbackQuery, session):
    pid = int(c.data[4:])
    await session.execute(update(Product).where(Product.id == pid).values(stock=func.coalesce(Product.stock, 0) + 10))
    await session.commit()
    await prod_card(c, session)


@router.callback_query(F.data.startswith("apd:"))
async def prod_del(c: CallbackQuery, session):
    await session.execute(delete(Product).where(Product.id == int(c.data[4:])))
    await session.commit()
    await prods(c, session)


@router.callback_query(F.data.startswith("app:"))
async def price_edit(c: CallbackQuery, state: FSMContext):
    await state.set_state(EditPrice.price)
    await state.update_data(pid=int(c.data[4:]))
    await show(c, "Новая цена в ₽:", ikb([("❌ Отмена", "admin")]))


@router.message(EditPrice.price, F.text)
async def price_set(m: Message, session, state: FSMContext):
    try:
        price = Decimal(m.text.replace(",", "."))
        assert price > 0
    except (InvalidOperation, AssertionError):
        return await m.answer("Введите положительное число")
    d = await state.get_data()
    await session.execute(update(Product).where(Product.id == d["pid"]).values(price=price))
    await session.commit()
    await state.clear()
    await m.answer("✅ Цена обновлена", reply_markup=admin_kb())


# ── Рассылка ──
@router.callback_query(F.data == "a_bc")
async def bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.msg)
    await show(c, "Отправьте сообщение для рассылки (текст/фото/видео — любое):", ikb([("❌ Отмена", "admin")]))


@router.message(Broadcast.msg)
async def bc_send(m: Message, session, state: FSMContext):
    await state.clear()
    ids = list(await session.scalars(select(User.id).where(User.banned.is_(False))))
    status = await m.answer(f"Рассылка на {len(ids)} пользователей…")
    ok = 0
    for uid in ids:
        try:
            await m.copy_to(uid)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Доставлено: {ok}/{len(ids)}")


# ── Команды ──
@router.message(Command("addbalance"))
async def addbalance(m: Message, session, command: CommandObject):
    try:
        uid, amt = (command.args or "").split()
        amt = Decimal(amt.replace(",", "."))
        r = await session.execute(update(User).where(User.id == int(uid)).values(balance=User.balance + amt))
        await session.commit()
        assert r.rowcount
    except Exception:
        return await m.answer("Формат: /addbalance <id> <сумма> (пользователь должен был запускать бота)")
    await m.answer(f"✅ Баланс {uid}: {'+' if amt >= 0 else ''}{money(amt)}")
    try:
        await m.bot.send_message(int(uid), f"💰 Ваш баланс изменён на {money(amt)}")
    except Exception:
        pass


@router.message(Command("ban", "unban"))
async def ban(m: Message, session, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.answer("Формат: /ban <id>")
    flag = command.command == "ban"
    await session.execute(update(User).where(User.id == int(command.args)).values(banned=flag))
    await session.commit()
    await m.answer("🚫 Заблокирован" if flag else "✅ Разблокирован")


# ── Ответы в поддержку (реплаем в админ-чате) ──
ID_RE = re.compile(r"ID:(\d+)")


@router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def support_reply(m: Message):
    r = m.reply_to_message
    mt = ID_RE.search((r.text or r.caption or "")) or (
        r.reply_to_message and ID_RE.search(r.reply_to_message.text or ""))
    if not mt:
        return
    uid = int(mt.group(1))
    try:
        await m.bot.send_message(uid, "💬 <b>Ответ поддержки:</b>")
        await m.copy_to(uid)
        await m.reply("✅ Отправлено пользователю")
    except Exception:
        await m.reply("⚠️ Не удалось доставить (пользователь заблокировал бота?)")
