"""Сборка роутеров.

Здесь же импортируются модули игр: импорт — это и есть регистрация в каталоге
(декоратор @implement помечает игру рабочей). Пока модуль не импортирован,
меню честно показывает игру как «скоро».

Порядок регистрации значим: fallback идёт последним, иначе он съест апдейты,
адресованные конкретным хендлерам.
"""

from games import (blackjack, coin, crash, dice_games, dice_sum,  # noqa: F401
                   mines, pvp, roulette, tower)

from . import admin, balance, chat, chats, common, daily, games

routers = [
    common.router,
    admin.router,
    balance.router,
    games.router,
    # Ежедневный кейс. Тот же кейс открывается и из Mini App, логика общая
    # (daily.py), поэтому роутер отвечает только за экран внутри бота.
    daily.router,
    # Привязка чатов к владельцам: my_chat_member и экран «Мои чаты».
    chats.router,
    # Роутеры игр, у которых есть свои кнопки внутри раунда.
    coin.router,
    roulette.router,
    dice_games.router,
    dice_sum.router,
    blackjack.router,
    crash.router,
    mines.router,
    tower.router,
    pvp.router,
    # Текстовые команды — после всех FSM-хендлеров: если бот ждёт от игрока
    # сумму, «5» должно попасть в ввод, а не в разбор команд.
    chat.router,
]

# Ловит всё, что не разобрали выше.
fallback = common.fallback_router
