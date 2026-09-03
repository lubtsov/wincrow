"""Клавиатуры.

Функциями, а не 284 строками модульных констант, как было раньше. Константа
нужна одна и та же кнопка на всех экранах — а тут почти каждая клавиатура
зависит от баланса, ставки или роли, и константой её не выразить.

Схема callback_data — простые строки через двоеточие, парсятся split(':').
Ключевое: в игровых кнопках всегда едет round_id, чтобы клик из старого
сообщения нельзя было применить к новой ставке.
"""

from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           WebAppInfo)

import config
from db import fmt
from games.registry import GAMES, GROUPS, by_group

BACK = '⬅️ Назад'


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def add_to_chat(bot_username: str | None) -> list[InlineKeyboardButton] | None:
    """Кнопка «добавить бота в чат» — строкой, готовой к вставке в клавиатуру.

    startgroup — deep link, а не callback, поэтому без имени бота ссылку не
    собрать; без имени кнопки просто нет, мёртвая ссылка хуже её отсутствия.
    """
    if not bot_username:
        return None
    return [InlineKeyboardButton(
        text='➕ Добавить бота в чат',
        url=f'https://t.me/{bot_username}?startgroup=true')]


def support_row() -> list[InlineKeyboardButton] | None:
    """Кнопка поддержки. Ведёт в личку, если в конфиге @username, а не текст."""
    url = config.support_url()
    if url is None:
        return None
    return [InlineKeyboardButton(text='💬 Поддержка', url=url)]


CASE_LABEL = '🎁 Ежедневный кейс'


def webapp_row(private: bool, label: str = CASE_LABEL, screen: str = 'case'
               ) -> list[InlineKeyboardButton] | None:
    """Кнопка Mini App. None — Mini App не настроен или чат не приватный.

    web_app-кнопки Telegram принимает только в личке; в группе он отклонит из-за
    неё всю клавиатуру целиком, поэтому там кнопки просто нет — кейс открывается
    в личке, кнопкой `case`.

    screen — с какого экрана открыть приложение (`config.webapp_screen_url`):
    кнопка кейса ведёт сразу на кейс, кнопка слотов — на слоты, и игроку не
    приходится искать нужную вкладку после каждого открытия.
    """
    if not private or not config.WEBAPP_URL:
        return None
    return [InlineKeyboardButton(
        text=label, web_app=WebAppInfo(url=config.webapp_screen_url(screen)))]


def case_row(private: bool) -> list[InlineKeyboardButton]:
    """Вход в кейс — всегда экран внутри бота.

    Раньше в личке здесь стояла web_app-кнопка, и кейс открывался только в
    приложении: если приложение не открылось, кейса у игрока не было вовсе.
    Теперь кнопка одна и та же в личке и в группе, а приложение — отдельный
    вход рядом.
    """
    return [btn(CASE_LABEL, 'case')]


APP_LABEL = '🎮 Играть в приложении'


def app_row(private: bool) -> list[InlineKeyboardButton] | None:
    """Вход в Mini App. None — приложения нет или это группа.

    Ведёт на каталог игр приложения: там и слот, и краш с минами, которых в боте
    нет. Запасной кнопки внутри бота у этой строки нет — без приложения её просто
    не рисуем.
    """
    return webapp_row(private, APP_LABEL, 'games')


# --- меню -------------------------------------------------------------------

