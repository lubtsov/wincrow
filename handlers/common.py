"""Общие экраны: /start, меню, профиль, рефералка, ТОП, честная игра, история.

Главное меню — единственное место, где собрана вся сводка по игроку:
приветствие, ID, баланс, число сыгранных игр и оборот. Уезжает гифкой с
подписью (ui.render_animation), поэтому текст меню держим коротким: подпись у
Telegram ограничена, и служебные строки вроде «отдача 97%» из неё убраны — то
же самое написано на экранах игр и честной игры.

Отдельного раздела «Помощь» нет: он был пересказом остальных экранов и заодно
прятал за собой профиль с балансом. Профиль и баланс теперь в главном меню,
честная игра и команды в чате — на экране игр, промокод — в кассе.
"""

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

import config
import db
import emoji as E
import keyboards as kb
from db import fmt
from games.registry import GAMES
from ui import is_private, render, render_animation

router = Router(name='common')

# Регистрируется последним: ловит текст, который не ждёт ни один FSM-хендлер.
fallback_router = Router(name='fallback')


def _name(user_row) -> str:
    if user_row['username']:
        return '@' + html.escape(user_row['username'])
    return f'ID {user_row["user_id"]}'


def _greeting(tg_user) -> str:
    """Как обращаться к игроку: имя из Telegram, иначе ник, иначе ID."""
    if tg_user is not None and tg_user.first_name:
        return html.escape(tg_user.first_name)
    if tg_user is not None and tg_user.username:
        return '@' + html.escape(tg_user.username)
    return f'ID {tg_user.id}' if tg_user is not None else 'игрок'


async def bot_username(bot) -> str | None:
    """Имя бота для deep link «Добавить в чат». aiogram кеширует me()."""
    try:
        return (await bot.me()).username
    except Exception:
        return None


# --- главное меню -----------------------------------------------------------

async def menu_text(user, tg_user=None) -> str:
    played = await db.games_played(user['user_id'])

    return (
        f'{E.SLOTS} <b>{html.escape(config.CASINO_NAME)}</b> — казино в Telegram\n\n'
        f'Привет, <b>{_greeting(tg_user)}</b>!\n\n'
        f'{E.ID} ID: <code>{user["user_id"]}</code>\n'
        f'{E.MONEY} Баланс: <b>{fmt(user["balance_cents"])}</b>\n'
        f'{E.DICE} Сыграно игр: <b>{played}</b>\n'
        f'{E.STATS} Оборот: <b>{fmt(user["wagered_cents"])}</b>'
    )


async def show_menu(event, user, is_admin: bool, tg_user=None):
    """Рисует главное меню. Годится и для клика, и для команды из чата.

    Экран уезжает гифкой с подписью, а не текстом: render_animation сам решает,
    править подпись у уже присланной гифки или заменить текстовое сообщение.

    private передаётся в клавиатуру: кнопка ежедневного кейса в личке —
    web_app-кнопка Mini App, а в группе такую Telegram не принимает.
    """
    if tg_user is None:
        tg_user = getattr(event, 'from_user', None)
    return await render_animation(event, await menu_text(user, tg_user),
                                  kb.main_menu(is_admin,
                                               await bot_username(event.bot),
                                               is_private(event)))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user, is_admin: bool):
    await state.clear()
    await drop_old_keyboard(message)
    await show_menu(message, user, is_admin)
    if user['referer_id']:
        await message.answer(
            'Тебя пригласил игрок — процент с твоих пополнений идёт ему, '
            'на твой баланс это не влияет.')


async def drop_old_keyboard(message: Message) -> None:
    """Снимает нижнюю клавиатуру, если она у игрока осталась с прошлых версий.

    Раньше после /start приезжало второе сообщение с кнопкой «Играть в
    приложении». Сообщение убрали, но нижняя клавиатура у Telegram живёт в чате,
    пока её явно не снять, — а снять её можно только вместе с каким-нибудь
    сообщением. Поэтому служебное сообщение уходит и сразу удаляется: клавиатура
    исчезает, в переписке ничего не остаётся.
    """
    if message.chat.type != 'private':
        return
    try:
        notice = await message.answer('⌛', reply_markup=ReplyKeyboardRemove())
        await notice.delete()
    except Exception:
        # Не критично: не дали отправить или удалить — меню всё равно приедет.
        pass


