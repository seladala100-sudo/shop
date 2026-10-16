import logging
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, update

from ..config import ADMIN_CHAT_ID, ADMIN_IDS, MIN_TOPUP, REF_PERCENT, REVIEWS_CHANNEL_ID, TOP_DISCOUNT
from ..db import Category, Order, Payment, Product, Review, User, VpnKey
from ..services import cryptopay, marzban
from ..services.payments import check_pending
from ..utils import esc, ikb, money, show

log = logging.getLogger(__name__)
router = Router()

STATUS = {"processing": "⏳ В работе", "done": "✅ Выполнен", "refunded": "↩️ Возврат"}


class Buy(StatesGroup):
    inp = State()


class TopUp(StatesGroup):
    amount = State()


class Support(StatesGroup):
    msg = State()


class ReviewFSM(StatesGroup):
    text = State()


# ───────────────────────── Меню ─────────────────────────
def menu_kb(uid: int):
    rows = [
        [("🛒 Каталог", "catalog"), ("🔐 VPN", "vpn")],
        [("💳 Пополнить", "topup"), ("👤 Профиль", "profile")],
        [("📦 Мои заказы", "orders"), ("🏆 Топ покупателей", "top")],
        [("⭐ Отзывы", "reviews"), ("🆘 Поддержка", "support")],
    ]
    if uid in ADMIN_IDS:
        rows.append([("⚙️ Админ-панель", "admin")])
    return ikb(*rows)


MENU_TEXT = ("👋 <b>Добро пожаловать!</b>\n\n"
             "⭐ Звёзды · 🎁 Подарки и NFT · 💎 TON · 🔐 VPN · 🎮 Донат\n"
             "Выберите раздел:")


@router.message(CommandStart())
async def start(m: Message, state: FSMContext, session, user: User, is_new: bool, command: CommandObject):
    await state.clear()
    arg = command.args or ""
    if is_new and arg.startswith("ref") and arg[3:].isdigit():
        rid = int(arg[3:])
        if rid != user.id and await session.get(User, rid):
            user.referrer_id = rid
            await session.commit()
    await m.answer(MENU_TEXT, reply_markup=menu_kb(user.id))


@router.message(Command("menu"))
async def menu_cmd(m: Message, state: FSMContext, user: User):
    await state.clear()
    await m.answer(MENU_TEXT, reply_markup=menu_kb(user.id))


@router.callback_query(F.data == "menu")
async def menu_cb(c: CallbackQuery, state: FSMContext, user: User):
    await state.clear()
    await show(c, MENU_TEXT, menu_kb(user.id))


# ───────────────────────── Профиль ─────────────────────────
async def top3_ids(s) -> list[int]:
    return list(await s.scalars(select(User.id).where(User.spent > 0).order_by(User.spent.desc()).limit(3)))


async def price_for(s, user: User, product: Product) -> Decimal:
    price = Decimal(product.price)
    if TOP_DISCOUNT > 0 and user.id in await top3_ids(s):
        price = (price * (100 - TOP_DISCOUNT) / 100).quantize(Decimal("0.01"))
    return price


@router.callback_query(F.data == "profile")
async def profile(c: CallbackQuery, session, user: User):
    await session.refresh(user)
    n = await session.scalar(select(func.count(Order.id)).where(Order.user_id == user.id))
    refs = await session.scalar(select(func.count(User.id)).where(User.referrer_id == user.id))
    me = await c.bot.me()
    top = user.id in await top3_ids(session)
    text = (f"👤 <b>Профиль</b>\n\nID: <code>{user.id}</code>\n"
            f"💰 Баланс: <b>{money(user.balance)}</b>\n🧾 Потрачено: {money(user.spent)}\n"
            f"📦 Заказов: {n}\n👥 Рефералов: {refs}\n"
            + (f"🏆 Вы в топ-3 — скидка {TOP_DISCOUNT}% на всё!\n" if top and TOP_DISCOUNT else "")
            + f"\n🔗 Ваша ссылка ({REF_PERCENT}% с пополнений друзей):\n"
              f"<code>https://t.me/{me.username}?start=ref{user.id}</code>")
    await show(c, text, ikb([("💳 Пополнить", "topup")], [("⬅️ В меню", "menu")]))