def main_menu(is_admin: bool = False, bot_username: str | None = None,
              private: bool = True) -> InlineKeyboardMarkup:
    """Главное меню.

    bot_username нужен для «Добавить бота в чат»: startgroup — это deep link,
    а не callback, и без имени бота ссылку не собрать. Если имя не передали,
    кнопка просто не рисуется — мёртвая ссылка хуже её отсутствия.

    private — меню рисуется в личке. От этого зависит кнопка приложения: web_app
    Telegram принимает только в личке, поэтому в группе её нет. Кейс работает и
    там и там: он открывается экраном внутри бота.

    Раздела «Помощь» здесь нет: экран-пересказ того, что и так написано на
    экранах кассы и правил, только прятал за собой профиль и баланс. Теперь
    они лежат прямо в меню, а честная игра и команды в чате — на экране игр.
    """
    rows = [
        [btn('🎮 Игры', 'games')],
        case_row(private),
        [btn('⬆️ Пополнить', 'dep'), btn('⬇️ Вывод', 'wd')],
        [btn('👤 Профиль', 'profile'), btn('💳 Баланс', 'balance')],
        [btn('👥 Пригласить', 'refs'), btn('🏆 ТОП-10', 'top')],
    ]
    for row in (app_row(private), add_to_chat(bot_username), support_row()):
        if row:
            rows.append(row)
    if is_admin:
        rows.append([btn('🛠 Админка', 'admin')])
    return _kb(rows)


def refs_menu(bot_username: str | None = None) -> InlineKeyboardMarkup:
    """Экран рефералки. Двe ветки заработка — друзья и чаты — рядом."""
    rows = [[btn('🤖 Мои чаты', 'mychats')]]
    row = add_to_chat(bot_username)
    if row:
        rows.append(row)
    rows.append([btn(BACK, 'menu')])
    return _kb(rows)


