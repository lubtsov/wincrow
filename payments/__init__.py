"""Платёжный слой.

Один канал — @CryptoBot (Crypto Pay API). Всё, что было в прежней версии
(QIWI, QIWI P2P, ЮMoney, CrystalPay, Coinbase), удалено вместе с ключами:
пять полумёртвых интеграций с четырьмя разными форматами суммы — это пять
мест, где деньги могут разъехаться с балансом.
"""

from payments.cryptobot import (CryptoPay, CryptoPayError, amount_str,
                                check_invoices, client, credit, credited_text,
                                open_invoice, pay_withdrawal, poll_invoices)

__all__ = [
    'CryptoPay', 'CryptoPayError', 'amount_str', 'check_invoices', 'client',
    'credit', 'credited_text', 'open_invoice', 'pay_withdrawal', 'poll_invoices',
]
