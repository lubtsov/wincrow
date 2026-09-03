"""«Сочный шторм» — слот 6×5 с каскадами. Игра живёт в Mini App.

В каталоге игр бота (`games/registry.py`) её нет намеренно: экран из тридцати
падающих символов в переписываемом сообщении Telegram не показать, а держать в
меню кнопку, которая ничего не открывает, хуже, чем не держать её вовсе. Зато
деньги, случайность и раунды у слота ровно те же, что у остальных игр: ставку
снимает `engine.start_round`, выплату считает `engine.finish`, история попадает
в ту же таблицу `rounds` — значит, слот виден и в профиле, и в ТОП-10, и в RTP
админки.

Механика
--------
Выигрыш платится за **скопление**: 8 и больше одинаковых фруктов в любых
местах поля, линий нет. Выигрышные символы исчезают, верхние падают вниз, сверху
досыпаются новые — и если снова собралось скопление, каскад продолжается.

Символ шторма (🌪) несёт множитель ×2…×25. В скопления он не входит и никуда не
исчезает, поэтому все штормы, выпавшие за спин, лежат на финальном поле. Если
спин вообще что-то заплатил, сумма штормов умножает весь выигрыш.

Отдача
------
Форма таблицы выплат задана руками, а её масштаб — нет: `SCALE` подобран
прогоном Монте-Карло так, чтобы фактическая отдача сошлась с `config.RTP`.
Проверяется это в `tests/test_storm.py`, и любая правка весов или выплат
уронит тест, пока масштаб не пересчитают заново.
"""

import json
from dataclasses import dataclass

import aiosqlite

import config
import db
from games import engine

GAME = 'storm'
TITLE = 'Сочный шторм'

COLS, ROWS = 6, 5
CELLS = COLS * ROWS

# Меньше восьми одинаковых символов не платят ничего — это и есть «скопление».
MIN_CLUSTER = 8

# Верхняя граница выплаты за спин, в ставках. Нужна не игроку, а кассе: без неё
# теоретический максимум — тридцать алмазов с четырьмя штормами по ×25.
MAX_MULTIPLIER = 1000.0

@dataclass(frozen=True)
class Symbol:
    key: str
    emoji: str
    title: str
    weight: int
    # Выплата за 8–9, 10–11 и 12+ символов, в ставках и до масштабирования.
    pays: tuple[float, float, float]


SYMBOLS: tuple[Symbol, ...] = (
    Symbol('cherry', '🍒', 'Черешня', 20, (0.25, 0.75, 2.0)),
    Symbol('lemon', '🍋', 'Лимон', 18, (0.40, 1.00, 2.5)),
    Symbol('orange', '🍊', 'Апельсин', 16, (0.50, 1.25, 3.0)),
    Symbol('kiwi', '🥝', 'Киви', 14, (0.75, 2.00, 5.0)),
    Symbol('grape', '🍇', 'Виноград', 12, (1.00, 2.50, 8.0)),
    Symbol('melon', '🍉', 'Арбуз', 10, (1.50, 4.00, 12.0)),
    Symbol('star', '⭐', 'Звезда', 6, (2.50, 8.00, 25.0)),
    Symbol('gem', '💎', 'Алмаз', 4, (5.00, 15.00, 50.0)),
)

BY_KEY = {s.key: s for s in SYMBOLS}

# Шторм: множитель вместо фрукта. Вес маленький, потому что в паре с выигрышем
# он умножает всю выплату, и частый шторм пришлось бы гасить копеечной таблицей.
STORM_WEIGHT = 1
STORM_VALUES: tuple[tuple[int, int], ...] = (   # (множитель, вес)
    (2, 45), (3, 25), (5, 15), (10, 10), (25, 5),
)

# Масштаб таблицы выплат. Не подобран на глаз: прогон в 800 000 спинов даёт
# средний множитель 2.1455 при SCALE = 1, отсюда 0.97 / 2.1455 = 0.4521.
# Любая правка весов, выплат или штормов ломает это равенство — пересчитывать
# заново, `tools/calibrate_storm.py` умеет это одной командой. Проверяет
# tests/test_storm.py::test_rtp_matches_config.
SCALE = 0.4521

