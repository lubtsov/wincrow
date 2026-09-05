"""Денежный слой и доступ к базе.

Главное правило файла: **баланс меняется только функциями из этого модуля**.
В коде игр не должно быть ни одного прямого `UPDATE ... balance_cents`.

Почему так. В прежней версии каждая игра делала
`SELECT balance` -> await -> `UPDATE balance = ?`, и между чтением и записью
успевал вклиниться другой клик. Здесь списание ставки — один условный UPDATE,
который либо проходит целиком, либо не проходит вовсе.
"""

import asyncio
import contextlib
import re
import secrets
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import aiosqlite

import config

_conn: aiosqlite.Connection | None = None

# Защищает многошаговые транзакции: две корутины не должны одновременно
# открыть BEGIN на одном соединении.
_tx_lock = asyncio.Lock()

# Сумма от игрока: до 12 целых знаков, дробная часть необязательна.
# Ни экспонент, ни знака, ни пробелов внутри.
#
# re.ASCII обязателен: без него \d матчит любую Unicode-цифру, а Decimal их
# принимает — '٣' (арабо-индийская тройка) проходила как 300 центов.
_AMOUNT_RE = re.compile(r'^(\d{1,12}(\.\d*)?|\.\d+)$', re.ASCII)


# --- деньги как текст -------------------------------------------------------

def fmt(cents: int) -> str:
    """1234 -> '$12.34'. Единственный способ показать сумму пользователю."""
    cents = int(cents)
    sign = '-' if cents < 0 else ''
    cents = abs(cents)
    return f'{sign}${cents // 100}.{cents % 100:02d}'


def parse_cents(text: str) -> int | None:
    """'12.5' / '12,5' -> 1250. None, если это не положительная сумма.

    Через Decimal, а не float: float('0.07') * 100 == 7.000000000000001.

    Форма суммы проверяется регуляркой, а не одним Decimal(), потому что
    Decimal принимает лишнее. '1e999999999' — валидное значение, и попытка
    превратить его в int съела бы память процесса. 'nan' тоже валиден, а
    сравнение NaN с нулём бросает InvalidOperation уже за пределами try. То
    есть одного сообщения в чат хватило бы, чтобы уронить хендлер.
    """
    if not isinstance(text, str):
        return None
    raw = text.strip().replace(',', '.')
    if _AMOUNT_RE.match(raw) is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    cents = int((value * 100).to_integral_value(rounding=ROUND_DOWN))
    return cents or None


def now() -> int:
    return int(time.time())


# --- соединение и схема -----------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id               INTEGER PRIMARY KEY,
    username              TEXT,
    balance_cents         INTEGER NOT NULL DEFAULT 0,
    -- DEFAULT дублирует config.MIN_BET_CENTS: из SQL до конфига не дотянуться.
    -- Живому игроку ставка всё равно проставляется явно при создании.
    bet_cents             INTEGER NOT NULL DEFAULT 10,
    banned                INTEGER NOT NULL DEFAULT 0,
    referer_id            INTEGER,
    referrals             INTEGER NOT NULL DEFAULT 0,
    referral_earned_cents INTEGER NOT NULL DEFAULT 0,
    -- Заработок с чатов, куда игрок привёл бота, — считается отдельно от
    -- реферального, чтобы в профиле было видно, что именно приносит деньги.
    chat_earned_cents     INTEGER NOT NULL DEFAULT 0,
    wagered_cents         INTEGER NOT NULL DEFAULT 0,
    won_cents             INTEGER NOT NULL DEFAULT 0,
    deposited_cents       INTEGER NOT NULL DEFAULT 0,
    promo_percent         INTEGER,
    voucher_cents         INTEGER,
    created_at            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    game             TEXT    NOT NULL,
    bet_cents        INTEGER NOT NULL,
    multiplier       REAL,
    payout_cents     INTEGER,
    state            TEXT,
    status           TEXT    NOT NULL,
    -- Чат, из которого запущен раунд. NULL — личка. Нужен, чтобы при проигрыше
    -- знать, владельцу какой группы капает процент (pay_chat_owner).
    chat_id          INTEGER,
    -- Уникальный id действия на стороне клиента. Нужен там, где запрос может
    -- повториться сам: HTTP из Mini App уходит заново на любом обрыве связи.
    -- Под ним частичный уникальный индекс (см. _migrate).
    client_id        TEXT,
    server_seed_hash TEXT    NOT NULL,
    client_seed      TEXT    NOT NULL,
    nonce            INTEGER NOT NULL,
    created_at       INTEGER NOT NULL,
    finished_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rounds_user ON rounds(user_id, id DESC);

CREATE TABLE IF NOT EXISTS seeds (
    user_id          INTEGER PRIMARY KEY,
    server_seed      TEXT    NOT NULL,
    server_seed_hash TEXT    NOT NULL,
    client_seed      TEXT    NOT NULL,
    nonce            INTEGER NOT NULL DEFAULT 0,
    prev_server_seed TEXT,
    rotated_at       INTEGER
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    bonus_cents  INTEGER NOT NULL DEFAULT 0,
    asset        TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    credited     INTEGER NOT NULL DEFAULT 0,
    pay_url      TEXT,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_open ON invoices(status, credited);

CREATE TABLE IF NOT EXISTS withdrawals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    address      TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    processed_at INTEGER,
    admin_id     INTEGER,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status, id);

