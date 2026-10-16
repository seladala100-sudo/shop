from aiogram import BaseMiddleware

from .config import ADMIN_IDS
from .db import Session, User


class DbMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        tg = data.get("event_from_user")
        async with Session() as s:
            data["session"] = s
            if tg and not tg.is_bot:
                user = await s.get(User, tg.id)
                data["is_new"] = False
                if not user:
                    user = User(id=tg.id, username=tg.username, full_name=tg.full_name)
                    s.add(user)
                    await s.commit()
                    data["is_new"] = True
                elif user.username != tg.username or user.full_name != tg.full_name:
                    user.username, user.full_name = tg.username, tg.full_name
                    await s.commit()
                if user.banned and tg.id not in ADMIN_IDS:
                    return
                data["user"] = user
            return await handler(event, data)
