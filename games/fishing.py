"""Рыбалка: одно колесо на всех, раунд идёт сам.

Как это работает
----------------
Раунд не запускает игрок — раунд идёт всегда. Его номер считается из времени:
`no = int(time.time() // ROUND_SECONDS)`. Поэтому он одинаковый у всех клиентов,
рассылать состояние не нужно, зашедший посреди раунда попадает в тот же раунд, а
перезапуск процесса счёт не сбивает.

    0…10 c   ставки открыты, колесо крутится
    10…16 c  ставки закрыты, иксы рыб открыты, на них падает множитель,
             колесо тормозит
    16…22 c  результат: добычу вытягивают, выплаты уже начислены

Колесо
------
По кругу идёт семь звеньев, рыба, семь звеньев, рыба — и так семь раз: рыбьих
секторов семь, у синей их четыре, у оранжевой два, у красной один. Всего 56
секторов. Звенья через одно белые и серые, рыбьи секторы не покрашены.

Позиций для ставки пять: три рыбы и два цвета звена. Рыба платит свой икс этого
раунда: у синей от ×3 до ×100, у оранжевой до ×200, у красной до ×300 — какой
именно, открывается вместе с закрытием ставок. Цвет платит
config.FISHING_SHADE_MULT, а когда колесо встало на рыбу, ставка на цвет
возвращается: рыбий сектор не белый и не серый, значит цвет в этом раунде не
играл. Без возврата цвет платил бы 83% против 97% у рыб — обещанные ×1.9 иначе
не сходятся ни при какой раскладке колеса.

Множитель
---------
С вероятностью config.FISHING_DROP_CHANCE на рыб падает множитель от ×2 до ×10
и умножает их иксы: оранжевая давала ×20, упал ×2 — стала ×40. Достаётся он
случайной части стаи: иногда одной рыбе, иногда всем трём.

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

# Цвета звеньев — две позиции для ставки. Звенья занимают почти весь круг, и
# цвет играет каждый раунд: белое и серое делят его ровно поровну.
WHITE, GREY = 'white', 'grey'
SHADES = (WHITE, GREY)
SHADE_TITLES = {WHITE: 'Белое звено', GREY: 'Серое звено'}
SHADE_EMOJI = {WHITE: '⚪', GREY: '⚫'}

# Рыбы: минимальный и максимальный икс. Больше задавать нечего — лестница иксов,
# её веса и доля круга выводятся из этих двух чисел. Чем крупнее рыба, тем реже
# её сектор.
FISH = (
    {'key': 'blue',   'title': 'Синяя рыба',     'emoji': '🐟',
     'low': 3,  'high': 100},
    {'key': 'orange', 'title': 'Оранжевая рыба', 'emoji': '🐠',
     'low': 4,  'high': 200},
    {'key': 'red',    'title': 'Красная рыба',   'emoji': '🐡',
     'low': 10, 'high': 300},
)
FISH_BY_KEY = {spec['key']: spec for spec in FISH}
PICKS = tuple([spec['key'] for spec in FISH] + list(SHADES))

# Сколько секторов на колесе у каждой рыбы. На математику это не влияет: доля
# круга у рыбы своя, её секторы делят долю поровну. Но одним сектором синяя
# заняла бы узкую щель, а колесо читается тем лучше, чем чаще на нём попадается
# частая рыба.
FISH_SECTORS = {'blue': 4, 'orange': 2, 'red': 1}

# Сколько ступеней в лестнице иксов рыбы, считая минимум и максимум.
LADDER_STEPS = 7

# Множители, которые падают на рыб: ровно то, что обещано игроку — от ×2 до ×10.
BOOSTS = tuple(range(2, 11))

# Куда падает множитель: случайное непустое подмножество стаи. Их семь, и каждая
# рыба входит в четыре — отсюда вероятность, что множитель достанется именно ей.
DROP_SETS = tuple(
    tuple(spec['key'] for i, spec in enumerate(FISH) if mask >> i & 1)
    for mask in range(1, 1 << len(FISH)))

ROUND_SECONDS = (config.FISHING_BET_SECONDS + config.FISHING_SPIN_SECONDS
                 + config.FISHING_RESULT_SECONDS)

# --- иксы рыб ---------------------------------------------------------------


def _ladder(low: int, high: int) -> tuple[int, ...]:
    """Лестница иксов рыбы: LADDER_STEPS ступеней от low до high.

    Шаг геометрический, а не одинаковый: лестница читается как ×3, ×5, ×10, ×17,
    ×31, ×56, ×100, а не «плюс шестнадцать». Края точные — минимум и максимум это
    то, что обещано игроку.
    """
    ratio = (high / low) ** (1.0 / (LADDER_STEPS - 1))
    out: list[int] = []
    for i in range(LADDER_STEPS):
        value = int(round(low * ratio ** i))
        out.append(max(value, out[-1] + 1) if out else value)
    out[0], out[-1] = int(low), int(high)
    if any(a >= b for a, b in zip(out, out[1:])):
        raise ValueError(f'лестница иксов не растёт: {out}')
    return tuple(out)


def ladder(key: str) -> tuple[int, ...]:
    """Лестница иксов этой рыбы."""
    spec = FISH_BY_KEY[key]
    return _ladder(spec['low'], spec['high'])


def _weights(values: tuple[int, ...]) -> tuple[float, ...]:
    """Вероятности ступеней — обратные их иксам.

    Тогда p * v у всех ступеней одинаково: каждая приносит в кассу столько же,
    сколько остальные. Поэтому ×3 выпадает часто, ×100 редко, и веса не нужно
    подбирать руками — их задаёт сама лестница.
    """
    total = sum(1.0 / value for value in values)
    return tuple((1.0 / value) / total for value in values)


def _mean(values: tuple[int, ...]) -> float:
    """Средний икс при таких весах — среднее гармоническое лестницы."""
    return len(values) / sum(1.0 / value for value in values)


def _at(values: tuple[int, ...], roll: float) -> int:
    """Ступень по числу из потока: в чей вес попал roll, та и выпала."""
    edge = 0.0
    for value, weight in zip(values, _weights(values)):
        edge += weight
        if roll < edge:
            return value
    return values[-1]


BOOST_MEAN = _mean(BOOSTS)


def drop_share(key: str) -> float:
    """Вероятность, что в этом раунде множитель упадёт на эту рыбу.

    Сам множитель падает с вероятностью config.FISHING_DROP_CHANCE, а достаётся
    случайному непустому подмножеству стаи: конкретной рыбе — в четырёх наборах
    из семи.
    """
    hits = sum(1 for target in DROP_SETS if key in target)
    return config.FISHING_DROP_CHANCE * hits / len(DROP_SETS)


def average(key: str) -> float:
    """Средний икс позиции — столько она платит, когда колесо встало на неё.

    У рыбы это её лестница, поднятая множителем. Множитель падает не всегда,
    поэтому лестница умножается на 1 + p * (среднее множителя − 1).
    """
    if key in SHADES:
        return float(config.FISHING_SHADE_MULT)
    return _mean(ladder(key)) * (1.0 + drop_share(key) * (BOOST_MEAN - 1.0))


# --- колесо -----------------------------------------------------------------
#
# Ставка платит, только если колесо встало на её сектор, поэтому отдача равна
# p * m. Иксы рыб заданы правилами игры, значит свободна ровно одна величина —
# доля круга:
#
#     p = RTP / m
#
# Поэтому в игре нет ни одной подобранной руками константы: поменяли максимум
# рыбы, число ступеней или частоту множителей — доля круга подстроилась сама, а
# отдача осталась равной config.RTP. Остаток круга забирают звенья, и он же даёт
# кассе маржу: на звене все ставки на рыб проигрывают.


def fish_share(key: str) -> float:
    """Доля круга у рыбы. Выведена из отдачи, а не задана."""
    return config.RTP / average(key)


def links_share() -> float:
    """Сколько круга осталось звеньям — всё, что не забрали рыбы."""
    left = 1.0 - sum(fish_share(spec['key']) for spec in FISH)
    if left <= 0.0:
        raise ValueError('рыбам не хватает круга: с такими иксами отдача выше '
                         'RTP ещё до того, как на колесо встали звенья')
    return left


def _fish_order() -> tuple[str, ...]:
    """В каком порядке рыбы идут по кругу: частые вразбег, редкая посередине.

    Мест семь, у синей из них четыре, у оранжевой два, у красной одно.
    Раскладываются по наибольшему остатку, а не блоками, поэтому получается
    синяя, оранжевая, синяя, красная, синяя, оранжевая, синяя — двух одинаковых
    рыб подряд на колесе нет.
    """
    slots = sum(FISH_SECTORS[spec['key']] for spec in FISH)
    credit = {spec['key']: 0.0 for spec in FISH}
    out = []
    for _ in range(slots):
        for key in credit:
            credit[key] += FISH_SECTORS[key] / slots
        key = max(credit, key=lambda k: (credit[k], FISH_SECTORS[k]))
        credit[key] -= 1.0
        out.append(key)
    return tuple(out)


def _rim() -> tuple[str, ...]:
    """Порядок секторов по кругу: семь звеньев, рыба, семь звеньев, рыба.

    Цвета идут через одно, и счёт не сбрасывается на рыбе — поэтому чередование
    не рвётся нигде по кругу. Звеньев 49 на семь рыбьих секторов, число
    нечётное, так что одного цвета на колесе на одно больше.
    """
    out, step = [], 0
    for key in _fish_order():
        for _ in range(max(1, config.FISHING_LINKS_PER_FISH)):
            out.append(SHADES[step % len(SHADES)])
            step += 1
        out.append(key)
    return tuple(out)


def _build_sectors() -> list[dict]:
    """Колесо: сектор, его доля круга и угол от метки сверху по часовой.

    Доля рыбы делится между её секторами поровну, поэтому ставка выигрывает на
    любом из них, а вероятность у неё одна. Цвета делят круг не по числу
    звеньев, а по углу: их 25 и 24, поэтому доля цвета делится между своими
    звеньями поровну, и белое с серым платят одинаково. Разница в ширине звеньев
    из-за этого — четверть градуса, на глаз её не видно, а вот отдачу она
    разводила бы на четыре процента.
    """
    rim = _rim()
    half = links_share() / len(SHADES)
    share = {spec['key']: fish_share(spec['key']) / rim.count(spec['key'])
             for spec in FISH}
    for shade in SHADES:
        share[shade] = half / rim.count(shade)
    out, angle = [], 0.0
    for index, key in enumerate(rim):
        start = round(angle, 4)
        angle += 360.0 * share[key]
        # Ширина считается как разница уже округлённых границ, а не округляется
        # сама: иначе на 56 секторах круг не сходился бы на пару тысячных.
        end = round(angle, 4) if index < len(rim) - 1 else 360.0
        out.append({'index': index, 'pick': key, 'weight': share[key],
                    'from': start, 'width': round(end - start, 4)})
    return out


SECTORS = _build_sectors()


def chance(key: str) -> float:
    """Вероятность, что колесо встанет на эту позицию."""
    return sum(s['weight'] for s in SECTORS if s['pick'] == key)


def rtp_of(key: str) -> float:
    """Фактическая отдача ставки на эту позицию.

    У рыб она равна config.RTP — из неё же выведена их доля круга. У цвета чуть
    ниже: ×1.9 платит только половина звеньев, зато на рыбе ставка возвращается.
    Возврат её и держит: без него у цвета выходило бы 83%.
    """
    if key in SHADES:
        return (chance(key) * config.FISHING_SHADE_MULT
                + sum(chance(spec['key']) for spec in FISH))
    return chance(key) * average(key)


def rtp() -> float:
    """Отдача колеса — по худшей из позиций, чтобы не обещать лишнего."""
    return min(rtp_of(key) for key in PICKS)


def payout_mult(pick: str, result: dict) -> float:
    """Чем закончилась ставка на позицию: множитель к ставке.

    Ставка на цвет возвращается, когда колесо встало на рыбу: рыбий сектор не
    белый и не серый, значит цвет в этом раунде не играл.
    """
    if pick == result['pick']:
        return float(result['mults'][pick])
    if pick in SHADES and result['pick'] in FISH_BY_KEY:
        return 1.0
    return 0.0


def positions(mults: dict | None = None, bases: dict | None = None,
              mine: dict | None = None) -> list[dict]:
    """Позиции для ставки — в том порядке, в каком они стоят на экране."""
    out = []
    for spec in FISH:
        steps = ladder(spec['key'])
        out.append({'key': spec['key'], 'title': spec['title'],
                    'emoji': spec['emoji'], 'kind': 'fish',
                    'floor': float(steps[0]), 'top': float(steps[-1]),
                    'ladder': [float(value) for value in steps]})
    shade_mult = round(float(config.FISHING_SHADE_MULT), 4)
    for shade in SHADES:
        out.append({'key': shade, 'title': SHADE_TITLES[shade],
                    'emoji': SHADE_EMOJI[shade], 'kind': 'shade',
                    'floor': shade_mult, 'top': shade_mult, 'ladder': []})
    for item in out:
        item['chance'] = round(chance(item['key']), 4)
        item['rtp'] = round(rtp_of(item['key']), 4)
        # До закрытия ставок на экране стоит минимум позиции: икс раунда ещё не
        # раскрыт. base — икс до множителя, mult — то, что реально заплатит.
        item['base'] = (bases or {}).get(item['key'], item['floor'])
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
    внутри сектора, по числу на икс каждой рыбы, затем три числа на множитель —
    упал ли он, на кого и какой. Эти три берутся всегда, даже когда множитель не
    упал: так раздачу проще пересчитать руками по раскрытому сиду.
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

    # Икс раунда у каждой рыбы — ступень её лестницы.
    bases = {spec['key']: float(_at(ladder(spec['key']), next(stream)))
             for spec in FISH}

    mults, drops = dict(bases), []
    fell, whom, which = next(stream), next(stream), next(stream)
    if fell < config.FISHING_DROP_CHANCE:
        boost = _at(BOOSTS, which)
        for key in DROP_SETS[min(int(whom * len(DROP_SETS)),
                                 len(DROP_SETS) - 1)]:
            mults[key] = bases[key] * boost
            drops.append({'fish': key, 'boost': boost, 'mult': mults[key]})
    for shade in SHADES:
        mults[shade] = round(float(config.FISHING_SHADE_MULT), 4)

    closes = plan(no)['closes_at']
    for i, drop in enumerate(drops):
        # Когда множитель показывать. Время общее для всех, поэтому на всех
        # экранах он падает одновременно.
        drop['show_at'] = closes + 0.7 + 1.0 * i

    return {'sector': sector['index'], 'pick': sector['pick'],
            # Насколько повернуть колесо, чтобы метка сверху пришлась на mark.
            'angle': round((360.0 - mark) % 360.0, 3),
            'mult': mults[sector['pick']], 'mults': mults, 'bases': bases,
            'drops': drops}

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

    Сколько платит позиция, решает payout_mult: рыба — свой икс этого раунда,
    цвет — ×1.9, а когда выпала рыба, ставка на цвет возвращается множителем 1.
    Проигравшие закрываются множителем 0: раунд в rounds должен закрыться так же,
    как выигравший, иначе он висел бы 'active' и его подобрал бы возврат.
    """
    rows = await (await db.conn().execute(
        'SELECT id, user_id, pick, round_id FROM fishing_bets '
        'WHERE no = ? AND payout_cents IS NULL ORDER BY id', (no,))).fetchall()
    for bet in rows:
        mult = payout_mult(bet['pick'], result)
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

    Иксы рыб открываются, когда ставки уже закрыты, и это не украшательство:
    колесо от них не зависит, поэтому рыба с видимым ×100 на своей доле круга
    давала бы отдачу 640%. Так же устроены Crazy Time и Ice Fishing — верхний
    множитель показывают, когда ставить уже нельзя.

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
        'positions': positions(result['mults'] if revealed else None,
                               result['bases'] if revealed else None, mine),
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
        'shade_mult': round(float(config.FISHING_SHADE_MULT), 4),
        'links_per_fish': max(1, config.FISHING_LINKS_PER_FISH),
        'drop_chance': round(float(config.FISHING_DROP_CHANCE), 4),
        'boosts': [float(value) for value in BOOSTS],
        'rtp': round(rtp(), 4),
        'rtp_by_pick': {key: round(rtp_of(key), 4) for key in PICKS},
    }
