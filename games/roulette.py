"""Европейская рулетка: 37 чисел, 0–36.

Отдача берётся из зеро и больше ниоткуда: 18 красных из 37 чисел,
18/37 × 2 = 0.973. Дюжина 12/37 × 3 = 0.973, число 1/37 × 36 = 0.973 —
все ставки равноценны, подкручивать выплаты не требуется.

Два бага прежней версии, которые здесь закрыты:
* `randint(1, 36)` — зеро не существовало, edge был ровно нулевой;
* красное/чёрное решалось отдельным `choice()`, не связанным с числом,
  которое показывали игроку. Теперь цвет — свойство выпавшего числа.
"""

import asyncio

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

import db
import keyboards as kb
from db import fmt
from games import engine
from games.registry import implement
from ui import chat_id_of, render

router = Router(name='roulette')

POCKETS = 37

# Разметка настоящего колеса. Единственный источник правды о цвете.
RED = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18,
                 19, 21, 23, 25, 27, 30, 32, 34, 36})

# (ключ, подпись, множитель, предикат)
BETS = {
    'red':   ('🔴 Красное', 2.0, lambda n: n in RED),
    'black': ('⚫ Чёрное', 2.0, lambda n: n != 0 and n not in RED),
    'even':  ('Чёт', 2.0, lambda n: n != 0 and n % 2 == 0),
    'odd':   ('Нечет', 2.0, lambda n: n % 2 == 1),
    'low':   ('1–18', 2.0, lambda n: 1 <= n <= 18),
    'high':  ('19–36', 2.0, lambda n: 19 <= n <= 36),
    'd1':    ('1-я дюжина', 3.0, lambda n: 1 <= n <= 12),
    'd2':    ('2-я дюжина', 3.0, lambda n: 13 <= n <= 24),
    'd3':    ('3-я дюжина', 3.0, lambda n: 25 <= n <= 36),
}

STRAIGHT_MULT = 36.0

# Как ставку называют в чате. Число распознаётся отдельно — оно не слово.
BET_WORDS = {
    'красное': 'red', 'красный': 'red', 'кр': 'red', 'к': 'red', 'red': 'red',
    'r': 'red',
    'чёрное': 'black', 'черное': 'black', 'чёрный': 'black', 'черный': 'black',
    'чёрн': 'black', 'черн': 'black', 'ч': 'black', 'black': 'black', 'b': 'black',
    'чёт': 'even', 'чет': 'even', 'чётное': 'even', 'четное': 'even',
    'even': 'even',
    'нечет': 'odd', 'нечёт': 'odd', 'нечётное': 'odd', 'нечетное': 'odd',
    'odd': 'odd',
    'малое': 'low', 'мало': 'low', '1-18': 'low', '1–18': 'low', 'low': 'low',
    'большое': 'high', 'много': 'high', '19-36': 'high', '19–36': 'high',
    'high': 'high',
    'д1': 'd1', 'дюжина1': 'd1', 'd1': 'd1',
    'д2': 'd2', 'дюжина2': 'd2', 'd2': 'd2',
    'д3': 'd3', 'дюжина3': 'd3', 'd3': 'd3',
}


def parse_bet(word: str) -> tuple[str, int | None] | None:
    """Слово из чата → (вид ставки, число). Число не None только для straight.

    Возвращает None, если слово вообще не про рулетку, — вызывающий отличит
    «не понял» от «понял, но не то».
    """
    w = word.strip().lower()
    if w in BET_WORDS:
        return BET_WORDS[w], None
    if w.isdigit() and 0 <= int(w) <= 36:
        return 'n', int(w)
    return None


def color_emoji(n: int) -> str:
    if n == 0:
        return '🟢'
    return '🔴' if n in RED else '⚫'


def describe(n: int) -> str:
    if n == 0:
        return '🟢 <b>0</b> — зеро, все внешние ставки проигрывают'
    parts = ['красное' if n in RED else 'чёрное',
             'чёт' if n % 2 == 0 else 'нечет',
             '1–18' if n <= 18 else '19–36',
             f'{(n - 1) // 12 + 1}-я дюжина']
    return f'{color_emoji(n)} <b>{n}</b> — ' + ', '.join(parts)