CREATE TABLE IF NOT EXISTS promocodes (
    code         TEXT PRIMARY KEY,
    percent      INTEGER NOT NULL,
    usage_max    INTEGER NOT NULL,
    usage_actual INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS vouchers (
    code         TEXT PRIMARY KEY,
    amount_cents INTEGER NOT NULL,
    usage_max    INTEGER NOT NULL,
    usage_actual INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);

-- Один код — один раз на юзера. В прежней версии этого не было вообще.
CREATE TABLE IF NOT EXISTS code_uses (
    kind    TEXT    NOT NULL,
    code    TEXT    NOT NULL,
    user_id INTEGER NOT NULL,
    used_at INTEGER NOT NULL,
    PRIMARY KEY (kind, code, user_id)
);

CREATE TABLE IF NOT EXISTS admins (
    user_id  INTEGER PRIMARY KEY,
    username TEXT,
    added_at INTEGER NOT NULL
);

-- PvP: комната собирает банк из взносов, казино берёт только рейк.
CREATE TABLE IF NOT EXISTS pvp_rooms (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    game             TEXT    NOT NULL,   -- 'duel' | 'jackpot'
    creator_id       INTEGER NOT NULL,
    stake_cents      INTEGER NOT NULL,   -- взнос создателя; в дуэли он же обязателен для второго
    pot_cents        INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL,   -- 'open' | 'playing' | 'done' | 'cancelled'
    winner_id        INTEGER,
    payout_cents     INTEGER,
    rake_cents       INTEGER,
    result           TEXT,               -- JSON: броски, доли, раскрытый сид
    server_seed      TEXT    NOT NULL,
    server_seed_hash TEXT    NOT NULL,
    created_at       INTEGER NOT NULL,
    finished_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pvp_open ON pvp_rooms(game, status, id);

CREATE TABLE IF NOT EXISTS pvp_players (
    room_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    stake_cents INTEGER NOT NULL,
    chat_id     INTEGER,
    message_id  INTEGER,
    joined_at   INTEGER NOT NULL,
    PRIMARY KEY (room_id, user_id)
);

-- Чаты, куда привели бота. Продолжение рефералки: владельцу группы капает
-- процент с каждого проигрыша в его чате.
CREATE TABLE IF NOT EXISTS chats (
    chat_id      INTEGER PRIMARY KEY,
    title        TEXT,
    owner_id     INTEGER NOT NULL,        -- кому капает процент
    added_by     INTEGER NOT NULL,        -- кто привёл бота; для истории
    earned_cents INTEGER NOT NULL DEFAULT 0,
    losses       INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,   -- бота выгнали — 0
    added_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chats_owner ON chats(owner_id, chat_id);

-- Ежедневный кейс. Одна строка — одна выдача: где лежит приз, сервер решает
-- ещё до первого клика и держит это здесь. Клиенту win_index не уезжает
-- никогда — ни в боте, ни в Mini App, иначе выигрыш можно было бы подсмотреть
-- в трафике и всегда открывать нужную карточку.
CREATE TABLE IF NOT EXISTS daily_cases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    win_index    INTEGER NOT NULL,      -- 0..cards-1, решено сервером при выдаче
    prize_cents  INTEGER NOT NULL,      -- что лежит в выигрышной карточке
    cards        INTEGER NOT NULL,      -- сколько карточек было в этом кейсе
    streak       INTEGER NOT NULL DEFAULT 1,  -- какой это день серии подряд
    picked_index INTEGER,               -- NULL — кейс ещё не открыт
    payout_cents INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL,      -- 'open' | 'done'
    created_at   INTEGER NOT NULL,
    opened_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_daily_user ON daily_cases(user_id, id DESC);

-- Обязательные подписки для кейса. Каналы задаёт админ через панель, в коде
-- их нет ни одного: список живёт только здесь.
CREATE TABLE IF NOT EXISTS required_channels (
    chat_id    INTEGER PRIMARY KEY,
    username   TEXT,                    -- без @, если у канала он есть
    title      TEXT,
    invite_url TEXT,                    -- ссылка для кнопки игроку
    broken     INTEGER NOT NULL DEFAULT 0,   -- бот не может проверить подписку
    note       TEXT,                    -- почему не может — админу в список
    added_by   INTEGER NOT NULL,
    added_at   INTEGER NOT NULL
);

-- Рыбалка: раунд один на всех, поэтому у него своя таблица, а не строка на
-- игрока. Номер раунда (no) считается из времени в games/fishing.py, он же
-- первичный ключ — два одновременных запроса создадут раунд один раз.
--
-- Сид пишется при создании раунда и больше не меняется: из него выводятся и
-- сектор остановки, и множители рыб. Поэтому результат восстановим, даже если
-- процесс убили посреди раунда, а клиенту до расчёта уезжает только его sha256.
CREATE TABLE IF NOT EXISTS fishing_rounds (
    no               INTEGER PRIMARY KEY,   -- номер раунда = int(time // длина)
    server_seed      TEXT    NOT NULL,
    server_seed_hash TEXT    NOT NULL,
    status           TEXT    NOT NULL,      -- 'live' | 'done'
    result           TEXT,                  -- JSON: сектор, угол, множители
    started_at       INTEGER NOT NULL,
    settled_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fishing_live ON fishing_rounds(status, no);

-- Ставка в раунде рыбалки. Деньги живут не здесь: на каждую ставку заводится
-- обычный раунд в rounds (engine.start_round снимает, engine.finish платит), а
-- эта строка только связывает его с номером раунда и выбранной позицией.
CREATE TABLE IF NOT EXISTS fishing_bets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    no           INTEGER NOT NULL,      -- раунд рыбалки
    user_id      INTEGER NOT NULL,
    pick         TEXT    NOT NULL,      -- 'blue' | 'orange' | 'red' | 'link'
    bet_cents    INTEGER NOT NULL,
    round_id     INTEGER NOT NULL,      -- rounds.id: там ставка и выплата
    multiplier   REAL,                  -- NULL — раунд ещё не посчитан
    payout_cents INTEGER,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fishing_bets_round ON fishing_bets(no, id);
CREATE INDEX IF NOT EXISTS idx_fishing_bets_user ON fishing_bets(user_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fishing_bets_src ON fishing_bets(round_id);
"""

# Колонки, появившиеся после первого релиза. CREATE TABLE IF NOT EXISTS уже
# существующую таблицу не трогает, поэтому в живой базе их приходится доливать
# руками — иначе бот на старой casino.db упадёт на первом же запросе.
_ADDED_COLUMNS = (
    ('users', 'chat_earned_cents', 'INTEGER NOT NULL DEFAULT 0'),
    ('rounds', 'chat_id', 'INTEGER'),
    ('rounds', 'client_id', 'TEXT'),
    ('daily_cases', 'streak', 'INTEGER NOT NULL DEFAULT 1'),
)

# Индексы, которые нельзя создать вместе со схемой: они стоят на колонках из
# _ADDED_COLUMNS, а в живой базе те появляются только что, строкой выше.
_ADDED_INDEXES = (
    # Частичный уникальный индекс: спин из Mini App с тем же client_id второй
    # раз не запишется. NULL в SQLite уникальности не нарушает, поэтому раунды
    # из бота (у них client_id пуст) индекс не задевает вовсе.
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_rounds_client '
    'ON rounds(client_id) WHERE client_id IS NOT NULL',
)


async def _migrate(c: aiosqlite.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        cols = await (await c.execute(f'PRAGMA table_info({table})')).fetchall()
        if any(row['name'] == column for row in cols):
            continue
        await c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')
    for statement in _ADDED_INDEXES:
        await c.execute(statement)


async def init() -> None:
    global _conn
    _conn = await aiosqlite.connect(config.DB_PATH, isolation_level=None)
    _conn.row_factory = aiosqlite.Row
    # WAL: читатели не блокируют писателя. Без него при паре десятков
    # одновременных игроков SQLite начинает отдавать "database is locked".
    await _conn.execute('PRAGMA journal_mode=WAL')
    await _conn.execute('PRAGMA synchronous=NORMAL')
    await _conn.execute('PRAGMA foreign_keys=ON')
    await _conn.executescript(SCHEMA)
    await _migrate(_conn)


async def close() -> None:
    if _conn is not None:
        await _conn.close()


def conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError('db.init() не вызван')
    return _conn


@contextlib.asynccontextmanager
async def transaction():
    """BEGIN IMMEDIATE ... COMMIT для операций, которые нельзя выразить одним UPDATE.

    Лок нужен потому, что соединение одно: без него две корутины могут
    открыть BEGIN одновременно и получить вложенную транзакцию.
    """
    async with _tx_lock:
        c = conn()
        await c.execute('BEGIN IMMEDIATE')
        try:
            yield c
        except Exception:
            await c.execute('ROLLBACK')
            raise
        else:
            await c.execute('COMMIT')


# --- пользователи -----------------------------------------------------------

async def ensure_user(user_id: int, username: str | None,
                      referer_id: int | None = None) -> bool:
    """Создаёт юзера, если его нет. True — юзер новый.

    Реферер записывается только при создании, чтобы его нельзя было
    переприсвоить повторным /start со ссылкой.
    """
    async with transaction() as c:
        row = await (await c.execute(
            'SELECT user_id FROM users WHERE user_id = ?', (user_id,))).fetchone()
        if row is not None:
            await c.execute(
                'UPDATE users SET username = ? WHERE user_id = ?', (username, user_id))
            return False

        if referer_id == user_id:
            referer_id = None
        if referer_id is not None:
            ref = await (await c.execute(
                'SELECT user_id FROM users WHERE user_id = ?', (referer_id,))).fetchone()
            if ref is None:
                referer_id = None

        await c.execute(
            'INSERT INTO users (user_id, username, bet_cents, referer_id, created_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (user_id, username, config.MIN_BET_CENTS, referer_id, now()))

        if referer_id is not None:
            await c.execute(
                'UPDATE users SET referrals = referrals + 1 WHERE user_id = ?',
                (referer_id,))
        return True


async def get_user(user_id: int) -> aiosqlite.Row | None:
    return await (await conn().execute(
        'SELECT * FROM users WHERE user_id = ?', (user_id,))).fetchone()


async def is_banned(user_id: int) -> bool:
    row = await (await conn().execute(
        'SELECT banned FROM users WHERE user_id = ?', (user_id,))).fetchone()
    return bool(row and row['banned'])


async def set_banned(user_id: int, banned: bool) -> bool:
    cur = await conn().execute(
        'UPDATE users SET banned = ? WHERE user_id = ? AND banned != ?',
        (int(banned), user_id, int(banned)))
    return cur.rowcount == 1


async def resolve_user(ident: str) -> int | None:
    """Принимает '123456' или '@nick' / 'nick'. Отдаёт user_id."""
    ident = ident.strip()
    if ident.lstrip('-').isdigit():
        row = await (await conn().execute(
            'SELECT user_id FROM users WHERE user_id = ?', (int(ident),))).fetchone()
        return row['user_id'] if row else None
    row = await (await conn().execute(
        'SELECT user_id FROM users WHERE username = ? COLLATE NOCASE',
        (ident.lstrip('@'),))).fetchone()
    return row['user_id'] if row else None


# --- баланс -----------------------------------------------------------------

async def place_bet(user_id: int, bet_cents: int) -> bool:
    """Атомарно списывает ставку. False — денег не хватило.

    Здесь закрываются сразу три дыры прежней версии: проверка `balance > 0`
    вместо `balance >= bet`, гонка при спаме кнопки и уход баланса в минус.
    Условие `balance_cents >= ?` — часть того же UPDATE, обойти его нечем.
    """
    if bet_cents <= 0:
        return False
    cur = await conn().execute(
        'UPDATE users SET balance_cents = balance_cents - ?, '
        '                 wagered_cents = wagered_cents + ? '
        'WHERE user_id = ? AND balance_cents >= ? AND banned = 0',
        (bet_cents, bet_cents, user_id, bet_cents))
    return cur.rowcount == 1


async def add_balance(user_id: int, cents: int, *, as_win: bool = False) -> None:
    """Начисление. Отрицательное значение допускается только для админской
    коррекции и может увести баланс в минус — на игровом пути не используется."""
    if cents == 0:
        return
    if as_win:
        await conn().execute(
            'UPDATE users SET balance_cents = balance_cents + ?, '
            '                 won_cents = won_cents + ? WHERE user_id = ?',
            (cents, cents, user_id))
    else:
        await conn().execute(
            'UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?',
            (cents, user_id))


async def take_balance(user_id: int, cents: int) -> bool:
    """Списание не-ставки (например, заявка на вывод). Атомарно, без минуса."""
    if cents <= 0:
        return False
    cur = await conn().execute(
        'UPDATE users SET balance_cents = balance_cents - ? '
        'WHERE user_id = ? AND balance_cents >= ?',
        (cents, user_id, cents))
    return cur.rowcount == 1


async def get_balance(user_id: int) -> int:
    row = await (await conn().execute(
        'SELECT balance_cents FROM users WHERE user_id = ?', (user_id,))).fetchone()
    return row['balance_cents'] if row else 0


# --- ставка -----------------------------------------------------------------

async def set_bet(user_id: int, bet_cents: int) -> int:
    """Кладёт ставку в допустимый диапазон и сохраняет. Отдаёт итоговое значение."""
    bet_cents = max(config.MIN_BET_CENTS, min(config.MAX_BET_CENTS, bet_cents))
    await conn().execute(
        'UPDATE users SET bet_cents = ? WHERE user_id = ?', (bet_cents, user_id))
    return bet_cents


async def get_bet(user_id: int) -> int:
    row = await (await conn().execute(
        'SELECT bet_cents FROM users WHERE user_id = ?', (user_id,))).fetchone()
    return row['bet_cents'] if row else config.MIN_BET_CENTS


# --- рефералка --------------------------------------------------------------

def referral_level(referrals: int) -> tuple[int, int]:
    """(уровень, процент) по числу приглашённых."""
    level, percent = config.REFERRAL_LEVELS[0][1], config.REFERRAL_LEVELS[0][2]
    for threshold, lvl, pct in config.REFERRAL_LEVELS:
        if referrals >= threshold:
            level, percent = lvl, pct
    return level, percent


async def pay_referral(user_id: int, deposit_cents: int) -> tuple[int, int] | None:
    """Начисляет рефереру процент от пополнения. Отдаёт (referer_id, сумма).

    Прежняя версия читала баланс донора и записывала его рефереру
    (payment.py:91), затирая чужой счёт. Здесь начисление — инкремент,
    и читается именно строка реферера.
    """
    row = await (await conn().execute(
        'SELECT referer_id FROM users WHERE user_id = ?', (user_id,))).fetchone()
    if not row or not row['referer_id']:
        return None

    referer_id = row['referer_id']
    ref = await (await conn().execute(
        'SELECT referrals FROM users WHERE user_id = ?', (referer_id,))).fetchone()
    if ref is None:
        return None

    _, percent = referral_level(ref['referrals'])
    reward = deposit_cents * percent // 100
    if reward <= 0:
        return None

    await conn().execute(
        'UPDATE users SET balance_cents = balance_cents + ?, '
        '                 referral_earned_cents = referral_earned_cents + ? '
        'WHERE user_id = ?',
        (reward, reward, referer_id))
    return referer_id, reward


# --- чаты -------------------------------------------------------------------
#
# Вторая половина рефералки: приглашается не игрок, а сразу чат. Кто привёл
# бота в группу, тот получает процент с каждой проигранной там ставки.


def chat_reward(lost_cents: int) -> int:
    """Сколько капнет владельцу с проигрыша в его чате. Округление вниз.

    Отдельной функцией — её же зовёт экран «Мои чаты», чтобы показать порог, с
    которого начисление вообще перестаёт быть нулевым.
    """
    if lost_cents <= 0 or config.CHAT_OWNER_PERCENT <= 0:
        return 0
    return lost_cents * config.CHAT_OWNER_PERCENT // 100


async def link_chat(chat_id: int, title: str | None, owner_id: int,
                    added_by: int) -> bool:
    """Привязывает чат к владельцу. True — привязка новая.

    Владелец фиксируется при первой привязке и больше не меняется. Иначе бота
    достаточно было бы выгнать и позвать заново, чтобы перевести чужой чат на
    себя. Повторное добавление обновляет название и снова включает начисления.
    """
    async with transaction() as c:
        row = await (await c.execute(
            'SELECT owner_id FROM chats WHERE chat_id = ?', (chat_id,))).fetchone()
        if row is not None:
            await c.execute(
                'UPDATE chats SET title = ?, active = 1 WHERE chat_id = ?',
                (title, chat_id))
            return False
        await c.execute(
            'INSERT INTO chats (chat_id, title, owner_id, added_by, added_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (chat_id, title, owner_id, added_by, now()))
        return True


async def set_chat_active(chat_id: int, active: bool) -> bool:
    """Бота выгнали — начисления останавливаются, заработанное остаётся."""
    cur = await conn().execute(
        'UPDATE chats SET active = ? WHERE chat_id = ? AND active != ?',
        (int(active), chat_id, int(active)))
    return cur.rowcount == 1


async def get_chat(chat_id: int) -> aiosqlite.Row | None:
    return await (await conn().execute(
        'SELECT * FROM chats WHERE chat_id = ?', (chat_id,))).fetchone()


async def owner_chats(owner_id: int, limit: int = 10) -> list[aiosqlite.Row]:
    """Чаты игрока: сначала активные, внутри — по заработку."""
    return await (await conn().execute(
        'SELECT * FROM chats WHERE owner_id = ? '
        'ORDER BY active DESC, earned_cents DESC, chat_id LIMIT ?',
        (owner_id, limit))).fetchall()


async def owner_chats_count(owner_id: int) -> tuple[int, int]:
    """(всего чатов, из них активных) — для сводки в рефералке."""
    row = await (await conn().execute(
        'SELECT COUNT(*) n, COALESCE(SUM(active), 0) live FROM chats '
        'WHERE owner_id = ?', (owner_id,))).fetchone()
    return row['n'], row['live']


async def pay_chat_owner(c: aiosqlite.Connection, chat_id: int, user_id: int,
                         lost_cents: int) -> tuple[int, int] | None:
    """Процент владельцу чата с проигрыша. (owner_id, сумма) или None.

    Соединение передаётся снаружи: начисление обязано попасть в тот же коммит,
    что и закрытие раунда. Иначе двойной клик «Открыть» мог бы заплатить
    владельцу дважды за один проигрыш — раунд-то закроется один раз, но вторая
    попытка успела бы пройти здесь. Открывать свою транзакцию нельзя вдвойне:
    лок в `transaction()` не реентерабельный, и вложенный вызов встал бы навсегда.

    Платит казино из своей маржи. Ставка ушла с баланса игрока ещё в
    start_round, и здесь у него не удерживается ничего.
    """
    reward = chat_reward(lost_cents)
    if reward <= 0:
        return None

    row = await (await c.execute(
        'SELECT owner_id FROM chats WHERE chat_id = ? AND active = 1',
        (chat_id,))).fetchone()
    # Своим же проигрышам владелец не радуется: это был бы кэшбек, который
    # поднимает его личную отдачу выше заявленной.
    if row is None or row['owner_id'] == user_id:
        return None

    cur = await c.execute(
        'UPDATE users SET balance_cents = balance_cents + ?, '
        '                 chat_earned_cents = chat_earned_cents + ? '
        'WHERE user_id = ? AND banned = 0', (reward, reward, row['owner_id']))
    if cur.rowcount != 1:
        return None                 # владелец забанен или не заводил аккаунт

    await c.execute(
        'UPDATE chats SET earned_cents = earned_cents + ?, losses = losses + 1 '
        'WHERE chat_id = ?', (reward, chat_id))
    return row['owner_id'], reward


# --- промокоды и ваучеры ----------------------------------------------------

def norm_code(code: str) -> str:
    return code.strip().upper()


async def redeem_code(user_id: int, code: str) -> tuple[str, int]:
    """Активирует промокод или ваучер. Отдаёт (статус, значение).

    Статусы: 'promo' (значение — процент), 'voucher' (значение — центы),
    'not_found', 'used', 'exhausted'.

    Всё внутри одной транзакции: счётчик использований и отметка «этот юзер
    уже активировал» ставятся вместе, иначе код с limit=1 расходится на
    несколько игроков при одновременном вводе.
    """
    code = norm_code(code)
    async with transaction() as c:
        for kind, table, column in (('promo', 'promocodes', 'percent'),
                                    ('voucher', 'vouchers', 'amount_cents')):
            row = await (await c.execute(
                f'SELECT {column} v, usage_max, usage_actual FROM {table} '
                f'WHERE code = ?', (code,))).fetchone()
            if row is None:
                continue

            used = await (await c.execute(
                'SELECT 1 FROM code_uses WHERE kind = ? AND code = ? AND user_id = ?',
                (kind, code, user_id))).fetchone()
            if used is not None:
                return 'used', 0
            if row['usage_actual'] >= row['usage_max']:
                return 'exhausted', 0

            await c.execute(
                'INSERT INTO code_uses (kind, code, user_id, used_at) VALUES (?, ?, ?, ?)',
                (kind, code, user_id, now()))
            await c.execute(
                f'UPDATE {table} SET usage_actual = usage_actual + 1 WHERE code = ?',
                (code,))

            if kind == 'promo':
                await c.execute(
                    'UPDATE users SET promo_percent = ? WHERE user_id = ?',
                    (row['v'], user_id))
            else:
                # Ваучеры складываются: два по $5 дадут $10 к пополнению.
                await c.execute(
                    'UPDATE users SET voucher_cents = COALESCE(voucher_cents, 0) + ? '
                    'WHERE user_id = ?', (row['v'], user_id))
            return kind, row['v']

        return 'not_found', 0


async def code_exists(code: str) -> str | None:
    """'promo' | 'voucher' | None. Имена не должны пересекаться между таблицами:
    redeem_code смотрит промокоды первыми, и одноимённый ваучер стал бы мёртвым."""
    code = norm_code(code)
    for kind, table in (('promo', 'promocodes'), ('voucher', 'vouchers')):
        row = await (await conn().execute(
            f'SELECT 1 FROM {table} WHERE code = ?', (code,))).fetchone()
        if row is not None:
            return kind
    return None


async def add_promo(code: str, percent: int, usage_max: int) -> bool:
    """False — код занят."""
    code = norm_code(code)
    if await code_exists(code):
        return False
    try:
        await conn().execute(
            'INSERT INTO promocodes (code, percent, usage_max, created_at) '
            'VALUES (?, ?, ?, ?)', (code, percent, usage_max, now()))
    except aiosqlite.IntegrityError:
        return False
    return True


async def add_voucher(code: str, amount_cents: int, usage_max: int) -> bool:
    code = norm_code(code)
    if await code_exists(code):
        return False
    try:
        await conn().execute(
            'INSERT INTO vouchers (code, amount_cents, usage_max, created_at) '
            'VALUES (?, ?, ?, ?)', (code, amount_cents, usage_max, now()))
    except aiosqlite.IntegrityError:
        return False
    return True


async def list_promos(limit: int = 20) -> list[aiosqlite.Row]:
    return await (await conn().execute(
        'SELECT * FROM promocodes ORDER BY created_at DESC LIMIT ?',
        (limit,))).fetchall()


async def list_vouchers(limit: int = 20) -> list[aiosqlite.Row]:
    return await (await conn().execute(
        'SELECT * FROM vouchers ORDER BY created_at DESC LIMIT ?',
        (limit,))).fetchall()


async def delete_code(kind: str, code: str) -> bool:
    """Удаляет код. Уже выданные игрокам бонусы не отбирает — они в users."""
    table = 'promocodes' if kind == 'promo' else 'vouchers'
    cur = await conn().execute(f'DELETE FROM {table} WHERE code = ?',
                               (norm_code(code),))
    return cur.rowcount == 1


async def deposit_bonus_preview(user_id: int, deposit_cents: int) -> int:
    """Сколько бонуса даст пополнение. Ничего не гасит — только показать.

    Настоящее начисление живёт в credit_invoice: бонус считается и гасится в
    одной транзакции с зачислением. Если гасить его раньше, одним промокодом
    можно оплатить десяток счетов.
    """
    row = await (await conn().execute(
        'SELECT promo_percent, voucher_cents FROM users WHERE user_id = ?',
        (user_id,))).fetchone()
    if row is None:
        return 0
    bonus = 0
    if row['promo_percent']:
        bonus += deposit_cents * row['promo_percent'] // 100
    if row['voucher_cents']:
        bonus += row['voucher_cents']
    return bonus


# --- пополнения -------------------------------------------------------------

async def add_invoice(invoice_id: str, user_id: int, amount_cents: int,
                      asset: str, pay_url: str) -> None:
    await conn().execute(
        'INSERT OR REPLACE INTO invoices (invoice_id, user_id, amount_cents, '
        'asset, status, credited, pay_url, created_at) '
        'VALUES (?, ?, ?, ?, "active", 0, ?, ?)',
        (str(invoice_id), user_id, amount_cents, asset, pay_url, now()))


async def get_invoice(invoice_id: str) -> aiosqlite.Row | None:
    return await (await conn().execute(
        'SELECT * FROM invoices WHERE invoice_id = ?', (str(invoice_id),))).fetchone()


async def open_invoices(limit: int = 100) -> list[aiosqlite.Row]:
    return await (await conn().execute(
        'SELECT * FROM invoices WHERE status = "active" AND credited = 0 '
        'ORDER BY created_at LIMIT ?', (limit,))).fetchall()


async def user_open_invoice(user_id: int) -> aiosqlite.Row | None:
    return await (await conn().execute(
        'SELECT * FROM invoices WHERE user_id = ? AND status = "active" '
        'AND credited = 0 ORDER BY created_at DESC LIMIT 1', (user_id,))).fetchone()


async def set_invoice_status(invoice_id: str, status: str) -> bool:
    """Закрывает неоплаченный счёт. Оплаченный не трогает — там уже деньги."""
    cur = await conn().execute(
        'UPDATE invoices SET status = ? WHERE invoice_id = ? AND credited = 0 '
        'AND status = "active"', (status, str(invoice_id)))
    return cur.rowcount == 1


async def expire_invoices(ttl: int) -> list[str]:
    rows = await (await conn().execute(
        'SELECT invoice_id FROM invoices WHERE status = "active" AND credited = 0 '
        'AND created_at < ?', (now() - ttl,))).fetchall()
    dead = []
    for r in rows:
        if await set_invoice_status(r['invoice_id'], 'expired'):
            dead.append(r['invoice_id'])
    return dead


async def credit_invoice(invoice_id: str) -> dict | None:
    """Зачисляет оплаченный счёт ровно один раз. None — уже зачислен.

    Это та самая дыра №1 из аудита: прежний payment.py не гасил счёт вообще,
    и «Проверить оплату» можно было жать до посинения. Здесь флаг credited
    поднимается условным UPDATE, и дальше проходит только тот вызов, который
    его поднял.
    """
    invoice_id = str(invoice_id)
    async with transaction() as c:
        cur = await c.execute(
            'UPDATE invoices SET status = "paid", credited = 1 '
            'WHERE invoice_id = ? AND credited = 0', (invoice_id,))
        if cur.rowcount != 1:
            return None

        inv = await (await c.execute(
            'SELECT user_id, amount_cents FROM invoices WHERE invoice_id = ?',
            (invoice_id,))).fetchone()
        user_id, amount = inv['user_id'], inv['amount_cents']

        # Бонус считается и гасится здесь же, в одной транзакции с зачислением.
        row = await (await c.execute(
            'SELECT promo_percent, voucher_cents FROM users WHERE user_id = ?',
            (user_id,))).fetchone()
        bonus = 0
        if row is not None:
            if row['promo_percent']:
                bonus += amount * row['promo_percent'] // 100
            if row['voucher_cents']:
                bonus += row['voucher_cents']
            if bonus:
                await c.execute(
                    'UPDATE users SET promo_percent = NULL, voucher_cents = NULL '
                    'WHERE user_id = ?', (user_id,))

        total = amount + bonus
        await c.execute(
            'UPDATE users SET balance_cents = balance_cents + ?, '
            '                 deposited_cents = deposited_cents + ? '
            'WHERE user_id = ?', (total, amount, user_id))
        await c.execute('UPDATE invoices SET bonus_cents = ? WHERE invoice_id = ?',
                        (bonus, invoice_id))
        return {'user_id': user_id, 'amount_cents': amount,
                'bonus_cents': bonus, 'total_cents': total}


# --- выводы -----------------------------------------------------------------

async def create_withdrawal(user_id: int, amount_cents: int,
                            address: str) -> int | None:
    """Заявка на вывод. Баланс списывается сразу. None — не хватило денег."""
    if amount_cents <= 0:
        return None
    async with transaction() as c:
        cur = await c.execute(
            'UPDATE users SET balance_cents = balance_cents - ? '
            'WHERE user_id = ? AND balance_cents >= ? AND banned = 0',
            (amount_cents, user_id, amount_cents))
        if cur.rowcount != 1:
            return None
        cur = await c.execute(
            'INSERT INTO withdrawals (user_id, amount_cents, address, status, '
            'created_at) VALUES (?, ?, ?, "pending", ?)',
            (user_id, amount_cents, address, now()))
        return cur.lastrowid


async def get_withdrawal(withdrawal_id: int) -> aiosqlite.Row | None:
    return await (await conn().execute(
        'SELECT w.*, u.username FROM withdrawals w '
        'LEFT JOIN users u ON u.user_id = w.user_id WHERE w.id = ?',
        (withdrawal_id,))).fetchone()


async def pending_withdrawals(limit: int = 20) -> list[aiosqlite.Row]:
    return await (await conn().execute(
        'SELECT w.*, u.username FROM withdrawals w '
        'LEFT JOIN users u ON u.user_id = w.user_id '
        'WHERE w.status = "pending" ORDER BY w.id LIMIT ?', (limit,))).fetchall()


async def user_withdrawals(user_id: int, limit: int = 10) -> list[aiosqlite.Row]:
    return await (await conn().execute(
        'SELECT * FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT ?',
        (user_id, limit))).fetchall()


async def claim_withdrawal(withdrawal_id: int, admin_id: int) -> aiosqlite.Row | None:
    """Помечает заявку выплаченной. None — её уже кто-то обработал.

    Вызывается ДО перевода: только тот, кому вернулась заявка, дёргает
    transfer. Так двойное нажатие ✅ не отправит монеты дважды.
    """
    async with transaction() as c:
        cur = await c.execute(
            'UPDATE withdrawals SET status = "paid", processed_at = ?, admin_id = ? '
            'WHERE id = ? AND status = "pending"', (now(), admin_id, withdrawal_id))
        if cur.rowcount != 1:
            return None
        return await (await c.execute(
            'SELECT * FROM withdrawals WHERE id = ?', (withdrawal_id,))).fetchone()


async def set_withdrawal_note(withdrawal_id: int, note: str) -> bool:
    """Пометка к заявке без смены статуса.

    Нужна для случая «связь с Crypto Pay оборвалась»: заявка остаётся 'paid',
    деньги на баланс не возвращаются (перевод мог пройти), а админ видит в
    заметке, что результат неизвестен и стоит повторить.
    """
    cur = await conn().execute(
        'UPDATE withdrawals SET note = ? WHERE id = ?',
        (note[:400], withdrawal_id))
    return cur.rowcount == 1


async def fail_withdrawal(withdrawal_id: int, note: str) -> bool:
    """Перевод не прошёл: заявка в 'failed', деньги обратно на баланс."""
    async with transaction() as c:
        row = await (await c.execute(
            'SELECT user_id, amount_cents FROM withdrawals WHERE id = ?',
            (withdrawal_id,))).fetchone()
        if row is None:
            return False
        cur = await c.execute(
            'UPDATE withdrawals SET status = "failed", note = ? '
            'WHERE id = ? AND status = "paid"', (note[:400], withdrawal_id))
        if cur.rowcount != 1:
            return False
        await c.execute(
            'UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?',
            (row['amount_cents'], row['user_id']))
        return True


async def reject_withdrawal(withdrawal_id: int, admin_id: int,
                            note: str = '') -> aiosqlite.Row | None:
    """Отклоняет заявку и возвращает сумму на баланс. None — уже обработана."""
    async with transaction() as c:
        row = await (await c.execute(
            'SELECT user_id, amount_cents FROM withdrawals WHERE id = ?',
            (withdrawal_id,))).fetchone()
        if row is None:
            return None
        cur = await c.execute(
            'UPDATE withdrawals SET status = "rejected", processed_at = ?, '
            'admin_id = ?, note = ? WHERE id = ? AND status = "pending"',
            (now(), admin_id, note[:400], withdrawal_id))
        if cur.rowcount != 1:
            return None
        await c.execute(
            'UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?',
            (row['amount_cents'], row['user_id']))
        return await (await c.execute(
            'SELECT * FROM withdrawals WHERE id = ?', (withdrawal_id,))).fetchone()


# --- PvP-комнаты ------------------------------------------------------------

# Открытая комната держит чужие деньги, поэтому она не может висеть вечно.
# Сборщик вызывается лениво — при входе в лобби, отдельная задача не нужна.
PVP_TTL = 30 * 60


async def _charge(c: aiosqlite.Connection, user_id: int, cents: int) -> bool:
    """Списание взноса внутри уже открытой транзакции. Тот же гейт, что в place_bet."""
    cur = await c.execute(
        'UPDATE users SET balance_cents = balance_cents - ?, '
        '                 wagered_cents = wagered_cents + ? '
        'WHERE user_id = ? AND balance_cents >= ? AND banned = 0',
        (cents, cents, user_id, cents))
    return cur.rowcount == 1


async def _refund_room(c: aiosqlite.Connection, room_id: int) -> None:
    """Возврат взносов всем участникам с откатом оборота."""
    rows = await (await c.execute(
        'SELECT user_id, stake_cents FROM pvp_players WHERE room_id = ?',
        (room_id,))).fetchall()
    for r in rows:
        await c.execute(
            'UPDATE users SET balance_cents = balance_cents + ?, '
            '                 wagered_cents = wagered_cents - ? WHERE user_id = ?',
            (r['stake_cents'], r['stake_cents'], r['user_id']))


async def pvp_create(game: str, user_id: int, stake_cents: int,
                     server_seed: str, seed_hash: str) -> int | None:
    """Открывает комнату и сразу вносит взнос создателя. None — не хватило денег."""
    if stake_cents <= 0:
        return None
    async with transaction() as c:
        if not await _charge(c, user_id, stake_cents):
            return None
        cur = await c.execute(
            'INSERT INTO pvp_rooms (game, creator_id, stake_cents, pot_cents, '
            'status, server_seed, server_seed_hash, created_at) '
            'VALUES (?, ?, ?, ?, "open", ?, ?, ?)',
            (game, user_id, stake_cents, stake_cents, server_seed, seed_hash, now()))
        room_id = cur.lastrowid
        await c.execute(
            'INSERT INTO pvp_players (room_id, user_id, stake_cents, joined_at) '
            'VALUES (?, ?, ?, ?)', (room_id, user_id, stake_cents, now()))
        return room_id


async def pvp_set_card(room_id: int, user_id: int,
                       chat_id: int, message_id: int) -> None:
    """Запоминает сообщение-карточку игрока, чтобы дописать в него результат."""
    await conn().execute(
        'UPDATE pvp_players SET chat_id = ?, message_id = ? '
        'WHERE room_id = ? AND user_id = ?', (chat_id, message_id, room_id, user_id))


async def pvp_join(room_id: int, user_id: int, stake_cents: int, *,
                   max_players: int, fixed_stake: bool, topup: bool,
                   chat_id: int | None = None,
                   message_id: int | None = None) -> str:
    """Вход в комнату. Статусы: ok / closed / full / already / stake / no_money.

    Проверка мест и списание — одна транзакция, поэтому третий игрок не влезет
    в дуэль, даже если нажмёт кнопку одновременно со вторым.
    """
    async with transaction() as c:
        room = await (await c.execute(
            'SELECT * FROM pvp_rooms WHERE id = ?', (room_id,))).fetchone()
        if room is None or room['status'] != 'open':
            return 'closed'
        if fixed_stake and stake_cents != room['stake_cents']:
            return 'stake'

        mine = await (await c.execute(
            'SELECT stake_cents FROM pvp_players WHERE room_id = ? AND user_id = ?',
            (room_id, user_id))).fetchone()
        if mine is not None and not topup:
            return 'already'

        if mine is None:
            seats = await (await c.execute(
                'SELECT COUNT(*) n FROM pvp_players WHERE room_id = ?',
                (room_id,))).fetchone()
            if seats['n'] >= max_players:
                return 'full'

        if not await _charge(c, user_id, stake_cents):
            return 'no_money'

        if mine is None:
            await c.execute(
                'INSERT INTO pvp_players (room_id, user_id, stake_cents, chat_id, '
                'message_id, joined_at) VALUES (?, ?, ?, ?, ?, ?)',
                (room_id, user_id, stake_cents, chat_id, message_id, now()))
        else:
            await c.execute(
                'UPDATE pvp_players SET stake_cents = stake_cents + ?, '
                'chat_id = COALESCE(?, chat_id), message_id = COALESCE(?, message_id) '
                'WHERE room_id = ? AND user_id = ?',
                (stake_cents, chat_id, message_id, room_id, user_id))
        await c.execute('UPDATE pvp_rooms SET pot_cents = pot_cents + ? WHERE id = ?',
                        (stake_cents, room_id))
        return 'ok'


async def pvp_leave(room_id: int, user_id: int) -> bool:
    """Выход из открытой комнаты с возвратом своего взноса.

    Создателю выходить нельзя — для него это отмена комнаты целиком.
    """
    async with transaction() as c:
        room = await (await c.execute(
            'SELECT status, creator_id FROM pvp_rooms WHERE id = ?',
            (room_id,))).fetchone()
        if room is None or room['status'] != 'open' or room['creator_id'] == user_id:
            return False
        row = await (await c.execute(
            'SELECT stake_cents FROM pvp_players WHERE room_id = ? AND user_id = ?',
            (room_id, user_id))).fetchone()
        if row is None:
            return False
        await c.execute('DELETE FROM pvp_players WHERE room_id = ? AND user_id = ?',
                        (room_id, user_id))
        await c.execute('UPDATE pvp_rooms SET pot_cents = pot_cents - ? WHERE id = ?',
                        (row['stake_cents'], room_id))
        await c.execute(
            'UPDATE users SET balance_cents = balance_cents + ?, '
            '                 wagered_cents = wagered_cents - ? WHERE user_id = ?',
            (row['stake_cents'], row['stake_cents'], user_id))
        return True


async def pvp_lock(room_id: int, min_players: int) -> aiosqlite.Row | None:
    """Переводит комнату в 'playing'. None — мало игроков или уже не открыта.

    Единственная точка входа в розыгрыш: `WHERE status = "open"` плюс проверка
    rowcount делают так, что два одновременных клика запустят раунд один раз.
    """
    async with transaction() as c:
        seats = await (await c.execute(
            'SELECT COUNT(*) n FROM pvp_players WHERE room_id = ?',
            (room_id,))).fetchone()
        if seats['n'] < min_players:
            return None
        cur = await c.execute(
            'UPDATE pvp_rooms SET status = "playing" WHERE id = ? AND status = "open"',
            (room_id,))
        if cur.rowcount != 1:
            return None
        return await (await c.execute(
            'SELECT * FROM pvp_rooms WHERE id = ?', (room_id,))).fetchone()


async def pvp_finish(room_id: int, winner_id: int, payout_cents: int,
                     rake_cents: int, result: str) -> bool:
    """Закрывает комнату и платит победителю. False — уже закрыта."""
    async with transaction() as c:
        cur = await c.execute(
            'UPDATE pvp_rooms SET status = "done", winner_id = ?, payout_cents = ?, '
            'rake_cents = ?, result = ?, finished_at = ? '
            'WHERE id = ? AND status = "playing"',
            (winner_id, payout_cents, rake_cents, result, now(), room_id))
        if cur.rowcount != 1:
            return False
        await c.execute(
            'UPDATE users SET balance_cents = balance_cents + ?, '
            '                 won_cents = won_cents + ? WHERE user_id = ?',
            (payout_cents, payout_cents, winner_id))
        return True


async def pvp_cancel(room_id: int, *, expect: str = 'open',
                     by_user: int | None = None) -> bool:
    """Распускает комнату и возвращает взносы. False — статус уже другой."""
    async with transaction() as c:
        room = await (await c.execute(
            'SELECT creator_id FROM pvp_rooms WHERE id = ?', (room_id,))).fetchone()
        if room is None:
            return False
        if by_user is not None and room['creator_id'] != by_user:
            return False
        cur = await c.execute(
            'UPDATE pvp_rooms SET status = "cancelled", finished_at = ? '
            'WHERE id = ? AND status = ?', (now(), room_id, expect))
        if cur.rowcount != 1:
            return False
        await _refund_room(c, room_id)
        return True


async def pvp_expire(ttl: int = PVP_TTL) -> list[int]:
    """Распускает открытые комнаты, которые никто не забрал. Отдаёт их id."""
    rows = await (await conn().execute(
        'SELECT id FROM pvp_rooms WHERE status = "open" AND created_at < ?',
        (now() - ttl,))).fetchall()
    dead = []
    for r in rows:
        if await pvp_cancel(r['id']):
            dead.append(r['id'])
    return dead


async def pvp_room(room_id: int) -> aiosqlite.Row | None:
    return await (await conn().execute(
        'SELECT * FROM pvp_rooms WHERE id = ?', (room_id,))).fetchone()


async def pvp_players(room_id: int) -> list[aiosqlite.Row]:
    return await (await conn().execute(
        'SELECT p.*, u.username FROM pvp_players p '
        'LEFT JOIN users u ON u.user_id = p.user_id '
        'WHERE p.room_id = ? ORDER BY p.joined_at, p.user_id', (room_id,))).fetchall()


async def pvp_open_rooms(game: str, limit: int = 8) -> list[aiosqlite.Row]:
    return await (await conn().execute(
        'SELECT r.*, u.username, '
        '       (SELECT COUNT(*) FROM pvp_players p WHERE p.room_id = r.id) players '
        'FROM pvp_rooms r LEFT JOIN users u ON u.user_id = r.creator_id '
        'WHERE r.game = ? AND r.status = "open" ORDER BY r.id LIMIT ?',
        (game, limit))).fetchall()


async def pvp_my_room(game: str, user_id: int) -> aiosqlite.Row | None:
    """Комната, в которой игрок уже сидит. Вторую заводить незачем."""
    return await (await conn().execute(
        'SELECT r.* FROM pvp_rooms r JOIN pvp_players p ON p.room_id = r.id '
        'WHERE r.game = ? AND p.user_id = ? AND r.status IN ("open", "playing") '
        'ORDER BY r.id DESC LIMIT 1', (game, user_id))).fetchone()


# --- ежедневный кейс --------------------------------------------------------
#
# Три правила, которые держатся на уровне базы, а не хендлера:
#
# * кейс раз в config.DAILY_COOLDOWN, и пауза считается от момента открытия
#   предыдущего;
# * выданный кейс открывается ровно один раз;
# * приз начисляется в той же транзакции, что и отметка «открыт».
#
# Поэтому двойной клик, две вкладки Mini App и перезапуск бота заплатить дважды
# не могут: второй вызов просто не найдёт строку в статусе 'open'.
#
# Четвёртое правило — серия. Номер дня подряд пишется в саму выдачу
# (`daily_cases.streak`) вместе с призом этого дня, поэтому приз нельзя раздуть
# задним числом: он зафиксирован в момент выдачи и в открытии уже не считается.


def streak_prize(streak: int) -> int:
    """Приз за указанный день серии, в центах.

    День 1 — базовый `config.DAILY_PRIZE_CENTS`, каждый следующий на шаг больше:
    при базе $0.05 и шаге в цент серия 3 даёт $0.07. После
    `config.DAILY_STREAK_MAX_DAYS` приз замирает — предел стоит ради кассы.
    """
    days = max(1, min(streak, config.DAILY_STREAK_MAX_DAYS))
    return config.DAILY_PRIZE_CENTS + config.DAILY_STREAK_STEP_CENTS * (days - 1)


def streak_deadline(opened_at: int | None) -> int:
    """До какого момента серия жива после открытия кейса. 0 — открытий не было.

    Кейс становится доступен через сутки, и ещё сутки (`DAILY_STREAK_GRACE`)
    даётся на то, чтобы за ним прийти: иначе серию срывал бы сдвиг на пару
    часов — сегодня зашёл вечером, завтра утром.
    """
    if opened_at is None:
        return 0
    return opened_at + config.DAILY_COOLDOWN + config.DAILY_STREAK_GRACE


def _next_streak(last_row, at: int) -> int:
    """Каким будет день серии у кейса, выданного в момент `at`.

    Серия живёт на двух условиях сразу: прошлую карточку игрок угадал и пришёл
    за новым кейсом до срока. Пустая карточка гасит огонёк ровно так же, как
    пропущенный день, — иначе «серия» считала бы заходы, а не удачу.
    """
    if last_row is None or last_row['opened_at'] is None:
        return 1
    if not last_row['payout_cents']:
        return 1
    if at <= streak_deadline(last_row['opened_at']):
        return (last_row['streak'] or 1) + 1
    return 1


async def open_daily_case(user_id: int) -> aiosqlite.Row | None:
    """Выданный, но ещё не открытый кейс игрока."""
    return await (await conn().execute(
        'SELECT * FROM daily_cases WHERE user_id = ? AND status = "open" '
        'ORDER BY id DESC LIMIT 1', (user_id,))).fetchone()


async def last_daily_case(user_id: int) -> aiosqlite.Row | None:
    """Последний открытый кейс — от него считается пауза."""
    return await (await conn().execute(
        'SELECT * FROM daily_cases WHERE user_id = ? AND status = "done" '
        'ORDER BY id DESC LIMIT 1', (user_id,))).fetchone()


async def daily_ready_at(user_id: int) -> int:
    """Время, когда игроку снова положен кейс. 0 — положен хоть сейчас."""
    row = await last_daily_case(user_id)
    if row is None or row['opened_at'] is None:
        return 0
    ready = row['opened_at'] + config.DAILY_COOLDOWN
    return ready if ready > now() else 0


async def daily_streak(user_id: int) -> dict:
    """Серия кейсов игрока: сколько дней подряд угадано и что будет дальше.

    * `streak` — угаданных карточек подряд, 0 — огонёк потух (не угадал в
      прошлый раз или опоздал за кейсом);
    * `day` — день серии для кейса, о котором идёт речь: у выданного — его
      собственный, иначе у следующего;
    * `prize_cents` — что лежит в этом кейсе;
    * `next_prize_cents` — что будет в следующем, если и этот угадать;
    * `expires_at` — когда серия сгорит сама; 0 — гореть нечему или кейс уже на
      руках, а он ничего не потеряет: день и приз записаны при выдаче.
    """
    case = await open_daily_case(user_id)
    last = await last_daily_case(user_id)

    alive = (last is not None and last['opened_at'] is not None
             and bool(last['payout_cents'])
             and now() <= streak_deadline(last['opened_at']))
    streak = (last['streak'] or 1) if alive else 0

    if case is not None:
        day, prize, expires_at = case['streak'] or 1, case['prize_cents'], 0
    else:
        day = streak + 1
        prize = streak_prize(day)
        expires_at = streak_deadline(last['opened_at']) if alive else 0

    return {
        'streak': streak,
        'day': day,
        'prize_cents': prize,
        'next_prize_cents': streak_prize(day + 1),
        'expires_at': expires_at,
    }


async def issue_daily_case(user_id: int, *, prize_cents: int | None = None,
                           cards: int | None = None
                           ) -> tuple[aiosqlite.Row | None, str]:
    """Выдаёт кейс. Статусы: 'issued', 'open' (уже выдан), 'cooldown'.

    Идемпотентна: пока выданный кейс не открыт, повторный вызов отдаёт его же,
    а не плодит новые. Выигрышная карточка выбирается secrets, а не random:
    random предсказуем по seed, и приз считался бы наперёд.

    День серии и приз этого дня считаются здесь же, в одной транзакции с
    выдачей: приз, лежащий в кейсе, не должен меняться от того, когда игрок
    дошёл до карточек.
    """
    count = config.DAILY_CARDS if cards is None else cards
    async with transaction() as c:
        row = await (await c.execute(
            'SELECT * FROM daily_cases WHERE user_id = ? AND status = "open" '
            'ORDER BY id DESC LIMIT 1', (user_id,))).fetchone()
        if row is not None:
            return row, 'open'

        last = await (await c.execute(
            'SELECT opened_at, streak, payout_cents FROM daily_cases '
            'WHERE user_id = ? AND status = "done" '
            'ORDER BY id DESC LIMIT 1', (user_id,))).fetchone()
        if last is not None and last['opened_at'] is not None \
                and last['opened_at'] + config.DAILY_COOLDOWN > now():
            return None, 'cooldown'

        streak = _next_streak(last, now())
        prize = streak_prize(streak) if prize_cents is None else prize_cents
        cur = await c.execute(
            'INSERT INTO daily_cases (user_id, win_index, prize_cents, cards, '
            'streak, status, created_at) VALUES (?, ?, ?, ?, ?, "open", ?)',
            (user_id, secrets.randbelow(count), prize, count, streak, now()))
        fresh = await (await c.execute(
            'SELECT * FROM daily_cases WHERE id = ?', (cur.lastrowid,))).fetchone()
        return fresh, 'issued'


async def pick_daily_case(user_id: int, case_id: int, index: int
                          ) -> tuple[aiosqlite.Row | None, str]:
    """Открывает выбранную карточку. Статусы: 'ok', 'already', 'not_found', 'bad_index'.

    Отметка «открыт» и начисление приза — одна транзакция, а гейт
    `status = "open"` в UPDATE вместе с проверкой rowcount делает повторное
    начисление невозможным: платит только тот вызов, который сам и закрыл кейс.
    """
    async with transaction() as c:
        row = await (await c.execute(
            'SELECT * FROM daily_cases WHERE id = ? AND user_id = ?',
            (case_id, user_id))).fetchone()
        if row is None:
            return None, 'not_found'
        if row['status'] != 'open':
            return row, 'already'
        if not 0 <= index < row['cards']:
            return row, 'bad_index'

        payout = row['prize_cents'] if index == row['win_index'] else 0
        cur = await c.execute(
            'UPDATE daily_cases SET picked_index = ?, payout_cents = ?, '
            'status = "done", opened_at = ? WHERE id = ? AND status = "open"',
            (index, payout, now(), case_id))
        if cur.rowcount != 1:
            return row, 'already'
        if payout:
            # Это подарок казино, а не выигрыш в игре: won_cents не двигаем,
            # иначе розданные центы поедут в фактический RTP статистики.
            await c.execute(
                'UPDATE users SET balance_cents = balance_cents + ? '
                'WHERE user_id = ?', (payout, user_id))
        fresh = await (await c.execute(
            'SELECT * FROM daily_cases WHERE id = ?', (case_id,))).fetchone()
        return fresh, 'ok'


async def daily_stats(user_id: int | None = None) -> dict:
    """Сводка по кейсам: по всем игрокам или по одному (профиль в Mini App)."""
    if user_id is None:
        row = await (await conn().execute(
            'SELECT COUNT(*) n, COALESCE(SUM(payout_cents), 0) paid, '
            '       COUNT(DISTINCT user_id) players '
            'FROM daily_cases WHERE status = "done"')).fetchone()
    else:
        row = await (await conn().execute(
            'SELECT COUNT(*) n, COALESCE(SUM(payout_cents), 0) paid, '
            '       COUNT(DISTINCT user_id) players '
            'FROM daily_cases WHERE status = "done" AND user_id = ?',
            (user_id,))).fetchone()
    return {'opened': row['n'], 'paid': row['paid'], 'players': row['players']}


# --- обязательные подписки --------------------------------------------------


async def add_channel(chat_id: int, username: str | None, title: str | None,
                      invite_url: str | None, added_by: int) -> bool:
    """Заводит обязательный канал. False — уже был, данные обновлены."""
    async with transaction() as c:
        row = await (await c.execute(
            'SELECT chat_id FROM required_channels WHERE chat_id = ?',
            (chat_id,))).fetchone()
        if row is not None:
            await c.execute(
                'UPDATE required_channels SET username = ?, title = ?, '
                'invite_url = ?, broken = 0, note = NULL WHERE chat_id = ?',
                (username, title, invite_url, chat_id))
            return False
        await c.execute(
            'INSERT INTO required_channels (chat_id, username, title, '
            'invite_url, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?)',
            (chat_id, username, title, invite_url, added_by, now()))
        return True


async def remove_channel(chat_id: int) -> bool:
    cur = await conn().execute(
        'DELETE FROM required_channels WHERE chat_id = ?', (chat_id,))
    return cur.rowcount == 1


async def list_channels() -> list[aiosqlite.Row]:
    return await (await conn().execute(
        'SELECT * FROM required_channels ORDER BY added_at, chat_id')).fetchall()


async def mark_channel(chat_id: int, broken: bool, note: str = '') -> None:
    """Помечает канал непроверяемым — админ увидит причину в списке."""
    await conn().execute(
        'UPDATE required_channels SET broken = ?, note = ? WHERE chat_id = ?',
        (int(broken), note[:200] if note else None, chat_id))


# --- админы -----------------------------------------------------------------

async def is_admin(user_id: int) -> bool:
    if user_id == config.OWNER_ID:
        return True
    row = await (await conn().execute(
        'SELECT 1 FROM admins WHERE user_id = ?', (user_id,))).fetchone()
    return row is not None


async def add_admin(user_id: int, username: str | None) -> bool:
    if user_id == config.OWNER_ID:
        return False
    cur = await conn().execute(
        'INSERT OR IGNORE INTO admins (user_id, username, added_at) VALUES (?, ?, ?)',
        (user_id, username, now()))
    return cur.rowcount == 1


async def remove_admin(user_id: int) -> bool:
    """Владельца снять нельзя — иначе панель можно потерять безвозвратно."""
    if user_id == config.OWNER_ID:
        return False
    cur = await conn().execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
    return cur.rowcount == 1


async def list_admins() -> list[aiosqlite.Row]:
    return await (await conn().execute(
        'SELECT * FROM admins ORDER BY added_at')).fetchall()


async def all_user_ids() -> list[int]:
    rows = await (await conn().execute(
        'SELECT user_id FROM users WHERE banned = 0')).fetchall()
    return [r['user_id'] for r in rows]


# --- статистика -------------------------------------------------------------

async def games_played(user_id: int) -> int:
    """Сколько раундов игрок доиграл: обычные игры плюс закрытые PvP-комнаты.

    Ничьи (status 'void') не считаются — раунд не состоялся, ставка вернулась.
    PvP учитывается, потому что для игрока это такая же сыгранная игра, просто
    деньги пришли не из кассы, а от соперника.
    """
    c = conn()
    rounds = await (await c.execute(
        'SELECT COUNT(*) n FROM rounds WHERE user_id = ? '
        'AND status IN ("won", "lost")', (user_id,))).fetchone()
    pvp = await (await c.execute(
        'SELECT COUNT(*) n FROM pvp_players p '
        'JOIN pvp_rooms r ON r.id = p.room_id '
        'WHERE p.user_id = ? AND r.status = "done"', (user_id,))).fetchone()
    return rounds['n'] + pvp['n']


async def top_players(limit: int = 10) -> list[aiosqlite.Row]:
    """ТОП по обороту. Забаненные и не игравшие в список не попадают.

    Оборот, а не баланс: баланс показывает, сколько человек занёс, а не
    сколько он играл, и топ по нему был бы просто списком самых богатых.
    """
    return await (await conn().execute(
        'SELECT user_id, username, wagered_cents, won_cents, '
        '       (won_cents - wagered_cents) net '
        'FROM users WHERE banned = 0 AND wagered_cents > 0 '
        'ORDER BY wagered_cents DESC, user_id LIMIT ?', (limit,))).fetchall()


async def stats() -> dict:
    c = conn()
    users = await (await c.execute(
        'SELECT COUNT(*) n, COALESCE(SUM(balance_cents), 0) bal, '
        '       COALESCE(SUM(deposited_cents), 0) dep '
        'FROM users')).fetchone()
    banned = await (await c.execute(
        'SELECT COUNT(*) n FROM users WHERE banned = 1')).fetchone()
    rounds = await (await c.execute(
        'SELECT COUNT(*) n, COALESCE(SUM(bet_cents), 0) wagered, '
        '       COALESCE(SUM(payout_cents), 0) paid '
        'FROM rounds WHERE status IN ("won", "lost")')).fetchone()
    paid_out = await (await c.execute(
        'SELECT COALESCE(SUM(amount_cents), 0) s FROM withdrawals '
        'WHERE status = "paid"')).fetchone()
    pending = await (await c.execute(
        'SELECT COUNT(*) n FROM withdrawals WHERE status = "pending"')).fetchone()
    pvp = await (await c.execute(
        'SELECT COUNT(*) n, COALESCE(SUM(pot_cents), 0) pot, '
        '       COALESCE(SUM(rake_cents), 0) rake '
        'FROM pvp_rooms WHERE status = "done"')).fetchone()

    wagered, paid = rounds['wagered'], rounds['paid']
    return {
        'users': users['n'],
        'banned': banned['n'],
        'balances': users['bal'],
        'deposited': users['dep'],
        'withdrawn': paid_out['s'],
        'pending_withdrawals': pending['n'],
        'rounds': rounds['n'],
        'wagered': wagered,
        'paid_to_players': paid,
        'gross_profit': wagered - paid,
        # Фактический RTP по ledger. Должен сходиться с config.RTP на объёме.
        'actual_rtp': (paid / wagered) if wagered else None,
        # PvP считается отдельно: там казино не играет, а берёт рейк с банка.
        'pvp_rooms': pvp['n'],
        'pvp_pot': pvp['pot'],
        'pvp_rake': pvp['rake'],
    }


# --- обслуживание -----------------------------------------------------------

async def reap_active_rounds(games: tuple[str, ...] = ('crash', 'storm')) -> int:
    """Возвращает ставки по раундам, зависшим в 'active' после перезапуска.

    Crash сам по себе не завершится: множитель тикает в памяти процесса, и
    после падения бота раунд остаётся открытым навсегда. Считаем такой раунд
    несостоявшимся и возвращаем ставку — это честнее, чем оставить игрока без
    денег и без раунда. Вызывается один раз при старте.

    Слот из Mini App здесь же: ставка снимается запросом на спин, а результат
    записывается следующим шагом, и процесс, убитый между ними, оставил бы
    открытый раунд с уже снятыми деньгами.
    """
    if not games:
        return 0
    marks = ','.join('?' * len(games))
    rows = await (await conn().execute(
        f'SELECT id, user_id, bet_cents FROM rounds '
        f'WHERE status = "active" AND game IN ({marks})', games)).fetchall()

    reaped = 0
    for r in rows:
        async with transaction() as c:
            cur = await c.execute(
                'UPDATE rounds SET status = "void", multiplier = 1.0, '
                'payout_cents = ?, finished_at = ? '
                'WHERE id = ? AND status = "active"',
                (r['bet_cents'], now(), r['id']))
            if cur.rowcount != 1:
                continue
            await c.execute(
                'UPDATE users SET balance_cents = balance_cents + ?, '
                '                 wagered_cents = wagered_cents - ? '
                'WHERE user_id = ?',
                (r['bet_cents'], r['bet_cents'], r['user_id']))
            reaped += 1
    return reaped