@router.message(Command('menu'))
async def cmd_menu(message: Message, state: FSMContext, user, is_admin: bool):
    await state.clear()
    await show_menu(message, user, is_admin)


@router.callback_query(F.data == 'menu')
async def cb_menu(call: CallbackQuery, state: FSMContext, user, is_admin: bool):
    await state.clear()
    await show_menu(call, user, is_admin)
    await call.answer()


# --- профиль ----------------------------------------------------------------

async def profile_text(user, is_admin: bool = False) -> str:
    """Профиль игрока.

    «Итог» (получено минус оборот) видит только админ: игроку эта строка
    показывает его же минус в лицо и ничего не даёт, а балансом и оборотом он
    и так распоряжается. Для админа она остаётся — по ней читается, кто в
    плюсе.
    """
    played = await db.games_played(user['user_id'])
    level, percent = db.referral_level(user['referrals'])
    net = user['won_cents'] - user['wagered_cents']

    return (f'{E.PROFILE} <b>Профиль</b>\n\n'
            f'ID: <code>{user["user_id"]}</code>\n'
            f'Ник: {_name(user)}\n'
            f'Баланс: <b>{fmt(user["balance_cents"])}</b>\n'
            f'Текущая ставка: {fmt(user["bet_cents"])}\n\n'
            f'Сыграно игр: <b>{played}</b>\n'
            f'{E.STATS} Оборот: <b>{fmt(user["wagered_cents"])}</b>\n'
            f'Получено: {fmt(user["won_cents"])}\n'
            + (f'Итог: <b>{fmt(net)}</b>\n' if is_admin else '') +
            f'\nПополнено: {fmt(user["deposited_cents"])}\n'
            f'Друзей: {user["referrals"]} · уровень {level} ({percent}%)\n'
            f'С рефералов: {fmt(user["referral_earned_cents"])}\n'
            f'С чатов: {fmt(user["chat_earned_cents"])}')


@router.callback_query(F.data == 'profile')
async def cb_profile(call: CallbackQuery, user, is_admin: bool):
    await render(call, await profile_text(user, is_admin), kb.back_menu())
    await call.answer()


# --- ТОП-10 -----------------------------------------------------------------

MEDALS = E.MEDALS


async def top_text(viewer_id: int | None = None,
                   is_admin: bool = False) -> str:
    rows = await db.top_players(10)
    if not rows:
        return (f'{E.STATS_TOP} <b>ТОП-10</b>\n\nПока никто не сыграл ни '
                f'одного раунда — место свободно.')

    lines = []
    for i, r in enumerate(rows):
        mark = MEDALS[i] if i < len(MEDALS) else f'{i + 1}.'
        nick = ('@' + html.escape(r['username'])) if r['username'] \
            else f'ID {r["user_id"]}'
        if r['user_id'] == viewer_id:
            nick = f'<b>{nick} — это ты</b>'
        # Чужой «итог» — не дело игрока: в списке остаётся оборот, по которому
        # и считается место. Админу итог показываем, ему он нужен.
        tail = f' · итог {fmt(r["net"])}' if is_admin else ''
        lines.append(f'{mark} {nick}\n     оборот {fmt(r["wagered_cents"])}'
                     f'{tail}')

    return (f'{E.STATS_TOP} <b>ТОП-10 по обороту</b>\n\n' +
            '\n'.join(lines) +
            '\n\nМесто считается по обороту, а не по балансу: важно, сколько '
            'человек играл, а не сколько занёс.')


