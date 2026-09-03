"""Ежедневный кейс: правила выдачи и проверка подписки.

Модуль намеренно ничего не знает ни про хендлеры aiogram, ни про HTTP. И
кнопки в боте (`handlers/daily.py`), и Mini App (`webapp/server.py`) ходят
сюда, поэтому правила у них одни и те же, а не две расходящиеся копии.

Что здесь решается:

* положен ли игроку кейс прямо сейчас (пауза считается в `db.daily_ready_at`);
* какой день серии идёт и сколько лежит в кейсе (`db.daily_streak`);
* на какие каналы он не подписан — список каналов задаёт админ, в коде их нет;
* что делать, если подписку проверить нечем.

Про последнее подробнее. `getChatMember` работает только если бот сам сидит в
канале администратором. Если админ добавил канал и потом выгнал бота, Telegram
отвечает отказом, и у нас два варианта: не выдавать кейс никому или не считать
такой канал обязательным. Выбран второй: игрок в этой ошибке не виноват и
починить её не может, а бесконечное «подпишись» на канал, где он и так
подписан, выглядит как поломка бота. Канал помечается `broken`, админ видит
это в списке вместе с текстом ошибки.
"""

import logging

from aiogram.exceptions import TelegramAPIError

import config
import db

log = logging.getLogger(__name__)

# Статусы участника, при которых подписка считается живой. 'restricted'
# проверяется отдельно: там участие определяет отдельный флаг is_member.
MEMBER_STATUSES = ('member', 'administrator', 'creator', 'owner')


def channel_url(row) -> str | None:
    """Ссылка на канал для кнопки. Нечего показать — None."""
    if row['username']:
        return f'https://t.me/{row["username"]}'
    return row['invite_url'] or None


def channel_title(row) -> str:
    return row['title'] or (f'@{row["username"]}' if row['username']
                            else str(row['chat_id']))


def as_dict(row) -> dict:
    """Канал в виде, пригодном для JSON (Mini App) и для кнопок."""
    return {'chat_id': row['chat_id'], 'title': channel_title(row),
            'url': channel_url(row), 'broken': bool(row['broken'])}


async def _member_status(bot, chat_id: int, user_id: int) -> tuple[bool | None, str]:
    """(подписан?, текст ошибки). None — проверить не удалось."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as e:
        return None, str(e)
    except Exception as e:                      # сеть, таймаут — тоже не отказ
        return None, str(e)

    status = getattr(member, 'status', None)
    status = getattr(status, 'value', status)
    if status in MEMBER_STATUSES:
        return True, ''
    if status == 'restricted':
        return bool(getattr(member, 'is_member', False)), ''
    return False, ''


async def check_channels(bot, user_id: int) -> tuple[list, list]:
    """(каналы без подписки, каналы без проверки). Оба списка — строки базы.

    Заодно поддерживает пометку `broken`: канал, который снова проверяется,
    из проблемных выходит сам, без ручного вмешательства админа.
    """
    missing, broken = [], []
    for row in await db.list_channels():
        ok, note = await _member_status(bot, row['chat_id'], user_id)
        if ok is None:
            broken.append(row)
            if not row['broken']:
                log.warning('канал %s не проверяется: %s', row['chat_id'], note)
            await db.mark_channel(row['chat_id'], True, note)
            continue
        if row['broken']:
            await db.mark_channel(row['chat_id'], False, '')
        if not ok:
            missing.append(row)
    return missing, broken


async def state(bot, user_id: int) -> dict:
    """Полное состояние кейса для экрана — одинаковое в боте и в Mini App.

    status:
      'ready'     — кейс положен, можно выдавать;
      'open'      — кейс уже выдан, ждёт выбора карточки;
      'subscribe' — не хватает подписок;
      'cooldown'  — уже получен, ждём следующего.

    Серия отдаётся здесь же, чтобы оба экрана считали её одинаково:

      streak        — сколько карточек подряд игрок угадал; 0 — огонёк потух;
      streak_day    — какой это день серии для кейса, о котором идёт речь:
                      для выданного — его собственный, иначе — следующего;
      prize_cents   — что лежит в этом кейсе;
      next_prize_cents — что будет в следующем, если и этот угадать;
      streak_seconds_left — сколько осталось, чтобы серия не сгорела.
    """
    case = await db.open_daily_case(user_id)
    ready_at = await db.daily_ready_at(user_id)
    last = await db.last_daily_case(user_id)
    streak = await db.daily_streak(user_id)

    missing: list = []
    broken: list = []
    if case is not None or ready_at == 0:
        # Пока кейс не положен, каналы дёргать незачем: экран всё равно покажет
        # таймер, а каждый лишний getChatMember — запрос к Telegram на игрока.
        missing, broken = await check_channels(bot, user_id)

    if case is not None:
        status = 'subscribe' if missing else 'open'
    elif ready_at:
        status = 'cooldown'
    else:
        status = 'subscribe' if missing else 'ready'

    return {
        'status': status,
        'case': case,
        'last': last,
        'ready_at': ready_at,
        'seconds_left': max(0, ready_at - db.now()) if ready_at else 0,
        'missing': missing,
        'broken': broken,
        'prize_cents': streak['prize_cents'],
        'cards': config.DAILY_CARDS,
        'cooldown': config.DAILY_COOLDOWN,
        'streak': streak['streak'],
        'streak_day': streak['day'],
        'next_prize_cents': streak['next_prize_cents'],
        'streak_step_cents': config.DAILY_STREAK_STEP_CENTS,
        'streak_max_days': config.DAILY_STREAK_MAX_DAYS,
        'streak_expires_at': streak['expires_at'],
        'streak_seconds_left': max(0, streak['expires_at'] - db.now())
                               if streak['expires_at'] else 0,
    }


async def issue(bot, user_id: int) -> tuple[dict, str]:
    """Выдаёт кейс с проверкой подписки. (состояние, статус выдачи).

    Статусы: 'issued', 'open', 'cooldown', 'subscribe'. Проверка подписки идёт
    здесь, а не в базе: база про Telegram ничего не знает и знать не должна.
    """
    st = await state(bot, user_id)
    if st['status'] in ('subscribe', 'cooldown'):
        return st, st['status']

    case, result = await db.issue_daily_case(user_id)
    if result == 'cooldown':
        return await state(bot, user_id), 'cooldown'
    st['case'] = case
    st['status'] = 'open'
    return st, result


def left_text(seconds: int) -> str:
    """900 -> '15 мин'. Для таймера в подписи и в Mini App."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours} ч {minutes:02d} мин'
    if minutes:
        return f'{minutes} мин {secs:02d} с'
    return f'{secs} с'
