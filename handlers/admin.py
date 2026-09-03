"""Панель админа: статистика, заявки, юзеры, рассылка, промо, ваучеры, роли.

Все баги прежней adminpanel.py закрыты по построению:

* уровни рефералки считает db.referral_level по config.REFERRAL_LEVELS —
  цепочки `20 >= x <= 11` (adminpanel.py:52), которая ломала все уровни, тут
  нет вообще;
* поиск юзера идёт через db.resolve_user, а не через `user.isnumeric == False`
  (adminpanel.py:352,398 — сравнение метода с булевым, всегда False);
* разбан ставит banned = 0 (в adminpanel.py:415 он ставил ban = 1, то есть
  разбан банил);
* в кнопках заявки едет её id, а не chat_id админа (adminpanel.py:747), из-за
  чего «одобрить вывод» применялось к самому админу;
* ни одного обращения к неопределённому имени (adminpanel.py:582 — NameError
  на user_id).
"""

import asyncio
import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

import config
import daily
import db
import emoji as E
import keyboards as kb
import payments
from db import fmt
from states import (AdminBroadcast, AdminChannel, AdminPromo, AdminRole,
                    AdminUser, AdminVoucher)
from ui import render

log = logging.getLogger(__name__)

CODE_RE = re.compile(r'^[A-Z0-9_-]{3,32}$')

# Пометка «результат перевода неизвестен»: по ней разрешаем повторную попытку
# по уже выплаченной заявке. spend_id тот же, дважды монеты не уйдут.
UNKNOWN = 'unknown:'


class IsAdmin(BaseFilter):
    """Гейт на весь роутер. is_admin приходит из UserMiddleware."""

    async def __call__(self, event: TelegramObject, is_admin: bool = False) -> bool:
        return bool(is_admin)


router = Router(name='admin')
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _nick(row) -> str:
    if row is None:
        return '—'
    if row['username']:
        return '@' + html.escape(row['username'])
    return f'ID {row["user_id"]}'


# --- главный экран ----------------------------------------------------------

