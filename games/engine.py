"""Движок раундов: provably fair случайность, атомарная ставка, выплата.

Игры не считают деньги сами. Игра получает Round, берёт из него случайность,
возвращает множитель — и всё. Любая арифметика с балансом живёт здесь и в db.

Provably fair
-------------
У каждого игрока есть server_seed (секрет), его sha256 (публикуется заранее),
client_seed (можно задать свой) и счётчик nonce. Результат раунда:

    HMAC_SHA256(server_seed, "client_seed:nonce:cursor")

Из 32 байт дайджеста нарезается 8 чисел в [0, 1). Нужно больше — растёт
cursor. Подкрутить исход задним числом нельзя: sha256(server_seed) опубликован
до раунда, а после ротации сид раскрывается и любой прошлый раунд
пересчитывается вручную.
"""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from typing import Iterator

import aiosqlite

import db


# --- случайность ------------------------------------------------------------

def seed_hash(server_seed: str) -> str:
    return hashlib.sha256(server_seed.encode()).hexdigest()


def new_seed() -> str:
    return secrets.token_hex(32)


def float_stream(server_seed: str, client_seed: str, nonce: int) -> Iterator[float]:
    """Бесконечный поток чисел в [0, 1), однозначно заданный тройкой сидов."""
    cursor = 0
    while True:
        digest = hmac.new(server_seed.encode(),
                          f'{client_seed}:{nonce}:{cursor}'.encode(),
                          hashlib.sha256).digest()
        for i in range(0, 32, 4):
            yield int.from_bytes(digest[i:i + 4], 'big') / 4_294_967_296
        cursor += 1


# --- раунд ------------------------------------------------------------------

