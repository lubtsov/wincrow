"""Баланс: пополнение, вывод, промокоды, ваучеры.

Пополнение и вывод идут только через @CryptoBot (Crypto Pay API) —
единственный платёжный канал. QIWI, ЮMoney, CrystalPay и Coinbase из прежней
версии удалены вместе с их ключами.

Оба места, где раньше текли деньги, закрыты в db:
* зачисление счёта — условный UPDATE по флагу credited (дыра №1 аудита:
  прежний payment.py счёт не гасил, и «Проверить оплату» начисляло бесконечно);
* заявка на вывод — списание и вставка одной транзакцией, возврат при отказе
  тоже атомарный.
"""

import html
import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
import db
import emoji as E
import keyboards as kb
import payments
from db import fmt
from states import CodeInput, Deposit, Withdraw
from ui import notify_admins, render

log = logging.getLogger(__name__)

router = Router(name='balance')

# Больше трёх заявок в очереди от одного игрока — это уже не вывод, а спам.
MAX_PENDING_WITHDRAWALS = 3

_STATUS_MARK = {
    'pending': f'{E.WAIT} в обработке',
    'paid': f'{E.OK} выплачено',
    'rejected': f'{E.FAIL} отклонено, возвращено на баланс',
    'failed': '⚠️ перевод не прошёл, возвращено на баланс',
}

# Заголовки кассы. Премиальный значок один на пополнение и вывод, а обычный
# символ под ним прежний — стрелка направления. В тексте кнопок его нет:
# у Telegram в кнопке нет сущностей, тег уехал бы туда строкой (emoji.py).
DEPOSIT_HEAD = f'{E.CASHIER_IN} <b>Пополнение</b>'
WITHDRAW_HEAD = f'{E.CASHIER_OUT} <b>Вывод</b>'


# --- экран баланса ----------------------------------------------------------

def balance_text(user) -> str:
    text = (f'{E.CARD} <b>Баланс</b>\n\n'
            f'Доступно: <b>{fmt(user["balance_cents"])}</b>\n')

    bonus = []
    if user['promo_percent']:
        bonus.append(f'промокод <b>+{user["promo_percent"]}%</b>')
    if user['voucher_cents']:
        bonus.append(f'ваучер <b>+{fmt(user["voucher_cents"])}</b>')
    if bonus:
        text += f'\nК следующему пополнению: {", ".join(bonus)}.\n'

    text += (f'\n{E.CASHIER} Пополнение с {fmt(config.MIN_DEPOSIT_CENTS)}, '
             f'вывод с {fmt(config.MIN_WITHDRAWAL_CENTS)}.\n\n'
             f'Поддержка: {html.escape(config.SUPPORT_NAME)}')
    return text


@router.callback_query(F.data == 'balance')
async def cb_balance(call: CallbackQuery, state: FSMContext, user):
    await state.clear()
    await render(call, balance_text(user), kb.balance_menu())
    await call.answer()


def _offline(kind: str) -> str:
    return (f'{kind}\n\n'
            f'{E.WAIT} Касса пока не работает. Напиши '
            f'{html.escape(config.SUPPORT_NAME)}.')


# --- пополнение -------------------------------------------------------------

@router.callback_query(F.data == 'dep')
async def cb_deposit(call: CallbackQuery, state: FSMContext, user):
    await state.clear()
    await deposit_screen(call, user)
    await call.answer()


async def deposit_screen(event, user) -> None:
    """Экран выбора суммы. Зовётся и кнопкой, и командой «деп» без суммы."""
    if not payments.client.enabled:
        await render(event, _offline(DEPOSIT_HEAD), kb.back_to('balance'))
        return

    text = f'{DEPOSIT_HEAD}\n\nВыбери сумму.'
    if user['promo_percent']:
        text += (f'\n\n{E.GIFT} Промокод даст '
                 f'<b>+{user["promo_percent"]}%</b> сверху.')
    if user['voucher_cents']:
        text += f'\n\n🎟 Ваучер добавит <b>{fmt(user["voucher_cents"])}</b>.'

    pending = await db.user_open_invoice(user['user_id'])
    if pending is not None:
        text += (f'\n\n{E.WAIT} Уже есть неоплаченный счёт на '
                 f'<b>{fmt(pending["amount_cents"])}</b>.')
        await render(event, text, kb.deposit_amounts(pending['invoice_id'],
                                                    pending['amount_cents']))
    else:
        await render(event, text, kb.deposit_amounts())


