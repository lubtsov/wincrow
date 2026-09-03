"""Два и три кубика: ставка на сумму — больше, меньше или точное число.

Кубики кидает Telegram и кидает их бот: игрок только выбирает ставку, дальше
бот сам бросает, сам ждёт анимацию и сам объявляет результат.

Про допущение — честно. Дуэли (dice_games.py) обходятся без него: там бот
кидает тот же кубик, что и игрок, шансы симметричны по построению, и знать
распределение Telegram не нужно. Со ставкой на сумму так не выйдет — выплата за
«ровно 7» зависит от того, насколько кубик ровный, поэтому таблица считается от
равномерного 1–6. Значение по-прежнему приходит от Telegram и подкрутить его
бот не может, но и доказать равномерность не может тоже: это единственное место
в казино, где отдача 97% опирается на допущение, и в правилах игры так и
написано.

Проверить допущение можно по базе: все броски раунда уезжают в rounds.state,
так что фактическое распределение считается по реально сыгранным раундам.

Множители нигде не вписаны руками. Число раскладов каждой суммы считается
свёрткой, дальше выплата = RTP × число исходов / число выигрышных. Отсюда
×1.94 на «больше/меньше» с тремя кубиками и ×209.52 на сумму 3 — обе цифры
выведены, а не подобраны.
"""

import asyncio
import logging
import math
from dataclasses import dataclass, field

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

import config
import db
import emoji as E
import keyboards as kb
from db import fmt
from games import engine
from games.dice_games import ANIM
from games.registry import implement
from ui import chat_id_of, render

log = logging.getLogger(__name__)
router = Router(name='dice_sum')

# Обычный кубик Telegram: шесть граней, значение приходит в ответе на send_dice.
CUBE = '🎲'
FACES = 6

# Числительное для текста: «кидаю два кубика», «сумма двух кубиков».
COUNT_WORD = {2: 'два', 3: 'три'}
COUNT_GEN = {2: 'двух', 3: 'трёх'}


def counts(dice: int) -> dict[int, int]:
    """Сумма -> сколько раскладов её дают. Свёртка, а не таблица руками.

    Для двух кубиков это школьные 36 исходов, для трёх — 216, и переписывать их
    в код незачем: свёртка даёт то же самое и не врёт при опечатке.
    """
    ways = {0: 1}
    for _ in range(dice):
        nxt: dict[int, int] = {}
        for total, count in ways.items():
            for face in range(1, FACES + 1):
                nxt[total + face] = nxt.get(total + face, 0) + count
        ways = nxt
    return dict(sorted(ways.items()))


@dataclass(frozen=True)
class Table:
    """Одна игра: сколько кубиков и где граница «больше/меньше».

    pivot дробный у трёх кубиков (10.5) и целый у двух (7). Разница
    принципиальная: 10.5 делит 216 исходов ровно пополам, а целая семёрка
    оставляет 6 исходов из 36, на которых проигрывают обе внешние ставки. Это
    и есть зеро двух кубиков — оттуда берётся вся маржа казино.
    """

    key: str
    title: str
    dice: int
    pivot: float
    ways: dict[int, int] = field(compare=False, repr=False)

    @property
    def total(self) -> int:
        return FACES ** self.dice

    @property
    def low(self) -> int:
        return self.dice

    @property
    def high(self) -> int:
        return self.dice * FACES

    def outcomes(self, kind: str, number: int | None = None) -> int:
        """Сколько исходов из total выигрывают такую ставку."""
        if kind == 'over':
            return sum(w for s, w in self.ways.items() if s > self.pivot)
        if kind == 'under':
            return sum(w for s, w in self.ways.items() if s < self.pivot)
        if kind == 'n':
            return self.ways.get(number, 0)
        return 0

    @property
    def push_outcomes(self) -> int:
        """Исходы, на которых «больше» и «меньше» проигрывают оба."""
        return self.total - self.outcomes('over') - self.outcomes('under')

    def mult(self, kind: str, number: int | None = None) -> float:
        """Выплата из отдачи: RTP × все исходы / выигрышные."""
        wins = self.outcomes(kind, number)
        return config.RTP * self.total / wins if wins else 0.0

    def mult_range(self) -> tuple[float, float]:
        """Разброс выплат за точную сумму — для экрана выбора."""
        all_mults = [self.mult('n', n) for n in range(self.low, self.high + 1)]
        return min(all_mults), max(all_mults)

    def label(self, kind: str, number: int | None = None) -> str:
        if kind == 'over':
            return f'больше {math.floor(self.pivot)}'
        if kind == 'under':
            return f'меньше {math.ceil(self.pivot)}'
        return f'сумму {number}'

    def span(self, kind: str) -> str:
        """Какие суммы выигрывают: «8–12». Игроку понятнее, чем граница."""
        if kind == 'over':
            return f'{math.floor(self.pivot) + 1}–{self.high}'
        return f'{self.low}–{math.ceil(self.pivot) - 1}'

    def wins(self, kind: str, number: int | None, total: int) -> bool:
        if kind == 'over':
            return total > self.pivot
        if kind == 'under':
            return total < self.pivot
        return total == number

    def valid(self, kind: str, number: int | None) -> bool:
        if kind in ('over', 'under'):
            return True
        return (kind == 'n' and number is not None
                and self.low <= number <= self.high)


