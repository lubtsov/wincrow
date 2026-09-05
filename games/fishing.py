"""Рыбалка: одно колесо на всех, раунд идёт сам.

Как это работает
----------------
Раунд не запускает игрок — раунд идёт всегда. Его номер считается из времени:
`no = int(time.time() // ROUND_SECONDS)`. Поэтому он одинаковый у всех клиентов,
рассылать состояние не нужно, зашедший посреди раунда попадает в тот же раунд, а
перезапуск процесса счёт не сбивает.

    0…10 c   ставки открыты, колесо крутится
    10…16 c  ставки закрыты, на рыб падают прибавки, колесо тормозит
    16…22 c  результат: рыбу вытягивают, выплаты уже начислены

Позиций для ставки четыре: три рыбы — синяя, оранжевая, красная — и «звено».
Звеньев на колесе несколько (config.FISHING_LINKS), но ставка на звено одна: она
выигрывает на любом из них. Иначе не сходится арифметика — чтобы каждое из
четырёх звеньев платило x1.9, каждому нужна вероятность 0.51, а вместе они
заняли бы два круга.

Деньги
------
Своей кассы у игры нет. Каждая ставка — обычный раунд в таблице rounds:
engine.start_round снимает ставку, engine.finish начисляет выплату. Поэтому
рыбалка сама попадает в профиль, историю, статистику и процент владельцу чата, а
повторный запрос с тем же bet_id второй раз ставку не снимает — по колонке
client_id стоит уникальный индекс.
"""

import json
import time

import aiosqlite

import config
import db
from games import engine

GAME = 'fishing'
TITLE = 'Рыбалка'
LINK = 'link'

# Рыбы: минимальный множитель и прибавки, которые могут на рыбу упасть. Минимумы
# заданы правилами игры — синяя x3, оранжевая x4, красная x10: чем реже рыба, тем
# крупнее иксы. Прибавка выбирается из своего списка равновероятно, так что
# список задаёт и разброс, и среднее.
FISH = (
    {'key': 'blue',   'title': 'Синяя рыба',     'emoji': '🐟',
     'floor': 3,  'boosts': (1, 2, 2, 3, 4)},
    {'key': 'orange', 'title': 'Оранжевая рыба', 'emoji': '🐠',
     'floor': 4,  'boosts': (2, 3, 3, 4, 6)},
    {'key': 'red',    'title': 'Красная рыба',   'emoji': '🐡',
     'floor': 10, 'boosts': (4, 6, 8, 12, 28)},
)
FISH_BY_KEY = {spec['key']: spec for spec in FISH}
PICKS = tuple([spec['key'] for spec in FISH] + [LINK])

# Сколько секторов колеса отдано каждой рыбе. На математику это не влияет: доля
# круга у рыбы своя, секторы её просто делят. Но одним сектором синяя заняла бы
# четверть круга, и колесо выглядело бы кривым.
FISH_SECTORS = {'blue': 3, 'orange': 2, 'red': 1}

ROUND_SECONDS = (config.FISHING_BET_SECONDS + config.FISHING_SPIN_SECONDS
                 + config.FISHING_RESULT_SECONDS)

# --- множители и колесо -----------------------------------------------------
#
# Ставка платит, только если колесо встало на её позицию, поэтому отдача позиции
# равна p * m. Чтобы ни одна позиция не была выгоднее другой, доля круга берётся
# обратной множителю:
#
#     p_i = (1/m_i) / Σ(1/m_j)   =>   p_i * m_i = 1 / Σ(1/m_j) для любой i
#
# Справа — одно число, одинаковое для всех позиций. Это и есть отдача игры.


def _boost_mean(spec: dict) -> float:
    return sum(spec['boosts']) / len(spec['boosts'])


def _rtp_at(drop: float) -> float:
    """Отдача при такой вероятности прибавки. По drop монотонно растёт."""
    total = 1.0 / config.FISHING_LINK_MULT
    for spec in FISH:
        total += 1.0 / (spec['floor'] + drop * _boost_mean(spec))
    return 1.0 / total