@router.callback_query(F.data.startswith('dep:'))
async def cb_deposit_pick(call: CallbackQuery, state: FSMContext, user):
    arg = call.data.split(':', 1)[1]

    if arg == 'ask':
        await state.set_state(Deposit.amount)
        await render(call,
            f'{E.EDIT} <b>Своя сумма</b>\n\n'
            f'Пришли сумму в долларах — от {fmt(config.MIN_DEPOSIT_CENTS)} '
            f'до {fmt(config.MAX_DEPOSIT_CENTS)}.\n'
            'Можно с центами: <code>12.50</code>.',
            kb.cancel_to('balance'))
        await call.answer()
        return

    if not arg.isdigit():
        await call.answer('Кнопка устарела.', show_alert=True)
        return

    await state.clear()
    await _issue_invoice(call, user['user_id'], int(arg))
    await call.answer()


@router.message(Deposit.amount)
async def deposit_amount(message: Message, state: FSMContext, user):
    cents = db.parse_cents(message.text or '')
    if cents is None:
        await message.answer(f'{E.FAIL} Не понял сумму. Пришли число, например '
                             f'<code>25</code> или <code>12.50</code>.',
                             reply_markup=kb.cancel_to('balance'))
        return
    if cents < config.MIN_DEPOSIT_CENTS:
        await message.answer(
            f'{E.FAIL} Минимальное пополнение — {fmt(config.MIN_DEPOSIT_CENTS)}.',
            reply_markup=kb.cancel_to('balance'))
        return
    if cents > config.MAX_DEPOSIT_CENTS:
        await message.answer(
            f'{E.FAIL} Максимум за раз — {fmt(config.MAX_DEPOSIT_CENTS)}. '
            f'Если нужно больше, пополни несколькими счетами.',
            reply_markup=kb.cancel_to('balance'))
        return

    await state.clear()
    await _issue_invoice(message, user['user_id'], cents)


async def _issue_invoice(event, user_id: int, amount_cents: int) -> None:
    """Выставляет счёт и показывает карточку оплаты."""
    try:
        inv = await payments.open_invoice(user_id, amount_cents)
    except payments.CryptoPayError as e:
        log.warning('createInvoice отказал: %s', e)
        await render(event,
            f'{E.FAIL} Crypto Pay отказал: <code>{html.escape(e.name)}</code>\n\n'
            'Попробуй другую сумму или напиши в поддержку.',
            kb.back_to('balance'))
        return
    except Exception as e:
        log.warning('createInvoice недоступен: %s', e)
        await render(event,
            f'{E.FAIL} Crypto Pay не отвечает. Попробуй через минуту — '
            'деньги при этом никуда не делись.',
            kb.back_to('balance'))
        return

    if not inv['pay_url']:
        await render(event,
            f'{E.FAIL} Crypto Pay не выдал ссылку на оплату. Напиши в поддержку.',
            kb.back_to('balance'))
        return

    bonus = await db.deposit_bonus_preview(user_id, amount_cents)
    text = (f'{E.GEM} <b>Счёт на {fmt(amount_cents)}</b>\n\n'
            f'К оплате: <b>{payments.amount_str(amount_cents)} '
            f'{config.CRYPTO_ASSET}</b>\n')
    if bonus:
        text += (f'Бонус к зачислению: <b>+{fmt(bonus)}</b>\n'
                 f'Итого на баланс: <b>{fmt(amount_cents + bonus)}</b>\n')
    text += f'\nСчёт живёт {config.INVOICE_TTL // 60} минут.'

    await render(event, text, kb.invoice_kb(inv['pay_url'], inv['invoice_id']))