# ───────────────────────── Пополнение ─────────────────────────
@router.callback_query(F.data == "topup")
async def topup(c: CallbackQuery, state: FSMContext):
    await state.clear()
    if not cryptopay.enabled():
        return await show(c, "💳 Автоматическое пополнение временно недоступно.\nНапишите в поддержку.",
                          ikb([("🆘 Поддержка", "support")], [("⬅️ В меню", "menu")]))
    rows = [[(f"{a} ₽", f"tu:{a}") for a in (100, 300, 500)], [(f"{a} ₽", f"tu:{a}") for a in (1000, 2000, 5000)],
            [("✏️ Своя сумма", "tu:custom")], [("⬅️ В меню", "menu")]]
    await show(c, "💳 <b>Пополнение баланса</b>\nОплата криптой через @CryptoBot (USDT, TON и др.).\nВыберите сумму:", ikb(*rows))


@router.callback_query(F.data == "tu:custom")
async def tu_custom(c: CallbackQuery, state: FSMContext):
    await state.set_state(TopUp.amount)
    await show(c, f"Введите сумму в рублях (от {MIN_TOPUP}):", ikb([("❌ Отмена", "menu")]))


@router.message(TopUp.amount)
async def tu_amount(m: Message, state: FSMContext, session, user: User):
    try:
        amount = Decimal((m.text or "").replace(",", ".").strip())
    except InvalidOperation:
        return await m.answer("Введите число, например 250")
    await state.clear()
    await create_invoice(m, session, user, amount)


@router.callback_query(F.data.regexp(r"^tu:\d+$"))
async def tu_fixed(c: CallbackQuery, session, user: User):
    await create_invoice(c, session, user, Decimal(c.data.split(":")[1]))


async def create_invoice(event, session, user: User, amount: Decimal):
    if amount < MIN_TOPUP or amount > 100000:
        return await show(event, f"Сумма должна быть от {MIN_TOPUP} до 100000 ₽.", ikb([("⬅️ Назад", "topup")]))
    try:
        inv = await cryptopay.create_invoice(amount, f"Пополнение баланса #{user.id}", str(user.id))
    except Exception:
        log.exception("create_invoice")
        return await show(event, "⚠️ Не удалось создать счёт, попробуйте позже.", ikb([("⬅️ В меню", "menu")]))
    p = Payment(user_id=user.id, amount=amount, invoice_id=inv["invoice_id"])
    session.add(p)
    await session.commit()
    await show(event, f"🧾 Счёт на <b>{money(amount)}</b> создан. Оплатите и нажмите «Проверить» "
                      f"(зачисление также происходит автоматически).",
               ikb([("💸 Оплатить", inv["bot_invoice_url"])], [("🔄 Проверить оплату", f"chk:{p.id}")],
                   [("⬅️ В меню", "menu")]))


@router.callback_query(F.data.startswith("chk:"))
async def chk(c: CallbackQuery, session):
    pid = int(c.data.split(":")[1])
    p = await session.get(Payment, pid)
    if not p or p.user_id != c.from_user.id:
        return await c.answer()
    await session.refresh(p)
    if p.status == "paid":
        return await c.answer("Уже зачислено ✅", show_alert=True)
    try:
        await check_pending(c.bot, only=pid)
    except Exception:
        log.exception("chk")
    await session.refresh(p)
    await c.answer("Оплата получена ✅" if p.status == "paid" else "Оплата пока не найдена", show_alert=True)


# ───────────────────────── Каталог ─────────────────────────
@router.callback_query(F.data == "catalog")
async def catalog(c: CallbackQuery, session, state: FSMContext):
    await state.clear()
    cats = (await session.scalars(select(Category).where(Category.active).order_by(Category.sort, Category.id))).all()
    rows = [[(x.title, f"cat:{x.id}")] for x in cats] + [[("⬅️ В меню", "menu")]]
    await show(c, "🛒 <b>Каталог</b>\nВыберите категорию:", ikb(*rows))


@router.callback_query(F.data.startswith("cat:"))
async def category(c: CallbackQuery, session):
    cid = int(c.data.split(":")[1])
    cat = await session.get(Category, cid)
    prods = (await session.scalars(select(Product).where(Product.category_id == cid, Product.active)
                                   .order_by(Product.id))).all()
    if not prods:
        return await show(c, f"{cat.title}\n\nПока пусто 🤷", ikb([("⬅️ Назад", "catalog")]))
    rows = [[(f"{p.title} — {money(p.price)}", f"prod:{p.id}")] for p in prods] + [[("⬅️ Назад", "catalog")]]
    await show(c, f"<b>{esc(cat.title)}</b>\nВыберите товар:", ikb(*rows))


