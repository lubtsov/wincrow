"""Текстовые команды: бот играбелен прямо в чате.

Грамматика одна на всё: <b>игра ставка параметр</b>.

    мины 0.5 2      — поле с 2 минами на ставку $0.50
    краш 1 2.5      — краш с авто-выводом на ×2.5
    рулетка 1 17    — доллар на число 17
    деп 5           — счёт на $5

Ставка необязательна: без неё берётся сохранённая. Третий токен свой у каждой
игры (сколько мин, какая сторона монеты, на что в рулетке, где выйти из краша);
у игр без параметра он просто игнорируется.

Разбор — это фильтр, а не хендлер: если слово не команда, апдейт уходит дальше,
и в личке его по-прежнему подхватывает фолбэк с меню. Иначе меню перестало бы
показываться на случайный текст.

Почему в группе игра требует ставку числом. Бот с выключенным privacy mode
видит всю болтовню, и одно слово «футбол» не должно списывать деньги у того,
кто просто обсуждает футбол. В личке двусмысленности нет — там команда без
ставки открывает экран.
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import config
import db
import emoji as E
import keyboards as kb
from db import fmt
from games import coin, crash, dice_games, dice_sum, mines, roulette
from games.registry import GAMES
from ui import ChatCall, render

from . import balance as balance_h
from . import chats as chats_h
from . import common
from . import games as games_h

router = Router(name='chat')

# Больше трёх слов — это фраза, а не команда. Ограничение заодно защищает от
# срабатывания на середину сообщения: команда всегда идёт первым словом.
MAX_TOKENS = 3

# Как игру зовут в чате. Односложные и бытовые слова («мяч», «куб») намеренно
# не берём: цена ложного срабатывания — списанная ставка.
GAME_WORDS = {
    'мины': 'mines', 'мина': 'mines', 'mines': 'mines',
    'башня': 'tower', 'вышка': 'tower', 'tower': 'tower',
    'краш': 'crash', 'crash': 'crash',
    'монетка': 'coin', 'монета': 'coin', 'коин': 'coin', 'coin': 'coin',
    'рулетка': 'roulette', 'рулет': 'roulette', 'roulette': 'roulette',
    'блэкджек': 'blackjack', 'блекджек': 'blackjack', 'бж': 'blackjack',
    'blackjack': 'blackjack', 'bj': 'blackjack',
    'слоты': 'slots', 'слот': 'slots', 'slots': 'slots', 'казик': 'slots',
    'кости': 'dice', 'кубик': 'dice', 'dice': 'dice',
    # Ставка на сумму. Множественное «кубики» — двойка, тройка просит цифру:
    # односложного имени, которое не путалось бы с дуэльным «кубик», нет.
    'кубики': 'dice2', 'кубики2': 'dice2', 'дайс2': 'dice2', 'dice2': 'dice2',
    'кубики3': 'dice3', 'дайс3': 'dice3', 'dice3': 'dice3',
    'дартс': 'darts', 'darts': 'darts',
    'футбол': 'football', 'football': 'football',
    'баскет': 'basketball', 'баскетбол': 'basketball',
    'basketball': 'basketball',
    'боулинг': 'bowling', 'кегли': 'bowling', 'bowling': 'bowling',
    'дуэль': 'duel', 'дуель': 'duel', 'duel': 'duel', 'пвп': 'duel',
    'pvp': 'duel',
    'джекпот': 'jackpot', 'джек': 'jackpot', 'jackpot': 'jackpot',
}

# Всё, что не игра. Значение — ветка в _run.
CMD_WORDS = {
    'меню': 'menu', 'menu': 'menu', 'старт': 'menu', 'start': 'menu',
    'баланс': 'balance', 'бал': 'balance', 'balance': 'balance',
    'счёт': 'balance', 'счет': 'balance',
    'профиль': 'profile', 'profile': 'profile', 'стата': 'profile',
    'статистика': 'profile',
    'деп': 'dep', 'депозит': 'dep', 'пополнить': 'dep', 'пополнение': 'dep',
    'dep': 'dep', 'deposit': 'dep',
    'вывод': 'wd', 'вывести': 'wd', 'wd': 'wd', 'withdraw': 'wd',
    'ставка': 'bet', 'ставку': 'bet', 'bet': 'bet',
    'топ': 'top', 'top': 'top',
    'реф': 'refs', 'рефка': 'refs', 'пригласить': 'refs', 'ref': 'refs',
    'refs': 'refs',
    'чаты': 'mychats', 'chats': 'mychats', 'мойчат': 'mychats',
    'игры': 'games', 'игра': 'games', 'games': 'games',
    'помощь': 'help', 'хелп': 'help', 'help': 'help', 'команды': 'help',
    'код': 'code', 'промокод': 'code', 'promo': 'code',
    'история': 'history', 'history': 'history',
}


# --- разбор -----------------------------------------------------------------

def _head(token: str) -> str:
    """Первое слово в канонический вид: /Мины@pilot_bot -> мины."""
    return token.lstrip('/').split('@', 1)[0].lower()


def parse(text: str) -> tuple[str, str, int | None, list[str]] | None:
    """'мины 0.5 2' -> ('game', 'mines', 50, ['2']).

    Первый токен, который читается как сумма, считается ставкой; остальные —
    параметрами игры. Так работают и «мины 0.5 2», и «монетка орёл»: порядок
    из справки соблюдать удобно, но не обязательно.
    """
    tokens = (text or '').split()
    if not tokens or len(tokens) > MAX_TOKENS:
        return None

    head = _head(tokens[0])
    if head in GAME_WORDS:
        kind, key = 'game', GAME_WORDS[head]
    elif head in CMD_WORDS:
        kind, key = 'cmd', CMD_WORDS[head]
    else:
        return None

    cents: int | None = None
    params: list[str] = []
    for token in tokens[1:]:
        if cents is None:
            value = db.parse_cents(token)
            if value is not None:
                cents = value
                continue
        params.append(token)

    return kind, key, cents, params


async def chat_command(message: Message) -> dict | bool:
    """Фильтр-разборщик. Не команда — False, и апдейт живёт дальше."""
    parsed = parse(message.text or '')
    if parsed is None:
        return False
    kind, key, cents, params = parsed
    return {'kind': kind, 'key': key, 'cents': cents, 'params': params}


# --- параметры игр ----------------------------------------------------------

def _int_param(word: str, low: int, high: int) -> int | None:
    if not word.isdigit():
        return None
    value = int(word)
    return value if low <= value <= high else None


def _mult_param(word: str) -> float | None:
    """'2.5', '2,5', '×2.5', 'x2' -> 2.5. Иначе None."""
    raw = word.lower().lstrip('x×х*').replace(',', '.')
    try:
        return float(raw)
    except ValueError:
        return None


HINTS = {
    'mines': 'мины <ставка> <сколько мин 1–24>, например <code>мины 0.5 3</code>',
    'coin': 'монетка <ставка> <орёл|решка>, например <code>монетка 1 орёл</code>',
    'roulette': ('рулетка <ставка> <красное|чёрное|чёт|нечет|1-18|19-36|д1|д2|д3|'
                 'число 0–36>, например <code>рулетка 1 красное</code>'),
    'crash': 'краш <ставка> [авто-вывод], например <code>краш 1 2.5</code>',
    # Точная сумма — это число, и число же читается как ставка. Первым идёт
    # ставка, поэтому «кубики 1 7» — доллар на семёрку, а «кубики 7» — ставка $7.
    'dice2': ('кубики <ставка> <больше|меньше|сумма 2–12>, например '
              '<code>кубики 1 больше</code> или <code>кубики 1 7</code>'),
    'dice3': ('кубики3 <ставка> <больше|меньше|сумма 3–18>, например '
              '<code>кубики3 1 меньше</code> или <code>кубики3 1 11</code>'),
    # Дайс-игры со ставкой на исход. Слово-исход — третий токен, и оно же
    # решает, какой коэффициент считается.
    'football': ('футбол <ставка> <гол|мимо|штанга>, например '
                 '<code>футбол 1 штанга</code>'),
    'basketball': ('баскет <ставка> <гол|мимо|застрял>, например '
                   '<code>баскет 1 гол</code>'),
    'darts': ('дартс <ставка> <красное|белое|центр|мимо>, например '
              '<code>дартс 1 центр</code>'),
    'slots': ('слоты <ставка> <лимон|виноград|бар|семёрки>, например '
              '<code>слоты 1 семёрки</code>'),
    'dice': ('кости <ставка> <больше|меньше|чёт|нечет|число 1–6>, например '
             '<code>кости 1 больше</code> или <code>кости 1 4</code>'),
    'bowling': ('боулинг <ставка> <победа|поражение|ничья>, например '
                '<code>боулинг 1 победа</code>'),
}


async def _play_game(message: Message, user, state: FSMContext, key: str,
                     cents: int | None, params: list[str]) -> None:
    spec = GAMES.get(key)
    if spec is None or not spec.ready or spec.start is None:
        await message.reply('Эта игра ещё не открыта.')
        return

    # В группе играем только по явной ставке — см. модульный докстринг.
    if cents is None and message.chat.type != 'private':
        return

    if cents is not None:
        saved = await db.set_bet(user['user_id'], cents)
        if saved != cents:
            await message.reply(
                f'Ставка вне лимитов ({fmt(config.MIN_BET_CENTS)}–'
                f'{fmt(config.MAX_BET_CENTS)}) — играю на {fmt(saved)}.')
        # Строка юзера прочитана до правки ставки, а игры со своим экраном
        # входа берут ставку из неё, а не из базы.
        user = await db.get_user(user['user_id'])

    call = ChatCall(message)
    user_id = user['user_id']

    if key == 'mines' and params:
        n = _int_param(params[0], 1, mines.CELLS - 1)
        if n is None:
            await message.reply(f'Не понял параметр. Формат: {HINTS["mines"]}')
            return
        await mines.play(call, user_id, n)
        return

    if key == 'coin' and params:
        side = coin.parse_side(params[0])
        if side is None:
            await message.reply(f'Не понял сторону. Формат: {HINTS["coin"]}')
            return
        await coin.play(call, user_id, side)
        return

    if key == 'roulette' and params:
        bet = roulette.parse_bet(params[0])
        if bet is None:
            await message.reply(f'Не понял ставку. Формат: {HINTS["roulette"]}')
            return
        kind, number = bet
        await roulette.play(call, user_id, kind, number)
        return

    if key == 'crash' and params:
        auto = _mult_param(params[0])
        if auto is None:
            await message.reply(f'Не понял множитель. Формат: {HINTS["crash"]}')
            return
        await crash.play(call, user_id, auto)
        return

    if key in dice_sum.TABLES and params:
        pick = dice_sum.parse_pick(key, params[0])
        if pick is None:
            await message.reply(f'Не понял ставку. Формат: {HINTS[key]}')
            return
        await dice_sum.play(call, user_id, key, pick[0], pick[1])
        return

    # Дайс-игры на исход: слово-исход обязательно, иначе непонятно, какой
    # коэффициент считать. Без него открывается экран выбора.
    if key in dice_games.PICKS and params:
        code = dice_games.parse_pick(key, params[0])
        if code is None:
            await message.reply(f'Не понял исход. Формат: {HINTS[key]}')
            return
        await dice_games.play(call, user_id, key, code)
        return

    # Параметра нет или игре он не нужен — обычный вход в игру.
    await spec.start(call, user, state)


# --- прочие команды ---------------------------------------------------------

async def _run_cmd(message: Message, user, is_admin: bool, state: FSMContext,
                   key: str, cents: int | None, params: list[str]) -> None:
    user_id = user['user_id']

    if key == 'menu':
        await common.show_menu(message, user, is_admin)

    elif key == 'balance':
        await render(message, balance_h.balance_text(user), kb.balance_menu())

    elif key == 'profile':
        await render(message, await common.profile_text(user), kb.back_menu())

    elif key == 'dep':
        if cents is None:
            await balance_h.deposit_screen(message, user)
        else:
            await balance_h.request_deposit(message, user, cents)

    elif key == 'wd':
        if cents is None:
            # Кнопку с FSM-вводом в группе не даём: после неё бот начал бы
            # разбирать как сумму каждое следующее сообщение игрока в чате.
            hint = kb.back_to('balance', '💳 Баланс') \
                if message.chat.type == 'private' else None
            await render(message,
                f'{balance_h.WITHDRAW_HEAD}\n\nНапиши сумму в команде: '
                f'<code>вывод 5</code>. Минимум — '
                f'{fmt(config.MIN_WITHDRAWAL_CENTS)}.', hint)
        else:
            await balance_h.request_withdraw(message, user, state, cents)

    elif key == 'bet':
        if cents is None:
            await render(message,
                f'{E.MONEY} Текущая ставка: <b>{fmt(user["bet_cents"])}</b>\n\n'
                f'Сменить: <code>ставка 2</code>. Допустимо от '
                f'{fmt(config.MIN_BET_CENTS)} до {fmt(config.MAX_BET_CENTS)}.',
                kb.back_menu())
        else:
            saved = await db.set_bet(user_id, cents)
            note = ('' if saved == cents else
                    '\n\nЗапрошенная сумма вне лимитов, поставил ближайшую.')
            await render(message, f'{E.MONEY} Ставка: <b>{fmt(saved)}</b>{note}',
                         kb.back_menu())

    elif key == 'top':
        await render(message, await common.top_text(user_id), kb.top_menu())

    elif key == 'refs':
        await render(message, await common.refs_text(message.bot, user),
                     kb.refs_menu(await common.bot_username(message.bot)))

    elif key == 'mychats':
        # Строка юзера могла устареть на копейки заработка, но экран читает
        # chat_earned_cents именно из неё — перечитываем.
        fresh = await db.get_user(user_id) or user
        await render(message, await chats_h.chats_text(fresh),
                     kb.chats_menu(await common.bot_username(message.bot)))

    elif key == 'games':
        await games_h.show_catalog(message)

    elif key == 'help':
        await render(message, chat_help_text(), kb.chat_help_menu())

    elif key == 'history':
        await render(message, await common.history_text(user_id),
                     kb.back_to('balance', '⬅️ К балансу'))

    elif key == 'code':
        if not params:
            await render(message,
                f'{E.GIFT} Напиши код в команде: <code>код МОЙКОД</code>.',
                kb.back_to('balance'))
        else:
            await balance_h.apply_code(message, user, params[0])


# --- вход -------------------------------------------------------------------

@router.message(F.text, chat_command)
async def text_command(message: Message, state: FSMContext, user, is_admin: bool,
                       kind: str, key: str, cents: int | None,
                       params: list[str]) -> None:
    if kind == 'game':
        await _play_game(message, user, state, key, cents, params)
    else:
        await _run_cmd(message, user, is_admin, state, key, cents, params)


# --- справка ----------------------------------------------------------------

def chat_help_text() -> str:
    return (
        f'{E.CHAT} <b>Команды в чате — {config.CASINO_NAME}</b>\n\n'
        f'Формат: <b>игра ставка параметр</b>. Ставку можно не писать — '
        f'возьмётся текущая, {fmt(config.MIN_BET_CENTS)}–'
        f'{fmt(config.MAX_BET_CENTS)}.\n\n'
        f'<b>{E.GAMES} Игры</b>\n'
        f'<code>мины 0.5 3</code> — ставка $0.50, три мины\n'
        f'<code>краш 1 2.5</code> — авто-вывод на ×2.5\n'
        f'<code>монетка 1 орёл</code> · <code>монетка 1 решка</code>\n'
        f'<code>рулетка 1 красное</code> · <code>рулетка 1 д2</code> · '
        f'<code>рулетка 1 17</code>\n'
        f'<code>башня 1</code> · <code>бж 1</code>\n'
        f'<code>слоты 1 семёрки</code> · <code>кости 1 больше</code> · '
        f'<code>кости 1 4</code>\n'
        f'<code>футбол 1 штанга</code> · <code>баскет 1 гол</code> · '
        f'<code>дартс 1 центр</code> · <code>боулинг 1 победа</code>\n'
        f'<code>кубики 1 больше</code> · <code>кубики 1 7</code> — сумма двух\n'
        f'<code>кубики3 1 меньше</code> · <code>кубики3 1 11</code> — сумма трёх\n'
        f'<code>дуэль 1</code> · <code>джекпот 1</code> — против живых игроков\n\n'
        f'<b>{E.CARD} Касса и профиль</b>\n'
        f'<code>деп 5</code> · <code>вывод 5</code> · <code>баланс</code> · '
        f'<code>ставка 2</code>\n'
        f'<code>профиль</code> · <code>топ</code> · <code>реф</code> · '
        f'<code>чаты</code> · <code>история</code> · <code>код XXX</code> · '
        f'<code>меню</code>\n\n'
        f'Команды работают и в личке, и в группе. Деньги у каждого свои: '
        f'кнопку чужого раунда нажать нельзя, бот её не примет.\n\n'
        f'⚠️ В группе бот отвечает на игру только с суммой — чтобы слово '
        f'«футбол» в разговоре не списывало ставку.'
    )
