"""Выбор игры и экран ставки.

Один экран ставки на все игры вместо девяти копий `do_bet_increase`,
отличавшихся только `reply_markup`. Сама игра начинается в своём модуле,
через spec.start — здесь только выбор суммы.
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import db
import emoji as E
import keyboards as kb
from db import fmt
from games import dice_games
from games.registry import GAMES, GROUPS
from states import BetInput
from ui import render, render_animation

router = Router(name='games')


# --- каталог ----------------------------------------------------------------

async def show_catalog(event) -> None:
    """Каталог игр — второй и последний экран с гифкой (первый — меню).

    Текста на экране нет намеренно: заголовок и описания разделов пересказывали
    то, что и так написано на самих разделах, а под гифкой это выглядело как
    лишняя простыня перед кнопками. Остались гифка и кнопки.

    Пустая подпись возможна только вместе с гифкой: сообщение без текста и без
    медиа Telegram не принимает, поэтому если файла на диске нет, экран уедет
    текстом и заголовок ему всё-таки нужен.
    """
    animation = config.GAMES_ANIMATION
    text = '' if animation.is_file() else f'{E.GAMES} <b>Игры</b>'
    await render_animation(event, text, kb.groups_menu(), animation=animation)


@router.callback_query(F.data == 'games')
async def cb_groups(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_catalog(call)
    await call.answer()


@router.callback_query(F.data.startswith('grp:'))
async def cb_group(call: CallbackQuery, state: FSMContext):
    await state.clear()
    group = GROUPS.get(call.data.split(':', 1)[1])
    if group is None:
        await call.answer('Раздел не найден.', show_alert=True)
        return
    await render(call, f'{group.tag} <b>{group.title}</b>\n\n{group.note}',
                 kb.group_games(group.key))
    await call.answer()


# --- экран ставки -----------------------------------------------------------

def _bet_text(spec, balance_cents: int, bet_cents: int) -> str:
    text = (f'{spec.tag} <b>{spec.title}</b>\n'
            f'{spec.short}\n\n'
            f'Баланс: <b>{fmt(balance_cents)}</b>\n'
            f'Ставка: <b>{fmt(bet_cents)}</b>')
    if not spec.ready:
        text += f'\n\n{E.WAIT} Игра ещё не открыта.'
    elif bet_cents > balance_cents:
        text += (f'\n\n⚠️ На такую ставку не хватает — пополни баланс или '
                 f'убавь ставку.')
    return text


async def _show_bet(event, spec, state: FSMContext | None = None):
    user_id = event.from_user.id
    user = await db.get_user(user_id)
    if state is not None:
        await state.clear()
    await render(event, _bet_text(spec, user['balance_cents'], user['bet_cents']),
                 kb.bet_screen(spec.key, user['bet_cents'], spec.ready))


@router.callback_query(F.data.startswith('game:'))
async def cb_game(call: CallbackQuery, state: FSMContext, user):
    spec = GAMES.get(call.data.split(':', 1)[1])
    if spec is None:
        await call.answer('Игра не найдена.', show_alert=True)
        return
    # PvP-комнаты сами спрашивают ставку при создании — общий экран им не нужен.
    if spec.own_entry and spec.ready and spec.start is not None:
        await call.answer()
        await spec.start(call, user, state)
        return
    await _show_bet(call, spec, state)
    await call.answer()


@router.callback_query(F.data.startswith('rules:'))
async def cb_rules(call: CallbackQuery):
    spec = GAMES.get(call.data.split(':', 1)[1])
    if spec is None:
        await call.answer('Игра не найдена.', show_alert=True)
        return
    await render(call, f'{spec.tag} <b>{spec.title}</b>\n\n{spec.rules}',
                 kb.rules_screen(spec.key))
    await call.answer()


@router.callback_query(F.data.startswith('bet:'))
async def cb_bet(call: CallbackQuery, state: FSMContext, user):
    _, key, op = call.data.split(':', 2)
    spec = GAMES.get(key)
    if spec is None:
        await call.answer('Игра не найдена.', show_alert=True)
        return

    if op == 'ask':
        await state.set_state(BetInput.amount)
        await state.update_data(game=key)
        await render(call,
            f'{spec.tag} <b>{spec.title}</b>\n\n'
            f'Пришли сумму ставки числом, например <code>7.50</code>.\n'
            f'Допустимо от {fmt(config.MIN_BET_CENTS)} до '
            f'{fmt(config.MAX_BET_CENTS)}.',
            kb.cancel_to(f'game:{key}'))
        await call.answer()
        return

    if op == 'min':
        new_bet = config.MIN_BET_CENTS
    elif op == 'max':
        # Не выше баланса: ставка, на которую заведомо не хватает, бесполезна.
        new_bet = min(config.MAX_BET_CENTS,
                      max(config.MIN_BET_CENTS, user['balance_cents']))
    else:
        new_bet = user['bet_cents'] + int(op)

    saved = await db.set_bet(user['user_id'], new_bet)
    if saved != new_bet:
        limit = (f'минимум {fmt(config.MIN_BET_CENTS)}'
                 if new_bet < saved else f'максимум {fmt(config.MAX_BET_CENTS)}')
        await call.answer(f'Упёрлись в {limit}.')
    else:
        await call.answer()
    await _show_bet(call, spec)


@router.message(BetInput.amount)
async def bet_input(message: Message, state: FSMContext, user):
    data = await state.get_data()
    key = data.get('game')
    spec = GAMES.get(key)
    if spec is None:
        await state.clear()
        await message.answer('Игра не найдена, открой список заново.',
                             reply_markup=kb.back_menu())
        return

    # Дайс-игры спрашивают сумму уже под выбранный исход, и вернуться нужно
    # именно на его экран — общий экран ставки про исход ничего не знает.
    pick = data.get('pick')
    game = dice_games.PICKS.get(key) if pick else None
    outcome = game.find(pick) if game else None
    cancel = kb.cancel_to(f'pb:{key}:{pick}' if outcome else f'game:{key}')

    cents = db.parse_cents(message.text or '')
    if cents is None:
        await message.answer('Не похоже на сумму. Пришли число, например 7.50.',
                             reply_markup=cancel)
        return
    if not config.MIN_BET_CENTS <= cents <= config.MAX_BET_CENTS:
        await message.answer(
            f'Ставка должна быть от {fmt(config.MIN_BET_CENTS)} до '
            f'{fmt(config.MAX_BET_CENTS)}.',
            reply_markup=cancel)
        return

    await db.set_bet(user['user_id'], cents)
    await state.clear()
    if outcome is not None:
        await dice_games.show_stake(message, game, outcome)
        return
    fresh = await db.get_user(user['user_id'])
    await message.answer(_bet_text(spec, fresh['balance_cents'], cents),
                         reply_markup=kb.bet_screen(key, cents, spec.ready))


# --- запуск -----------------------------------------------------------------

@router.callback_query(F.data.startswith('play:'))
async def cb_play(call: CallbackQuery, state: FSMContext, user):
    spec = GAMES.get(call.data.split(':', 1)[1])
    if spec is None:
        await call.answer('Игра не найдена.', show_alert=True)
        return
    if not spec.ready or spec.start is None:
        await call.answer('Игра ещё не открыта.', show_alert=True)
        return

    # Проверка «хватает ли» здесь только для сообщения игроку. Настоящий гейт —
    # атомарный place_bet внутри движка, и обойти его кликом из старого
    # сообщения нельзя: списание и создание раунда происходят одним шагом.
    bet = await db.get_bet(user['user_id'])
    if await db.get_balance(user['user_id']) < bet:
        await call.answer(f'Не хватает на ставку {fmt(bet)}.', show_alert=True)
        return

    await call.answer()
    await spec.start(call, user, state)