@router.callback_query(F.data.startswith("prod:"))
async def product_card(c: CallbackQuery, session, user: User):
    p = await session.get(Product, int(c.data.split(":")[1]))
    if not p or not p.active:
        return await c.answer("Товар недоступен", show_alert=True)
    price = await price_for(session, user, p)
    disc = f" <s>{money(p.price)}</s> (скидка топ-3)" if price != p.price else ""
    stock = "" if p.stock is None else f"\n📦 В наличии: {p.stock}"
    text = f"<b>{esc(p.title)}</b>\n\n{esc(p.description)}\n\n💵 Цена: <b>{money(price)}</b>{disc}{stock}"
    back = "vpn" if p.kind == "vpn" else f"cat:{p.category_id}"
    await show(c, text, ikb([("🛒 Купить", f"buy:{p.id}")], [("⬅️ Назад", back)]))


@router.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery, session, state: FSMContext, user: User):
    p = await session.get(Product, int(c.data.split(":")[1]))
    if not p or not p.active or p.stock == 0:
        return await c.answer("Нет в наличии", show_alert=True)
    await state.clear()
    await state.update_data(pid=p.id, inp="")
    if p.input_prompt:
        await state.set_state(Buy.inp)
        return await show(c, f"✏️ {esc(p.input_prompt)}", ikb([("❌ Отмена", "menu")]))
    await confirm_screen(c, session, user, p, "")


async def confirm_screen(event, session, user: User, p: Product, inp: str):
    price = await price_for(session, user, p)
    await session.refresh(user)
    text = (f"🧾 <b>Подтверждение заказа</b>\n\nТовар: {esc(p.title)}\n"
            + (f"Данные: <code>{esc(inp)}</code>\n" if inp else "")
            + f"К оплате: <b>{money(price)}</b>\nВаш баланс: {money(user.balance)}")
    await show(event, text, ikb([("✅ Подтвердить", "confirm")], [("❌ Отмена", "menu")]))


@router.message(Buy.inp, F.text)
async def buy_input(m: Message, session, state: FSMContext, user: User):
    d = await state.get_data()
    p = await session.get(Product, d.get("pid"))
    inp = m.text.strip()[:200]
    await state.set_state(None)
    await state.update_data(inp=inp)
    await confirm_screen(m, session, user, p, inp)


@router.callback_query(F.data == "confirm")
async def confirm(c: CallbackQuery, session, state: FSMContext, user: User):
    d = await state.get_data()
    p = await session.get(Product, d.get("pid") or 0)
    if not p or not p.active:
        return await c.answer("Товар недоступен", show_alert=True)
    inp = d.get("inp", "")
    price = await price_for(session, user, p)
    title, pid, kind, days, gb = p.title, p.id, p.kind, p.vpn_days, p.vpn_gb
    if p.stock is not None:
        r = await session.execute(update(Product).where(Product.id == pid, Product.stock > 0)
                                  .values(stock=Product.stock - 1))
        if r.rowcount == 0:
            await session.rollback()
            return await c.answer("Товар закончился", show_alert=True)
    r = await session.execute(update(User).where(User.id == user.id, User.balance >= price)
                              .values(balance=User.balance - price, spent=User.spent + price))
    if r.rowcount == 0:
        await session.rollback()
        return await show(c, f"❌ Недостаточно средств (нужно {money(price)}).",
                          ikb([("💳 Пополнить", "topup")], [("⬅️ В меню", "menu")]))
    order = Order(user_id=user.id, product_id=pid, title=title, price=price, input_data=inp)
    session.add(order)
    await session.commit()
    await state.clear()

    if kind == "vpn" and marzban.enabled():
        try:
            url, exp = await marzban.issue(f"tg{user.id}", days, gb)
            session.add(VpnKey(user_id=user.id, order_id=order.id, sub_url=url, expire=exp))
            order.status, order.result = "done", url
            await session.commit()
            await show(c, f"✅ <b>VPN активирован до {exp:%d.%m.%Y}</b>\n\nВаша ссылка-подписка "
                          f"(вставьте в Hiddify / v2rayNG / Streisand и др.):\n<code>{url}</code>",
                       ikb([("⬅️ В меню", "menu")]))
            return await ask_review(c.bot, order)
        except Exception:
            log.exception("vpn issue")

    await show(c, f"✅ Заказ <b>#{order.id}</b> оплачен и принят в работу.\nМы сообщим о выполнении.",
               ikb([("📦 Мои заказы", "orders"), ("⬅️ В меню", "menu")]))
    await notify_admin_order(c.bot, order, user)


