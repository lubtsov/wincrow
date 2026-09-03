"""Crash: множитель растёт, пока игрок не заберёт или не сорвётся.

Точка краша: <code>0.97 / (1 - u)</code>, u — равномерное из provably fair
потока. Отдача при любой стратегии выхода одна и та же, и это считается в
две строки: показанный множитель m достигается с вероятностью
P(crash > m) = 0.97 / m, выплата m, значит EV = m × 0.97/m = 0.97. Тянуть до
×40 математически не лучше и не хуже, чем забирать на ×1.15.

При u < 0.03 множитель не дотягивает до 1.00 — мгновенный краш. Это ровно те
3%, из которых и состоит преимущество казино.
"""

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
import db
import emoji as E
import keyboards as kb
from db import fmt
from games import engine
from games.registry import implement
from ui import chat_id_of

log = logging.getLogger(__name__)
router = Router(name='crash')

TICK = 1.0          # пауза между тиками, секунды
GROWTH = 1.15       # во сколько раз множитель растёт за тик
MAX_MULT = 50.0     # выше этого — принудительный кэшаут (на отдачу не влияет)

# Значок игры в её сообщениях — премиальный. В каталоге и в истории остаётся
# юникодный spec.emoji из registry: там он едет в текст кнопки, где сущностей
# нет и тег отрисовался бы строкой (emoji.py).
HEAD = E.CRASH

# Живые раунды: round_id -> {'mult': текущий множитель, 'done': забрали/сорвались}.
# Множитель берётся отсюда, а не из callback_data, — иначе его можно подставить.
LIVE: dict[int, dict] = {}


def _kb(round_id: int, mult: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [kb.btn(f'💸 Забрать ×{mult:.2f}', f'cr:{round_id}')],
    ])


@implement('crash')
async def start_crash(call: CallbackQuery, user, state) -> None:
    await play(call, call.from_user.id)


async def play(event, user_id: int, auto: float | None = None) -> None:
    """Запускает раунд. auto — множитель авто-вывода («краш 1 2.5»).

    Авто-вывод не меняет отдачу: он лишь избавляет от необходимости успеть
    нажать кнопку. Забрать вручную раньше по-прежнему можно.
    """
    if auto is not None and not 1.0 < auto <= MAX_MULT:
        await event.answer(f'Авто-вывод бывает от 1.01 до {MAX_MULT:.0f}.',
                           show_alert=True)
        return

    bet = await db.get_bet(user_id)

    rnd = await engine.start_round(user_id, 'crash', bet,
                                   chat_id=chat_id_of(event))
    if rnd is None:
        await event.message.answer(f'Не хватает на ставку {fmt(bet)}.',
                                   reply_markup=kb.back_to('balance', '💳 Баланс'))
        return

    u = rnd.rnd()
    raw = config.RTP / (1 - u)
    crash_point = min(raw, MAX_MULT)
    rnd.state = {'u': round(u, 9), 'crash': round(raw, 4)}
    if auto is not None:
        rnd.state['auto'] = auto

    if raw < 1.0:
        await engine.finish(rnd, 0.0)
        balance = await db.get_balance(user_id)
        await event.message.answer(
            f'{HEAD} <b>×1.00 — мгновенный краш.</b>\n\n'
            f'Ставка {fmt(bet)} ушла.\n\nБаланс: <b>{fmt(balance)}</b>',
            reply_markup=kb.again('crash'))
        return

    await engine.save_state(rnd)
    LIVE[rnd.id] = {'mult': 1.0, 'done': False}

    head = (f'{HEAD} <b>×1.00</b>\n\nСтавка {fmt(bet)}\n'
            f'Забрать сейчас: {fmt(bet)}')
    if auto is not None:
        head += f'\nАвто-вывод на ×{auto:.2f}'
    msg = await event.message.answer(head, reply_markup=_kb(rnd.id, 1.0))

    try:
        await _run(msg, rnd, bet, crash_point, auto)
    finally:
        LIVE.pop(rnd.id, None)


