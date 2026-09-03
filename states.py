"""FSM-состояния.

Только те, где реально нужен ввод текста. Всё остальное ходит по callback'ам,
поэтому состояний тут в разы меньше, чем в прежней версии.
"""

from aiogram.fsm.state import State, StatesGroup


class BetInput(StatesGroup):
    """Ввод своей суммы ставки на экране игры."""
    amount = State()


class Deposit(StatesGroup):
    amount = State()


class Withdraw(StatesGroup):
    """Сумма, затем подтверждение.

    Адрес не спрашивается: Crypto Pay умеет переводить только Telegram-юзеру,
    и это сам заявитель. Поле для внешнего кошелька было бы обманом — монеты
    туда всё равно не уйдут.
    """
    amount = State()
    confirm = State()


class CodeInput(StatesGroup):
    """Ввод промокода или ваучера игроком."""
    code = State()


class FairInput(StatesGroup):
    """Своя client_seed для provably fair."""
    client_seed = State()


class AdminBroadcast(StatesGroup):
    content = State()
    confirm = State()


class AdminUser(StatesGroup):
    """Поиск юзера, затем сумма для ±баланса."""
    ident = State()
    amount = State()


class AdminPromo(StatesGroup):
    code = State()
    percent = State()
    usage_max = State()


class AdminVoucher(StatesGroup):
    code = State()
    amount = State()
    usage_max = State()


class AdminRole(StatesGroup):
    """Выдача и снятие админки."""
    ident = State()


class AdminChannel(StatesGroup):
    """Добавление канала обязательной подписки.

    Одно состояние на все способы задать канал: @ник, числовой id, ссылка или
    пересланный из канала пост — разбирает их один хендлер.
    """
    ident = State()
