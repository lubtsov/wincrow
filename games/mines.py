"""Мины: поле 5×5, число мин выбирает игрок.

Множитель после k безопасных открытий:

    0.97 × C(25, k) / C(25 - N, k)

Отдача 97% при любой стратегии, и проверяется это одной строкой: шанс открыть
k клеток без мины равен C(25-N, k) / C(25, k), а выплата — обратная величина,
умноженная на 0.97. Что бы игрок ни делал — забирал на первой клетке или шёл
до конца, — произведение одно и то же.

Раскладка мин выводится из provably fair сида в момент старта раунда, до
первого клика. Дорисовать мину под уже открытую клетку невозможно.
"""

import math

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

router = Router(name='mines')

CELLS = 25
SIDE = 5
MINE_CHOICES = (1, 3, 5, 10, 15, 24)


def multiplier(mines: int, opened: int) -> float:
    """Выплата после `opened` безопасных клеток при `mines` минах."""
    if opened <= 0:
        return 1.0
    safe = CELLS - mines
    if opened > safe:
        opened = safe
    return config.RTP * math.comb(CELLS, opened) / math.comb(safe, opened)


def _picker_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for n in MINE_CHOICES:
        row.append(kb.btn(f'{n} 💣 · ×{multiplier(n, 1):.2f}', f'mines:{n}'))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([kb.btn('💰 Ставка', 'game:mines'),
                 kb.btn('⬅️ К играм', 'grp:grow')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _field_kb(rnd: engine.Round) -> InlineKeyboardMarkup:
    opened = set(rnd.state['opened'])
    rows = []
    for r in range(SIDE):
        row = []
        for c in range(SIDE):
            i = r * SIDE + c
            if i in opened:
                row.append(kb.btn('💎', 'nop'))
            else:
                row.append(kb.btn('▫️', f'mn:{rnd.id}:{i}'))
        rows.append(row)
    if opened:
        mult = multiplier(rnd.state['n'], len(opened))
        rows.append([kb.btn(f'💸 Забрать {fmt(engine.payout_cents(rnd.bet_cents, mult))}',
                            f'mn:{rnd.id}:c')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _reveal(rnd: engine.Round, hit: int | None) -> str:
    """Текстовая картинка вскрытого поля."""
    mines = set(rnd.state['mines'])
    opened = set(rnd.state['opened'])
    lines = []
    for r in range(SIDE):
        cells = []
        for c in range(SIDE):
            i = r * SIDE + c
            if i == hit:
                cells.append(E.BOOM)
            elif i in mines:
                cells.append(E.MINE)
            elif i in opened:
                cells.append(E.GEM)
            else:
                cells.append('▫️')
        lines.append(''.join(cells))
    return '\n'.join(lines)


def _field_text(rnd: engine.Round) -> str:
    n = rnd.state['n']
    opened = len(rnd.state['opened'])
    mult = multiplier(n, opened)
    text = (f'{E.GEM} <b>Мины</b> · {n} {E.MINE} на поле\n\n'
            f'Ставка: {fmt(rnd.bet_cents)}\n'
            f'Открыто: <b>{opened}</b> из {CELLS - n}\n')
    if opened:
        text += (f'Множитель: <b>×{mult:.2f}</b>\n'
                 f'Забрать: <b>{fmt(engine.payout_cents(rnd.bet_cents, mult))}</b>\n')
    nxt = multiplier(n, opened + 1)
    if opened + 1 <= CELLS - n:
        text += f'\nСледующая клетка поднимет до ×{nxt:.2f}.'
    return text


@implement('mines')
async def start_mines(call: CallbackQuery, user, state) -> None:
    bet = await db.get_bet(call.from_user.id)
    await render(call,
        f'{E.GEM} <b>Мины</b>\n\n'
        f'Ставка: <b>{fmt(bet)}</b>\n\n'
        f'Сколько мин спрятать на поле 5×5? Чем больше — тем круче растёт '
        f'множитель и тем короче жизнь. Отдача одинаковая в любом варианте.',
        _picker_kb())


@router.callback_query(F.data.startswith('mines:'))
async def choose_mines(call: CallbackQuery, user) -> None:
    await play(call, call.from_user.id, int(call.data.split(':', 1)[1]))


async def play(event, user_id: int, n: int) -> None:
    """Ставит поле с n минами. Общий путь для кнопки и команды «мины 0.5 2»."""
    if not 1 <= n <= CELLS - 1:
        await event.answer(f'Мин может быть от 1 до {CELLS - 1}.', show_alert=True)
        return

    bet = await db.get_bet(user_id)
    rnd = await engine.start_round(user_id, 'mines', bet,
                                   chat_id=chat_id_of(event))
    if rnd is None:
        await event.answer(f'Не хватает на ставку {fmt(bet)}.', show_alert=True)
        return

    rnd.state = {'n': n, 'mines': sorted(rnd.sample(CELLS, n)), 'opened': []}
    await engine.save_state(rnd)
    await event.answer()
    await render(event, _field_text(rnd), _field_kb(rnd))


@router.callback_query(F.data.startswith('mn:'))
async def step(call: CallbackQuery, user) -> None:
    _, raw_id, action = call.data.split(':', 2)
    rnd = await engine.load_round(int(raw_id), call.from_user.id, 'mines')
    if rnd is None:
        await call.answer('Раунд устарел, начни заново.', show_alert=True)
        return

    mines = set(rnd.state['mines'])
    opened = list(rnd.state['opened'])

    # --- забрать ------------------------------------------------------------
    if action == 'c':
        if not opened:
            await call.answer('Открой хотя бы одну клетку.')
            return
        mult = multiplier(rnd.state['n'], len(opened))
        payout = await engine.finish(rnd, mult)
        if payout is None:
            await call.answer('Раунд уже закрыт.', show_alert=True)
            return
        await call.answer(f'Забрал {fmt(payout)}')
        balance = await db.get_balance(call.from_user.id)
        await render(call,
            f'{E.CASHIER} <b>Забрал на ×{mult:.2f}</b>\n\n{_reveal(rnd, None)}\n\n'
            f'{fmt(rnd.bet_cents)} × {mult:.2f} = <b>{fmt(payout)}</b>\n'
            f'Чистыми: <b>{fmt(payout - rnd.bet_cents)}</b>\n'
            f'Баланс: <b>{fmt(balance)}</b>', kb.again('mines'))
        return

    # --- открыть клетку -----------------------------------------------------
    cell = int(action)
    if not 0 <= cell < CELLS:
        await call.answer('Такой клетки нет.', show_alert=True)
        return
    if cell in opened:
        await call.answer('Уже открыта.')
        return

    if cell in mines:
        rnd.state['hit'] = cell
        if await engine.finish(rnd, 0.0) is None:
            await call.answer('Раунд уже закрыт.', show_alert=True)
            return
        await call.answer('💥')
        balance = await db.get_balance(call.from_user.id)
        await render(call,
            f'{E.BOOM} <b>Мина.</b>\n\n{_reveal(rnd, cell)}\n\n'
            f'Открыл {len(opened)} — ставка {fmt(rnd.bet_cents)} ушла.\n'
            f'Баланс: <b>{fmt(balance)}</b>', kb.again('mines'))
        return

    opened.append(cell)
    rnd.state['opened'] = opened
    await engine.save_state(rnd)

    # Открыты все безопасные клетки — раунд закрывается сам, максимум взят.
    if len(opened) == CELLS - rnd.state['n']:
        mult = multiplier(rnd.state['n'], len(opened))
        payout = await engine.finish(rnd, mult)
        if payout is None:
            return
        await call.answer('Поле вычищено!')
        balance = await db.get_balance(call.from_user.id)
        await render(call,
            f'{E.TROPHY} <b>Поле вычищено, ×{mult:.2f}</b>\n\n{_reveal(rnd, None)}\n\n'
            f'{fmt(rnd.bet_cents)} × {mult:.2f} = <b>{fmt(payout)}</b>\n'
            f'Баланс: <b>{fmt(balance)}</b>', kb.again('mines'))
        return

    await call.answer()
    await render(call, _field_text(rnd), _field_kb(rnd))
