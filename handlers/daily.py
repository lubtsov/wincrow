"""Ежедневный кейс внутри бота.

Второй вход в тот же кейс — Mini App (`webapp/server.py`). Оба идут через
`daily.py` и `db.py`, поэтому правила у них общие: выигрышную карточку выбирает
сервер, он же считает паузу и он же начисляет приз. Экран в боте — не заглушка
на случай, если Mini App не настроен, а полноценный второй интерфейс: кто-то
играет в Telegram Desktop старой версии, где Mini App просто не откроется.

Кнопки карточек несут id кейса — по тому же принципу, что round_id в играх:
клик из старого сообщения не должен применяться к новой выдаче.
"""

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import daily
import db
import emoji as E
import keyboards as kb
from db import fmt
from ui import is_private, render

router = Router(name='daily')
log = logging.getLogger(__name__)

HEAD = f'{E.GIFT} <b>Ежедневный кейс</b>'


# --- тексты -----------------------------------------------------------------

def _rules() -> str:
    return (f'Карточек {config.DAILY_CARDS}: в одной '
            f'<b>{fmt(config.DAILY_PRIZE_CENTS)}</b>, в остальных пусто. '
            f'Кейс — раз в {daily.left_text(config.DAILY_COOLDOWN)}.')


def _ready_text(user) -> str:
    return (f'{HEAD}\n\n{_rules()}\n\n'
            f'Баланс: <b>{fmt(user["balance_cents"])}</b>\n\n'
            f'Выбирай карточку — и удачи.')

def _subscribe_text(st: dict) -> str:
    lines = '\n'.join(f'• {html.escape(daily.channel_title(r))}'
                      for r in st['missing'])
    text = (f'{HEAD}\n\n'
            f'{E.LOCK} Кейс открывается за подписку. Не хватает:\n{lines}\n\n'
            f'Подпишись и нажми «✅ Проверить подписку».')
    if st['broken']:
        text += ('\n\n⚠️ Ещё несколько каналов бот проверить не может, поэтому '
                 'они не учитываются — админ увидит это в панели.')
    return text


def _cooldown_text(st: dict) -> str:
    last = st['last']
    got = fmt(last['payout_cents']) if last is not None else fmt(0)
    return (f'{HEAD}\n\n'
            f'{E.OK} Кейс на сегодня уже открыт — <b>{got}</b>.\n'
            f'{E.WAIT} Следующий через '
            f'<b>{daily.left_text(st["seconds_left"])}</b>.\n\n'
            f'{_rules()}')


def _result_text(case, balance_cents: int) -> str:
    picked = (case['picked_index'] or 0) + 1
    if case['payout_cents']:
        head = (f'{E.GIFT} <b>Карточка {picked} — '
                f'{fmt(case["payout_cents"])}!</b>\nПриз уже на балансе.')
    else:
        head = (f'{E.GIFT} <b>Карточка {picked} — пусто.</b>\n'
                f'Приз лежал в карточке {case["win_index"] + 1}.')
    return (f'{head}\n'
            f'Баланс: <b>{fmt(balance_cents)}</b>\n\n'
            f'{E.WAIT} Следующий кейс через '
            f'<b>{daily.left_text(config.DAILY_COOLDOWN)}</b>.')


# --- экран ------------------------------------------------------------------

async def show_case(event, user, *, notice: str = '') -> None:
    """Единственный экран кейса. Он же и выдаёт кейс, если он положен.

    Выдача идёт здесь, а не по отдельной кнопке: лишний шаг «получить кейс» ->
    «открыть карточку» ничего не добавляет, а `daily.issue` идемпотентна —
    сколько раз ни открой экран, кейс останется один.
    """
    st, _ = await daily.issue(event.bot, user['user_id'])
    private = is_private(event)

    if st['status'] == 'subscribe':
        text = _subscribe_text(st)
        markup = kb.case_subscribe([daily.as_dict(r) for r in st['missing']],
                                   private)
    elif st['status'] == 'cooldown':
        text, markup = _cooldown_text(st), kb.case_wait(private)
    else:
        case = st['case']
        text = _ready_text(user)
        markup = kb.case_cards(case['id'], case['cards'], private)

    await render(event, f'{notice}\n\n{text}' if notice else text, markup)

async def _animate(call: CallbackQuery, case) -> None:
    """Пара кадров перед результатом, чтобы открытие было видно, а не мгновенно.

    Приз к этому моменту уже начислен и записан в базу: анимация — украшение
    поверх готового результата, а не часть его вычисления.
    """
    frames = (f'{E.GIFT} Открываем карточку {(case["picked_index"] or 0) + 1}…',
              '✨ <b>·</b> ✨ <b>·</b> ✨')
    for frame in frames:
        await render(call, frame, None)
        await asyncio.sleep(0.45)


# --- хендлеры ---------------------------------------------------------------

@router.message(Command('case'), F.chat.type == 'private')
async def cmd_case(message: Message, state: FSMContext, user):
    await state.clear()
    await show_case(message, user)


@router.callback_query(F.data == 'case')
async def cb_case(call: CallbackQuery, state: FSMContext, user):
    await state.clear()
    await show_case(call, user)
    await call.answer()


@router.callback_query(F.data == 'case:check')
async def cb_check(call: CallbackQuery, user):
    """Перепроверка подписки. Единственная кнопка, которая её и запускает."""
    missing, _ = await daily.check_channels(call.bot, user['user_id'])
    if missing:
        await call.answer('Подписка ещё не на всех каналах.', show_alert=True)
    else:
        await call.answer('Подписка на месте!')
    await show_case(call, user)


@router.callback_query(F.data.startswith('case:pick:'))
async def cb_pick(call: CallbackQuery, user):
    parts = call.data.split(':')
    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
        await call.answer('Кнопка устарела, открой кейс заново.', show_alert=True)
        return
    case_id, index = int(parts[2]), int(parts[3])

    # Подписку проверяем и здесь: между выдачей и открытием игрок мог отписаться.
    missing, _ = await daily.check_channels(call.bot, user['user_id'])
    if missing:
        await call.answer('Сначала подписка на каналы.', show_alert=True)
        await show_case(call, user)
        return

    case, result = await db.pick_daily_case(user['user_id'], case_id, index)

    if result == 'not_found':
        await call.answer('Кейс не найден — открой меню заново.', show_alert=True)
        return
    if result == 'bad_index':
        await call.answer('Такой карточки в кейсе нет.', show_alert=True)
        return
    if result == 'already':
        # Двойной клик или вторая вкладка Mini App: приз начислен один раз,
        # показываем тот же результат, что и в первый.
        await call.answer('Этот кейс уже открыт.')
        await render(call, _result_text(case, await db.get_balance(user['user_id'])),
                     kb.case_result(case))
        return

    await call.answer('Открываем…')
    await _animate(call, case)
    await render(call, _result_text(case, await db.get_balance(user['user_id'])),
                 kb.case_result(case))
    log.info('кейс #%s: игрок %s выбрал карточку %s, приз %s',
             case_id, user['user_id'], index, case['payout_cents'])
