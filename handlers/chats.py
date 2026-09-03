"""Чаты как продолжение рефералки.

Привёл бота в группу — с каждой проигранной там ставки владельцу группы капает
процент (config.CHAT_OWNER_PERCENT). Платит казино из своей маржи, у игроков с
баланса не удерживается ничего.

Почему владелец — создатель группы, а не тот, кто позвал бота. Добавить бота
может любой участник с правами, и «кто позвал — тому и платим» превращается в
гонку: зашёл в чужой чат, успел добавить первым, снимаешь процент с чужой
аудитории. Создателя группы подменить нельзя, поэтому деньги идут ему; кто
позвал, остаётся в базе (`chats.added_by`) для истории.

Владелец фиксируется при первой привязке и больше не меняется — иначе бота
достаточно было бы выгнать и позвать заново, чтобы перевести чат на себя. Так
что «бота выгнали» и «бота вернули» — это про начисления (`chats.active`), а не
про смену получателя.
"""

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, ChatMemberUpdated

import config
import db
import emoji as E
import keyboards as kb
from db import fmt
from ui import render

from .common import bot_username

log = logging.getLogger(__name__)
router = Router(name='chats')

GROUPS = {'group', 'supergroup'}

# Статусы, при которых бот в чате работает. Всё остальное (left, kicked) —
# начисления останавливаются.
LIVE_STATUSES = {'member', 'administrator', 'creator', 'restricted'}


async def _owner_of(bot, chat_id: int, fallback) -> tuple[int, str | None]:
    """(id владельца, username). Создатель группы, иначе — кто позвал бота.

    getChatAdministrators доступен обычному участнику, но упасть может: у чата
    может не быть создателя (группа, миграция), бота могли выгнать в ту же
    секунду, наконец Telegram просто отдаёт ошибку. Ни один из этих случаев не
    повод потерять привязку — поэтому есть fallback.
    """
    try:
        for member in await bot.get_chat_administrators(chat_id):
            if member.status == 'creator' and not member.user.is_bot:
                return member.user.id, member.user.username
    except Exception as e:
        log.warning('не удалось узнать владельца чата %s: %s', chat_id, e)
    return fallback.id, fallback.username


@router.my_chat_member(F.chat.type.in_(GROUPS))
async def bot_in_chat(event: ChatMemberUpdated) -> None:
    """Бота добавили в группу или выгнали из неё."""
    chat = event.chat
    status = event.new_chat_member.status

    if status not in LIVE_STATUSES:
        if await db.set_chat_active(chat.id, False):
            log.info('бота убрали из чата %s (%s)', chat.id, status)
        return

    owner_id, owner_name = await _owner_of(event.bot, chat.id, event.from_user)
    await db.ensure_user(owner_id, owner_name)
    is_new = await db.link_chat(chat.id, chat.title, owner_id, event.from_user.id)

    if not is_new:
        return                          # вернулись в знакомый чат, без фанфар

    log.info('новый чат %s, владелец %s', chat.id, owner_id)
    try:
        await event.bot.send_message(chat.id, greeting_text())
    except Exception as e:
        # Писать в чат бот может ещё не иметь права — привязка от этого не
        # перестаёт работать, начисления пойдут с первой же игры.
        log.warning('приветствие в чат %s не ушло: %s', chat.id, e)


def greeting_text() -> str:
    return (
        f'{E.GAMES} <b>{html.escape(config.CASINO_NAME)} в чате.</b>\n\n'
        f'Играть можно прямо здесь: <code>мины 0.5 3</code>, '
        f'<code>монетка 1 орёл</code>, <code>краш 1 2.5</code>. '
        f'Полный список — <code>помощь</code>.\n\n'
        f'{E.MONEY} Владельцу этой группы капает '
        f'<b>{config.CHAT_OWNER_PERCENT}%</b> '
        f'с каждой проигранной здесь ставки. Платит казино из своей маржи — '
        f'у игроков с баланса не удерживается ничего.\n\n'
        f'⚠️ Чтобы бот видел команды, в @BotFather нужно '
        f'<code>/setprivacy</code> → <b>Disable</b>.')


# --- экран «Мои чаты» -------------------------------------------------------

async def chats_text(user) -> str:
    rows = await db.owner_chats(user['user_id'])
    percent = config.CHAT_OWNER_PERCENT
    # Порог, ниже которого целые центы съедают выплату целиком: при 1% это $1.00.
    threshold = 100 // percent if percent else 0

    head = (f'{E.ROBOT} <b>Мои чаты</b>\n\n'
            f'С каждой проигранной в твоей группе ставки тебе капает '
            f'<b>{percent}%</b>. Начисление идёт с того, что игрок реально '
            f'потерял: забрал из краша больше ставки — потери нет, платить '
            f'не с чего.\n\n')

    if not rows:
        return head + (
            f'Пока ни одного чата. Добавь бота в свою группу кнопкой ниже — '
            f'привязка появится сразу, а процент начнёт капать с первой '
            f'проигранной там ставки.\n\n'
            f'Получателем становится создатель группы, поэтому добавлять бота '
            f'в чужие чаты смысла нет.')

    lines = []
    for r in rows:
        title = html.escape(r['title']) if r['title'] else f'ID {r["chat_id"]}'
        mark = E.OK if r['active'] else E.CROSS
        lines.append(f'{mark} <b>{title}</b>\n'
                     f'     {fmt(r["earned_cents"])} с {r["losses"]} проигрышей')

    return head + ('\n'.join(lines) +
                   f'\n\nВсего заработано: '
                   f'<b>{fmt(user["chat_earned_cents"])}</b>\n'
                   f'{E.CROSS} — бота убрали из чата, начисления остановлены; '
                   f'заработанное остаётся.\n'
                   f'Выплата округляется вниз до цента, поэтому при {percent}% '
                   f'проигрыш меньше {fmt(threshold)} не приносит ничего.')


@router.callback_query(F.data == 'mychats')
async def cb_chats(call: CallbackQuery, user) -> None:
    await render(call, await chats_text(user),
                 kb.chats_menu(await bot_username(call.bot)))
    await call.answer()
