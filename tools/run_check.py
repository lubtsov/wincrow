# -*- coding: utf-8 -*-
"""Проверка живого запуска: `main.py` поднимает бота и Mini App, потом гасит оба.

    py -3.10 tools\\run_check.py

Внимание: работает боевым токеном. Значит уходит `delete_webhook` со сбросом
накопленных апдейтов, переставляется кнопка меню у игроков, а если бот уже
запущен где-то ещё — Telegram отдаст конфликт polling. Для обычной проверки
после правок хватает `smoke_app.py`: он проверяет то же самое, но в сеть не
ходит.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp  # noqa: E402

import config  # noqa: E402
import main  # noqa: E402


async def probe() -> None:
    base = f'http://127.0.0.1:{config.WEBAPP_PORT}'
    async with aiohttp.ClientSession() as session:
        async with session.get(base + '/health') as r:
            print('health:', r.status, await r.text())
        async with session.get(base + '/') as r:
            body = await r.text()
            print('страница:', r.status, len(body), 'байт,',
                  'заголовок есть:', 'Ежедневный кейс' in body)
        async with session.get(base + '/static/app.js') as r:
            print('app.js:', r.status, r.headers.get('Content-Type'))
        # Без подписи Telegram сервер обязан отказать.
        async with session.post(base + '/api/state', json={}) as r:
            print('api без initData:', r.status, await r.text())


async def run() -> None:
    task = asyncio.create_task(main.main())
    await asyncio.sleep(6)
    if task.done():                     # упал на старте — покажем причину
        await task
        return
    await probe()
    print('--- останавливаем ---')
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


asyncio.run(run())
print('процесс завершился сам, без зависших задач')