@router.callback_query(F.data == 'top')
async def cb_top(call: CallbackQuery, state: FSMContext, user, is_admin: bool):
    await state.clear()
    await render(call, await top_text(user['user_id'], is_admin),
                 kb.top_menu())
    await call.answer()


# --- рефералка --------------------------------------------------------------

async def refs_text(bot, user) -> str:
    me = await bot.me()
    link = f'https://t.me/{me.username}?start={user["user_id"]}'
    level, percent = db.referral_level(user['referrals'])

    table = '\n'.join(
        ('<b>' if lvl == level else '') +
        f'  {lvl} уровень — от {threshold} друзей, {pct}%' +
        ('</b>' if lvl == level else '')
        for threshold, lvl, pct in config.REFERRAL_LEVELS)

    total, live = await db.owner_chats_count(user['user_id'])

    return (f'{E.FRIENDS} <b>Пригласить друзей</b>\n\n'
            f'Твоя ссылка:\n<code>{link}</code>\n\n'
            f'Приглашено: <b>{user["referrals"]}</b>\n'
            f'Уровень: <b>{level}</b> — <b>{percent}%</b> с каждого их '
            f'пополнения\n'
            f'Заработано: <b>{fmt(user["referral_earned_cents"])}</b>\n\n'
            f'Уровни:\n{table}\n\n'
            f'За друга процент капает с пополнений, а не с проигрышей — '
            f'сколько друг наиграет, на эту выплату не влияет.\n\n'
            f'{E.ROBOT} <b>Второй способ — привести чат.</b>\n'
            f'Добавь бота в свою группу, и с каждой проигранной там ставки '
            f'тебе капает <b>{config.CHAT_OWNER_PERCENT}%</b>. Платит казино: '
            f'у игроков с баланса не удерживается ничего.\n'
            f'Чатов: <b>{total}</b> (активных {live})\n'
            f'Заработано с чатов: <b>{fmt(user["chat_earned_cents"])}</b>')


@router.callback_query(F.data == 'refs')
async def cb_refs(call: CallbackQuery, user):
    await render(call, await refs_text(call.bot, user),
                 kb.refs_menu(await bot_username(call.bot)))
    await call.answer()


# --- честная игра -----------------------------------------------------------
#
# Заодно единственный экран про математику казино: ставки, отдача, округление.
# Раньше это лежало в «Помощи» отдельным пересказом — теперь стоит рядом с
# формулой, по которой считается результат.

@router.callback_query(F.data == 'fair')
async def cb_fair(call: CallbackQuery, user):
    row = await (await db.conn().execute(
        'SELECT server_seed_hash, client_seed, nonce FROM seeds WHERE user_id = ?',
        (user['user_id'],))).fetchone()

    if row is None:
        current = 'Сид создастся при первом раунде.'
    else:
        current = (
            f'Хеш серверного сида:\n<code>{row["server_seed_hash"]}</code>\n'
            f'Твой client_seed: <code>{html.escape(row["client_seed"])}</code>\n'
            f'Раундов на этом сиде: {row["nonce"]}')

    await render(call,
        f'{E.LOCK} <b>Честная игра</b>\n\n'
        'Результат раунда считается до броска и не зависит от того, сколько '
        'ты выиграл или проиграл до этого:\n\n'
        '<code>result = HMAC_SHA256(server_seed, client_seed:nonce)</code>\n\n'
        'Хеш серверного сида публикуется <b>заранее</b> — подкрутить сид '
        'задним числом нельзя, хеш перестанет сходиться. После смены сида '
        'старый раскрывается, и любой прошлый раунд пересчитывается '
        'самостоятельно.\n\n'
        f'{current}\n\n'
        'Telegram-дайсы (слоты, кости, дартс, футбол, баскетбол, боулинг) '
        'бросает сам Telegram — там результат виден в анимации, и бот на него '
        'не влияет вообще.\n\n'
        'Ставка на сумму двух и трёх кубиков — тот же дайс Telegram, но её '
        'выплаты посчитаны от равномерного кубика 1–6: своё распределение '
        'Telegram не публикует. Это единственное допущение в казино, и его '
        'можно проверить — все броски раунда сохраняются и видны в истории.\n\n'
        f'{E.STATS} <b>Ставки и отдача</b>\n'
        f'Ставка — от {fmt(config.MIN_BET_CENTS)} до '
        f'{fmt(config.MAX_BET_CENTS)}, списывается до броска. Поэтому '
        f'множитель значит именно то, что написано: ×2 — вернул ставку и '
        f'столько же сверху.\n'
        f'Отдача — <b>{config.RTP * 100:.0f}%</b> на всех играх. Коэффициенты '
        f'посчитаны из вероятностей, а не подобраны на глаз: в правилах каждой '
        f'игры расписано, откуда взялось число.\n'
        f'Выплата округляется вниз до цента. На ставке от $1 это незаметно, на '
        f'{fmt(config.MIN_BET_CENTS)} срезает чуть больше — мельче цента '
        f'платить просто нечем.\n\n'
        f'Играй на то, что готов потерять. Математика казино на длинной '
        f'дистанции всегда на стороне казино — это и есть смысл цифры '
        f'{config.RTP * 100:.0f}%.',
        kb.back_to('games', '⬅️ К играм'))
    await call.answer()


