"""Монетка.

Шанс ровно 1/2, поэтому множитель — единственная переменная, и он выводится
из отдачи: 2 × 0.97 = 1.94.

Сторона выбирается ДО того, как создан раунд. Так на балансе не остаётся
подвешенных активных раундов, если игрок ушёл с экрана выбора, а списание всё
равно атомарно — оно происходит внутри engine.start_round.
"""

import asyncio

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

import config
import db
import emoji as E
import keyboards as kb
from db import fmt
from games import engine
from games.registry import implement
from ui import chat_id_of, render

router = Router(name='coin')

MULT = 2 * config.RTP

# Значок стороны — юникодный: он едет и в текст, и в подпись кнопки. В текстах
# он поднимается до премиального через E.tag (emoji.py), у орла значка нет.
SIDES = {'heads': ('🦅', 'орёл'), 'tails': ('🪙', 'решка')}

# Как сторону называют в чате. Ключи только в нижнем регистре.
SIDE_WORDS = {
    'орёл': 'heads', 'орел': 'heads', 'о': 'heads', 'heads': 'heads',
    'h': 'heads', 'аверс': 'heads',
    'решка': 'tails', 'решку': 'tails', 'р': 'tails', 'tails': 'tails',
    't': 'tails', 'реверс': 'tails',
}


def parse_side(word: str) -> str | None:
    return SIDE_WORDS.get(word.strip().lower())


def _choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [kb.btn('🦅 Орёл', 'coin:heads'), kb.btn('🪙 Решка', 'coin:tails')],
        [kb.btn('💰 Ставка', 'game:coin'), kb.btn('⬅️ К играм', 'grp:classic')],
    ])


@implement('coin')
async def start_coin(call: CallbackQuery, user, state) -> None:
    bet = await db.get_bet(call.from_user.id)
    await render(call,
        f'{E.COIN} <b>Монетка</b>\n\n'
        f'Ставка: <b>{fmt(bet)}</b>\n'
        f'Угадал сторону — <b>×{MULT:.2f}</b>\n\n'
        f'Выбирай.',
        _choice_kb())


async def play(event, user_id: int, side: str) -> None:
    """Один бросок на выбранную сторону.

    Сторона приходит либо кнопкой, либо словом из чата — саму игру это не
    касается, поэтому обе точки входа зовут отсюда.
    """
    bet = await db.get_bet(user_id)
    rnd = await engine.start_round(user_id, 'coin', bet,
                                   chat_id=chat_id_of(event))
    if rnd is None:
        await event.answer(f'Не хватает на ставку {fmt(bet)}.', show_alert=True)
        return
    await event.answer()

    picked_emoji, picked_name = SIDES[side]
    await render(event,
                 f'{E.COIN} Монета в воздухе… ставка {fmt(bet)} на {picked_name}')
    await asyncio.sleep(1.3)

    result = 'heads' if rnd.pick(2) == 0 else 'tails'
    res_emoji, res_name = SIDES[result]
    rnd.state = {'pick': side, 'result': result}

    if result == side:
        payout = await engine.finish(rnd, MULT)
        if payout is None:
            return
        head = (f'{E.tag(res_emoji)} <b>{res_name.capitalize()}</b> — угадал.\n\n'
                f'{fmt(bet)} × {MULT:.2f} = <b>{fmt(payout)}</b>\n'
                f'Чистыми: <b>{fmt(payout - bet)}</b>')
    else:
        if await engine.finish(rnd, 0.0) is None:
            return
        head = (f'{E.tag(res_emoji)} <b>{res_name.capitalize()}</b> — не угадал.\n\n'
                f'Ставка {fmt(bet)} ушла.')

    balance = await db.get_balance(user_id)
    await render(event, f'{head}\n\nБаланс: <b>{fmt(balance)}</b>',
                 kb.again('coin'))


@router.callback_query(F.data.startswith('coin:'))
async def flip(call: CallbackQuery, user) -> None:
    side = call.data.split(':', 1)[1]
    if side not in SIDES:
        await call.answer('Неизвестная сторона.', show_alert=True)
        return
    await play(call, call.from_user.id, side)