TOTAL_WEIGHT = sum(s.weight for s in SYMBOLS) + STORM_WEIGHT
_STORM_TOTAL = sum(w for _, w in STORM_VALUES)


def pay(symbol: str, count: int) -> float:
    """Выплата за скопление, в ставках. Меньше MIN_CLUSTER — ноль.

    Округление до четвёртого знака стоит здесь, а не в конце спина: игрок
    видит выплату каждого скопления отдельно, и сумма показанных выплат должна
    в точности сходиться с выигрышем спина. Считать по неокруглённым, а
    показывать округлённые — это расхождение в четвёртом знаке, которое в
    интерфейсе выглядит как ошибка кассы.
    """
    spec = BY_KEY.get(symbol)
    if spec is None or count < MIN_CLUSTER:
        return 0.0
    tier = 0 if count < 10 else 1 if count < 12 else 2
    return round(spec.pays[tier] * SCALE, 4)


def is_storm(cell: str) -> bool:
    return cell.startswith('x')


def storm_value(cell: str) -> int:
    return int(cell[1:]) if is_storm(cell) else 0

# --- само поле --------------------------------------------------------------
#
# Поле — список из шести колонок по пять ячеек: grid[col][row], row 0 — верх.
# Гравитация тянет к большим индексам, поэтому «упасть» — это сдвинуться вниз
# по row. Порядок обращений к rnd() — часть проверки честности: сначала
# заполняется первая колонка сверху вниз, потом вторая и так далее.


def _cell(rnd: engine.Round) -> str:
    """Одна ячейка: фрукт по весам либо шторм с множителем."""
    roll = rnd.rnd() * TOTAL_WEIGHT
    for spec in SYMBOLS:
        if roll < spec.weight:
            return spec.key
        roll -= spec.weight
    # Остался вес шторма — выбираем множитель по своим весам.
    roll = rnd.rnd() * _STORM_TOTAL
    for value, weight in STORM_VALUES:
        if roll < weight:
            return f'x{value}'
        roll -= weight
    return f'x{STORM_VALUES[0][0]}'


def _fresh_grid(rnd: engine.Round) -> list[list[str]]:
    return [[_cell(rnd) for _ in range(ROWS)] for _ in range(COLS)]


def clusters(grid: list[list[str]]) -> list[dict]:
    """Скопления на поле: 8+ одинаковых фруктов где угодно, линий нет."""
    seen: dict[str, list[list[int]]] = {}
    for col in range(COLS):
        for row in range(ROWS):
            cell = grid[col][row]
            if not is_storm(cell):
                seen.setdefault(cell, []).append([col, row])

    out = []
    for symbol, cells in seen.items():
        if len(cells) < MIN_CLUSTER:
            continue
        out.append({'symbol': symbol, 'count': len(cells),
                    'win': pay(symbol, len(cells)), 'cells': cells})
    out.sort(key=lambda c: (-c['win'], c['symbol']))
    return out


def collapse(grid: list[list[str]], dead: set[tuple[int, int]],
             rnd: engine.Round) -> list[list[str]]:
    """Убирает выигрышные ячейки, роняет остальные, досыпает новые сверху."""
    out = []
    for col in range(COLS):
        kept = [grid[col][row] for row in range(ROWS)
                if (col, row) not in dead]
        fresh = [_cell(rnd) for _ in range(ROWS - len(kept))]
        out.append(fresh + kept)          # новые сверху, старые осели вниз
    return out

# Предохранитель от бесконечного каскада. Вероятность дойти до сорока подряд
# исчезающе мала, но цикл, зависящий от случайности, обязан иметь предел.
MAX_CASCADES = 40


