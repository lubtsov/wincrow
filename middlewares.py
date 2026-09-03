"""Middleware: гарантия строки юзера, бан-гейт, антиспам.

Раньше `ensure_user` и проверка бана дублировались в каждом хендлере, и часть
хендлеров их просто не делала. Здесь это один слой на все апдейты.
"""

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Update

import db

# Минимальная пауза между действиями одного юзера, секунды. Атомарный
# place_bet уже закрывает гонки на спаме кнопки, так что порог низкий — он
# гасит только машинный флуд, живому игроку в минах/башне не мешает.
THROTTLE = 0.2

BAN_TEXT = '🚫 Доступ к боту закрыт.'


class UserMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._last: dict[int, float] = {}

    async def __call__(self, handler: Callable[..., Awaitable[Any]],
                       event: Update, data: dict[str, Any]) -> Any:
        tg_user = data.get('event_from_user')
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        # Реферер вытаскивается до создания строки: если сначала создать юзера,
        # а потом читать /start, ссылка уже не сработает.
        await db.ensure_user(tg_user.id, tg_user.username, _referer_from_start(event))

        user = await db.get_user(tg_user.id)
        if user is not None and user['banned']:
            await _reject(event)
            return None

        now = time.monotonic()
        if now - self._last.get(tg_user.id, 0.0) < THROTTLE:
            if event.callback_query is not None:
                await event.callback_query.answer('Не так быстро 🙂')
            return None
        self._last[tg_user.id] = now

        data['user'] = user
        data['is_admin'] = await db.is_admin(tg_user.id)
        return await handler(event, data)


def _referer_from_start(event: Update) -> int | None:
    """/start 6766372415 -> 6766372415. Кто угодно другой -> None."""
    msg = event.message
    if msg is None or not msg.text:
        return None
    parts = msg.text.split(maxsplit=1)
    if parts[0].split('@')[0] != '/start' or len(parts) < 2:
        return None
    payload = parts[1].strip()
    return int(payload) if payload.isdigit() else None


async def _reject(event: Update) -> None:
    if event.callback_query is not None:
        await event.callback_query.answer(BAN_TEXT, show_alert=True)
    elif event.message is not None:
        await event.message.answer(BAN_TEXT)
