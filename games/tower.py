"""Башня: три двери на этаж, за одной обрыв.

Множитель после k этажей — <code>0.97 × 1.5^k</code>. Проверка отдачи
арифметическая: дойти до k этажа получается с вероятностью (2/3)^k, выплата
0.97 × 1.5^k, произведение = 0.97 × (2/3 × 1.5)^k = 0.97 при любом k. Значит
осторожная игра и игра до верха дают одну и ту же отдачу.

Плохие двери на всех этажах разложены из provably fair сида в момент старта,
а не подбираются в момент клика.
"""

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

router = Router(name='tower')

DOORS = 3
FLOORS = 8
STEP_MULT = DOORS / (DOORS - 1)     # 1.5 — плата за риск 1 к 3


def multiplier(level: int) -> float:
    return config.RTP * STEP_MULT ** level


def _tower_text(rnd: engine.Round) -> str:
    level = rnd.state['level']
    lines = []
    for floor in range(FLOORS, 0, -1):
        mark = E.OK if floor <= level else (E.ARROW if floor == level + 1
                                            else '▫️')
        lines.append(f'{mark} {floor} этаж — ×{multiplier(floor):.2f}')

    text = '🗼 <b>Башня</b>\n\n' + '\n'.join(lines) + '\n\n'
    text += f'Ставка: {fmt(rnd.bet_cents)}\n'
    if level:
        text += (f'Сейчас: <b>×{multiplier(level):.2f}</b> = '
                 f'<b>{fmt(engine.payout_cents(rnd.bet_cents, multiplier(level)))}</b>\n')
    text += f'Выбирай дверь на {level + 1} этаж.'
    return text


def _doors_kb(rnd: engine.Round) -> InlineKeyboardMarkup:
    rows = [[kb.btn(f'🚪 {d + 1}', f'tw:{rnd.id}:{d}') for d in range(DOORS)]]
    if rnd.state['level']:
        payout = engine.payout_cents(rnd.bet_cents, multiplier(rnd.state['level']))
        rows.append([kb.btn(f'💸 Забрать {fmt(payout)}', f'tw:{rnd.id}:c')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _reveal(rnd: engine.Round, upto: int) -> str:
    """Показывает, где были обрывы на пройденных и текущем этаже."""
    bad = rnd.state['bad']
    path = rnd.state['path']
    lines = []
    for floor in range(min(upto, FLOORS), 0, -1):
        cells = []
        for d in range(DOORS):
            if d == bad[floor - 1]:
                cells.append(E.SKULL)
            elif len(path) >= floor and path[floor - 1] == d:
                cells.append(E.OK)
            else:
                cells.append('▫️')
        lines.append(f'{floor}: ' + ''.join(cells))
    return '\n'.join(lines)


@implement('tower')
async def start_tower(call: CallbackQuery, user, state) -> None:
    user_id = call.from_user.id
    bet = await db.get_bet(user_id)

    rnd = await engine.start_round(user_id, 'tower', bet,
                                   chat_id=chat_id_of(call))
    if rnd is None:
        await call.message.answer(f'Не хватает на ставку {fmt(bet)}.',
                                  reply_markup=kb.back_to('balance', '💳 Баланс'))
        return

    rnd.state = {'bad': [rnd.pick(DOORS) for _ in range(FLOORS)],
                 'level': 0, 'path': []}
    await engine.save_state(rnd)
    await render(call, _tower_text(rnd), _doors_kb(rnd))


@router.callback_query(F.data.startswith('tw:'))
async def step(call: CallbackQuery, user) -> None:
    _, raw_id, action = call.data.split(':', 2)
    rnd = await engine.load_round(int(raw_id), call.from_user.id, 'tower')
    if rnd is None:
        await call.answer('Раунд устарел, начни заново.', show_alert=True)
        return

    level = rnd.state['level']

    # --- забрать ------------------------------------------------------------
    if action == 'c':
        if level == 0:
            await call.answer('Сначала пройди хотя бы этаж.')
            return
        mult = multiplier(level)
        payout = await engine.finish(rnd, mult)
        if payout is None:
            await call.answer('Раунд уже закрыт.', show_alert=True)
            return
        await call.answer(f'Забрал {fmt(payout)}')
        balance = await db.get_balance(call.from_user.id)
        await render(call,
            f'{E.CASHIER} <b>Забрал на {level} этаже, ×{mult:.2f}</b>\n\n'
            f'{_reveal(rnd, level)}\n\n'
            f'{fmt(rnd.bet_cents)} × {mult:.2f} = <b>{fmt(payout)}</b>\n'
            f'Чистыми: <b>{fmt(payout - rnd.bet_cents)}</b>\n'
            f'Баланс: <b>{fmt(balance)}</b>', kb.again('tower'))
        return

    # --- открыть дверь ------------------------------------------------------
    door = int(action)
    if not 0 <= door < DOORS:
        await call.answer('Такой двери нет.', show_alert=True)
        return

    if door == rnd.state['bad'][level]:
        rnd.state['path'] = rnd.state['path'] + [door]
        if await engine.finish(rnd, 0.0) is None:
            await call.answer('Раунд уже закрыт.', show_alert=True)
            return
        await call.answer('💀')
        balance = await db.get_balance(call.from_user.id)
        await render(call,
            f'{E.SKULL} <b>Обрыв на {level + 1} этаже.</b>\n\n'
            f'{_reveal(rnd, level + 1)}\n\n'
            f'Прошёл {level}, ставка {fmt(rnd.bet_cents)} ушла.\n'
            f'Баланс: <b>{fmt(balance)}</b>', kb.again('tower'))
        return

    rnd.state['level'] = level + 1
    rnd.state['path'] = rnd.state['path'] + [door]

    # Верх башни — раунд закрывается сам с максимальной выплатой.
    if rnd.state['level'] >= FLOORS:
        mult = multiplier(FLOORS)
        payout = await engine.finish(rnd, mult)
        if payout is None:
            return
        await call.answer('Вершина!')
        balance = await db.get_balance(call.from_user.id)
        await render(call,
            f'{E.TROPHY} <b>Вершина башни, ×{mult:.2f}</b>\n\n'
            f'{_reveal(rnd, FLOORS)}\n\n'
            f'{fmt(rnd.bet_cents)} × {mult:.2f} = <b>{fmt(payout)}</b>\n'
            f'Баланс: <b>{fmt(balance)}</b>', kb.again('tower'))
        return

    await engine.save_state(rnd)
    await call.answer()
    await render(call, _tower_text(rnd), _doors_kb(rnd))