def chats_menu(bot_username: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    row = add_to_chat(bot_username)
    if row:
        rows.append(row)
    rows.append([btn('🔄 Обновить', 'mychats')])
    rows.append([btn('⬅️ К рефералке', 'refs'), btn('🏠 Меню', 'menu')])
    return _kb(rows)


def top_menu() -> InlineKeyboardMarkup:
    return _kb([[btn('🔄 Обновить', 'top')], [btn(BACK, 'menu')]])


def chat_help_menu() -> InlineKeyboardMarkup:
    return _kb([[btn('⬅️ К играм', 'games')], [btn('🏠 Меню', 'menu')]])


def back_menu(text: str = BACK) -> InlineKeyboardMarkup:
    return _kb([[btn(text, 'menu')]])


def back_to(target: str, text: str = BACK) -> InlineKeyboardMarkup:
    return _kb([[btn(text, target)]])


# --- ежедневный кейс --------------------------------------------------------
#
# Списки каналов сюда приезжают готовыми словарями (daily.as_dict), а не
# строками базы: клавиатуры не должны знать ни про базу, ни про Telegram.


def case_cards(case_id: int, cards: int,
               private: bool = True) -> InlineKeyboardMarkup:
    """Закрытые карточки кейса.

    В callback_data едет id кейса — по тому же принципу, что round_id в играх:
    клик из старого сообщения не может открыть карточку в новом кейсе.
    """
    rows = [[btn(f'🎁 {i + 1}', f'case:pick:{case_id}:{i}') for i in range(cards)]]
    row = webapp_row(private, '🚀 Открыть в Mini App')
    if row:
        rows.append(row)
    rows.append([btn('🏠 Меню', 'menu')])
    return _kb(rows)


def case_result(case) -> InlineKeyboardMarkup:
    """Кейс открыт: все карточки раскрыты и больше ничего не делают."""
    row = []
    for i in range(case['cards']):
        label = fmt(case['prize_cents']) if i == case['win_index'] else fmt(0)
        mark = '👉 ' if i == case['picked_index'] else ''
        row.append(btn(mark + label, 'nop'))
    return _kb([row, [btn('🎮 Играть', 'games'), btn('🏠 Меню', 'menu')]])


def case_subscribe(channels: list[dict],
                   private: bool = True) -> InlineKeyboardMarkup:
    """Каналы, куда надо подписаться, и кнопка перепроверки."""
    rows = [[InlineKeyboardButton(text='📢 ' + (ch['title'] or 'Канал')[:40],
                                  url=ch['url'])]
            for ch in channels[:8] if ch.get('url')]
    rows.append([btn('✅ Проверить подписку', 'case:check')])
    row = webapp_row(private, '🚀 Открыть в Mini App')
    if row:
        rows.append(row)
    rows.append([btn('🏠 Меню', 'menu')])
    return _kb(rows)


def case_wait(private: bool = True) -> InlineKeyboardMarkup:
    """Кейс уже получен — только таймер и обновление."""
    rows = [[btn('🔄 Обновить', 'case')]]
    row = webapp_row(private, '🚀 Открыть в Mini App')
    if row:
        rows.append(row)
    rows.append([btn('🎮 Играть', 'games'), btn('🏠 Меню', 'menu')])
    return _kb(rows)


# --- игры -------------------------------------------------------------------

def groups_menu() -> InlineKeyboardMarkup:
    """Разделы игр.

    Здесь же честная игра и команды в чате: оба экрана про то, как играть, и
    после сноса «Помощи» им нужен был живой вход, а не мёртвая ссылка.
    """
    rows = []
    for g in GROUPS.values():
        total = by_group(g.key)
        live = sum(1 for s in total if s.ready)
        rows.append([btn(f'{g.emoji} {g.title} · {live}/{len(total)}', f'grp:{g.key}')])
    rows.append([btn('🔒 Честная игра', 'fair'), btn('💬 Команды в чате', 'chatcmd')])
    rows.append([btn(BACK, 'menu')])
    return _kb(rows)


def group_games(group: str) -> InlineKeyboardMarkup:
    rows = []
    for spec in by_group(group):
        label = f'{spec.emoji} {spec.title}'
        if not spec.ready:
            label += ' · скоро'
        rows.append([btn(label, f'game:{spec.key}')])
    rows.append([btn('⬅️ К группам', 'games')])
    return _kb(rows)


def bet_screen(game_key: str, bet_cents: int, ready: bool) -> InlineKeyboardMarkup:
    """Экран ставки. Кнопка запуска подписана суммой — видно, чем играешь."""
    step = config.BET_STEP_CENTS
    big = step * 10
    rows = [
        [
            btn(f'−{fmt(big)}', f'bet:{game_key}:-{big}'),
            btn(f'−{fmt(step)}', f'bet:{game_key}:-{step}'),
            btn(f'+{fmt(step)}', f'bet:{game_key}:{step}'),
            btn(f'+{fmt(big)}', f'bet:{game_key}:{big}'),
        ],
        [
            btn('Мин', f'bet:{game_key}:min'),
            btn('✏️ Своя', f'bet:{game_key}:ask'),
            btn('Макс', f'bet:{game_key}:max'),
        ],
    ]
    if ready:
        rows.append([btn(f'▶️ Играть на {fmt(bet_cents)}', f'play:{game_key}')])
    rows.append([btn('📖 Правила', f'rules:{game_key}')])
    rows.append([btn('⬅️ К играм', f'grp:{GAMES[game_key].group}')])
    return _kb(rows)


def rules_screen(game_key: str) -> InlineKeyboardMarkup:
    return _kb([[btn('⬅️ К ставке', f'game:{game_key}')]])


def again(game_key: str) -> InlineKeyboardMarkup:
    """После завершённого раунда. Ставка берётся заново из профиля."""
    return _kb([
        [btn('🔄 Ещё раз', f'play:{game_key}')],
        [btn('💰 Ставка', f'game:{game_key}'), btn('🎮 Игры', 'games')],
    ])


# --- ставка на исход дайса --------------------------------------------------
# Дайс-игры спрашивают сначала исход, потом сумму, поэтому выбранный исход
# едет в callback_data каждой кнопки: «Играть» не может применить набранную
# сумму к другому варианту, а «Ещё раз» повторяет именно ту же ставку.

def pick_stake(game_key: str, code: str, bet_cents: int) -> InlineKeyboardMarkup:
    """Сумма ставки на уже выбранный исход."""
    step = config.BET_STEP_CENTS
    big = step * 10
    head = f'pb:{game_key}:{code}'
    return _kb([
        [
            btn(f'−{fmt(big)}', f'{head}:-{big}'),
            btn(f'−{fmt(step)}', f'{head}:-{step}'),
            btn(f'+{fmt(step)}', f'{head}:{step}'),
            btn(f'+{fmt(big)}', f'{head}:{big}'),
        ],
        [
            btn('Мин', f'{head}:min'),
            btn('✏️ Своя', f'{head}:ask'),
            btn('Макс', f'{head}:max'),
        ],
        [btn(f'▶️ Играть на {fmt(bet_cents)}', f'pp:{game_key}:{code}')],
        [btn('📖 Правила', f'rules:{game_key}'),
         btn('⬅️ Исходы', f'pk:{game_key}')],
    ])


def pick_again(game_key: str, code: str) -> InlineKeyboardMarkup:
    """После раунда: повторить ту же ставку или выбрать другой исход."""
    return _kb([
        [btn('🔄 Ещё раз', f'pp:{game_key}:{code}')],
        [btn('🎲 Другой исход', f'pk:{game_key}'), btn('🎮 Игры', 'games')],
    ])


# --- баланс -----------------------------------------------------------------

def balance_menu() -> InlineKeyboardMarkup:
    return _kb([
        [btn('⬆️ Пополнить', 'dep'), btn('⬇️ Вывести', 'wd')],
        [btn('🎁 Промокод / ваучер', 'code')],
        [btn('📜 История игр', 'history'), btn('💸 Мои выводы', 'wd:list')],
        [btn(BACK, 'menu')],
    ])


def deposit_amounts(pending_id: str | None = None,
                    pending_cents: int = 0) -> InlineKeyboardMarkup:
    rows, row = [], []
    # Пресеты в центах, а не в долларах: минимальный деп — $0.10, и его тоже
    # надо уметь нажать кнопкой. Всё, что ниже минимума, из списка выпадает.
    for cents in (10, 50, 100, 500, 1_000, 2_500, 10_000, 25_000):
        if not config.MIN_DEPOSIT_CENTS <= cents <= config.MAX_DEPOSIT_CENTS:
            continue
        row.append(btn(fmt(cents), f'dep:{cents}'))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn('✏️ Своя сумма', 'dep:ask')])
    if pending_id:
        # Неоплаченный счёт лучше показать, чем плодить рядом ещё один.
        rows.append([btn(f'⏳ Счёт на {fmt(pending_cents)}', f'inv:{pending_id}')])
    rows.append([btn(BACK, 'balance')])
    return _kb(rows)