def play(rnd: engine.Round) -> dict:
    """Спин целиком: каскады до упора. Чистая функция от Round.

    Отдаёт и результат для кассы (`multiplier`), и раскадровку для анимации
    (`steps`): клиент проигрывает то, что уже посчитано, а не считает сам.
    """
    grid = _fresh_grid(rnd)
    steps: list[dict] = []
    base = 0.0

    for _ in range(MAX_CASCADES):
        found = clusters(grid)
        if not found:
            break
        win = sum(c['win'] for c in found)
        base += win
        steps.append({'grid': [list(col) for col in grid],
                      'clusters': found, 'win': round(win, 4)})
        dead = {(col, row) for c in found for col, row in c['cells']}
        grid = collapse(grid, dead, rnd)

    storms = [{'cell': [col, row], 'x': storm_value(grid[col][row])}
              for col in range(COLS) for row in range(ROWS)
              if is_storm(grid[col][row])]
    storm_total = sum(s['x'] for s in storms)

    # Базу фиксируем до умножения на штормы: и клиент, и касса считают от одного
    # числа, иначе штормы размножают погрешность двоичной суммы в разы.
    base = round(base, 4)
    total = base * storm_total if base > 0 and storm_total else base
    return {
        'grid': [list(col) for col in grid],
        'steps': steps,
        'storms': storms,
        'storm_total': storm_total,
        'base_win': base,
        'multiplier': round(min(total, MAX_MULTIPLIER), 4),
    }


def paytable() -> list[dict]:
    """Таблица выплат для экрана правил в Mini App."""
    return [{'key': s.key, 'emoji': s.emoji, 'title': s.title,
             'pays': [round(v * SCALE, 3) for v in s.pays]} for s in SYMBOLS]


def bets() -> list[int]:
    """Пресеты ставок в центах — те же границы, что у остальных игр."""
    return [c for c in (10, 20, 50, 100, 250, 500, 1000, 2500)
            if config.MIN_BET_CENTS <= c <= config.MAX_BET_CENTS]

# --- спин целиком -----------------------------------------------------------
#
# Клиент присылает только сумму ставки и свой уникальный id спина. Всё
# остальное — где что выпало, какой множитель и сколько платить — считается
# здесь и уезжает в ту же таблицу rounds, что и у остальных игр.


def _shape(result: dict, *, round_id: int, bet_cents: int, payout_cents: int,
           seed_hash: str, client_seed: str, nonce: int,
           replayed: bool = False) -> dict:
    """Ответ клиенту: результат плюс данные для проверки честности."""
    return dict(result, round_id=round_id, bet_cents=bet_cents,
                payout_cents=payout_cents, bet=db.fmt(bet_cents),
                win=db.fmt(payout_cents), replayed=replayed,
                fair={'server_seed_hash': seed_hash, 'client_seed': client_seed,
                      'nonce': nonce})


async def round_by_client(user_id: int, client_id: str) -> aiosqlite.Row | None:
    return await (await db.conn().execute(
        'SELECT * FROM rounds WHERE user_id = ? AND game = ? AND client_id = ?',
        (user_id, GAME, client_id))).fetchone()