def _bets_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [kb.btn('🔴 Красное ×2', 'rl:red'), kb.btn('⚫ Чёрное ×2', 'rl:black')],
        [kb.btn('Чёт ×2', 'rl:even'), kb.btn('Нечет ×2', 'rl:odd')],
        [kb.btn('1–18 ×2', 'rl:low'), kb.btn('19–36 ×2', 'rl:high')],
        [kb.btn('1-я ×3', 'rl:d1'), kb.btn('2-я ×3', 'rl:d2'),
         kb.btn('3-я ×3', 'rl:d3')],
        [kb.btn('🎯 На число ×36', 'rl:grid')],
        [kb.btn('💰 Ставка', 'game:roulette'), kb.btn('⬅️ К играм', 'grp:classic')],
    ])


def _grid_kb() -> InlineKeyboardMarkup:
    rows = [[kb.btn('🟢 0', 'rl:n:0')]]
    for start in range(1, 37, 6):
        rows.append([kb.btn(f'{color_emoji(n)}{n}', f'rl:n:{n}')
                     for n in range(start, start + 6)])
    rows.append([kb.btn('⬅️ К ставкам', 'game:roulette')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@implement('roulette')
async def start_roulette(call: CallbackQuery, user, state) -> None:
    bet = await db.get_bet(call.from_user.id)
    await render(call,
        f'🎡 <b>Рулетка</b>\n\n'
        f'Ставка: <b>{fmt(bet)}</b>\n'
        f'Колесо: 0–36, зеро зелёное.\n\n'
        f'На что ставим?',
        _bets_kb())


@router.callback_query(F.data == 'rl:grid')
async def grid(call: CallbackQuery) -> None:
    bet = await db.get_bet(call.from_user.id)
    await render(call,
        f'🎯 <b>Ставка на число</b> · {fmt(bet)}\n\n'
        f'Выплата <b>×{STRAIGHT_MULT:.0f}</b>, шанс 1 из {POCKETS}.',
        _grid_kb())
    await call.answer()


@router.callback_query(F.data.startswith('rl:'))
async def spin(call: CallbackQuery, user) -> None:
    parts = call.data.split(':')
    kind = parts[1]
    number = int(parts[2]) if kind == 'n' else None
    await play(call, call.from_user.id, kind, number)


async def play(event, user_id: int, kind: str, number: int | None = None) -> None:
    """Один спин на заданную ставку. Общий путь для кнопки и команды в чате."""
    if kind == 'n':
        if number is None or not 0 <= number <= 36:
            await event.answer('Такого числа на колесе нет.', show_alert=True)
            return
        label, mult, wins = (f'число {number}', STRAIGHT_MULT,
                             lambda n, target=number: n == target)
    elif kind in BETS:
        label, mult, wins = BETS[kind]
    else:
        await event.answer('Неизвестная ставка.', show_alert=True)
        return

    bet = await db.get_bet(user_id)
    rnd = await engine.start_round(user_id, 'roulette', bet,
                                   chat_id=chat_id_of(event))
    if rnd is None:
        await event.answer(f'Не хватает на ставку {fmt(bet)}.', show_alert=True)
        return
    await event.answer()

    await render(event, f'🎡 Колесо крутится… {fmt(bet)} на {label}')
    await asyncio.sleep(1.6)

    rolled = rnd.pick(POCKETS)
    rnd.state = {'bet': kind if kind != 'n' else f'n{number}', 'number': rolled}

    if wins(rolled):
        payout = await engine.finish(rnd, mult)
        if payout is None:
            return
        head = (f'{describe(rolled)}\n\n'
                f'Ставка на {label} зашла: {fmt(bet)} × {mult:.0f} = '
                f'<b>{fmt(payout)}</b>\nЧистыми: <b>{fmt(payout - bet)}</b>')
    else:
        if await engine.finish(rnd, 0.0) is None:
            return
        head = (f'{describe(rolled)}\n\nСтавка на {label} не зашла — '
                f'{fmt(bet)} ушли.')

    balance = await db.get_balance(user_id)
    await render(event, f'{head}\n\nБаланс: <b>{fmt(balance)}</b>',
                 kb.again('roulette'))