TABLES: dict[str, Table] = {
    'dice2': Table('dice2', 'Два кубика', 2, 7, counts(2)),
    'dice3': Table('dice3', 'Три кубика', 3, 10.5, counts(3)),
}

# Короткий префикс в callback_data: 64 байта на кнопку, и на «dice3:n:18» их
# жалеть незачем, но короткое читается в логах лучше.
PREFIX = {'d2': 'dice2', 'd3': 'dice3'}
CODE = {key: code for code, key in PREFIX.items()}


# --- разбор из чата ---------------------------------------------------------

# Как ставку называют в чате. Точная сумма — число, она распознаётся отдельно.
BET_WORDS = {
    'больше': 'over', 'больш': 'over', 'б': 'over', '>': 'over',
    'выше': 'over', 'over': 'over', 'up': 'over',
    'меньше': 'under', 'меньш': 'under', 'м': 'under', '<': 'under',
    'ниже': 'under', 'under': 'under', 'down': 'under',
}


def parse_pick(key: str, word: str) -> tuple[str, int | None] | None:
    """Слово из чата -> (вид ставки, сумма). None — слово не про эту игру."""
    table = TABLES.get(key)
    if table is None:
        return None
    w = word.strip().lower()
    if w in BET_WORDS:
        return BET_WORDS[w], None
    if w.isdigit() and table.low <= int(w) <= table.high:
        return 'n', int(w)
    return None


# --- экраны -----------------------------------------------------------------

def _pick_kb(table: Table) -> InlineKeyboardMarkup:
    """Экран выбора ставки. Подписи кнопок — юникод: тег там уехал бы строкой."""
    code = CODE[table.key]
    out_mult = table.mult('over')          # «больше» и «меньше» симметричны
    low, high = table.mult_range()
    return InlineKeyboardMarkup(inline_keyboard=[
        [kb.btn(f'⬇️ {table.span("under")} ×{out_mult:.2f}', f'{code}:under'),
         kb.btn(f'⬆️ {table.span("over")} ×{out_mult:.2f}', f'{code}:over')],
        [kb.btn(f'🎯 Точная сумма ×{low:.2f}–×{high:.2f}', f'{code}:grid')],
        [kb.btn('💰 Ставка', f'game:{table.key}'),
         kb.btn('⬅️ К играм', 'grp:dice')],
    ])