async def request_deposit(event, user, cents: int) -> None:
    """Пополнение с уже известной суммой — путь команды «деп 5» из чата.

    Отдельная функция, а не переиспользованный FSM-хендлер: там на неверной
    сумме нужно остаться в состоянии ввода и переспросить, здесь — просто
    объяснить, почему сумма не подошла.
    """
    if not payments.client.enabled:
        await render(event, _offline(DEPOSIT_HEAD), kb.back_to('balance'))
        return
    if cents < config.MIN_DEPOSIT_CENTS:
        await render(event,
            f'{E.FAIL} Минимальное пополнение — {fmt(config.MIN_DEPOSIT_CENTS)}.',
            kb.back_to('dep', '⬆️ Пополнить'))
        return
    if cents > config.MAX_DEPOSIT_CENTS:
        await render(event,
            f'{E.FAIL} Максимум за раз — {fmt(config.MAX_DEPOSIT_CENTS)}. '
            f'Если нужно больше, выстави несколько счетов.',
            kb.back_to('dep', '⬆️ Пополнить'))
        return
    await _issue_invoice(event, user['user_id'], cents)


@router.callback_query(F.data.startswith('inv:'))
async def cb_invoice_check(call: CallbackQuery, user):
    invoice_id = call.data.split(':', 1)[1]
    row = await db.get_invoice(invoice_id)
    if row is None or row['user_id'] != user['user_id']:
        await call.answer('Счёт не найден.', show_alert=True)
        return

    if row['credited']:
        # Второе нажатие по уже зачтённому счёту. Ровно тот случай, который в
        # прежней версии начислял деньги повторно.
        fresh = await db.get_user(user['user_id'])
        await render(call,
            f'{E.OK} Этот счёт уже зачислен.\n\n{balance_text(fresh)}',
            kb.balance_menu())
        await call.answer('Уже зачислено')
        return

    if row['status'] != 'active':
        await render(call,
            f'{E.FAIL} Счёт закрыт ({row["status"]}). Выстави новый.',
            kb.back_to('dep', '⬆️ Пополнить'))
        await call.answer()
        return

    try:
        items = await payments.client.get_invoices(invoice_ids=[invoice_id])
    except Exception as e:
        log.warning('getInvoices недоступен: %s', e)
        await call.answer('Crypto Pay не ответил. Попробуй ещё раз.',
                          show_alert=True)
        return

    status = items[0].get('status') if items else None

    if status == 'paid':
        res = await payments.credit(call.bot, invoice_id, notify=False)
        if res is None:
            await call.answer('Оплата уже зачтена.', show_alert=True)
            return
        await render(call, await payments.credited_text(res), kb.balance_menu())
        await call.answer('Зачислено')
        return

    if status == 'expired':
        await db.set_invoice_status(invoice_id, 'expired')
        await render(call,
            f'{E.EXPIRED} Счёт просрочен — выстави новый, деньги по нему не '
            f'спишутся.',
            kb.back_to('dep', '⬆️ Пополнить'))
        await call.answer()
        return

    await call.answer('Оплата пока не пришла. Если ты только что заплатил — '
                      'подожди полминуты и нажми ещё раз.', show_alert=True)


@router.callback_query(F.data.startswith('invx:'))
async def cb_invoice_cancel(call: CallbackQuery, user):
    invoice_id = call.data.split(':', 1)[1]
    row = await db.get_invoice(invoice_id)
    if row is None or row['user_id'] != user['user_id']:
        await call.answer('Счёт не найден.', show_alert=True)
        return
    if row['credited']:
        await call.answer('Счёт уже оплачен — отменять нечего.', show_alert=True)
        return

    await db.set_invoice_status(invoice_id, 'cancelled')
    try:
        await payments.client.delete_invoice(invoice_id)
    except Exception as e:
        # Не критично: у себя счёт мы уже закрыли, зачислить его больше нельзя.
        log.debug('deleteInvoice не прошёл: %s', e)

    fresh = await db.get_user(user['user_id'])
    await render(call, f'{E.FAIL} Счёт отменён.\n\n{balance_text(fresh)}',
                 kb.balance_menu())
    await call.answer()


# --- вывод ------------------------------------------------------------------

@router.callback_query(F.data == 'wd:list')
async def cb_withdrawals(call: CallbackQuery, state: FSMContext, user):
    await state.clear()
    rows = await db.user_withdrawals(user['user_id'])
    if not rows:
        body = 'Заявок пока не было.'
    else:
        body = '\n'.join(
            f'#{r["id"]} · <b>{fmt(r["amount_cents"])}</b> · '
            f'{_STATUS_MARK.get(r["status"], r["status"])}'
            + (f'\n   <i>{html.escape(r["note"])}</i>' if r['note'] else '')
            for r in rows)
    await render(call, f'{E.CASHIER} <b>Мои выводы</b>\n\n{body}',
                 kb.withdrawals_list(rows))
    await call.answer()