def _solve_drop() -> float:
    """Как часто должна падать прибавка, чтобы отдача равнялась config.RTP.

    Одних минимумов на 97% не хватает: с x3, x4, x10 и звеном x1.9 выходит 82.7%.
    Дотягивают отдачу прибавки, и вопрос лишь в том, насколько часто они падают —
    это одно уравнение с одним неизвестным. Решается делением отрезка, потому что
    отдача по drop монотонна.

    Поэтому в игре нет ни одной подобранной руками константы: поменяли
    коэффициент звена в конфиге — частота прибавок подстроилась сама, а отдача
    осталась прежней.
    """
    low, high = 0.0, 1.0
    if _rtp_at(low) >= config.RTP:
        return low          # даже без прибавок отдача уже не ниже нужной
    if _rtp_at(high) <= config.RTP:
        return high         # прибавки на каждой рыбе — и всё равно мало
    for _ in range(60):
        mid = (low + high) / 2
        if _rtp_at(mid) < config.RTP:
            low = mid
        else:
            high = mid
    return (low + high) / 2


# Вероятность, что на рыбу упадёт прибавка. Считается из RTP, а не задаётся:
# см. _solve_drop. Крайние случаи (0.0 или 1.0) значат, что с такими минимумами
# и коэффициентом звена нужной отдачи не получить — это ошибка конфигурации, и
# её ловит tests/test_fishing.py.
DROP_CHANCE = _solve_drop()


def average(key: str) -> float:
    """Средний множитель позиции — с учётом того, что прибавка падает не всегда."""
    if key == LINK:
        return float(config.FISHING_LINK_MULT)
    spec = FISH_BY_KEY[key]
    return spec['floor'] + DROP_CHANCE * _boost_mean(spec)


def _rim() -> tuple[str, ...]:
    """Порядок секторов по кругу: звенья вразбег, рыбы между ними.

    Звено — самая частая позиция, поэтому звенья расставляются равномерно, а рыбы
    раскладываются в промежутки между ними по очереди, а не блоками: так рядом не
    оказывается двух одинаковых рыб и колесо читается с любого места.
    """
    left = {spec['key']: FISH_SECTORS[spec['key']] for spec in FISH}
    order = []
    while any(left.values()):
        for spec in FISH:
            if left[spec['key']]:
                order.append(spec['key'])
                left[spec['key']] -= 1

    links = max(1, config.FISHING_LINKS)
    total, out = len(order), []
    for i in range(links):
        out.append(LINK)
        share = total // links + (1 if i < total % links else 0)
        out.extend(order[:share])
        order = order[share:]
    return tuple(out)


def _build_sectors() -> list[dict]:
    """Колесо: сектор, его доля круга и угол от метки сверху по часовой.

    Доля позиции делится между её секторами ровно поровну, поэтому ставка на
    звено выигрывает на любом из четырёх, а вероятность у неё одна.
    """
    rim = _rim()
    share = {key: (1.0 / average(key)) / rim.count(key) for key in set(rim)}
    total = sum(share[key] for key in rim)
    out, angle = [], 0.0
    for index, key in enumerate(rim):
        width = 360.0 * share[key] / total
        out.append({'index': index, 'pick': key, 'weight': share[key] / total,
                    'from': round(angle, 4), 'width': round(width, 4)})
        angle += width
    return out


SECTORS = _build_sectors()


def rtp() -> float:
    """Фактическая отдача игры. На всех позициях она одна и та же."""
    return _rtp_at(DROP_CHANCE)


def chance(key: str) -> float:
    """Вероятность, что колесо встанет на эту позицию."""
    return sum(s['weight'] for s in SECTORS if s['pick'] == key)