async def _menu(event, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    pending = await db.pending_withdrawals()
    await render(event,
        '🛠 <b>Админка</b>\n\n'
        f'Заявок на вывод: <b>{len(pending)}</b>\n'
        f'Крипто-канал: '
        f'{"включён" if payments.client.enabled else "<b>выключен</b> (нет токена)"}'
        f'{" · testnet" if config.CRYPTO_PAY_TESTNET else ""}',
        kb.admin_menu(len(pending)))


@router.message(Command('admin'))
async def cmd_admin(message: Message, state: FSMContext):
    await _menu(message, state)


@router.callback_query(F.data == 'admin')
async def cb_admin(call: CallbackQuery, state: FSMContext):
    await _menu(call, state)
    await call.answer()


# --- статистика -------------------------------------------------------------

@router.callback_query(F.data == 'admin:stats')
async def cb_stats(call: CallbackQuery, state: FSMContext):
    await state.clear()
    s = await db.stats()
    case = await db.daily_stats()
    rtp = f'{s["actual_rtp"] * 100:.2f}%' if s['actual_rtp'] is not None else '—'

    await render(call,
        f'{E.STATS} <b>Статистика</b>\n\n'
        f'Игроков: <b>{s["users"]}</b> (в бане {s["banned"]})\n'
        f'На балансах: <b>{fmt(s["balances"])}</b>\n\n'
        f'<b>Касса</b>\n'
        f'Пополнено: {fmt(s["deposited"])}\n'
        f'Выведено: {fmt(s["withdrawn"])}\n'
        f'Заявок в очереди: {s["pending_withdrawals"]}\n'
        f'Сальдо кассы: <b>{fmt(s["deposited"] - s["withdrawn"])}</b>\n\n'
        f'<b>Игры против казино</b>\n'
        f'Раундов: {s["rounds"]}\n'
        f'Оборот: {fmt(s["wagered"])}\n'
        f'Выплачено: {fmt(s["paid_to_players"])}\n'
        f'Прибыль: <b>{fmt(s["gross_profit"])}</b>\n'
        f'Фактический RTP: <b>{rtp}</b> (цель {config.RTP * 100:.0f}%)\n\n'
        f'<b>PvP</b>\n'
        f'Комнат разыграно: {s["pvp_rooms"]}\n'
        f'Банков собрано: {fmt(s["pvp_pot"])}\n'
        f'Рейк: <b>{fmt(s["pvp_rake"])}</b>\n\n'
        f'{E.GIFT} <b>Ежедневный кейс</b>\n'
        f'Открыто: {case["opened"]} (игроков {case["players"]})\n'
        f'Роздано: <b>{fmt(case["paid"])}</b>\n\n'
        f'<i>Кейс — подарок, а не игра: в оборот и RTP он не входит, поэтому '
        f'считается отдельной строкой.</i>\n\n'
        f'<i>Фактический RTP сходится с целевым только на объёме: '
        f'на первых сотнях раундов разброс нормален.</i>',
        kb.admin_back())
    await call.answer()


# --- заявки на вывод --------------------------------------------------------

@router.callback_query(F.data == 'admin:wd')
async def cb_withdrawals(call: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await db.pending_withdrawals()
    if not rows:
        body = 'Очередь пуста.'
    else:
        body = '\n'.join(
            f'#{r["id"]} · <b>{fmt(r["amount_cents"])}</b> · '
            f'{("@" + html.escape(r["username"])) if r["username"] else r["user_id"]}'
            for r in rows)
    await render(call, f'{E.CASHIER} <b>Заявки на вывод</b>\n\n{body}',
                 kb.admin_wd_list(rows))
    await call.answer()


async def _wd_card(event, wd_id: int) -> None:
    wd = await db.get_withdrawal(wd_id)
    if wd is None:
        await render(event, f'{E.FAIL} Заявка не найдена.',
                     kb.admin_back('admin:wd'))
        return

    player = await db.get_user(wd['user_id'])
    head = {'pending': f'{E.WAIT} ждёт решения', 'paid': f'{E.OK} выплачена',
            'rejected': f'{E.FAIL} отклонена',
            'failed': '⚠️ перевод не прошёл'}
    text = (f'{E.CASHIER} <b>Заявка #{wd["id"]}</b> — '
            f'{head.get(wd["status"], wd["status"])}\n\n'
            f'Игрок: {_nick(player)} (<code>{wd["user_id"]}</code>)\n'
            f'Сумма: <b>{fmt(wd["amount_cents"])}</b>\n')
    if player is not None:
        text += (f'Баланс сейчас: {fmt(player["balance_cents"])}\n'
                 f'Пополнял: {fmt(player["deposited_cents"])}\n'
                 f'Оборот: {fmt(player["wagered_cents"])}\n'
                 f'Выиграл: {fmt(player["won_cents"])}\n')
    if wd['note']:
        text += f'\nЗаметка: <i>{html.escape(wd["note"])}</i>\n'

    if wd['status'] == 'pending':
        await render(event, text, kb.withdrawal_card(wd['id']))
    elif wd['status'] == 'paid' and (wd['note'] or '').startswith(UNKNOWN):
        text += ('\n⚠️ Результат перевода неизвестен — связь оборвалась. '
                 'Проверь в @CryptoBot и, если монеты не ушли, нажми '
                 '«Выплатить» ещё раз: повтор безопасен, spend_id тот же.')
        await render(event, text, kb.withdrawal_card(wd['id']))
    else:
        await render(event, text, kb.admin_back('admin:wd'))


@router.callback_query(F.data.startswith('awd:'))
async def cb_wd_open(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _wd_card(call, int(call.data.split(':')[1]))
    await call.answer()


@router.callback_query(F.data.startswith('wdok:'))
async def cb_wd_pay(call: CallbackQuery, user):
    wd_id = int(call.data.split(':')[1])

    wd = await db.claim_withdrawal(wd_id, user['user_id'])
    if wd is None:
        # Либо уже обработана, либо это повтор по «неизвестному» переводу.
        existing = await db.get_withdrawal(wd_id)
        if existing is None:
            await call.answer('Заявка не найдена.', show_alert=True)
            return
        if not (existing['status'] == 'paid'
                and (existing['note'] or '').startswith(UNKNOWN)):
            await call.answer('Заявку уже обработали.', show_alert=True)
            await _wd_card(call, wd_id)
            return
        wd = existing

    await call.answer('Отправляю…')
    status, note = await payments.pay_withdrawal(
        wd_id, wd['user_id'], wd['amount_cents'])

    if status == 'sent':
        await db.set_withdrawal_note(wd_id, note)
        text = (f'{E.OK} <b>Заявка #{wd_id} выплачена</b>\n\n'
                f'Сумма: <b>{fmt(wd["amount_cents"])}</b>\n'
                f'Игрок: <code>{wd["user_id"]}</code>\n'
                f'{html.escape(note)}')
        await _notify_player(call.bot, wd['user_id'],
            f'{E.OK} <b>Вывод #{wd_id} выполнен</b>\n\n'
            f'Сумма: <b>{fmt(wd["amount_cents"])}</b>\n'
            f'{config.CRYPTO_ASSET} ушли в @CryptoBot. Если это чек — открой '
            f'ссылку из сообщения бота @CryptoBot и активируй его.')
    elif status == 'failed':
        await db.fail_withdrawal(wd_id, note)
        text = (f'⚠️ <b>Заявка #{wd_id}: перевод не прошёл</b>\n\n'
                f'{html.escape(note)}\n\n'
                f'Сумма <b>{fmt(wd["amount_cents"])}</b> вернулась игроку '
                f'на баланс.')
        await _notify_player(call.bot, wd['user_id'],
            f'⚠️ <b>Вывод #{wd_id} не удался</b>\n\n'
            f'Сумма <b>{fmt(wd["amount_cents"])}</b> вернулась на баланс. '
            f'Причина: {html.escape(note)}\n\n'
            f'Открой @CryptoBot хотя бы раз и попробуй снова — переводы '
            f'приходят только тем, у кого есть кошелёк.')
    else:
        await db.set_withdrawal_note(wd_id, UNKNOWN + note)
        text = (f'{E.QUESTION} <b>Заявка #{wd_id}: результат неизвестен</b>\n\n'
                f'{html.escape(note)}\n\n'
                f'Заявка осталась выплаченной, деньги игроку НЕ возвращены — '
                f'перевод мог пройти. Проверь в @CryptoBot и при необходимости '
                f'нажми «Выплатить» повторно: spend_id тот же, дважды не уйдёт.')

    log.info('заявка #%s: %s (%s), админ %s', wd_id, status, note, user['user_id'])
    await render(call, text, kb.admin_back('admin:wd'))


@router.callback_query(F.data.startswith('wdno:'))
async def cb_wd_reject(call: CallbackQuery, user):
    wd_id = int(call.data.split(':')[1])
    wd = await db.reject_withdrawal(wd_id, user['user_id'],
                                    'отклонено администратором')
    if wd is None:
        await call.answer('Заявку уже обработали.', show_alert=True)
        await _wd_card(call, wd_id)
        return

    await render(call,
        f'{E.FAIL} <b>Заявка #{wd_id} отклонена</b>\n\n'
        f'Сумма <b>{fmt(wd["amount_cents"])}</b> возвращена игроку '
        f'<code>{wd["user_id"]}</code> на баланс.',
        kb.admin_back('admin:wd'))
    await call.answer('Отклонено')

    await _notify_player(call.bot, wd['user_id'],
        f'{E.FAIL} <b>Заявка на вывод #{wd_id} отклонена</b>\n\n'
        f'Сумма <b>{fmt(wd["amount_cents"])}</b> вернулась на баланс целиком. '
        f'За разъяснениями — {html.escape(config.SUPPORT_NAME)}.')
    log.info('заявка #%s отклонена админом %s', wd_id, user['user_id'])


async def _notify_player(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=kb.balance_menu())
    except Exception as e:
        log.warning('игрок %s недоступен: %s', user_id, e)


# --- юзеры ------------------------------------------------------------------

@router.callback_query(F.data == 'admin:users')
async def cb_users(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminUser.ident)
    await render(call,
        f'{E.PROFILE} <b>Юзеры</b>\n\n'
        'Пришли <code>ID</code> или <code>@ник</code>.\n\n'
        'Ник ищется по тому, что Telegram присылал последним, — если игрок его '
        'сменил, надёжнее искать по ID.',
        kb.admin_back())
    await call.answer()


async def _user_card(event, target_id: int) -> None:
    row = await db.get_user(target_id)
    if row is None:
        await render(event, f'{E.FAIL} Такого игрока нет в базе.',
                     kb.admin_back())
        return

    level, percent = db.referral_level(row['referrals'])
    net = row['won_cents'] - row['wagered_cents']
    pending = [w for w in await db.user_withdrawals(target_id)
               if w['status'] == 'pending']

    status = '🚫 в бане' if row['banned'] else f'{E.OK} активен'

    await render(event,
        f'{E.PROFILE} <b>{_nick(row)}</b>\n\n'
        f'ID: <code>{row["user_id"]}</code>\n'
        f'Баланс: <b>{fmt(row["balance_cents"])}</b>\n'
        f'Статус: {status}\n\n'
        f'Пополнено: {fmt(row["deposited_cents"])}\n'
        f'Оборот: {fmt(row["wagered_cents"])}\n'
        f'Получено: {fmt(row["won_cents"])}\n'
        f'Итог игрока: <b>{fmt(net)}</b>\n\n'
        f'Друзей: {row["referrals"]} · уровень {level} ({percent}%)\n'
        f'С рефералов: {fmt(row["referral_earned_cents"])}\n'
        f'Пригласил: {row["referer_id"] or "—"}\n'
        f'Заявок в очереди: {len(pending)}',
        kb.admin_user_card(row['user_id'], bool(row['banned'])))


@router.message(AdminUser.ident)
async def user_search(message: Message, state: FSMContext):
    target = await db.resolve_user(message.text or '')
    if target is None:
        await message.answer(f'{E.FAIL} Не нашёл. Пришли другой ID или @ник.',
                             reply_markup=kb.admin_back())
        return
    await state.clear()
    await _user_card(message, target)


@router.callback_query(F.data.startswith('auser:'))
async def cb_user_action(call: CallbackQuery, state: FSMContext, user):
    _, action, raw = call.data.split(':')
    target = int(raw)

    if action in ('ban', 'unban'):
        if target == config.OWNER_ID:
            await call.answer('Владельца банить нельзя.', show_alert=True)
            return
        await db.set_banned(target, action == 'ban')
        await call.answer('Забанен' if action == 'ban' else 'Разбанен')
        await _user_card(call, target)
        log.info('админ %s: %s юзера %s', user['user_id'], action, target)
        return

    # ±баланс: сумму спрашиваем текстом, знак уже выбран кнопкой.
    await state.set_state(AdminUser.amount)
    await state.update_data(target=target, sign=1 if action == 'add' else -1)
    sign_text = 'начислить' if action == 'add' else 'списать'
    await render(call,
        f'{E.MONEY} <b>Баланс игрока {target}</b>\n\n'
        f'Сколько {sign_text}? Пришли сумму в долларах, например '
        f'<code>25</code> или <code>10.50</code>.',
        kb.admin_back())
    await call.answer()


@router.message(AdminUser.amount)
async def user_balance(message: Message, state: FSMContext, user):
    cents = db.parse_cents(message.text or '')
    if cents is None:
        await message.answer(f'{E.FAIL} Не понял сумму. Пришли положительное '
                             f'число.', reply_markup=kb.admin_back())
        return

    data = await state.get_data()
    target = int(data.get('target') or 0)
    sign = int(data.get('sign') or 1)
    await state.clear()

    if not target:
        await message.answer(f'{E.FAIL} Потерял, кому начислять. Начни заново.',
                             reply_markup=kb.admin_back())
        return

    if sign > 0:
        await db.add_balance(target, cents)
        ok, note = True, f'начислено {fmt(cents)}'
        player_text = (f'{E.MONEY} Администратор начислил тебе '
                       f'<b>{fmt(cents)}</b>.')
    else:
        ok = await db.take_balance(target, cents)
        note = f'списано {fmt(cents)}' if ok else 'не хватило баланса'
        player_text = (f'{E.MINUS} Администратор списал <b>{fmt(cents)}</b> '
                       f'с твоего баланса.')

    if ok:
        # Начисление админа — не выигрыш: won_cents не двигаем, иначе поедет
        # фактический RTP в статистике.
        await _notify_player(message.bot, target, player_text)
        log.info('админ %s: %s юзеру %s', user['user_id'], note, target)
    await message.answer(f'{E.OK if ok else E.FAIL} {note}.')
    await _user_card(message, target)


# --- рассылка ---------------------------------------------------------------

@router.callback_query(F.data == 'admin:cast')
async def cb_cast(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminBroadcast.content)
    total = len(await db.all_user_ids())
    await render(call,
        '📣 <b>Рассылка</b>\n\n'
        f'Пришли сообщение — текст, фото, видео, что угодно. Оно уйдёт копией '
        f'всем незабаненным игрокам (<b>{total}</b>).\n\n'
        f'Пауза между отправками {config.BROADCAST_DELAY} с, так что '
        f'{total} сообщений займут около '
        f'{int(total * config.BROADCAST_DELAY) // 60 + 1} мин.',
        kb.admin_back())
    await call.answer()


@router.message(AdminBroadcast.content)
async def cast_content(message: Message, state: FSMContext):
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(AdminBroadcast.confirm)
    total = len(await db.all_user_ids())
    await message.answer(
        f'📣 Вот это сообщение уйдёт <b>{total}</b> игрокам. Отправляем?',
        reply_markup=kb.broadcast_confirm(total))


@router.callback_query(F.data == 'acast:go', AdminBroadcast.confirm)
async def cb_cast_go(call: CallbackQuery, state: FSMContext, user):
    data = await state.get_data()
    await state.clear()
    src_chat, src_msg = data.get('chat_id'), data.get('message_id')
    if not src_chat or not src_msg:
        await call.answer('Потерял сообщение, начни заново.', show_alert=True)
        return

    ids = await db.all_user_ids()
    await render(call, f'📣 Рассылка пошла: 0 / {len(ids)}…', None)
    await call.answer()

    sent = failed = 0
    for i, uid in enumerate(ids, 1):
        try:
            await call.bot.copy_message(chat_id=uid, from_chat_id=src_chat,
                                        message_id=src_msg)
            sent += 1
        except Exception:
            failed += 1
        if i % 50 == 0:
            await render(call, f'📣 Рассылка: {i} / {len(ids)}…', None)
        await asyncio.sleep(config.BROADCAST_DELAY)

    log.info('рассылка админа %s: %s доставлено, %s нет',
             user['user_id'], sent, failed)
    await render(call,
        f'📣 <b>Рассылка закончена</b>\n\n'
        f'Доставлено: <b>{sent}</b>\n'
        f'Не дошло: {failed} (заблокировали бота или удалили аккаунт)',
        kb.admin_back())


# --- промокоды и ваучеры ----------------------------------------------------

async def _codes_screen(event, kind: str) -> None:
    if kind == 'promo':
        rows = await db.list_promos()
        head = f'{E.GIFT} <b>Промокоды</b> — процент к пополнению'
        lines = [f'<code>{r["code"]}</code> · +{r["percent"]}% · '
                 f'{r["usage_actual"]}/{r["usage_max"]}' for r in rows]
    else:
        rows = await db.list_vouchers()
        head = '🎟 <b>Ваучеры</b> — фикс к пополнению'
        lines = [f'<code>{r["code"]}</code> · +{fmt(r["amount_cents"])} · '
                 f'{r["usage_actual"]}/{r["usage_max"]}' for r in rows]

    body = '\n'.join(lines) if lines else 'Пока ни одного.'
    await render(event,
        f'{head}\n\n{body}\n\n'
        f'Бонус начисляется в момент зачисления пополнения, а не при вводе '
        f'кода. Кнопка с кодом — удаление.',
        kb.admin_codes(kind, rows))


@router.callback_query(F.data == 'admin:promo')
async def cb_promos(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _codes_screen(call, 'promo')
    await call.answer()


@router.callback_query(F.data == 'admin:vouch')
async def cb_vouchers(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _codes_screen(call, 'vouch')
    await call.answer()


@router.callback_query(F.data == 'apromo:new')
async def cb_promo_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminPromo.code)
    await render(call,
        f'{E.GIFT} <b>Новый промокод</b>\n\n'
        'Пришли код: латиница, цифры, дефис и подчёркивание, 3–32 символа.',
        kb.admin_back('admin:promo'))
    await call.answer()


@router.callback_query(F.data == 'avouch:new')
async def cb_voucher_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminVoucher.code)
    await render(call,
        '🎟 <b>Новый ваучер</b>\n\n'
        'Пришли код: латиница, цифры, дефис и подчёркивание, 3–32 символа.',
        kb.admin_back('admin:vouch'))
    await call.answer()


async def _take_code(message: Message, back: str) -> str | None:
    code = db.norm_code(message.text or '')
    if not CODE_RE.match(code):
        await message.answer(f'{E.FAIL} Код не подходит: только латиница, цифры, '
                             f'<code>-</code> и <code>_</code>, 3–32 символа.',
                             reply_markup=kb.admin_back(back))
        return None
    busy = await db.code_exists(code)
    if busy:
        await message.answer(
            f'{E.FAIL} Код <code>{code}</code> уже занят '
            f'({"промокод" if busy == "promo" else "ваучер"}).',
            reply_markup=kb.admin_back(back))
        return None
    return code


@router.message(AdminPromo.code)
async def promo_code(message: Message, state: FSMContext):
    code = await _take_code(message, 'admin:promo')
    if code is None:
        return
    await state.update_data(code=code)
    await state.set_state(AdminPromo.percent)
    await message.answer(f'Код <code>{code}</code>. Сколько процентов к '
                         f'пополнению? Пришли целое число, например <code>10</code>.')


@router.message(AdminPromo.percent)
async def promo_percent(message: Message, state: FSMContext):
    text = (message.text or '').strip()
    if not text.isdigit() or not 1 <= int(text) <= 500:
        await message.answer(f'{E.FAIL} Нужно целое от 1 до 500.')
        return
    await state.update_data(percent=int(text))
    await state.set_state(AdminPromo.usage_max)
    await message.answer('Сколько активаций всего? Целое число.')


@router.message(AdminPromo.usage_max)
async def promo_usage(message: Message, state: FSMContext, user):
    text = (message.text or '').strip()
    if not text.isdigit() or not 1 <= int(text) <= 1_000_000:
        await message.answer(f'{E.FAIL} Нужно целое от 1 до 1000000.')
        return

    data = await state.get_data()
    await state.clear()
    ok = await db.add_promo(data['code'], data['percent'], int(text))
    if ok:
        log.info('админ %s создал промокод %s', user['user_id'], data['code'])
        await message.answer(
            f'{E.OK} Промокод <code>{data["code"]}</code>: '
            f'+{data["percent"]}% к пополнению, {text} активаций.')
    else:
        await message.answer(f'{E.FAIL} Код успели занять, попробуй другой.')
    await _codes_screen(message, 'promo')


@router.message(AdminVoucher.code)
async def voucher_code(message: Message, state: FSMContext):
    code = await _take_code(message, 'admin:vouch')
    if code is None:
        return
    await state.update_data(code=code)
    await state.set_state(AdminVoucher.amount)
    await message.answer(f'Код <code>{code}</code>. Какая сумма к пополнению? '
                         f'Например <code>5</code> или <code>2.50</code>.')


@router.message(AdminVoucher.amount)
async def voucher_amount(message: Message, state: FSMContext):
    cents = db.parse_cents(message.text or '')
    if cents is None:
        await message.answer(f'{E.FAIL} Не понял сумму. Пришли положительное '
                             f'число.')
        return
    await state.update_data(amount=cents)
    await state.set_state(AdminVoucher.usage_max)
    await message.answer('Сколько активаций всего? Целое число.')


@router.message(AdminVoucher.usage_max)
async def voucher_usage(message: Message, state: FSMContext, user):
    text = (message.text or '').strip()
    if not text.isdigit() or not 1 <= int(text) <= 1_000_000:
        await message.answer(f'{E.FAIL} Нужно целое от 1 до 1000000.')
        return

    data = await state.get_data()
    await state.clear()
    ok = await db.add_voucher(data['code'], data['amount'], int(text))
    if ok:
        log.info('админ %s создал ваучер %s', user['user_id'], data['code'])
        await message.answer(
            f'{E.OK} Ваучер <code>{data["code"]}</code>: '
            f'+{fmt(data["amount"])} к пополнению, {text} активаций.')
    else:
        await message.answer(f'{E.FAIL} Код успели занять, попробуй другой.')
    await _codes_screen(message, 'vouch')


@router.callback_query(F.data.startswith('apromo:del:'))
async def cb_promo_del(call: CallbackQuery, user):
    code = call.data.split(':', 2)[2]
    ok = await db.delete_code('promo', code)
    await call.answer(f'Удалён {code}' if ok else 'Уже удалён')
    log.info('админ %s удалил промокод %s', user['user_id'], code)
    await _codes_screen(call, 'promo')


@router.callback_query(F.data.startswith('avouch:del:'))
async def cb_voucher_del(call: CallbackQuery, user):
    code = call.data.split(':', 2)[2]
    ok = await db.delete_code('voucher', code)
    await call.answer(f'Удалён {code}' if ok else 'Уже удалён')
    log.info('админ %s удалил ваучер %s', user['user_id'], code)
    await _codes_screen(call, 'vouch')


# --- каналы для кейса -------------------------------------------------------
#
# Список обязательных подписок живёт только в базе (таблица required_channels):
# в коде нет ни одного канала, добавляет и убирает их админ, а не деплой.

# Чем админ может задать канал: @ник, ссылка на него или числовой id.
CHANNEL_REF_RE = re.compile(r'^(?:https?://)?(?:t\.me/|telegram\.me/)?@?'
                            r'([A-Za-z][A-Za-z0-9_]{3,31})$')


def _channel_line(row) -> str:
    title = html.escape(daily.channel_title(row))
    ref = (f'@{row["username"]}' if row['username']
           else f'<code>{row["chat_id"]}</code>')
    if row['broken']:
        note = html.escape((row['note'] or 'подписку проверить нечем')[:120])
        return f'⚠️ <b>{title}</b> · {ref}\n     не проверяется: {note}'
    return f'{E.OK} <b>{title}</b> · {ref}'


async def _channels_screen(event) -> None:
    rows = await db.list_channels()
    body = '\n'.join(_channel_line(r) for r in rows) if rows else \
        'Список пуст — кейс выдаётся без подписки.'
    await render(event,
        f'📢 <b>Каналы для кейса</b>\n\n{body}\n\n'
        f'Пока игрок не подписан на все каналы из списка, ежедневный кейс он '
        f'не получит.\n\n'
        f'Кнопка канала — удалить. Чтобы обновить название или ссылку, добавь '
        f'канал ещё раз: данные перезапишутся, дубля не будет.\n\n'
        f'⚠️ значит, что Telegram не отвечает боту, подписан ли игрок. Такой '
        f'канал в проверке не участвует — иначе кейс не получил бы никто. '
        f'Чаще всего лечится тем, что бота делают админом канала.',
        kb.admin_channels(rows))


@router.callback_query(F.data == 'admin:chan')
async def cb_channels(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _channels_screen(call)
    await call.answer()


@router.callback_query(F.data == 'achan:add')
async def cb_channel_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminChannel.ident)
    await render(call,
        '📢 <b>Добавить канал</b>\n\n'
        'Пришли одним сообщением что угодно из этого:\n\n'
        '• <code>@username</code> канала\n'
        '• ссылку <code>https://t.me/username</code>\n'
        '• числовой id вида <code>-1001234567890</code>\n'
        '• или просто перешли сюда любой пост из канала\n\n'
        'Бот должен быть <b>администратором</b> канала: иначе Telegram не '
        'отвечает на вопрос о подписке, и канал попадёт в список с ⚠️.',
        kb.admin_back('admin:chan'))
    await call.answer()


def _ref_from(message: Message) -> str | int | None:
    """Что админ имел в виду: id пересланного канала, @ник или числовой id."""
    origin = getattr(message, 'forward_origin', None)
    chat = getattr(origin, 'chat', None)
    if chat is not None:
        return chat.id
    try:                                    # старое поле, до Bot API 7.0
        legacy = message.forward_from_chat
    except Exception:
        legacy = None
    if legacy is not None:
        return legacy.id

    text = (message.text or message.caption or '').strip()
    if not text:
        return None
    if text.lstrip('-').isdigit():
        return int(text)
    m = CHANNEL_REF_RE.match(text)
    return '@' + m.group(1) if m else None


async def _check_bot_rights(bot: Bot, chat) -> tuple[bool, str]:
    """Сможет ли бот спрашивать у Telegram про подписку на этот чат."""
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramAPIError as e:
        return False, f'бота нет в канале или он без прав: {e}'
    status = getattr(member, 'status', None)
    status = getattr(status, 'value', status)
    if status in ('administrator', 'creator', 'owner'):
        return True, ''
    if chat.type == 'channel':
        return False, ('в канале бот обязан быть администратором, иначе '
                       'Telegram не отвечает на запрос о подписке')
    if status == 'member':
        return True, ''
    return False, f'бот в чате со статусом {status}'


@router.message(AdminChannel.ident)
async def channel_add(message: Message, state: FSMContext, user):
    ref = _ref_from(message)
    if ref is None:
        await message.answer(
            f'{E.FAIL} Не понял, какой это канал. Пришли @ник, ссылку, '
            f'числовой id или перешли пост из канала.',
            reply_markup=kb.admin_back('admin:chan'))
        return

    try:
        chat = await message.bot.get_chat(ref)
    except TelegramAPIError as e:
        await message.answer(
            f'{E.FAIL} Telegram не отдал такой чат:\n'
            f'<code>{html.escape(str(e))}</code>\n\n'
            f'Обычно это значит, что бота в канале нет. Добавь его '
            f'администратором и пришли канал снова.',
            reply_markup=kb.admin_back('admin:chan'))
        return

    ok, note = await _check_bot_rights(message.bot, chat)
    invite = getattr(chat, 'invite_link', None) or (
        f'https://t.me/{chat.username}' if chat.username else None)

    created = await db.add_channel(chat.id, chat.username, chat.title, invite,
                                   user['user_id'])
    if not ok:
        await db.mark_channel(chat.id, True, note)

    await state.clear()
    log.info('админ %s %s канал %s (%s)', user['user_id'],
             'добавил' if created else 'обновил', chat.id, chat.title)
    await message.answer(
        f'{E.OK} Канал {"добавлен" if created else "обновлён"}: '
        f'<b>{html.escape(chat.title or str(chat.id))}</b>'
        + ('' if ok else f'\n\n⚠️ {html.escape(note)}'))
    await _channels_screen(message)


@router.callback_query(F.data.startswith('achan:del:'))
async def cb_channel_del(call: CallbackQuery, user):
    chat_id = int(call.data.split(':')[2])
    ok = await db.remove_channel(chat_id)
    await call.answer('Канал убран' if ok else 'Уже убран')
    log.info('админ %s убрал канал %s', user['user_id'], chat_id)
    await _channels_screen(call)


# --- админы -----------------------------------------------------------------

async def _roles_screen(event) -> None:
    rows = await db.list_admins()
    body = '\n'.join(f'{_nick(r)} — <code>{r["user_id"]}</code>' for r in rows) \
        or 'Кроме владельца — никого.'
    await render(event,
        f'🛠 <b>Админы</b>\n\n'
        f'Владелец: <code>{config.OWNER_ID}</code> (снять нельзя)\n\n{body}\n\n'
        f'Админ видит статистику и заявки, может менять балансы и банить. '
        f'Кнопка с ником — снять админку.',
        kb.admin_roles(rows))


@router.callback_query(F.data == 'admin:roles')
async def cb_roles(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _roles_screen(call)
    await call.answer()


@router.callback_query(F.data == 'arole:add')
async def cb_role_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminRole.ident)
    await render(call,
        '🛠 <b>Выдать админку</b>\n\n'
        'Пришли <code>ID</code> или <code>@ник</code>. Игрок должен хотя бы '
        'раз запустить бота, иначе его нет в базе.',
        kb.admin_back('admin:roles'))
    await call.answer()


@router.message(AdminRole.ident)
async def role_add(message: Message, state: FSMContext, user):
    target = await db.resolve_user(message.text or '')
    if target is None:
        await message.answer(f'{E.FAIL} Не нашёл такого игрока.',
                             reply_markup=kb.admin_back('admin:roles'))
        return

    await state.clear()
    row = await db.get_user(target)
    if await db.add_admin(target, row['username'] if row else None):
        await message.answer(f'{E.OK} {_nick(row)} теперь админ.')
        log.info('админ %s выдал админку %s', user['user_id'], target)
        try:
            await message.bot.send_message(
                target, '🛠 Тебе выдали админку. Панель — /admin.')
        except Exception as e:
            log.debug('новый админ %s недоступен: %s', target, e)
    else:
        await message.answer(f'{E.FAIL} Уже админ или это владелец.')
    await _roles_screen(message)


@router.callback_query(F.data.startswith('arole:del:'))
async def cb_role_del(call: CallbackQuery, user):
    target = int(call.data.split(':')[2])
    if await db.remove_admin(target):
        await call.answer('Снято')
        log.info('админ %s снял админку с %s', user['user_id'], target)
    else:
        await call.answer('Владельца снять нельзя.', show_alert=True)
    await _roles_screen(call)