@dataclass
class Round:
    id: int
    user_id: int
    game: str
    bet_cents: int
    server_seed: str
    server_seed_hash: str
    client_seed: str
    nonce: int
    # Чат, из которого запущен раунд. Личка — None. Нужен только на финише: по
    # нему владельцу группы капает процент с проигрыша.
    chat_id: int | None = None
    state: dict = field(default_factory=dict)
    _rng: Iterator[float] | None = None

    def rnd(self) -> float:
        """Следующее число из потока. Порядок вызовов — часть проверки."""
        if self._rng is None:
            self._rng = float_stream(self.server_seed, self.client_seed, self.nonce)
        return next(self._rng)

    def pick(self, n: int) -> int:
        """Целое из [0, n)."""
        return min(int(self.rnd() * n), n - 1)

    def shuffle(self, items: list) -> list:
        """Тасовка Фишера-Йетса на том же потоке — воспроизводимая."""
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.pick(i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    def sample(self, n: int, k: int) -> list[int]:
        """k различных чисел из [0, n) — раскладка мин и подобное."""
        return self.shuffle(list(range(n)))[:k]


async def _ensure_seed(user_id: int) -> aiosqlite.Row:
    row = await (await db.conn().execute(
        'SELECT * FROM seeds WHERE user_id = ?', (user_id,))).fetchone()
    if row is not None:
        return row
    server = new_seed()
    await db.conn().execute(
        'INSERT OR IGNORE INTO seeds (user_id, server_seed, server_seed_hash, '
        'client_seed, nonce) VALUES (?, ?, ?, ?, 0)',
        (user_id, server, seed_hash(server), secrets.token_hex(8)))
    return await (await db.conn().execute(
        'SELECT * FROM seeds WHERE user_id = ?', (user_id,))).fetchone()


async def start_round(user_id: int, game: str, bet_cents: int,
                      state: dict | None = None,
                      chat_id: int | None = None,
                      client_id: str | None = None) -> Round | None:
    """Списывает ставку и открывает раунд. None — денег не хватило.

    Ставка снимается ЗДЕСЬ, до броска. Поэтому множитель значит то же, что
    и везде: ×2 — вернул ставку и столько же сверху. В прежней версии выигрыш
    начислялся поверх неснятой ставки, и слоты давали игроку +6.25% EV.

    Списание, инкремент nonce и запись раунда — одна транзакция. Клик из
    старого сообщения сюда не попадёт: в кнопках игры едет round_id, а он
    выдаётся только вместе с уже снятой ставкой.

    chat_id — группа, в которой играют (ui.chat_id_of). Запоминается вместе с
    раундом, потому что кнопки многошаговых игр приходят уже без него, а на
    финише надо знать, владельцу какого чата капает процент.

    client_id — уникальный id действия на стороне клиента. Нужен там, где
    запрос может прийти дважды сам по себе: HTTP из Mini App повторяется на
    любом обрыве связи. По колонке стоит уникальный индекс, поэтому второй
    такой запрос падает на INSERT — внутри этой же транзакции, то есть вместе
    со снятием ставки, и деньги возвращаются сами. Кнопкам бота он не нужен:
    там от повтора защищает round_id.
    """
    seed = await _ensure_seed(user_id)

    async with db.transaction() as c:
        cur = await c.execute(
            'UPDATE users SET balance_cents = balance_cents - ?, '
            '                 wagered_cents = wagered_cents + ? '
            'WHERE user_id = ? AND balance_cents >= ? AND banned = 0',
            (bet_cents, bet_cents, user_id, bet_cents))
        if cur.rowcount != 1:
            return None

        await c.execute('UPDATE seeds SET nonce = nonce + 1 WHERE user_id = ?',
                        (user_id,))
        nonce = (await (await c.execute(
            'SELECT nonce FROM seeds WHERE user_id = ?', (user_id,))).fetchone())['nonce']

        cur = await c.execute(
            'INSERT INTO rounds (user_id, game, bet_cents, state, status, '
            'chat_id, client_id, server_seed_hash, client_seed, nonce, created_at) '
            'VALUES (?, ?, ?, ?, "active", ?, ?, ?, ?, ?, ?)',
            (user_id, game, bet_cents, json.dumps(state or {}), chat_id,
             client_id, seed['server_seed_hash'], seed['client_seed'], nonce,
             db.now()))
        round_id = cur.lastrowid

    return Round(id=round_id, user_id=user_id, game=game, bet_cents=bet_cents,
                 server_seed=seed['server_seed'],
                 server_seed_hash=seed['server_seed_hash'],
                 client_seed=seed['client_seed'], nonce=nonce, chat_id=chat_id,
                 state=dict(state or {}))


async def load_round(round_id: int, user_id: int, game: str) -> Round | None:
    """Активный раунд по id. None — не найден, чужой, не тот или уже закрыт.

    Это и есть защита от эксплойта со старой клавиатурой: кнопка из прошлого
    сообщения несёт свой round_id, он давно не 'active', и клик отбивается.
    """
    row = await (await db.conn().execute(
        'SELECT * FROM rounds WHERE id = ? AND user_id = ? AND game = ? '
        'AND status = "active"', (round_id, user_id, game))).fetchone()
    if row is None:
        return None
    seed = await (await db.conn().execute(
        'SELECT server_seed FROM seeds WHERE user_id = ?', (user_id,))).fetchone()
    return Round(id=row['id'], user_id=row['user_id'], game=row['game'],
                 bet_cents=row['bet_cents'], server_seed=seed['server_seed'],
                 server_seed_hash=row['server_seed_hash'],
                 client_seed=row['client_seed'], nonce=row['nonce'],
                 chat_id=row['chat_id'],
                 state=json.loads(row['state'] or '{}'))


async def save_state(rnd: Round) -> None:
    """Промежуточное состояние многошаговой игры (мины, башня, блэкджек)."""
    await db.conn().execute(
        'UPDATE rounds SET state = ? WHERE id = ? AND status = "active"',
        (json.dumps(rnd.state), rnd.id))


async def raise_stake(rnd: Round, extra_cents: int) -> bool:
    """Доливает ставку в уже открытый раунд (удвоение в блэкджеке).

    Ставка раунда растёт вместе со списанием, поэтому множители остаются
    прежними: выигрыш всё так же ×2, возврат при ничьей — вся ставка.

    Статус читается внутри той же BEGIN IMMEDIATE, что и списание, поэтому
    закрыть раунд между проверкой и доливом никто не успеет.
    """
    if extra_cents <= 0:
        return False
    async with db.transaction() as c:
        row = await (await c.execute(
            'SELECT status FROM rounds WHERE id = ?', (rnd.id,))).fetchone()
        if row is None or row['status'] != 'active':
            return False
        cur = await c.execute(
            'UPDATE users SET balance_cents = balance_cents - ?, '
            '                 wagered_cents = wagered_cents + ? '
            'WHERE user_id = ? AND balance_cents >= ?',
            (extra_cents, extra_cents, rnd.user_id, extra_cents))
        if cur.rowcount != 1:
            return False
        await c.execute('UPDATE rounds SET bet_cents = bet_cents + ? WHERE id = ?',
                        (extra_cents, rnd.id))
    rnd.bet_cents += extra_cents
    return True


async def active_round(user_id: int, game: str) -> Round | None:
    row = await (await db.conn().execute(
        'SELECT id FROM rounds WHERE user_id = ? AND game = ? AND status = "active" '
        'ORDER BY id DESC LIMIT 1', (user_id, game))).fetchone()
    if row is None:
        return None
    return await load_round(row['id'], user_id, game)


def payout_cents(bet_cents: int, multiplier: float) -> int:
    """Выплата в центах. Округление ВНИЗ — и это не жадность, а необходимость.

    Цент — самая мелкая единица учёта, а ставка от $0.10 состоит всего из десяти
    таких единиц. round() ошибается на полцента, то есть на 5% минимальной
    ставки, и ошибается в обе стороны. Пример: краш, выход на ×1.35, ставка
    $0.10. Точная выплата 13.5 цента, round() даёт 14, вероятность дойти до
    ×1.35 равна 0.97/1.35 — фактическая отдача 100.6%. То есть минимальной
    ставкой можно было бы стабильно доить кассу.

    Вниз — значит фактическая отдача никогда не выше заявленной, при любой
    ставке. Игрок теряет на округлении меньше цента с выплаты; на ставке от $1
    это меньше 0.5% и не видно, на $0.10 — заметнее, зато без дыры в кассе.

    Запас перед отсечением — против ошибки float, а не подарок игроку:
    100 * 0.29 == 28.999999999999996, и честные 29 центов превратились бы в 28
    без всякого умысла. Запас относительный, потому что шаг double растёт
    вместе с числом: у 29 он 3.6e-15, а у выплаты в 2.5 миллиона центов
    (максимум системы — ставка $500 на ×50 в краше) уже 4.7e-10, и
    фиксированного 1e-9 хватало бы едва-едва. 1e-12 от суммы — это меньше
    миллионной доли цента, до соседнего целого не дотянет никогда.
    """
    if bet_cents <= 0 or multiplier <= 0:
        return 0
    product = bet_cents * multiplier
    return int(product + max(1e-9, product * 1e-12))


async def finish(rnd: Round, multiplier: float) -> int | None:
    """Закрывает раунд и начисляет выплату. Отдаёт центы, None — уже закрыт.

    `WHERE status = "active"` плюс проверка rowcount делают выплату
    идемпотентной: два одновременных клика «Забрать» дадут одно начисление.
    Процент владельцу чата считается в той же транзакции и по той же причине.
    """
    payout = payout_cents(rnd.bet_cents, multiplier)
    async with db.transaction() as c:
        cur = await c.execute(
            'UPDATE rounds SET multiplier = ?, payout_cents = ?, status = ?, '
            'state = ?, finished_at = ? WHERE id = ? AND status = "active"',
            (multiplier, payout, 'won' if payout > 0 else 'lost',
             json.dumps(rnd.state), db.now(), rnd.id))
        if cur.rowcount != 1:
            return None
        if payout > 0:
            await c.execute(
                'UPDATE users SET balance_cents = balance_cents + ?, '
                '                 won_cents = won_cents + ? WHERE user_id = ?',
                (payout, payout, rnd.user_id))
        # Владельцу группы капает с того, что игрок реально потерял, а не со
        # ставки: выход из краша на ×1.5 — не проигрыш, пуш в блэкджеке тоже.
        if rnd.chat_id:
            await db.pay_chat_owner(c, rnd.chat_id, rnd.user_id,
                                    rnd.bet_cents - payout)
    return payout


async def void(rnd: Round) -> bool:
    """Раунд не состоялся: ставка возвращается, оборот откатывается.

    Нужно для ничьей в дайс-дуэлях и для отменённых PvP-комнат. Из оборота
    ставка вычитается обратно, иначе фактический RTP в статистике поедет.
    """
    async with db.transaction() as c:
        cur = await c.execute(
            'UPDATE rounds SET status = "void", multiplier = 1.0, '
            'payout_cents = ?, state = ?, finished_at = ? '
            'WHERE id = ? AND status = "active"',
            (rnd.bet_cents, json.dumps(rnd.state), db.now(), rnd.id))
        if cur.rowcount != 1:
            return False
        await c.execute(
            'UPDATE users SET balance_cents = balance_cents + ?, '
            '                 wagered_cents = wagered_cents - ? WHERE user_id = ?',
            (rnd.bet_cents, rnd.bet_cents, rnd.user_id))
    return True


# --- сиды -------------------------------------------------------------------

async def rotate_seed(user_id: int) -> str:
    """Меняет серверный сид и раскрывает прежний. Отдаёт раскрытый сид."""
    await _ensure_seed(user_id)
    async with db.transaction() as c:
        row = await (await c.execute(
            'SELECT server_seed FROM seeds WHERE user_id = ?', (user_id,))).fetchone()
        old = row['server_seed']
        server = new_seed()
        await c.execute(
            'UPDATE seeds SET server_seed = ?, server_seed_hash = ?, nonce = 0, '
            'prev_server_seed = ?, rotated_at = ? WHERE user_id = ?',
            (server, seed_hash(server), old, db.now(), user_id))
    return old


async def set_client_seed(user_id: int, client_seed: str) -> None:
    await _ensure_seed(user_id)
    await db.conn().execute(
        'UPDATE seeds SET client_seed = ? WHERE user_id = ?',
        (client_seed[:64], user_id))