async def notify_admin_order(bot, order: Order, user: User):
    if not ADMIN_CHAT_ID:
        return
    text = (f"🆕 <b>Заказ #{order.id}</b>\nТовар: {esc(order.title)}\nСумма: {money(order.price)}\n"
            f"Данные: <code>{esc(order.input_data) or '—'}</code>\n"
            f"Покупатель: {esc(user.full_name)} @{user.username or '—'} (<code>{user.id}</code>)")
    try:
        await bot.send_message(ADMIN_CHAT_ID, text, reply_markup=ikb(
            [("✅ Выполнено", f"od:{order.id}"), ("❌ Отказ + возврат", f"or:{order.id}")]))
    except Exception:
        log.exception("notify admin")


# ───────────────────────── VPN ─────────────────────────
@router.callback_query(F.data == "vpn")
async def vpn(c: CallbackQuery, session):
    prods = (await session.scalars(select(Product).where(Product.kind == "vpn", Product.active)
                                   .order_by(Product.price))).all()
    rows = [[(f"{p.title} — {money(p.price)}", f"prod:{p.id}")] for p in prods]
    rows += [[("🔑 Мои ключи", "keys")], [("⬅️ В меню", "menu")]]
    await show(c, "🔐 <b>VPN</b>\nБыстрый и стабильный. Ключ выдаётся автоматически сразу после оплаты.", ikb(*rows))


@router.callback_query(F.data == "keys")
async def keys(c: CallbackQuery, session, user: User):
    ks = (await session.scalars(select(VpnKey).where(VpnKey.user_id == user.id)
                                .order_by(VpnKey.id.desc()).limit(5))).all()
    if not ks:
        return await show(c, "У вас пока нет ключей.", ikb([("⬅️ Назад", "vpn")]))
    text = "🔑 <b>Ваши ключи</b>\n\n" + "\n\n".join(
        f"До {k.expire:%d.%m.%Y}\n<code>{k.sub_url}</code>" if k.expire else f"<code>{k.sub_url}</code>" for k in ks)
    await show(c, text, ikb([("⬅️ Назад", "vpn")]))


# ───────────────────────── Заказы ─────────────────────────
@router.callback_query(F.data == "orders")
async def orders(c: CallbackQuery, session, user: User):
    os_ = (await session.scalars(select(Order).where(Order.user_id == user.id)
                                 .order_by(Order.id.desc()).limit(10))).all()
    text = "📦 <b>Последние заказы</b>\n\n" + ("\n".join(
        f"#{o.id} · {esc(o.title)} · {money(o.price)} · {STATUS.get(o.status, o.status)}" for o in os_)
        or "Пока пусто.")
    await show(c, text, ikb([("⬅️ В меню", "menu")]))


# ───────────────────────── Топ покупателей ─────────────────────────
@router.callback_query(F.data == "top")
async def top(c: CallbackQuery, session):
    rows = (await session.scalars(select(User).where(User.spent > 0).order_by(User.spent.desc()).limit(10))).all()
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = []
    for i, u in enumerate(rows):
        name = (u.username or u.full_name or "user")[:3] + "***"
        lines.append(f"{medals[i]} {esc(name)} — {money(u.spent)}")
    text = "🏆 <b>Топ покупателей</b>\n\n" + ("\n".join(lines) or "Пока никого — станьте первым!")
    if TOP_DISCOUNT > 0:
        text += f"\n\n🎁 Топ-3 получают скидку <b>{TOP_DISCOUNT}%</b> на все товары."
    await show(c, text, ikb([("⬅️ В меню", "menu")]))


