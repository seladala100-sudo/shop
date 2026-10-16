import html
from decimal import Decimal

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

esc = html.escape


def money(x) -> str:
    return f"{Decimal(x):.2f} ₽"


def ikb(*rows) -> InlineKeyboardMarkup:
    """rows: списки кортежей (текст, callback_data | https-ссылка)"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, url=d) if d.startswith("http")
         else InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in rows
    ])


async def show(event, text: str, kb=None):
    """Редактирует сообщение при нажатии на кнопку, иначе отправляет новое."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await event.message.answer(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)
