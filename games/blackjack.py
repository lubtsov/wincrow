"""Блэкджек: 6 колод, дилер добирает на мягкой 17, блэкджек платит 6:5.

Колода не лежит в состоянии раунда. Она — чистая функция сидов: тот же
provably fair поток тасует 312 карт в один и тот же порядок при каждом клике.
В state хранятся только индексы выданных карт и курсор, поэтому раунд
восстанавливается точно и переживает рестарт бота, а 312 карт не ездят
туда-обратно в JSON.

Отдача. Блэкджек — единственная игра здесь, где итог зависит от решений
игрока: остановиться на 12 против шестёрки дилера никто не запретит. Точной
константы RTP тут не существует. При грамотной базовой стратегии выбранные
правила — 6 колод, добор дилера на мягкой 17, удвоение на любых двух картах,
без сплита и страховки, блэкджек 6:5 — дают около 97.4%. Основная часть
преимущества казино берётся именно из выплаты 6:5 вместо 3:2 (это ~1.4%),
остальное — из добора на мягкой 17 и отсутствия сплита.
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

import db
import emoji as E
import keyboards as kb
from db import fmt
from games import engine
from games.registry import implement
from ui import chat_id_of, render

router = Router(name='blackjack')

DECKS = 6
RANK_NAMES = ('A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K')
RANK_VALUE = (11, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10)
SUITS = ('♠', '♥', '♦', '♣')

# Карта — число 0..51: ранг = c % 13, масть = c // 13. Шесть колод подряд.
BASE_DECK = list(range(52)) * DECKS

WIN_MULT = 2.0
BJ_MULT = 2.2          # 6:5
DEALER_STAND = 17


# --- карты ------------------------------------------------------------------

def card(c: int) -> str:
    return f'{RANK_NAMES[c % 13]}{SUITS[c // 13]}'


def hand_value(cards: list[int]) -> tuple[int, bool]:
    """Сумма руки и признак «мягкая» (есть туз, считающийся за 11)."""
    total = sum(RANK_VALUE[c % 13] for c in cards)
    aces = sum(1 for c in cards if c % 13 == 0)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0


def is_blackjack(cards: list[int]) -> bool:
    return len(cards) == 2 and hand_value(cards)[0] == 21


def _deck(rnd: engine.Round) -> list[int]:
    """Порядок колоды из сидов раунда.

    Вызывать один раз и до любого другого обращения к случайности раунда:
    тасовка съедает поток, а восстановление раунда после клика опирается на
    то, что поток каждый раз начинается с нуля.
    """
    return rnd.shuffle(BASE_DECK)


def _cards(rnd: engine.Round, deck: list[int], who: str) -> list[int]:
    return [deck[i] for i in rnd.state[who]]


def _draw(rnd: engine.Round, who: str) -> None:
    rnd.state[who].append(rnd.state['cur'])
    rnd.state['cur'] += 1


# --- экраны -----------------------------------------------------------------

def _hand_text(cards: list[int]) -> str:
    total, soft = hand_value(cards)
    body = ' '.join(card(c) for c in cards)
    if is_blackjack(cards):
        return f'{body} → <b>блэкджек</b>'
    if total > 21:
        return f'{body} → <b>{total}</b>, перебор'
    return f'{body} → <b>{total}</b>' + (' (мягкие)' if soft else '')


def _table_text(rnd: engine.Round, deck: list[int], reveal: bool,
                head: str = '') -> str:
    player = _cards(rnd, deck, 'p')
    dealer = _cards(rnd, deck, 'd')
    dealer_line = _hand_text(dealer) if reveal \
        else f'{card(dealer[0])} {E.CARD_BACK} → ?'

    text = f'{E.CARDS} <b>Блэкджек</b>\n\n' if not head else f'{head}\n\n'
    text += (f'Дилер: {dealer_line}\n'
             f'Твои: {_hand_text(player)}\n\n'
             f'Ставка: <b>{fmt(rnd.bet_cents)}</b>')
    if rnd.state.get('doubled'):
        text += ' (удвоена)'
    return text


def _hand_kb(rnd: engine.Round, can_double: bool) -> InlineKeyboardMarkup:
    rows = [[kb.btn('🃏 Ещё', f'bj:{rnd.id}:h'),
             kb.btn('✋ Хватит', f'bj:{rnd.id}:s')]]
    if can_double:
        rows.append([kb.btn(f'✖️ Удвоить до {fmt(rnd.bet_cents * 2)}',
                            f'bj:{rnd.id}:d')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(call: CallbackQuery, rnd: engine.Round, deck: list[int]) -> None:
    """Стол в середине раздачи."""
    balance = await db.get_balance(rnd.user_id)
    can_double = (len(rnd.state['p']) == 2 and not rnd.state['doubled']
                  and balance >= rnd.bet_cents)
    hint = '\n\nДобирай или останавливайся.'
    if can_double:
        hint = ('\n\nМожно добрать, остановиться или удвоить — при удвоении '
                'приходит ровно одна карта.')
    await render(call, _table_text(rnd, deck, False) + hint,
                 _hand_kb(rnd, can_double))


# --- правила ----------------------------------------------------------------
#
# Ниже — вся развязка раздачи без единого обращения к Telegram: тем же кодом
# играет и бот, и Mini App (`webapp/games.py`). Правила казино должны лежать в
# одном месте, иначе два интерфейса однажды разойдутся в выплате.


def dealer_draws(rnd: engine.Round, deck: list[int]) -> None:
    """Дилер добирает по правилу мягкой 17. Меняет состояние раунда.

    Карты берутся только если есть с чем сравнивать: на переборе игрока и на его
    блэкджеке добор ничего не меняет.
    """
    player = _cards(rnd, deck, 'p')
    if hand_value(player)[0] > 21 or is_blackjack(player):
        return
    while rnd.state['cur'] < len(deck):
        total, soft = hand_value(_cards(rnd, deck, 'd'))
        if total > DEALER_STAND or (total == DEALER_STAND and not soft):
            break
        _draw(rnd, 'd')


def outcome(player: list[int], dealer: list[int]) -> tuple[float | None, str]:
    """(множитель, код исхода) по готовым рукам. None — пуш, ставка вернётся."""
    p_total = hand_value(player)[0]
    d_total = hand_value(dealer)[0]
    p_bj, d_bj = is_blackjack(player), is_blackjack(dealer)

    if p_total > 21:
        return 0.0, 'bust'
    if p_bj and d_bj:
        return None, 'push_bj'
    if p_bj:
        return BJ_MULT, 'player_bj'
    if d_bj:
        return 0.0, 'dealer_bj'
    if d_total > 21:
        return WIN_MULT, 'dealer_bust'
    if p_total > d_total:
        return WIN_MULT, 'win'
    if p_total < d_total:
        return 0.0, 'lose'
    return None, 'push'


# --- развязка ---------------------------------------------------------------

async def _settle(call: CallbackQuery, rnd: engine.Round, deck: list[int]) -> None:
    """Дилер добирает, руки сравниваются, раунд закрывается."""
    dealer_draws(rnd, deck)
    player, dealer = _cards(rnd, deck, 'p'), _cards(rnd, deck, 'd')
    p_total, d_total = hand_value(player)[0], hand_value(dealer)[0]
    mult, code = outcome(player, dealer)

    heads = {
        'bust': f'{E.BOOM} <b>Перебор.</b>',
        'push_bj': f'{E.DRAW} <b>Блэкджек у обоих.</b>',
        'player_bj': f'{E.CARDS} <b>Блэкджек!</b>',
        'dealer_bj': f'{E.CARDS} <b>Блэкджек у дилера.</b>',
        'dealer_bust': f'{E.BOOM} <b>Перебор у дилера — ты забираешь.</b>',
        'win': f'{E.OK} <b>{p_total} против {d_total}.</b>',
        'lose': f'{E.FAIL} <b>{d_total} у дилера против твоих {p_total}.</b>',
        'push': f'{E.DRAW} <b>Ничья, у обоих {p_total}.</b>',
    }
    head = heads[code]

    # mult is None — пуш: ставка возвращается, оборот откатывается.
    if mult is None:
        if not await engine.void(rnd):
            await call.answer('Раунд уже закрыт.', show_alert=True)
            return
        tail = f'Ставка {fmt(rnd.bet_cents)} вернулась целиком.'
    else:
        payout = await engine.finish(rnd, mult)
        if payout is None:
            await call.answer('Раунд уже закрыт.', show_alert=True)
            return
        if payout > 0:
            tail = (f'{fmt(rnd.bet_cents)} × {mult:.2f} = <b>{fmt(payout)}</b>\n'
                    f'Чистыми: <b>{fmt(payout - rnd.bet_cents)}</b>')
        else:
            tail = f'Ставка {fmt(rnd.bet_cents)} ушла.'

    balance = await db.get_balance(rnd.user_id)
    await render(call,
                 f'{_table_text(rnd, deck, True, head)}\n\n'
                 f'{tail}\nБаланс: <b>{fmt(balance)}</b>',
                 kb.again('blackjack'))


# --- вход -------------------------------------------------------------------

@implement('blackjack')
async def start_blackjack(call: CallbackQuery, user, state) -> None:
    user_id = call.from_user.id
    bet = await db.get_bet(user_id)

    rnd = await engine.start_round(user_id, 'blackjack', bet,
                                   chat_id=chat_id_of(call))
    if rnd is None:
        await call.message.answer(f'Не хватает на ставку {fmt(bet)}.',
                                  reply_markup=kb.back_to('balance', '💳 Баланс'))
        return

    deck = _deck(rnd)
    # Раздача по кругу: первая и третья карты игроку, вторая и четвёртая дилеру.
    rnd.state = {'p': [0, 2], 'd': [1, 3], 'cur': 4, 'doubled': False}

    # Блэкджек на руках — сравнивать уже нечего, раунд решается сразу.
    if is_blackjack(_cards(rnd, deck, 'p')) or is_blackjack(_cards(rnd, deck, 'd')):
        await _settle(call, rnd, deck)
        return

    await engine.save_state(rnd)
    await _show(call, rnd, deck)


@router.callback_query(F.data.startswith('bj:'))
async def step(call: CallbackQuery, user) -> None:
    _, raw_id, action = call.data.split(':', 2)
    rnd = await engine.load_round(int(raw_id), call.from_user.id, 'blackjack')
    if rnd is None:
        await call.answer('Раунд устарел, начни заново.', show_alert=True)
        return

    deck = _deck(rnd)

    # --- останов ------------------------------------------------------------
    if action == 's':
        await call.answer()
        await _settle(call, rnd, deck)
        return

    # --- удвоение -----------------------------------------------------------
    if action == 'd':
        if len(rnd.state['p']) != 2 or rnd.state['doubled']:
            await call.answer('Удвоить можно только на первых двух картах.',
                              show_alert=True)
            return
        if not await engine.raise_stake(rnd, rnd.bet_cents):
            await call.answer('Не хватает баланса на удвоение.', show_alert=True)
            return
        rnd.state['doubled'] = True
        _draw(rnd, 'p')                      # ровно одна карта и останов
        await call.answer(f'Ставка {fmt(rnd.bet_cents)}')
        await _settle(call, rnd, deck)
        return

    if action != 'h':
        await call.answer()
        return

    # --- добор --------------------------------------------------------------
    _draw(rnd, 'p')
    total, _ = hand_value(_cards(rnd, deck, 'p'))

    if total >= 21:
        # На 21 добирать бессмысленно, на переборе — нечего. Дилер играет сам.
        await call.answer()
        await _settle(call, rnd, deck)
        return

    await engine.save_state(rnd)
    await call.answer()
    await _show(call, rnd, deck)