async def _run(msg: Message, rnd: engine.Round, bet: int,
               crash_point: float, auto: float | None = None) -> None:
    mult = 1.0
    while True:
        await asyncio.sleep(TICK)
        live = LIVE.get(rnd.id)
        if live is None or live['done']:
            return                      # забрали — результат печатает хендлер

        nxt = round(mult * GROWTH, 2)
        # Краш проверяется раньше авто-вывода: если точка срыва ниже заказанного
        # множителя, раунд сорвался, и «автомат» его не спасает.
        if nxt >= crash_point:
            break

        mult = nxt
        live['mult'] = mult

        if auto is not None and mult >= auto:
            live['done'] = True
            payout = await engine.finish(rnd, mult)
            if payout is None:
                return
            balance = await db.get_balance(rnd.user_id)
            await _edit(msg,
                f'{E.ROBOT} <b>Авто-вывод на ×{mult:.2f}</b>\n\n'
                f'{fmt(bet)} × {mult:.2f} = <b>{fmt(payout)}</b>\n'
                f'Чистыми: <b>{fmt(payout - bet)}</b>\n\n'
                f'Краш был бы на ×{crash_point:.2f}\n'
                f'Баланс: <b>{fmt(balance)}</b>', kb.again('crash'))
            return

        suffix = f'\nАвто-вывод на ×{auto:.2f}' if auto is not None else ''
        await _edit(msg, f'{HEAD} <b>×{mult:.2f}</b>\n\nСтавка {fmt(bet)}\n'
                         f'Забрать сейчас: {fmt(engine.payout_cents(bet, mult))}'
                         f'{suffix}',
                    _kb(rnd.id, mult))

    # Сорвались. finish идемпотентен: если игрок успел нажать «Забрать»
    # в этот же момент, выплата уже прошла и сюда вернётся None.
    live = LIVE.get(rnd.id)
    if live is not None:
        live['done'] = True
    if await engine.finish(rnd, 0.0) is None:
        return

    balance = await db.get_balance(rnd.user_id)
    await _edit(msg,
        f'{E.BOOM} <b>Краш на ×{crash_point:.2f}</b>\n\n'
        f'Держал до ×{mult:.2f}. Ставка {fmt(bet)} ушла.\n\n'
        f'Баланс: <b>{fmt(balance)}</b>', kb.again('crash'))


@router.callback_query(F.data.startswith('cr:'))
async def cash_out(call: CallbackQuery, user) -> None:
    round_id = int(call.data.split(':', 1)[1])
    live = LIVE.get(round_id)
    rnd = await engine.load_round(round_id, call.from_user.id, 'crash')

    if rnd is None:
        await call.answer('Этот раунд уже закрыт.', show_alert=True)
        return
    if live is None or live['done']:
        # Раунд остался без своего цикла — например, бота перезапустили.
        await engine.void(rnd)
        await call.answer('Раунд потерян, ставка возвращена.', show_alert=True)
        return

    mult = live['mult']
    live['done'] = True
    payout = await engine.finish(rnd, mult)
    if payout is None:
        await call.answer('Не успел — уже сорвалось.', show_alert=True)
        return

    await call.answer(f'Забрал {fmt(payout)}')
    balance = await db.get_balance(call.from_user.id)
    await _edit(call.message,
        f'{E.CASHIER} <b>Забрал на ×{mult:.2f}</b>\n\n'
        f'{fmt(rnd.bet_cents)} × {mult:.2f} = <b>{fmt(payout)}</b>\n'
        f'Чистыми: <b>{fmt(payout - rnd.bet_cents)}</b>\n\n'
        f'Краш был бы на ×{rnd.state.get("crash", 0):.2f}\n'
        f'Баланс: <b>{fmt(balance)}</b>', kb.again('crash'))


async def _edit(msg: Message, text: str,
                markup: InlineKeyboardMarkup | None) -> None:
    """Тик может не пройти из-за лимитов Telegram — это не повод ронять раунд."""
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    except Exception as e:
        log.debug('crash edit failed: %s', e)
