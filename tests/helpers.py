"""Хелперы тестов: база в памяти, счётчик отдачи, общие допуски."""

import asyncio
import contextlib
import os

import config
import db

# Сколько раундов крутить в Монте-Карло. План требует миллион; по умолчанию
# берём меньше, чтобы прогон укладывался в секунды, а полный объём включается
# переменной окружения MC_ROUNDS.
MC_ROUNDS = int(os.getenv('MC_ROUNDS', '200000'))

# Допуск на отдачу из плана: отклонение от RTP менее 0.5%.
TOL = 0.005


@contextlib.asynccontextmanager
async def fresh_db():
    """Пустая база в памяти на один тест.

    Соединение в db одно и живёт в модуле, поэтому его приходится подменять
    руками. Файл не нужен: соединение всё равно единственное, а блокировки
    SQLite между процессами тесты не проверяют.
    """
    old_path = config.DB_PATH
    config.DB_PATH = ':memory:'
    # Лок транзакций привязывается к тому event loop, в котором его первый раз
    # ждали. У каждого теста loop свой, поэтому лок создаётся заново.
    db._tx_lock = asyncio.Lock()
    await db.init()
    try:
        yield
    finally:
        await db.close()
        db._conn = None
        config.DB_PATH = old_path


async def mk_user(user_id: int, balance_cents: int = 0,
                  referer_id: int | None = None) -> int:
    """Заводит игрока с балансом. Отдаёт user_id — удобно подставлять по месту."""
    await db.ensure_user(user_id, f'user{user_id}', referer_id)
    if balance_cents:
        await db.add_balance(user_id, balance_cents)
    return user_id


class Meter:
    """Копилка выплат: считает фактическую отдачу и допуск для неё.

    Допуск не константа. У редких крупных выплат (краш на ×10, джекпот в
    слотах) разброс огромный, и на любом конечном прогоне отклонение в 0.5%
    для них — обычный шум, а не ошибка в математике. Поэтому порог берётся как
    max(0.5%, четыре стандартные ошибки): тест ловит систематический перекос и
    не падает от дисперсии.
    """

    def __init__(self) -> None:
        self.rounds = 0
        self._sum = 0.0
        self._sq = 0.0

    def add(self, payout: float) -> None:
        """payout — выплата в долях ставки: 0 — проигрыш, 1.94 — победа в дуэли.

        Несостоявшиеся раунды (ничья в дуэли, пуш в блэкджеке) не добавляются
        вообще: ставка вернулась, оборота не было.
        """
        self.rounds += 1
        self._sum += payout
        self._sq += payout * payout

    @property
    def rtp(self) -> float:
        return self._sum / self.rounds

    @property
    def tol(self) -> float:
        var = max(self._sq / self.rounds - self.rtp ** 2, 0.0)
        return max(TOL, 4 * (var / self.rounds) ** 0.5)

    def check(self, expected: float = config.RTP) -> None:
        assert abs(self.rtp - expected) < self.tol, (
            f'отдача {self.rtp:.5f}, ожидали {expected:.5f} '
            f'± {self.tol:.5f} на {self.rounds} раундах')
