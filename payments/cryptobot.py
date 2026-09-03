"""Crypto Pay API (@CryptoBot) — единственный платёжный канал бота.

База: https://pay.crypt.bot/api, авторизация заголовком Crypto-Pay-API-Token.
Ответ всегда в конверте {"ok": true, "result": ...} либо
{"ok": false, "error": {"code": 400, "name": "AMOUNT_TOO_SMALL"}}.

Что здесь важно по деньгам:

* Зачисление идёт только через db.credit_invoice — условный UPDATE по флагу
  credited. Прежняя версия (payment.py:40) счёт не гасила вообще, и кнопку
  «Проверить оплату» можно было жать сколько влезет.
* Сумма зачисления берётся из НАШЕЙ записи в invoices, а не из ответа API:
  что мы выставили, то и зачисляем.
* Перевод при выводе идёт с spend_id = 'wd<id>'. Это идемпотентность на
  стороне Crypto Pay: повторный вызов с тем же spend_id не отправит монеты
  второй раз, даже если наш процесс упал между заявкой и ответом.
"""

import asyncio
import logging
from typing import Any

import aiohttp

import config
import db
import emoji as E
import keyboards as kb
from db import fmt

log = logging.getLogger(__name__)

MAINNET = 'https://pay.crypt.bot/api'
TESTNET = 'https://testnet-pay.crypt.bot/api'


class CryptoPayError(Exception):
    def __init__(self, name: str, code: int | None = None):
        self.name = name
        self.code = code
        super().__init__(f'{code} {name}' if code else name)


def amount_str(cents: int) -> str:
    """Центы -> строка суммы. Без float: '5.07', а не '5.070000000000001'."""
    sign = '-' if cents < 0 else ''
    cents = abs(int(cents))
    return f'{sign}{cents // 100}.{cents % 100:02d}'


def _iid(value) -> Any:
    """invoice_id у Crypto Pay числовой, но в базе лежит текстом."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class CryptoPay:
    def __init__(self, token: str, testnet: bool = False):
        self.token = token
        self.base = TESTNET if testnet else MAINNET
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={'Crypto-Pay-API-Token': self.token},
                timeout=aiohttp.ClientTimeout(total=25))
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def call(self, method: str, **params) -> Any:
        if not self.enabled:
            raise CryptoPayError('NO_TOKEN')
        session = await self._sess()
        payload = {k: v for k, v in params.items() if v is not None}
        async with session.post(f'{self.base}/{method}', json=payload) as resp:
            data = await resp.json(content_type=None)
        if not isinstance(data, dict) or not data.get('ok'):
            err = (data or {}).get('error') or {}
            raise CryptoPayError(err.get('name', 'UNKNOWN'), err.get('code'))
        return data['result']

    # --- методы API ---------------------------------------------------------

    async def me(self) -> dict:
        return await self.call('getMe')

    async def balance(self) -> list:
        return await self.call('getBalance')

    async def create_invoice(self, amount_cents: int, payload: str = '',
                             description: str | None = None,
                             expires_in: int | None = None) -> dict:
        return await self.call(
            'createInvoice',
            asset=config.CRYPTO_ASSET,
            amount=amount_str(amount_cents),
            description=description,
            payload=payload or None,
            expires_in=expires_in,
            allow_comments=False,
            allow_anonymous=False)

    async def get_invoices(self, invoice_ids: list | None = None,
                           status: str | None = None, count: int = 100) -> list:
        params: dict[str, Any] = {'count': count}
        if invoice_ids:
            params['invoice_ids'] = ','.join(str(_iid(i)) for i in invoice_ids)
        if status:
            params['status'] = status
        res = await self.call('getInvoices', **params)
        if isinstance(res, dict):
            return res.get('items', [])
        return res or []

    async def delete_invoice(self, invoice_id) -> bool:
        return bool(await self.call('deleteInvoice', invoice_id=_iid(invoice_id)))

    async def transfer(self, user_id: int, amount_cents: int, spend_id: str,
                       comment: str | None = None) -> dict:
        """Перевод монет игроку. spend_id — ключ идемпотентности Crypto Pay."""
        return await self.call(
            'transfer',
            user_id=user_id,
            asset=config.CRYPTO_ASSET,
            amount=amount_str(amount_cents),
            spend_id=spend_id[:64],
            comment=comment)

    async def create_check(self, amount_cents: int,
                           pin_to_user_id: int | None = None) -> dict:
        """Чек-ссылка. Фолбэк, когда transfer не проходит: получатель ни разу
        не открывал @CryptoBot, а чек можно активировать по ссылке."""
        return await self.call(
            'createCheck',
            asset=config.CRYPTO_ASSET,
            amount=amount_str(amount_cents),
            pin_to_user_id=pin_to_user_id)


client = CryptoPay(config.CRYPTO_PAY_TOKEN, config.CRYPTO_PAY_TESTNET)


# --- пополнение -------------------------------------------------------------

async def open_invoice(user_id: int, amount_cents: int) -> dict:
    """Создаёт счёт в Crypto Pay и запоминает его. Отдаёт dict с pay_url."""
    inv = await client.create_invoice(
        amount_cents,
        payload=str(user_id),
        description=f'Пополнение баланса на {amount_str(amount_cents)} '
                    f'{config.CRYPTO_ASSET}',
        expires_in=config.INVOICE_TTL)

    pay_url = (inv.get('bot_invoice_url') or inv.get('pay_url')
               or inv.get('mini_app_invoice_url') or '')
    invoice_id = str(inv.get('invoice_id'))
    await db.add_invoice(invoice_id, user_id, amount_cents,
                         config.CRYPTO_ASSET, pay_url)
    return {'invoice_id': invoice_id, 'pay_url': pay_url,
            'amount_cents': amount_cents}


async def credited_text(res: dict) -> str:
    """Текст об успешном зачислении. Общий для polling'а и кнопки «Проверить»."""
    text = (f'{E.OK} <b>Пополнение зачислено</b>\n\n'
            f'Сумма: <b>{fmt(res["amount_cents"])}</b>\n')
    if res['bonus_cents']:
        text += f'Бонус: <b>+{fmt(res["bonus_cents"])}</b>\n'
    balance = await db.get_balance(res['user_id'])
    return text + f'\nБаланс: <b>{fmt(balance)}</b>'