@router.callback_query(F.data == 'wd')
async def cb_withdraw(call: CallbackQuery, state: FSMContext, user):
    await state.clear()
    if not payments.client.enabled:
        await render(call, _offline(WITHDRAW_HEAD), kb.back_to('balance'))
        await call.answer()
        return

    if user['balance_cents'] < config.MIN_WITHDRAWAL_CENTS:
        await call.answer(
            f'Минимальная сумма вывода — {fmt(config.MIN_WITHDRAWAL_CENTS)}.',
            show_alert=True)
        return

    pending = [r for r in await db.user_withdrawals(user['user_id'])
               if r['status'] == 'pending']
    if len(pending) >= MAX_PENDING_WITHDRAWALS:
        await call.answer(
            f'У тебя уже {len(pending)} заявки в обработке. Дождись их — '
            f'потом можно новую.', show_alert=True)
        return

    await state.set_state(Withdraw.amount)
    await render(call,
        f'{WITHDRAW_HEAD}\n\n'
        f'Доступно: <b>{fmt(user["balance_cents"])}</b>\n'
        f'Минимум: {fmt(config.MIN_WITHDRAWAL_CENTS)}\n\n'
        'Пришли сумму числом.',
        kb.cancel_to('balance'))
    await call.answer()


def _confirm_text(cents: int, balance: int) -> str:
    return (f'{E.CASHIER_OUT} <b>Проверь заявку</b>\n\n'
            f'Сумма: <b>{fmt(cents)}</b>\n'
            f'Останется на балансе: {fmt(balance - cents)}\n\n'
            f'Сумма списывается сразу — заявку подтверждает администратор.')


@router.message(Withdraw.amount)
async def withdraw_amount(message: Message, state: FSMContext, user):
    cents = db.parse_cents(message.text or '')
    if cents is None:
        await message.answer(f'{E.FAIL} Не понял сумму. Пришли число, например '
                             '<code>25</code>.',
                             reply_markup=kb.cancel_to('balance'))
        return
    if cents < config.MIN_WITHDRAWAL_CENTS:
        await message.answer(
            f'{E.FAIL} Минимум — {fmt(config.MIN_WITHDRAWAL_CENTS)}.',
            reply_markup=kb.cancel_to('balance'))
        return

    balance = await db.get_balance(user['user_id'])
    if cents > balance:
        await message.answer(
            f'{E.FAIL} На балансе только {fmt(balance)}.',
            reply_markup=kb.cancel_to('balance'))
        return

    await state.update_data(amount=cents)
    await state.set_state(Withdraw.confirm)
    await message.answer(_confirm_text(cents, balance),
                         reply_markup=kb.withdraw_confirm(cents))


async def request_withdraw(event, user, state: FSMContext, cents: int) -> None:
    """Вывод с уже известной суммой — путь команды «вывод 5» из чата.

    Подтверждение не пропускается: деньги списываются с баланса сразу, и
    случайная опечатка в чате не должна отправлять заявку молча.
    """
    if not payments.client.enabled:
        await render(event, _offline(WITHDRAW_HEAD), kb.back_to('balance'))
        return
    if cents < config.MIN_WITHDRAWAL_CENTS:
        await render(event,
            f'{E.FAIL} Минимальная сумма вывода — {fmt(config.MIN_WITHDRAWAL_CENTS)}.',
            kb.back_to('balance'))
        return

    balance = await db.get_balance(user['user_id'])
    if cents > balance:
        await render(event, f'{E.FAIL} На балансе только {fmt(balance)}.',
                     kb.back_to('balance'))
        return

    pending = [r for r in await db.user_withdrawals(user['user_id'])
               if r['status'] == 'pending']
    if len(pending) >= MAX_PENDING_WITHDRAWALS:
        await render(event,
            f'{E.FAIL} У тебя уже {len(pending)} заявки в обработке. Дождись их — '
            f'потом можно новую.', kb.back_to('balance'))
        return

    await state.update_data(amount=cents)
    await state.set_state(Withdraw.confirm)
    await render(event, _confirm_text(cents, balance),
                 kb.withdraw_confirm(cents))