def positions(mults: dict | None = None, mine: dict | None = None) -> list[dict]:
    """Позиции для ставки — в том порядке, в каком они стоят на экране."""
    out = [{'key': spec['key'], 'title': spec['title'], 'emoji': spec['emoji'],
            'kind': 'fish', 'floor': float(spec['floor'])} for spec in FISH]
    out.append({'key': LINK, 'title': 'Звено', 'emoji': '🔗', 'kind': 'link',
                'floor': round(float(config.FISHING_LINK_MULT), 4)})
    for item in out:
        item['chance'] = round(chance(item['key']), 4)
        # До закрытия ставок на экране стоит минимум позиции: множители ещё не
        # раскрыты. После — то, что реально заплатит.
        item['mult'] = (mults or {}).get(item['key'], item['floor'])
        cents = (mine or {}).get(item['key'], 0)
        item['mine_cents'] = cents
        item['mine'] = db.fmt(cents)
    return out


def bets() -> list[int]:
    """Пресеты ставок в центах — те же границы, что у остальных игр."""
    return [c for c in (10, 20, 50, 100, 250, 500, 1000, 2500)
            if config.MIN_BET_CENTS <= c <= config.MAX_BET_CENTS]

# --- раунд ------------------------------------------------------------------


def clock() -> float:
    """Сейчас. Отдельной функцией — чтобы тесты могли подменить время."""
    return time.time()