async def credit(bot, invoice_id: str, *, notify: bool = True) -> dict | None:
    """Зачисляет оплаченный счёт и рассылает уведомления. None — уже зачтён.

    Реферальный процент платится здесь же — но только той стороной вызова,
    которая реально подняла флаг credited. Поэтому двойная проверка оплаты не
    удвоит ни баланс, ни выплату рефереру.

    notify=False — когда игрок сам нажал «Проверить оплату»: результат уедет
    в то же сообщение, второе уведомление ни к чему.
    """
    res = await db.credit_invoice(invoice_id)
    if res is None:
        return None

    user_id = res['user_id']
    if notify:
        try:
            await bot.send_message(user_id, await credited_text(res),
                                   reply_markup=kb.balance_menu())
        except Exception as e:
            log.warning('не смог уведомить %s о зачислении: %s', user_id, e)

    ref = await db.pay_referral(user_id, res['amount_cents'])
    if ref is not None:
        referer_id, reward = ref
        try:
            await bot.send_message(
                referer_id,
                f'{E.FRIENDS} Друг пополнил баланс — тебе <b>{fmt(reward)}</b>.',
                reply_markup=kb.back_to('refs', '👥 Друзья'))
        except Exception as e:
            log.debug('не смог уведомить реферера %s: %s', referer_id, e)

    log.info('зачислено %s центов юзеру %s по счёту %s',
             res['total_cents'], user_id, invoice_id)
    return res


async def check_invoices(bot) -> int:
    """Один проход по неоплаченным счётам. Отдаёт число зачисленных."""
    await db.expire_invoices(config.INVOICE_TTL)
    rows = await db.open_invoices()
    if not rows or not client.enabled:
        return 0

    by_id = {str(r['invoice_id']): r for r in rows}
    items = await client.get_invoices(invoice_ids=list(by_id))
    credited = 0
    for item in items:
        iid = str(item.get('invoice_id'))
        if iid not in by_id:
            continue
        status = item.get('status')
        if status == 'paid':
            if await credit(bot, iid) is not None:
                credited += 1
        elif status == 'expired':
            await db.set_invoice_status(iid, 'expired')
    return credited


async def poll_invoices(bot, interval: int | None = None) -> None:
    """Фоновая задача бота. Вебхуки требуют публичного HTTPS, опрос — нет."""
    interval = interval or config.INVOICE_POLL_INTERVAL
    log.info('опрос счетов Crypto Pay каждые %s с', interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await check_invoices(bot)
        except Exception:
            log.exception('опрос счетов сорвался, повтор через %s с', interval)


# --- вывод ------------------------------------------------------------------

async def pay_withdrawal(withdrawal_id: int, user_id: int,
                         amount_cents: int) -> tuple[str, str]:
    """Отправляет монеты по подтверждённой заявке.

    Отдаёт ('sent' | 'failed' | 'unknown', пояснение для админа).

    'unknown' — связь оборвалась, и мы НЕ знаем, ушли монеты или нет. Возврат
    на баланс в этом случае запрещён: если перевод всё-таки прошёл, игрок
    получит сумму дважды. Повторная попытка безопасна — spend_id тот же, и
    Crypto Pay не отправит монеты второй раз.

    Сначала пробуем transfer — деньги приходят игроку сразу. Если получатель
    ни разу не открывал @CryptoBot, transfer отказывает, и тогда выписываем
    чек: ссылку игрок активирует сам.
    """
    spend_id = f'wd{withdrawal_id}'
    try:
        await client.transfer(user_id, amount_cents, spend_id,
                              comment=f'Вывод #{withdrawal_id}')
        return 'sent', 'перевод отправлен'
    except CryptoPayError as e:
        log.warning('transfer по заявке %s не прошёл: %s', withdrawal_id, e)
        if e.name not in ('USER_UNAUTHORIZED', 'USER_NOT_FOUND'):
            return 'failed', f'Crypto Pay отказал: {e.name}'
    except Exception as e:
        log.exception('transfer по заявке %s оборвался', withdrawal_id)
        return 'unknown', f'связь с Crypto Pay оборвалась: {e}'

    try:
        check = await client.create_check(amount_cents, pin_to_user_id=user_id)
        url = check.get('bot_check_url') or ''
        return 'sent', f'у игрока нет кошелька, выписан чек: {url}'
    except CryptoPayError as e:
        return 'failed', f'ни перевод, ни чек не прошли: {e.name}'
    except Exception as e:
        return 'unknown', f'связь оборвалась на выписке чека: {e}'
