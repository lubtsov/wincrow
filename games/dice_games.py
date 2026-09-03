"""Игры на Telegram-дайсах: ставка на конкретный исход броска.

Дайс кидает Telegram, а не бот. Значение приходит в ответе на send_dice, и
подменить его нельзя — игрок видит ту же анимацию, из которой считается
результат. Своей случайности поверх этого значения здесь нет ни капли.

Что изменилось. Раньше пять игр из шести были одной и той же дуэлью «твой
бросок против броска бота»: эмодзи разные, механика одна, и выбирать игроку
было нечего. Теперь игрок ставит на конкретный исход — гол, штанга, центр
мишени, три семёрки — и выплата зависит от того, что именно выпало. Сравнение
двух бросков осталось ровно в одной игре, в боулинге, где оно и есть смысл
игры.

Одна механика на все игры, без копипасты. Игра — это таблица исходов
(PickGame + Outcome), поток один для всех:

    выбор исхода -> выбор суммы -> списание ставки -> бросок Telegram
        -> проверка исхода -> ставка × коэффициент или ноль

Коэффициенты вписаны в таблицу руками и не выводятся из RTP — это единственное
место в казино, где так. Распределения телеграмных дайсов не документированы, и
считать отдачу от предполагаемых вероятностей было бы гаданием. Зато каждое
значение раунда уезжает в rounds.state, поэтому фактическая отдача считается по
реально сыгранным раундам, а не по допущению.

Единственное, о чём надо помнить при правках: emoji в answer_dice передаётся
ИМЕНЕМ, а не позицией. У send_dice нет обязательного содержимого, поэтому
первый позиционный аргумент в aiogram — direct_messages_topic_id, и
answer_dice('⚽') падает валидацией ещё до отправки: мяч не летит вообще, и
игрок начинает кидать его руками.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

import config
import db
import emoji as E
import keyboards as kb
from db import fmt
from games import engine
from games.registry import GAMES, implement
from states import BetInput
from ui import chat_id_of, render

log = logging.getLogger(__name__)
router = Router(name='dice_games')

# Сколько ждать анимацию, прежде чем объявлять результат.
ANIM = {'🎲': 4.0, '🎯': 3.4, '⚽': 3.4, '🏀': 3.4, '🎳': 3.4, '🎰': 2.2}

# --- что реально возвращает Telegram ----------------------------------------
#
# Диапазоны Dice.value взяты из документации Bot API (объект Dice):
#   🎲 🎯 🎳 — 1–6, ⚽ 🏀 — 1–5, 🎰 — 1–64.
#
# Смысл конкретных значений Telegram не документирует нигде. Соответствия ниже
# известны из python-telegram-bot, где они описаны в самом классе Dice и прямо
# помечены как поведение Telegram, а не часть API:
#   🎯 1 — мимо мишени, 6 — центр, 2–5 — кольца от края к центру;
#   ⚽ 4–5 — гол, 1–3 — не гол;
#   🏀 4–5 — попадание, 1–3 — не попал;
#   🎳 1 — ни одной кегли, 6 — все; сравнение двух значений и есть счёт партии;
#   🎰 значение кодирует три барабана по основанию 4 (см. slot_reels).
#
# Ничего сверх этого не выдумано. Где Telegram не даёт различить категорию
# напрямую (штанга у мяча, цвет кольца у дартса), взято ближайшее по смыслу
# значение, и в правилах игры написано, какое именно: молчать о трактовке
# нельзя, а проверить её можно по rounds.state — там лежат все броски.

FACES = {'🎲': 6, '🎯': 6, '🎳': 6, '⚽': 5, '🏀': 5, '🎰': 64}

# Цифры кубика для сетки «угадай число» и для строки результата.
DIGITS = {1: '1️⃣', 2: '2️⃣', 3: '3️⃣', 4: '4️⃣', 5: '5️⃣', 6: '6️⃣'}

# Слоты: барабан из четырёх символов, три барабана — 64 равновероятные
# комбинации, и значение дайса кодирует их по основанию 4.
SLOT_SYMBOLS = ('BAR', '🍇', '🍋', '7️⃣')
SEVEN = 3


def slot_reels(value: int) -> tuple[int, int, int]:
    """Значение дайса 1..64 -> три барабана."""
    v = value - 1
    return v % 4, (v // 4) % 4, (v // 16) % 4


def slot_line(value: int) -> str:
    """Комбинация словами: «🍋 🍋 🍋»."""
    return ' '.join(SLOT_SYMBOLS[r] for r in slot_reels(value))


def triple_value(symbol: int) -> int:
    """Единственное значение дайса, дающее три одинаковых символа.

    v - 1 = s + 4s + 16s = 21s, отсюда BAR — 1, 🍇 — 22, 🍋 — 43, 7️⃣ — 64.
    Считаем, а не вписываем числа: опечатка в номере стоила бы игроку ставку,
    причём молча — комбинация просто никогда не заходила бы.
    """
    return 21 * symbol + 1

# --- исход и игра -----------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    """Один вариант ставки: подпись, коэффициент и что должно выпасть.

    values — значения дайса, на которых ставка заходит. У боулинга оно пустое:
    там исход решается сравнением двух бросков, а не самим значением.
    """

    code: str                        # едет в callback_data
    icon: str                        # юникодный значок для кнопки
    label: str
    mult: float
    values: frozenset[int] = frozenset()
    words: tuple[str, ...] = ()      # как это называют в чате

    @property
    def button(self) -> str:
        """Подпись кнопки. Только юникод: премиальный тег уехал бы строкой."""
        return f'{self.icon} {self.label} ×{self.mult:.2f}'

    @property
    def name(self) -> str:
        """Значок и название для текста сообщения."""
        return f'{self.icon} {self.label}'

    def hits(self, value: int) -> bool:
        return value in self.values


@dataclass(frozen=True)
class PickGame:
    """Игра целиком: чем кидаем, на что можно поставить и как это считается."""

    key: str
    emoji: str
    title: str
    lead: str                                     # строка на экране исходов
    throw: str                                    # «Бью…», «Кручу…»
    outcomes: tuple[Outcome, ...]
    rows: tuple[tuple[str, ...], ...]             # раскладка главного экрана
    # Боулинг и только он: два броска и сравнение вместо значения-исхода.
    duel: bool = False
    grid: tuple[str, ...] = ()                    # исходы на отдельном экране
    grid_button: str = ''
    grid_lead: str = ''
    # Строка «что выпало». По умолчанию исходы делят все значения без
    # пересечений, и выпавшее значение само называет исход.
    result: Callable[['PickGame', list[int]], str] | None = None

    @property
    def spec(self):
        """Метаданные из каталога: премиальный значок, группа, правила."""
        return GAMES[self.key]

    @property
    def dice(self) -> int:
        """Сколько бросков в раунде. Два — только у боулинга."""
        return 2 if self.duel else 1

    def find(self, code: str) -> Outcome | None:
        for outcome in self.outcomes:
            if outcome.code == code:
                return outcome
        return None

    def wins(self, outcome: Outcome, values: list[int]) -> bool:
        """Зашла ли ставка. Единственное место, где решается «выиграл или нет».

        Отдельного резолвера на игру не нужно: у одиночных игр исход — это
        множество значений, у боулинга — знак разности двух бросков. Больше
        вариантов в дайсах не бывает.
        """
        if self.duel:
            mine, theirs = values[0], values[1]
            if mine > theirs:
                return outcome.code == 'win'
            if mine < theirs:
                return outcome.code == 'lose'
            return outcome.code == 'draw'
        return outcome.hits(values[0])

    def describe(self, values: list[int]) -> str:
        """Что выпало, человеческими словами."""
        if self.result is not None:
            return self.result(self, values)
        return _partition_result(self, values)


# --- строка результата ------------------------------------------------------

def _partition_result(game: PickGame, values: list[int]) -> str:
    """Исходы делят значения без пересечений — выпавшее и есть исход.

    Годится футболу, баскетболу и дартсу: у них каждое значение принадлежит
    ровно одному варианту ставки, поэтому называть результат можно самим
    исходом, а не сырым числом.
    """
    for outcome in game.outcomes:
        if outcome.hits(values[0]):
            return outcome.name
    return f'значение {values[0]}'

def _dice_result(game: PickGame, values: list[int]) -> str:
    """У костей исходы пересекаются: 4 — это и «больше», и «чётное»."""
    value = values[0]
    return (f'{DIGITS[value]} <b>{value}</b> — '
            f'{"больше" if value > 3 else "меньше"}, '
            f'{"чётное" if value % 2 == 0 else "нечётное"}')


def _slots_result(game: PickGame, values: list[int]) -> str:
    return f'<b>{slot_line(values[0])}</b>'


def _bowling_result(game: PickGame, values: list[int]) -> str:
    return f'<b>{values[0]} : {values[1]}</b>'


# --- таблица игр ------------------------------------------------------------
#
# Коэффициенты живут только здесь. Поправить выплату — одна строка, и она же
# уедет и в кнопку, и в текст экрана, и в расчёт: разъехаться им негде.

PICKS: dict[str, PickGame] = {}


def _add(game: PickGame) -> PickGame:
    PICKS[game.key] = game
    return game


_add(PickGame(
    key='football', emoji='⚽', title='Футбол',
    lead='Бью по воротам. Ставишь на то, чем закончится удар.',
    throw='Бью…',
    # Штанга — значение 3, верхнее из «не гол». В анимации Telegram это самый
    # близкий к воротам промах, и у баскетбола то же значение означает мяч,
    # застрявший на кольце. Категорий у мяча всего пять, разделить их иначе,
    # ничего не выдумывая, нельзя.
    outcomes=(
        Outcome('goal', '⚽', 'Гол', 1.5, frozenset({4, 5}),
                ('гол', 'забил', 'goal')),
        Outcome('miss', '💨', 'Мимо', 2.3, frozenset({1, 2}),
                ('мимо', 'промах', 'miss')),
        Outcome('bar', '🥅', 'Штанга', 4.7, frozenset({3}),
                ('штанга', 'перекладина', 'bar')),
    ),
    rows=(('goal', 'miss'), ('bar',)),
))

_add(PickGame(
    key='basketball', emoji='🏀', title='Баскетбол',
    lead='Бросаю по кольцу. Ставишь на то, чем закончится бросок.',
    throw='Бросаю…',
    outcomes=(
        Outcome('goal', '🏀', 'Гол', 2.3, frozenset({4, 5}),
                ('гол', 'забил', 'попал', 'goal')),
        Outcome('miss', '💨', 'Мимо', 2.3, frozenset({1, 2}),
                ('мимо', 'промах', 'miss')),
        Outcome('stuck', '❌', 'Застрянет', 4.7, frozenset({3}),
                ('застрял', 'застрянет', 'кольцо', 'stuck')),
    ),
    rows=(('goal', 'miss'), ('stuck',)),
))

_add(PickGame(
    key='darts', emoji='🎯', title='Дартс',
    lead='Кидаю дротик. Ставишь на то, куда он попадёт.',
    throw='Кидаю дротик…',
    # Кольца идут от края к центру и чередуются по цвету: 2 — красное,
    # 3 — белое, 4 — красное, 5 — белое, 6 — центр. 1 — дротик вообще не в
    # мишени. Сравнения с броском бота здесь больше нет.
    outcomes=(
        Outcome('red', '🔴', 'Красное', 2.3, frozenset({2, 4}),
                ('красное', 'красный', 'кр', 'red')),
        Outcome('white', '⚪️', 'Белое', 2.3, frozenset({3, 5}),
                ('белое', 'белый', 'бел', 'white')),
        Outcome('center', '🎯', 'Центр', 4.7, frozenset({6}),
                ('центр', 'яблочко', 'центральное', 'center')),
        Outcome('miss', '❌', 'Мимо', 4.7, frozenset({1}),
                ('мимо', 'промах', 'miss')),
    ),
    rows=(('red', 'white'), ('center', 'miss')),
))

_add(PickGame(
    key='slots', emoji='🎰', title='Слоты',
    lead=('Крутить барабаны буду я. Ставишь на конкретную комбинацию: выпала '
          'она — платим по коэффициенту, любая другая забирает ставку.'),
    throw='Кручу барабаны…',
    outcomes=(
        Outcome('lemon', '🍋', 'Три лимона', 3.5,
                frozenset({triple_value(2)}), ('лимон', 'лимоны', '🍋')),
        Outcome('grape', '🍇', 'Три винограда', 4.2,
                frozenset({triple_value(1)}), ('виноград', 'винограды', '🍇')),
        Outcome('bar', '🎰', 'Три BAR', 5.4,
                frozenset({triple_value(0)}), ('бар', 'bar')),
        Outcome('seven', '7️⃣', 'Три семёрки', 25.0,
                frozenset({triple_value(SEVEN)}),
                ('семёрки', 'семерки', '777', 'семь')),
    ),
    rows=(('lemon', 'grape'), ('bar', 'seven')),
    result=_slots_result,
))

_add(PickGame(
    key='dice', emoji='🎲', title='Кости',
    lead='Кидаю кубик Telegram: выпадет число от 1 до 6.',
    throw='Кидаю кубик…',
    # Исходы здесь пересекаются, и это нормально: 4 — сразу и «больше», и
    # «чётное». Ставка всегда одна, поэтому пересечение ничего не ломает.
    outcomes=(
        Outcome('over', '🔼', 'Больше (4–6)', 1.9, frozenset({4, 5, 6}),
                ('больше', 'выше', 'б', '>', 'over')),
        Outcome('under', '🔽', 'Меньше (1–3)', 1.9, frozenset({1, 2, 3}),
                ('меньше', 'ниже', 'м', '<', 'under')),
        Outcome('even', '⚪', 'Чётное', 1.9, frozenset({2, 4, 6}),
                ('чёт', 'чет', 'чётное', 'четное', 'even')),
        Outcome('odd', '⚫', 'Нечётное', 1.9, frozenset({1, 3, 5}),
                ('нечёт', 'нечет', 'нечётное', 'нечетное', 'odd')),
    ) + tuple(
        Outcome(f'n{n}', DIGITS[n], f'Ровно {n}', 5.7, frozenset({n}), (str(n),))
        for n in range(1, 7)
    ),
    rows=(('over', 'under'), ('even', 'odd')),
    grid=tuple(f'n{n}' for n in range(1, 7)),
    grid_button='🎯 Угадай число ×5.70',
    grid_lead='Одно число из шести. Угадал — <b>×5.70</b>, не угадал — ставка уходит.',
    result=_dice_result,
))

_add(PickGame(
    key='bowling', emoji='🎳', title='Боулинг',
    lead=('Единственная игра, где мы играем друг против друга: кидаем по шару, '
          'у кого кеглей больше — тот и взял партию. Ставишь на исход партии.'),
    throw='Катим шары…',
    duel=True,
    # Здесь сравнение двух бросков осталось намеренно — в этом вся игра.
    # Ничья не возвращает ставку, а платит по своему коэффициенту: это такой
    # же исход, как победа, и ставить на неё можно отдельно.
    outcomes=(
        Outcome('win', '🏆', 'Победа', 1.9,
                words=('победа', 'выиграю', 'п', 'win')),
        Outcome('lose', '🚫', 'Поражение', 1.9,
                words=('поражение', 'проиграю', 'пр', 'lose')),
        Outcome('draw', '🤝', 'Ничья', 4.7,
                words=('ничья', 'ровно', 'н', 'draw')),
    ),
    rows=(('win', 'lose'), ('draw',)),
    result=_bowling_result,
))


# --- разбор из чата ---------------------------------------------------------

def parse_pick(key: str, word: str) -> str | None:
    """Слово из чата -> код исхода. None — слово не про эту игру."""
    game = PICKS.get(key)
    if game is None:
        return None
    w = word.strip().lower()
    for outcome in game.outcomes:
        if w == outcome.code or w in outcome.words:
            return outcome.code
    return None

# --- экраны -----------------------------------------------------------------

def _picks_kb(game: PickGame) -> InlineKeyboardMarkup:
    """Список исходов. Каждый коэффициент — отдельная кнопка со своей ставкой."""
    rows = [[kb.btn(game.find(code).button, f'pb:{game.key}:{code}')
             for code in row] for row in game.rows]
    if game.grid:
        rows.append([kb.btn(game.grid_button, f'pk:{game.key}:g')])
    rows.append([kb.btn('📖 Правила', f'rules:{game.key}'),
                 kb.btn('⬅️ К играм', f'grp:{game.spec.group}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _grid_kb(game: PickGame) -> InlineKeyboardMarkup:
    """Сетка чисел: 1️⃣ 2️⃣ 3️⃣ / 4️⃣ 5️⃣ 6️⃣."""
    codes = list(game.grid)
    rows = [[kb.btn(game.find(c).icon, f'pb:{game.key}:{c}')
             for c in codes[i:i + 3]] for i in range(0, len(codes), 3)]
    rows.append([kb.btn('⬅️ К исходам', f'pk:{game.key}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _money(user_id: int) -> tuple[int, int]:
    """Баланс и текущая ставка одним запросом — обе цифры нужны на всех экранах."""
    user = await db.get_user(user_id)
    if user is None:
        return 0, config.MIN_BET_CENTS
    return user['balance_cents'], user['bet_cents']


async def show_picks(event, game: PickGame) -> None:
    balance, bet = await _money(event.from_user.id)
    await render(event,
        f'{game.spec.tag} <b>{game.title}</b>\n{game.lead}\n\n'
        f'Баланс: <b>{fmt(balance)}</b>\n'
        f'Ставка: <b>{fmt(bet)}</b>\n\n'
        f'Выбери исход — сумму спрошу на следующем экране.',
        _picks_kb(game))


async def show_grid(event, game: PickGame) -> None:
    balance, bet = await _money(event.from_user.id)
    await render(event,
        f'{game.spec.tag} <b>{game.title}</b>\n\n{game.grid_lead}\n\n'
        f'Ставка: <b>{fmt(bet)}</b>',
        _grid_kb(game))


async def show_stake(event, game: PickGame, outcome: Outcome) -> None:
    """Сумма на уже выбранный исход. Исход едет в кнопках, а не в состоянии."""
    balance, bet = await _money(event.from_user.id)
    text = (f'{game.spec.tag} <b>{game.title}</b> · {outcome.name} '
            f'×{outcome.mult:.2f}\n\n'
            f'Ставка: <b>{fmt(bet)}</b>\n'
            f'Зайдёт — вернётся '
            f'<b>{fmt(engine.payout_cents(bet, outcome.mult))}</b>\n'
            f'Баланс: <b>{fmt(balance)}</b>')
    if bet > balance:
        text += ('\n\n⚠️ На такую ставку не хватает — пополни баланс или '
                 'убавь ставку.')
    await render(event, text, kb.pick_stake(game.key, outcome.code, bet))

def _make(key: str):
    """Вход в игру — список исходов. Отличаются игры только таблицей."""
    async def start(call, user, state) -> None:
        await show_picks(call, PICKS[key])
    start.__name__ = f'start_{key}'
    return implement(key)(start)


for _key in PICKS:
    _make(_key)


# --- раунд ------------------------------------------------------------------

async def _throw(game: PickGame, message) -> list[int]:
    """Броски раунда. emoji — обязательно по имени, иначе дайс не улетит вовсе."""
    if not game.duel:
        return [(await message.answer_dice(emoji=game.emoji)).dice.value]
    await message.answer('Твой шар:')
    mine = (await message.answer_dice(emoji=game.emoji)).dice.value
    await message.answer('Мой шар:')
    theirs = (await message.answer_dice(emoji=game.emoji)).dice.value
    return [mine, theirs]


async def play(event, user_id: int, key: str, code: str) -> None:
    """Списание, бросок, расчёт. Общий путь для кнопки и команды из чата."""
    game = PICKS.get(key)
    if game is None:
        await event.answer('Неизвестная игра.', show_alert=True)
        return
    outcome = game.find(code)
    if outcome is None:
        await event.answer('Такой ставки в этой игре нет.', show_alert=True)
        return

    bet = await db.get_bet(user_id)
    rnd = await engine.start_round(user_id, key, bet, chat_id=chat_id_of(event))
    if rnd is None:
        await event.answer(f'Не хватает на ставку {fmt(bet)}.', show_alert=True)
        return
    await event.answer()

    await render(event,
                 f'{game.spec.tag} <b>{game.title}</b> · {fmt(bet)} на '
                 f'{outcome.name} ×{outcome.mult:.2f}\n\n{game.throw}')

    chat_message = getattr(event, 'message', None)
    try:
        values = await _throw(game, chat_message)
    except Exception as e:
        # Бросок не ушёл — раунда не было, ставку возвращаем. Сказать об этом
        # обязательно: молча съеденная ставка выглядит как кража.
        log.warning('не смог кинуть %s для %s: %s', game.emoji, user_id, e)
        await engine.void(rnd)
        await render(event,
                     f'{E.FAIL} Не получилось кинуть {game.emoji} в этом чате — '
                     f'ставка {fmt(bet)} вернулась на баланс.',
                     kb.pick_again(key, code), new=True)
        return

    await asyncio.sleep(ANIM[game.emoji])

    rnd.state = {'pick': code, 'values': values}
    line = game.describe(values)

    if game.wins(outcome, values):
        payout = await engine.finish(rnd, outcome.mult)
        if payout is None:
            return
        head = (f'{E.TROPHY} {line}\n\n'
                f'Ставка на {outcome.name} зашла: {fmt(bet)} × '
                f'{outcome.mult:.2f} = <b>{fmt(payout)}</b>\n'
                f'Чистыми: <b>{fmt(payout - bet)}</b>')
    else:
        if await engine.finish(rnd, 0.0) is None:
            return
        head = (f'{E.FAIL} {line}\n\n'
                f'Ставка на {outcome.name} не зашла — {fmt(bet)} ушли.')

    balance = await db.get_balance(user_id)
    await render(event, f'{head}\n\nБаланс: <b>{fmt(balance)}</b>',
                 kb.pick_again(key, code), new=True)

# --- кнопки -----------------------------------------------------------------

def _parse(data: str) -> tuple[PickGame | None, Outcome | None, str]:
    """'pb:dice:n4:min' -> (игра, исход, операция). Мусор — (None, None, '')."""
    parts = data.split(':')
    game = PICKS.get(parts[1]) if len(parts) > 1 else None
    if game is None:
        return None, None, ''
    outcome = game.find(parts[2]) if len(parts) > 2 else None
    return game, outcome, parts[3] if len(parts) > 3 else ''


@router.callback_query(F.data.startswith('pk:'))
async def on_picks(call: CallbackQuery, state: FSMContext) -> None:
    game, _, _ = _parse(call.data)
    if game is None:
        await call.answer('Игра не найдена.', show_alert=True)
        return
    await state.clear()
    if call.data.endswith(':g') and game.grid:
        await show_grid(call, game)
    else:
        await show_picks(call, game)
    await call.answer()


@router.callback_query(F.data.startswith('pb:'))
async def on_stake(call: CallbackQuery, state: FSMContext, user) -> None:
    game, outcome, op = _parse(call.data)
    if game is None or outcome is None:
        await call.answer('Кнопка устарела.', show_alert=True)
        return

    if not op:
        await state.clear()
        await show_stake(call, game, outcome)
        await call.answer()
        return

    if op == 'ask':
        await state.set_state(BetInput.amount)
        await state.update_data(game=game.key, pick=outcome.code)
        await render(call,
            f'{game.spec.tag} <b>{game.title}</b> · {outcome.name}\n\n'
            f'Пришли сумму ставки числом, например <code>7.50</code>.\n'
            f'Допустимо от {fmt(config.MIN_BET_CENTS)} до '
            f'{fmt(config.MAX_BET_CENTS)}.',
            kb.cancel_to(f'pb:{game.key}:{outcome.code}'))
        await call.answer()
        return

    if op == 'min':
        new_bet = config.MIN_BET_CENTS
    elif op == 'max':
        # Не выше баланса: ставка, на которую заведомо не хватает, бесполезна.
        new_bet = min(config.MAX_BET_CENTS,
                      max(config.MIN_BET_CENTS, user['balance_cents']))
    else:
        try:
            new_bet = user['bet_cents'] + int(op)
        except ValueError:
            await call.answer('Кнопка устарела.', show_alert=True)
            return

    saved = await db.set_bet(user['user_id'], new_bet)
    if saved != new_bet:
        limit = (f'минимум {fmt(config.MIN_BET_CENTS)}'
                 if new_bet < saved else f'максимум {fmt(config.MAX_BET_CENTS)}')
        await call.answer(f'Упёрлись в {limit}.')
    else:
        await call.answer()
    await show_stake(call, game, outcome)


@router.callback_query(F.data.startswith('pp:'))
async def on_play(call: CallbackQuery, state: FSMContext) -> None:
    game, outcome, _ = _parse(call.data)
    if game is None or outcome is None:
        await call.answer('Кнопка устарела.', show_alert=True)
        return
    await state.clear()
    await play(call, call.from_user.id, game.key, outcome.code)