def invoice_kb(pay_url: str, invoice_id: str) -> InlineKeyboardMarkup:
    return _kb([
        [InlineKeyboardButton(text='💎 Оплатить', url=pay_url)],
        [btn('🔄 Проверить оплату', f'inv:{invoice_id}')],
        [btn('❌ Отменить', f'invx:{invoice_id}')],
    ])


def withdraw_confirm(amount_cents: int) -> InlineKeyboardMarkup:
    return _kb([
        [btn(f'✅ Вывести {fmt(amount_cents)}', 'wd:ok')],
        [btn('❌ Отмена', 'balance')],
    ])


def withdrawals_list(rows) -> InlineKeyboardMarkup:
    """Мои заявки: только просмотр, отменять уже нельзя — деньги в обработке."""
    return _kb([[btn('⬅️ К балансу', 'balance')]] if not rows else
               [[btn('🔄 Обновить', 'wd:list')], [btn('⬅️ К балансу', 'balance')]])


def cancel_to(target: str) -> InlineKeyboardMarkup:
    return _kb([[btn('❌ Отмена', target)]])


# --- админка ----------------------------------------------------------------

def admin_menu(pending: int = 0) -> InlineKeyboardMarkup:
    wd = '💸 Заявки' + (f' · {pending}' if pending else '')
    return _kb([
        [btn('📊 Статистика', 'admin:stats'), btn(wd, 'admin:wd')],
        [btn('👤 Юзеры', 'admin:users'), btn('📣 Рассылка', 'admin:cast')],
        [btn('🎁 Промокоды', 'admin:promo'), btn('🎟 Ваучеры', 'admin:vouch')],
        [btn('📢 Каналы для кейса', 'admin:chan')],
        [btn('🛠 Админы', 'admin:roles')],
        [btn(BACK, 'menu')],
    ])