def round_no(now: float | None = None) -> int:
    """Номер раунда. Считается из времени, поэтому одинаков у всех клиентов."""
    return int((clock() if now is None else now) // ROUND_SECONDS)


def plan(no: int) -> dict:
    """Расписание раунда в абсолютных секундах."""
    started = no * ROUND_SECONDS
    closes = started + config.FISHING_BET_SECONDS
    return {'started_at': started, 'closes_at': closes,
            'stops_at': closes + config.FISHING_SPIN_SECONDS,
            'ends_at': started + ROUND_SECONDS}


def phase(no: int, now: float) -> str:
    """'bets' — ставки идут, 'spin' — колесо докручивается, 'result' — итог."""
    times = plan(no)
    if now < times['closes_at']:
        return 'bets'
    return 'spin' if now < times['stops_at'] else 'result'


def result_of(no: int, server_seed: str) -> dict:
    """Что выпало в раунде. Однозначно задано его сидом и номером.

    Порядок обращений к потоку — часть проверки честности: сектор, смещение
    внутри сектора, затем по два числа на каждую рыбу (упала ли прибавка и
    какая). Два числа берутся всегда, даже когда прибавка не упала: так раздачу
    проще пересчитать руками по раскрытому сиду.
    """
    stream = engine.float_stream(server_seed, f'{GAME}:{no}', 0)

    roll, edge = next(stream), 0.0
    sector = SECTORS[-1]
    for item in SECTORS:
        edge += item['weight']
        if roll < edge:
            sector = item
            break

    # Колесо встаёт не строго по центру сектора: смещение внутри него делает
    # остановку живой, а на исход не влияет. Края сектора не берём — на них
    # непонятно, что выпало.
    mark = sector['from'] + sector['width'] * (0.2 + 0.6 * next(stream))

    mults, drops = {}, []
    for spec in FISH:
        hit, which = next(stream), next(stream)
        boost = 0
        if hit < DROP_CHANCE:
            boost = spec['boosts'][min(int(which * len(spec['boosts'])),
                                       len(spec['boosts']) - 1)]
            drops.append({'fish': spec['key'], 'boost': boost})
        mults[spec['key']] = float(spec['floor'] + boost)
    mults[LINK] = round(float(config.FISHING_LINK_MULT), 4)

    closes = plan(no)['closes_at']
    for i, drop in enumerate(drops):
        drop['mult'] = mults[drop['fish']]
        # Когда прибавку показывать. Время общее для всех, поэтому множители
        # появляются на всех экранах одновременно.
        drop['show_at'] = closes + 0.7 + 1.0 * i

    return {'sector': sector['index'], 'pick': sector['pick'],
            # Насколько повернуть колесо, чтобы метка сверху пришлась на mark.
            'angle': round((360.0 - mark) % 360.0, 3),
            'mult': mults[sector['pick']], 'mults': mults, 'drops': drops}

# --- хранение и ставки ------------------------------------------------------


async def _row(no: int) -> aiosqlite.Row | None:
    return await (await db.conn().execute(
        'SELECT * FROM fishing_rounds WHERE no = ?', (no,))).fetchone()


async def ensure(no: int) -> aiosqlite.Row:
    """Строка раунда. Создаётся при первом обращении — вместе с сидом.

    Сид рождается здесь и больше не меняется: из него выводятся и сектор, и
    множители, поэтому результат раунда восстановим даже если процесс убили
    посреди него. Клиенту до расчёта уезжает только sha256 сида.

    OR IGNORE потому, что раунд один на всех: два одновременных запроса создадут
    его один раз — номер раунда и есть первичный ключ.
    """
    row = await _row(no)
    if row is not None:
        return row
    seed = engine.new_seed()
    await db.conn().execute(
        'INSERT OR IGNORE INTO fishing_rounds (no, server_seed, '
        'server_seed_hash, status, started_at) VALUES (?, ?, ?, "live", ?)',
        (no, seed, engine.seed_hash(seed), plan(no)['started_at']))
    return await _row(no)


async def _mine(no: int, user_id: int) -> list[aiosqlite.Row]:
    return await (await db.conn().execute(
        'SELECT pick, bet_cents, payout_cents FROM fishing_bets '
        'WHERE no = ? AND user_id = ? ORDER BY id', (no, user_id))).fetchall()


async def place(user_id: int, no: int, pick: str, bet_cents: int, bet_id: str,
                now: float | None = None) -> tuple[dict, str]:
    """Ставка в живой раунд.

    Статусы: ok, repeat, closed, no_money, bad_bet, too_many.

    Время закрытия проверяется ЗДЕСЬ, по часам сервера. В клиенте таймер только
    рисуется, и подкрутить его в DevTools можно сколько угодно — на приём ставки
    это не влияет. Номер раунда клиент присылает, но не выбирает: он должен
    совпасть с текущим, иначе ставка не принимается.
    """
    now = clock() if now is None else now
    if pick not in PICKS or not isinstance(bet_cents, int) \
            or not config.MIN_BET_CENTS <= bet_cents <= config.MAX_BET_CENTS \
            or not bet_id or len(bet_id) > 64:
        return {}, 'bad_bet'
    if no != round_no(now) or now >= plan(no)['closes_at']:
        return {}, 'closed'

    row = await ensure(no)
    if row is None or row['status'] != 'live':
        return {}, 'closed'
    mine = await _mine(no, user_id)
    if len(mine) >= config.FISHING_MAX_BETS:
        return {}, 'too_many'

    # Дальше деньги. Ставку снимает engine.start_round — та же дорога, что у
    # остальных игр, поэтому рыбалка попадает в оборот, историю и статистику.
    try:
        rnd = await engine.start_round(user_id, GAME, bet_cents,
                                       state={'no': no, 'pick': pick},
                                       client_id=bet_id)
    except aiosqlite.IntegrityError:
        # Тот же bet_id уже был: сеть оборвалась, клиент повторил запрос. Ставка
        # уже стоит — снимать второй раз нечего.
        return {'bet_id': bet_id}, 'repeat'
    if rnd is None:
        return {}, 'no_money'

    try:
        await db.conn().execute(
            'INSERT INTO fishing_bets (no, user_id, pick, bet_cents, round_id, '
            'created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (no, user_id, pick, bet_cents, rnd.id, db.now()))
    except Exception:
        # Ставка снялась, а привязать её к раунду не вышло. Возвращаем деньги
        # сразу, иначе они висели бы до sweep().
        await engine.void(rnd)
        raise
    return {'bet_id': bet_id, 'round_id': rnd.id, 'pick': pick,
            'bet_cents': bet_cents, 'bet': db.fmt(bet_cents)}, 'ok'


async def settle(no: int, now: float | None = None) -> dict | None:
    """Считает раунд и платит по ставкам. Дважды не заплатит.

    Результат не «выпадает» в момент расчёта — он с самого начала лежал в сиде,
    поэтому пересчёт всегда даёт то же самое, а status='done' только фиксирует
    его в базе для ленты прошлых раундов.

    Выплаты идемпотентны сами: engine.finish закрывает раунд по
    `WHERE status = "active"` и на второй вызов возвращает None. Поэтому расчёт
    можно спокойно догонять — умер процесс посреди выплат, следующий вызов
    доплатит остальным и никому не заплатит дважды.
    """
    now = clock() if now is None else now
    row = await _row(no)
    if row is None or now < plan(no)['stops_at']:
        return None
    result = result_of(no, row['server_seed'])
    if row['status'] == 'live':
        await db.conn().execute(
            'UPDATE fishing_rounds SET status = "done", result = ?, '
            'settled_at = ? WHERE no = ? AND status = "live"',
            (json.dumps(result, ensure_ascii=False), db.now(), no))
    await _pay(no, result)
    return result


async def _pay(no: int, result: dict) -> None:
    """Начисляет по ставкам раунда. Уже оплаченные пропускает.

    Проигравшие закрываются множителем 0: раунд в rounds должен закрыться так же,
    как выигравший, иначе он висел бы 'active' и его подобрал бы возврат.
    """
    rows = await (await db.conn().execute(
        'SELECT id, user_id, pick, round_id FROM fishing_bets '
        'WHERE no = ? AND payout_cents IS NULL ORDER BY id', (no,))).fetchall()
    for bet in rows:
        mult = result['mult'] if bet['pick'] == result['pick'] else 0.0
        rnd = await engine.load_round(bet['round_id'], bet['user_id'], GAME)
        payout = await engine.finish(rnd, mult) if rnd is not None else None
        if payout is None:
            # Раунд закрыли раньше нас. Цифры берём из него, чтобы в истории
            # рыбалки не осталось дырки.
            paid = await (await db.conn().execute(
                'SELECT payout_cents FROM rounds WHERE id = ?',
                (bet['round_id'],))).fetchone()
            payout = (paid['payout_cents'] or 0) if paid is not None else 0
        await db.conn().execute(
            'UPDATE fishing_bets SET multiplier = ?, payout_cents = ? '
            'WHERE id = ?', (mult, payout, bet['id']))


async def sweep(now: float | None = None) -> None:
    """Догоняет то, что осталось после перерыва в работе процесса.

    Нерассчитанные раунды считаются по своим сидам — результат от этого не
    меняется. Ставки, которые сняли, но не привязали к раунду, возвращаются
    игроку: без строки в fishing_bets платить по ним некому.
    """
    now = clock() if now is None else now
    rows = await (await db.conn().execute(
        'SELECT no FROM fishing_rounds WHERE status = "live" AND no < ? '
        'ORDER BY no LIMIT 50', (round_no(now),))).fetchall()
    for row in rows:
        await settle(row['no'], now)

    stale = await (await db.conn().execute(
        'SELECT id, user_id FROM rounds WHERE game = ? AND status = "active" '
        'AND created_at < ? AND id NOT IN (SELECT round_id FROM fishing_bets) '
        'LIMIT 50', (GAME, db.now() - 2 * ROUND_SECONDS))).fetchall()
    for row in stale:
        rnd = await engine.load_round(row['id'], row['user_id'], GAME)
        if rnd is not None:
            await engine.void(rnd)

    # Пустые прошедшие раунды не нужны: без ставок в них нечего хранить, а
    # держать по строке на каждые 22 секунды работы бота — глупо.
    await db.conn().execute(
        'DELETE FROM fishing_rounds WHERE status = "done" AND no < ? '
        'AND no NOT IN (SELECT no FROM fishing_bets)',
        (round_no(now) - 3600 // ROUND_SECONDS,))


_swept = 0.0


async def tick(now: float | None = None) -> None:
    """Шаг фоновой задачи: досчитать вставший раунд, изредка подмести хвосты.

    Раунд считает и обычный запрос состояния, но полагаться на это нельзя:
    игрок может поставить и закрыть Telegram, и тогда выплата ждала бы
    следующего посетителя.
    """
    global _swept
    now = clock() if now is None else now
    await settle(round_no(now), now)
    if now - _swept >= ROUND_SECONDS:
        _swept = now
        await sweep(now)


async def recent(limit: int = 14) -> list[dict]:
    """Лента прошлых раундов: чем закончились и с каким множителем."""
    rows = await (await db.conn().execute(
        'SELECT no, result FROM fishing_rounds WHERE status = "done" '
        'ORDER BY no DESC LIMIT ?', (limit,))).fetchall()
    out = []
    for row in rows:
        data = json.loads(row['result'] or '{}')
        if data.get('pick'):
            out.append({'no': row['no'], 'pick': data['pick'],
                        'mult': data.get('mult')})
    return out


async def history(user_id: int, limit: int = 10) -> list[dict]:
    """Ставки игрока, по которым раунд уже посчитан."""
    rows = await (await db.conn().execute(
        'SELECT no, pick, bet_cents, multiplier, payout_cents FROM fishing_bets '
        'WHERE user_id = ? AND payout_cents IS NOT NULL '
        'ORDER BY id DESC LIMIT ?', (user_id, limit))).fetchall()
    return [{'no': r['no'], 'pick': r['pick'], 'bet_cents': r['bet_cents'],
             'bet': db.fmt(r['bet_cents']),
             'multiplier': r['multiplier'] or 0.0,
             'payout_cents': r['payout_cents'] or 0,
             'win': db.fmt(r['payout_cents'] or 0)} for r in rows]


async def state(user_id: int, now: float | None = None) -> dict:
    """Всё, что нужно экрану рыбалки. Секреты раскрываются по часам сервера.

    Множители открываются, когда ставки уже закрыты, и это не украшательство:
    колесо от них не зависит, поэтому рыба с видимым x7 вместо x3 давала бы
    отдачу 170%. Так же устроены Crazy Time и Ice Fishing — верхний множитель
    показывают, когда ставить уже нельзя.

    Угол остановки уезжает вместе с множителями, за шесть секунд до неё: клиенту
    нужно куда-то плавно тормозить, а поставить на этот раунд уже нельзя, так что
    выгоды из угла не извлечь. А вот кто победил и с каким множителем — только
    когда колесо встало.
    """
    now = clock() if now is None else now
    no = round_no(now)
    times = plan(no)
    row = await ensure(no)
    if now >= times['stops_at']:
        await settle(no, now)

    result = result_of(no, row['server_seed'])
    revealed = now >= times['closes_at']
    landed = now >= times['stops_at']

    mine, staked, won = {}, 0, 0
    for bet in await _mine(no, user_id):
        mine[bet['pick']] = mine.get(bet['pick'], 0) + bet['bet_cents']
        staked += bet['bet_cents']
        won += bet['payout_cents'] or 0

    return {
        'title': TITLE, 'no': no, 'now': round(now, 3),
        'phase': phase(no, now), 'times': times,
        'round_seconds': ROUND_SECONDS,
        'bet_seconds': config.FISHING_BET_SECONDS,
        'spin_seconds': config.FISHING_SPIN_SECONDS,
        'sectors': SECTORS,
        'positions': positions(result['mults'] if revealed else None, mine),
        'drops': result['drops'] if revealed else [],
        'angle': result['angle'] if revealed else None,
        'landed': {'pick': result['pick'], 'sector': result['sector'],
                   'mult': result['mult']} if landed else None,
        'bet_cents': staked, 'bet': db.fmt(staked),
        'win_cents': won, 'win': db.fmt(won),
        'recent': await recent(), 'history': await history(user_id, 8),
        'fair': {'hash': row['server_seed_hash'],
                 'seed': row['server_seed'] if landed else None},
        'bets': bets(), 'min_bet': config.MIN_BET_CENTS,
        'max_bet': config.MAX_BET_CENTS, 'max_bets': config.FISHING_MAX_BETS,
        'link_mult': round(float(config.FISHING_LINK_MULT), 4),
        'rtp': round(rtp(), 4),
    }