async def replay(row: aiosqlite.Row) -> tuple[dict, str]:
    """Повторный запрос с тем же id спина: отдаём прежний результат.

    Раскадровка не хранится в базе, а пересчитывается из сида: исход однозначно
    задан тройкой (server_seed, client_seed, nonce), и держать в каждом раунде
    килобайты JSON ради этого незачем. Сохранённая сводка нужна как раз для
    сверки — она ловит единственный случай, когда пересчёт разъедется: игрок
    успел сменить серверный сид (`engine.rotate_seed`).
    """
    if row['status'] == 'active':
        return {}, 'pending'

    saved = json.loads(row['state'] or '{}')
    seed = await (await db.conn().execute(
        'SELECT server_seed FROM seeds WHERE user_id = ?',
        (row['user_id'],))).fetchone()

    if seed is not None:
        rnd = engine.Round(id=row['id'], user_id=row['user_id'], game=GAME,
                           bet_cents=row['bet_cents'],
                           server_seed=seed['server_seed'],
                           server_seed_hash=row['server_seed_hash'],
                           client_seed=row['client_seed'], nonce=row['nonce'])
        result = play(rnd)
        same = (abs(result['base_win'] - float(saved.get('base', -1))) < 1e-6
                and result['storm_total'] == saved.get('storm'))
        if same:
            return _shape(result, round_id=row['id'],
                          bet_cents=row['bet_cents'],
                          payout_cents=row['payout_cents'] or 0,
                          seed_hash=row['server_seed_hash'],
                          client_seed=row['client_seed'], nonce=row['nonce'],
                          replayed=True), 'repeat'

    # Сид сменили — анимацию не восстановить, но сумма известна точно.
    return _shape({'grid': [], 'steps': [], 'storms': [], 'storm_total': 0,
                   'base_win': float(saved.get('base', 0.0)),
                   'multiplier': row['multiplier'] or 0.0},
                  round_id=row['id'], bet_cents=row['bet_cents'],
                  payout_cents=row['payout_cents'] or 0,
                  seed_hash=row['server_seed_hash'],
                  client_seed=row['client_seed'], nonce=row['nonce'],
                  replayed=True), 'repeat'

async def spin(user_id: int, bet_cents: int,
               client_id: str) -> tuple[dict, str]:
    """Спин: ставка, каскады, выплата. (результат, статус).

    Статусы: 'ok', 'repeat', 'pending', 'no_money', 'bad_bet'.

    От повторной отправки защищает уникальный индекс по `rounds.client_id`.
    Сначала смотрим, не считали ли уже спин с этим id; если два запроса пришли
    одновременно, второй упадёт на индексе — и упадёт внутри той же транзакции,
    в которой снимается ставка, поэтому деньги вернутся сами.
    """
    if not config.MIN_BET_CENTS <= bet_cents <= config.MAX_BET_CENTS:
        return {}, 'bad_bet'
    if not client_id or len(client_id) > 64:
        return {}, 'bad_bet'

    prev = await round_by_client(user_id, client_id)
    if prev is not None:
        return await replay(prev)

    try:
        rnd = await engine.start_round(user_id, GAME, bet_cents,
                                       client_id=client_id)
    except aiosqlite.IntegrityError:
        prev = await round_by_client(user_id, client_id)
        return await replay(prev) if prev is not None else ({}, 'pending')
    if rnd is None:
        return {}, 'no_money'

    result = play(rnd)
    # В раунде остаётся только сводка: раскадровка восстанавливается из сида.
    rnd.state = {'base': result['base_win'], 'storm': result['storm_total'],
                 'steps': len(result['steps'])}
    payout = await engine.finish(rnd, result['multiplier'])
    if payout is None:                      # раунд закрыли параллельно
        prev = await round_by_client(user_id, client_id)
        return await replay(prev) if prev is not None else ({}, 'pending')

    return _shape(result, round_id=rnd.id, bet_cents=bet_cents,
                  payout_cents=payout, seed_hash=rnd.server_seed_hash,
                  client_seed=rnd.client_seed, nonce=rnd.nonce), 'ok'


async def history(user_id: int, limit: int = 10) -> list[dict]:
    """Последние спины игрока — для истории в Mini App."""
    rows = await (await db.conn().execute(
        'SELECT id, bet_cents, multiplier, payout_cents, status, created_at '
        'FROM rounds WHERE user_id = ? AND game = ? AND status != "active" '
        'ORDER BY id DESC LIMIT ?', (user_id, GAME, limit))).fetchall()
    return [{'round_id': r['id'], 'bet_cents': r['bet_cents'],
             'bet': db.fmt(r['bet_cents']),
             'multiplier': r['multiplier'] or 0.0,
             'payout_cents': r['payout_cents'] or 0,
             'win': db.fmt(r['payout_cents'] or 0),
             'at': r['created_at']} for r in rows]