def admin_channels(rows) -> InlineKeyboardMarkup:
    """Каналы обязательной подписки. Кнопка канала — удаление."""
    out = [[btn('➕ Добавить канал', 'achan:add')]]
    for r in rows[:10]:
        title = r['title'] or (f'@{r["username"]}' if r['username']
                               else str(r['chat_id']))
        out.append([btn(('⚠️ ' if r['broken'] else '➖ ') + title[:40],
                        f'achan:del:{r["chat_id"]}')])
    out.append([btn('📋 Список каналов', 'admin:chan')])
    out.append([btn('⬅️ В админку', 'admin')])
    return _kb(out)


def withdrawal_card(withdrawal_id: int) -> InlineKeyboardMarkup:
    """В callback_data едет id заявки.

    В прежней версии сюда подставлялся chat.id админа (adminpanel.py:747),
    из-за чего «одобрить» применялось к самому админу.
    """
    return _kb([
        [btn('✅ Выплатить', f'wdok:{withdrawal_id}'),
         btn('❌ Отклонить', f'wdno:{withdrawal_id}')],
        [btn('📋 Все заявки', 'admin:wd')],
    ])


def admin_back(target: str = 'admin') -> InlineKeyboardMarkup:
    return _kb([[btn('⬅️ В админку', target)]])


def admin_wd_list(rows) -> InlineKeyboardMarkup:
    out = [[btn(f'#{r["id"]} · {fmt(r["amount_cents"])}', f'awd:{r["id"]}')]
           for r in rows[:10]]
    out.append([btn('🔄 Обновить', 'admin:wd'), btn('⬅️ В админку', 'admin')])
    return _kb(out)


def admin_user_card(user_id: int, banned: bool) -> InlineKeyboardMarkup:
    return _kb([
        [btn('➕ Баланс', f'auser:add:{user_id}'),
         btn('➖ Баланс', f'auser:sub:{user_id}')],
        [btn('✅ Разбанить', f'auser:unban:{user_id}') if banned
         else btn('🚫 Забанить', f'auser:ban:{user_id}')],
        [btn('🔎 Другой', 'admin:users'), btn('⬅️ В админку', 'admin')],
    ])


def admin_codes(kind: str, rows) -> InlineKeyboardMarkup:
    """kind: 'promo' (проценты) или 'vouch' (фикс). Кнопка = удаление кода."""
    out = [[btn('➕ Создать', f'a{kind}:new')]]
    out += [[btn(f'❌ {r["code"]}', f'a{kind}:del:{r["code"]}')] for r in rows[:10]]
    out.append([btn('⬅️ В админку', 'admin')])
    return _kb(out)


def admin_roles(rows) -> InlineKeyboardMarkup:
    out = [[btn('➕ Выдать админку', 'arole:add')]]
    out += [[btn('❌ ' + ('@' + r['username'] if r['username']
                         else str(r['user_id'])), f'arole:del:{r["user_id"]}')]
            for r in rows[:10]]
    out.append([btn('⬅️ В админку', 'admin')])
    return _kb(out)


def broadcast_confirm(total: int) -> InlineKeyboardMarkup:
    return _kb([
        [btn(f'📣 Отправить {total} игрокам', 'acast:go')],
        [btn('❌ Отмена', 'admin')],
    ])
