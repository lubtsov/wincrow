# -*- coding: utf-8 -*-
"""Проверка сборки бота и Mini App без единого запроса к Telegram.

    py -3.10 tools\\smoke_app.py

Отличие от `run_check.py`: тот поднимает настоящий polling живым токеном, то
есть сбрасывает накопленные апдейты и переставляет кнопку меню у всех игроков.
Здесь Bot создаётся с фиктивным токеном и в сеть не ходит — проверяется ровно
то, что ломается правками кода: импорты, регистрация роутеров и подъём
HTTP-сервера приложения.

Порт берётся свободный, а не из config: боевой бот на 8080 может быть запущен.
"""
import asyncio
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp                                              # noqa: E402
from aiogram import Bot, Dispatcher                         # noqa: E402
from aiogram.client.default import DefaultBotProperties     # noqa: E402
from aiogram.enums import ParseMode                         # noqa: E402

import config                                               # noqa: E402
import handlers                                             # noqa: E402
import webapp                                               # noqa: E402
from middlewares import UserMiddleware                      # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def build_dispatcher() -> Dispatcher:
    """Ровно та же сборка, что в main.py, — без запуска polling."""
    dp = Dispatcher()
    dp.update.outer_middleware(UserMiddleware())
    for router in handlers.routers:
        dp.include_router(router)
    dp.include_router(handlers.fallback)
    return dp


async def probe(port: int) -> None:
    base = f'http://127.0.0.1:{port}'
    async with aiohttp.ClientSession() as session:
        async with session.get(base + '/health') as r:
            print('health:', r.status, await r.text())
        async with session.get(base + '/') as r:
            body = await r.text()
            print('страница:', r.status, len(body), 'байт; экраны:',
                  all(k in body for k in ('view-games', 'view-slots', 'view-case',
                                          'view-profile')),
                  '; версия статики:', '?v=dev' not in body)
        for name in ('app.js', 'slots.js', 'case.js', 'games.js', 'profile.js',
                     'app.css'):
            async with session.get(base + '/static/' + name) as r:
                print(f'{name}:', r.status, r.headers.get('Content-Type'),
                      r.headers.get('Cache-Control'))
        # Без подписи Telegram сервер обязан отказать — и играм тоже.
        for path in ('/api/state', '/api/slots/state', '/api/slots/spin',
                     '/api/games/state', '/api/games/play', '/api/games/step',
                     '/api/profile'):
            async with session.post(base + path, json={}) as r:
                print(f'{path} без initData:', r.status, await r.text())


async def run() -> None:
    dp = build_dispatcher()
    print('роутеры:', ', '.join(r.name for r in handlers.routers))
    print('типы апдейтов:', ', '.join(dp.resolve_used_update_types()))

    config.WEBAPP_PORT = free_port()
    config.WEBAPP_ENABLED = True
    bot = Bot('123456:FAKE-TOKEN-NO-NETWORK-CALLS-HERE',
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    runner = None
    try:
        runner = await webapp.start(bot)
        assert runner is not None, 'сервер не поднялся'
        await probe(config.WEBAPP_PORT)
    finally:
        await webapp.stop(runner)
        await bot.session.close()


asyncio.run(run())
print('сборка в порядке, сервер поднялся и погас')