@router.callback_query(F.data == 'wd:ok', StateFilter(Withdraw.confirm))
async def cb_withdraw_ok(call: CallbackQuery, state: FSMContext, user):
    data = await state.get_data()
    amount = int(data.get('amount') or 0)
    await state.clear()

    if amount < config.MIN_WITHDRAWAL_CENTS:
        await call.answer('Заявка потерялась, начни заново.', show_alert=True)
        return

    # Адрес — сам заявитель: Crypto Pay переводит только Telegram-юзеру.
    wd_id = await db.create_withdrawal(user['user_id'], amount,
                                       f'tg:{user["user_id"]}')
    if wd_id is None:
        fresh = await db.get_user(user['user_id'])
        await render(call,
            f'{E.FAIL} Не хватило баланса — видимо, успел поставить. '
            f'\n\n{balance_text(fresh)}',
            kb.balance_menu())
        await call.answer()
        return

    fresh = await db.get_user(user['user_id'])
    await render(call,
        f'{E.OK} <b>Заявка #{wd_id} создана</b>\n\n'
        f'Сумма: <b>{fmt(amount)}</b>\n'
        f'Баланс: {fmt(fresh["balance_cents"])}\n\n'
        f'Ждём подтверждения администратора.',
        kb.balance_menu())
    await call.answer('Заявка отправлена')

    nick = ('@' + html.escape(user['username'])) if user['username'] \
        else f'ID {user["user_id"]}'
    queue = len(await db.pending_withdrawals())
    await notify_admins(call.bot,
        f'{E.CASHIER} <b>Заявка на вывод #{wd_id}</b>\n\n'
        f'Игрок: {nick} (<code>{user["user_id"]}</code>)\n'
        f'Сумма: <b>{fmt(amount)}</b>\n'
        f'Остаток: {fmt(fresh["balance_cents"])}\n\n'
        f'Пополнял: {fmt(fresh["deposited_cents"])}\n'
        f'Оборот: {fmt(fresh["wagered_cents"])}\n'
        f'Выиграл: {fmt(fresh["won_cents"])}\n\n'
        f'Заявок в очереди: {queue}',
        kb.withdrawal_card(wd_id))
    log.info('заявка на вывод #%s: %s центов, юзер %s',
             wd_id, amount, user['user_id'])


# --- промокоды и ваучеры ----------------------------------------------------

@router.callback_query(F.data == 'code')
async def cb_code(call: CallbackQuery, state: FSMContext):
    await state.set_state(CodeInput.code)
    await render(call,
        f'{E.GIFT} <b>Промокод или ваучер</b>\n\n'
        'Пришли код одним сообщением.\n\n'
        'Промокод добавляет процент к следующему пополнению, ваучер — '
        'фиксированную сумму. Один код активируется один раз на аккаунт.',
        kb.cancel_to('balance'))
    await call.answer()


@router.message(CodeInput.code)
async def code_input(message: Message, state: FSMContext, user):
    status = await apply_code(message, user, message.text or '',
                             fail_kb=kb.cancel_to('balance'))
    if status in ('promo', 'voucher'):
        await state.clear()


async def apply_code(event, user, raw: str,
                     fail_kb: InlineKeyboardMarkup | None = None) -> str:
    """Гасит промокод или ваучер и печатает результат.

    Возвращает статус, а не чистит состояние сама: FSM-ввод должен остаться в
    состоянии после неверного кода, команде из чата состояние ни к чему.
    """
    status, value = await db.redeem_code(user['user_id'], raw)

    if status == 'promo':
        text = f'{E.OK} Промокод принят: <b>+{value}%</b> к следующему пополнению.'
    elif status == 'voucher':
        text = f'{E.OK} Ваучер принят: <b>+{fmt(value)}</b> к следующему пополнению.'
    elif status == 'used':
        text = f'{E.FAIL} Этот код ты уже активировал.'
    elif status == 'exhausted':
        text = f'{E.FAIL} Код закончился — лимит активаций исчерпан.'
    else:
        text = f'{E.FAIL} Такого кода нет. Проверь раскладку и пробелы.'

    if status in ('promo', 'voucher'):
        fresh = await db.get_user(user['user_id'])
        await render(event, text + '\n\n' + balance_text(fresh), kb.balance_menu())
    else:
        await render(event, text, fail_kb or kb.back_to('balance'))
    return status