def _grid_kb(table: Table) -> InlineKeyboardMarkup:
    """Все суммы с их выплатами. Множитель на кнопке — чтобы не считать в уме."""
    code = CODE[table.key]
    nums = list(range(table.low, table.high + 1))
    rows = [[kb.btn(f'{n} ×{table.mult("n", n):.0f}', f'{code}:n:{n}')
             for n in nums[i:i + 3]] for i in range(0, len(nums), 3)]
    rows.append([kb.btn('⬅️ К ставкам', f'play:{table.key}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_pick(event, table: Table) -> None:
    bet = await db.get_bet(event.from_user.id)
    push = table.push_outcomes
    note = (f'Ровно {math.floor(table.pivot)} — проигрывают обе внешние ставки '
            f'({push} из {table.total} исходов).\n'
            if push else
            f'Граница {table.pivot} делит {table.total} исходов ровно пополам.\n')
    await render(event,
        f'{E.DICE} <b>{table.title}</b>\n\n'
        f'Ставка: <b>{fmt(bet)}</b>\n'
        f'Бросаю {COUNT_WORD[table.dice]} кубика, считаю сумму '
        f'({table.low}–{table.high}).\n'
        f'{note}\n'
        f'На что ставим?',
        _pick_kb(table))


async def _show_grid(event, table: Table) -> None:
    bet = await db.get_bet(event.from_user.id)
    await render(event,
        f'🎯 <b>Точная сумма</b> · {fmt(bet)}\n\n'
        f'Выплата = 0.97 × {table.total} / число раскладов суммы. Поэтому '
        f'края платят больше всего: сумма {table.low} собирается '
        f'единственным способом.',
        _grid_kb(table))


@implement('dice2')
async def start_dice2(call: CallbackQuery, user, state) -> None:
    await _show_pick(call, TABLES['dice2'])


@implement('dice3')
async def start_dice3(call: CallbackQuery, user, state) -> None:
    await _show_pick(call, TABLES['dice3'])


# --- раунд ------------------------------------------------------------------

async def play(event, user_id: int, key: str, kind: str,
               number: int | None = None) -> None:
    """Ставка, броски, расчёт. Общий путь для кнопки и команды из чата."""
    table = TABLES.get(key)
    if table is None:
        await event.answer('Неизвестная игра.', show_alert=True)
        return
    if not table.valid(kind, number):
        await event.answer(
            f'Сумма {COUNT_GEN[table.dice]} кубиков — от {table.low} '
            f'до {table.high}.', show_alert=True)
        return

    mult = table.mult(kind, number)
    label = table.label(kind, number)
    bet = await db.get_bet(user_id)
    rnd = await engine.start_round(user_id, key, bet, chat_id=chat_id_of(event))
    if rnd is None:
        await event.answer(f'Не хватает на ставку {fmt(bet)}.', show_alert=True)
        return
    await event.answer()

    await render(event,
                 f'{E.DICE} <b>{table.title}</b> · {fmt(bet)} на {label} '
                 f'×{mult:.2f}\n\nКидаю {COUNT_WORD[table.dice]} кубика…')

    # Кидает бот, игроку кидать нечего. emoji — по имени: первый позиционный
    # аргумент answer_dice занят direct_messages_topic_id, и кубик бы не улетел
    # (тот самый баг, из-за которого мяч приходилось кидать руками).
    chat_message = getattr(event, 'message', None)
    try:
        rolls = [(await chat_message.answer_dice(emoji=CUBE)).dice.value
                 for _ in range(table.dice)]
    except Exception as e:
        log.warning('не смог кинуть кубики для %s: %s', user_id, e)
        await engine.void(rnd)
        await render(event,
                     f'{E.FAIL} Не получилось кинуть кубики в этом чате — '
                     f'ставка {fmt(bet)} вернулась на баланс.',
                     kb.again(key), new=True)
        return

    await asyncio.sleep(ANIM[CUBE])

    total = sum(rolls)
    rnd.state = {'pick': kind, 'number': number, 'dice': rolls, 'sum': total}
    line = ' + '.join(str(v) for v in rolls) + f' = <b>{total}</b>'

    if table.wins(kind, number, total):
        payout = await engine.finish(rnd, mult)
        if payout is None:
            return
        head = (f'{E.TROPHY} {line}\n\n'
                f'Ставка на {label} зашла: {fmt(bet)} × {mult:.2f} = '
                f'<b>{fmt(payout)}</b>\nЧистыми: <b>{fmt(payout - bet)}</b>')
    else:
        if await engine.finish(rnd, 0.0) is None:
            return
        head = f'{E.FAIL} {line}\n\n'
        if kind in ('over', 'under') and total == table.pivot:
            head += (f'Ровно {total} — на этой сумме проигрывают и «больше», '
                     f'и «меньше». Ставка {fmt(bet)} ушла.')
        else:
            head += f'Ставка на {label} не зашла — {fmt(bet)} ушли.'

    balance = await db.get_balance(user_id)
    await render(event, f'{head}\n\nБаланс: <b>{fmt(balance)}</b>',
                 kb.again(key), new=True)


@router.callback_query(F.data.startswith('d2:') | F.data.startswith('d3:'))
async def on_pick(call: CallbackQuery, user) -> None:
    parts = call.data.split(':')
    table = TABLES.get(PREFIX.get(parts[0], ''))
    if table is None:
        await call.answer('Неизвестная игра.', show_alert=True)
        return

    kind = parts[1] if len(parts) > 1 else ''
    if kind == 'grid':
        await _show_grid(call, table)
        await call.answer()
        return

    number = int(parts[2]) if kind == 'n' and len(parts) > 2 else None
    await play(call, call.from_user.id, table.key, kind, number)