# ───────────────────────── Отзывы ─────────────────────────
async def ask_review(bot, order: Order):
    kb = ikb([(f"{'⭐' * n}", f"rate:{order.id}:{n}") for n in (1, 2, 3)],
             [(f"{'⭐' * n}", f"rate:{order.id}:{n}") for n in (4, 5)])
    try:
        await bot.send_message(order.user_id, f"Спасибо за покупку «{esc(order.title)}»! 🙏\nОцените заказ:", reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data.startswith("rate:"))
async def rate(c: CallbackQuery, session, state: FSMContext):
    _, oid, n = c.data.split(":")
    o = await session.get(Order, int(oid))
    if not o or o.user_id != c.from_user.id:
        return await c.answer()
    if await session.scalar(select(Review.id).where(Review.order_id == o.id)):
        return await show(c, "Вы уже оставили отзыв на этот заказ. Спасибо! ❤️")
    await state.set_state(ReviewFSM.text)
    await state.update_data(oid=o.id, rating=int(n))
    await show(c, f"Оценка: {'⭐' * int(n)}\nНапишите пару слов об опыте или пропустите:",
               ikb([("⏭ Пропустить", "skip_review")]))


async def save_review(bot, session, state: FSMContext, user: User, text: str):
    d = await state.get_data()
    await state.clear()
    if not d.get("oid") or await session.scalar(select(Review.id).where(Review.order_id == d["oid"])):
        return
    o = await session.get(Order, d["oid"])
    session.add(Review(user_id=user.id, order_id=d["oid"], rating=d["rating"], text=text))
    await session.commit()
    if REVIEWS_CHANNEL_ID:
        try:
            name = (user.username or user.full_name or "user")[:3] + "***"
            await bot.send_message(REVIEWS_CHANNEL_ID, f"{'⭐' * d['rating']}\n<b>{esc(o.title)}</b>\n"
                                                       f"{esc(text) or '—'}\n\n— {esc(name)}")
        except Exception:
            log.exception("post review")


@router.callback_query(F.data == "skip_review", ReviewFSM.text)
async def skip_review(c: CallbackQuery, session, state: FSMContext, user: User):
    await save_review(c.bot, session, state, user, "")
    await show(c, "Спасибо за оценку! ❤️", ikb([("⬅️ В меню", "menu")]))


@router.message(ReviewFSM.text, F.text)
async def review_text(m: Message, session, state: FSMContext, user: User):
    await save_review(m.bot, session, state, user, m.text[:500])
    await m.answer("Спасибо за отзыв! ❤️", reply_markup=ikb([("⬅️ В меню", "menu")]))


@router.callback_query(F.data == "reviews")
async def reviews(c: CallbackQuery, session):
    rs = (await session.scalars(select(Review).order_by(Review.id.desc()).limit(7))).all()
    avg = await session.scalar(select(func.avg(Review.rating)))
    lines = [f"{'⭐' * r.rating} {esc(r.text[:150]) or '—'}" for r in rs]
    head = f"⭐ <b>Отзывы</b>" + (f" · средняя оценка {float(avg):.1f}" if avg else "")
    await show(c, head + "\n\n" + ("\n\n".join(lines) or "Отзывов пока нет."), ikb([("⬅️ В меню", "menu")]))


# ───────────────────────── Поддержка ─────────────────────────
@router.callback_query(F.data == "support")
async def support(c: CallbackQuery, state: FSMContext):
    await state.set_state(Support.msg)
    await show(c, "🆘 <b>Поддержка</b>\nОпишите вопрос одним сообщением (можно с фото/файлом). Мы ответим здесь же.",
               ikb([("❌ Отмена", "menu")]))


@router.message(Support.msg)
async def support_msg(m: Message, state: FSMContext, user: User):
    if not ADMIN_CHAT_ID:
        return await m.answer("Поддержка временно недоступна.")
    head = await m.bot.send_message(
        ADMIN_CHAT_ID, f"🎫 Обращение от {esc(user.full_name)} @{user.username or '—'} ID:{user.id}\n"
                       f"<i>Ответьте реплаем на это сообщение</i>")
    await m.copy_to(ADMIN_CHAT_ID, reply_to_message_id=head.message_id)
    await state.clear()
    await m.answer("✅ Отправлено! Ответ придёт сюда.", reply_markup=ikb([("⬅️ В меню", "menu")]))
