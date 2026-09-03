"""Точка входа.

Запуск:
    py -3.10 main.py

Одна команда поднимает всё: Telegram-бота (polling) и HTTP-сервер Mini App
(`webapp/server.py`) — в одном процессе и одном event loop, с общим
соединением к базе. Отдельной команды для Web App нет и не нужно, а при
остановке бота сервер гасится в том же `finally`, что и остальное.

Здесь и только здесь создаётся объект Bot. В прежней версии их было два —
свой в main.py и свой в кассе (payment.py:16), — из-за чего сессии к Telegram
жили параллельно и закрывались как попало.

Касса одна: @CryptoBot (Crypto Pay API). Прежний слой на aiogram 2 с QIWI,
QIWI P2P, ЮMoney, CrystalPay и Coinbase удалён целиком — в проекте его больше
нет ни одним файлом, а ссылки вида `payment.py:40` в комментариях остались как
объяснение, откуда взялось нынешнее решение (см. «Legacy» в README).
"""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo

import config
import db
import handlers
import payments
import webapp
from middlewares import UserMiddleware

log = logging.getLogger('casino')


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s',
        datefmt='%H:%M:%S')
    logging.getLogger('aiogram.event').setLevel(logging.WARNING)

    if not config.TOKEN:
        sys.exit('BOT_TOKEN не задан: пропиши его в config.py или в окружение.')

    await db.init()
    log.info('база: %s', config.DB_PATH)

    # Crash без живого процесса не завершится сам: множитель тикает в памяти.
    # Раунды, оставшиеся открытыми после падения, закрываем возвратом ставки.
    reaped = await db.reap_active_rounds()
    if reaped:
        log.info('возвращено ставок по зависшим раундам: %s', reaped)

    bot = Bot(config.TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    # Один слой на все апдейты: строка юзера, бан-гейт, антифлуд.
    dp.update.outer_middleware(UserMiddleware())

    for router in handlers.routers:
        dp.include_router(router)
    dp.include_router(handlers.fallback)

    tasks: list[asyncio.Task] = []
    runner = None
    try:
        me = await bot.me()
        log.info('запуск @%s (id %s)', me.username, me.id)

        if payments.client.enabled:
            try:
                app = await payments.client.me()
                log.info('Crypto Pay: приложение %s, сеть %s',
                         app.get('name', '?'),
                         'testnet' if config.CRYPTO_PAY_TESTNET else 'mainnet')
            except Exception as e:
                # Не повод не стартовать: игры работают и без кассы.
                log.error('Crypto Pay не отвечает (%s) — касса может не работать', e)
            tasks.append(asyncio.create_task(payments.poll_invoices(bot),
                                             name='invoice-poller'))
        else:
            log.warning('CRYPTO_PAY_TOKEN пуст — пополнение и вывод выключены')

        # Mini App ежедневного кейса. Падать из-за него бот не должен: занятый
        # порт — не причина оставлять игроков без игр, кейс в этом случае
        # открывается кнопками внутри бота.
        try:
            runner = await webapp.start(bot)
        except OSError as e:
            log.error('Mini App не поднялся на %s:%s (%s) — кейс остаётся '
                      'в боте', config.WEBAPP_HOST, config.WEBAPP_PORT, e)

        # Кнопка «меню» у поля ввода — постоянный вход в приложение. Ставится
        # один раз на бота и переживает перезапуск, поэтому её достаточно
        # обновлять при старте.
        if config.WEBAPP_URL:
            try:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text='🎰 Приложение',
                        web_app=WebAppInfo(url=config.WEBAPP_URL)))
            except Exception as e:
                log.warning('кнопка меню Mini App не выставилась: %s', e)

        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot,
                               allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        await webapp.stop(runner)
        await payments.client.close()
        await db.close()
        await bot.session.close()
        log.info('остановлен')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