# --- история ----------------------------------------------------------------

async def history_text(user_id: int) -> str:
    rows = await (await db.conn().execute(
        'SELECT game, bet_cents, multiplier, payout_cents, status FROM rounds '
        'WHERE user_id = ? ORDER BY id DESC LIMIT 12', (user_id,))).fetchall()

    if not rows:
        return '📜 <b>Последние раунды</b>\n\nПока пусто.'

    lines = []
    for r in rows:
        spec = GAMES.get(r['game'])
        title = f'{spec.tag} {spec.title}' if spec else r['game']
        if r['status'] == 'won':
            lines.append(f'{title} · {fmt(r["bet_cents"])} → '
                         f'×{r["multiplier"]:.2f} = <b>{fmt(r["payout_cents"])}</b>')
        elif r['status'] == 'lost':
            lines.append(f'{title} · {fmt(r["bet_cents"])} → {E.CROSS}')
        elif r['status'] == 'void':
            lines.append(f'{title} · {fmt(r["bet_cents"])} → ничья, возврат')
        else:
            lines.append(f'{title} · {fmt(r["bet_cents"])} → в процессе')
    return '📜 <b>Последние раунды</b>\n\n' + '\n'.join(lines)


@router.callback_query(F.data == 'history')
async def cb_history(call: CallbackQuery, user):
    await render(call, await history_text(user['user_id']),
                 kb.back_to('balance', '⬅️ К балансу'))
    await call.answer()


# --- команды в чате ---------------------------------------------------------

@router.callback_query(F.data == 'chatcmd')
async def cb_chat_help(call: CallbackQuery, state: FSMContext):
    from handlers.chat import chat_help_text          # цикл импорта иначе
    await state.clear()
    await render(call, chat_help_text(), kb.chat_help_menu())
    await call.answer()


# --- фолбэк -----------------------------------------------------------------

@router.callback_query(F.data == 'nop')
async def cb_nop(call: CallbackQuery):
    """Декоративная кнопка (открытая клетка в минах и подобное)."""
    await call.answer()


@fallback_router.message(StateFilter(None), F.chat.type == 'private')
async def any_text(message: Message, user, is_admin: bool):
    """Нераспознанный текст в личке — показываем меню.

    В группах этого хендлера нет намеренно: там бот видит всю болтовню, и
    меню в ответ на каждое сообщение превратило бы его во флудера.
    """
    await show_menu(message, user, is_admin)


@fallback_router.callback_query()
async def dead_callback(call: CallbackQuery):
    """Клик по кнопке из сообщения, которое бот уже не обслуживает."""
    await call.answer('Кнопка устарела, открой меню заново.', show_alert=True)
