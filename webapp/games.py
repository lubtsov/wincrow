"""Игры Mini App: монетка, рулетка, мины, башня, краш и блэкджек.

Своей математики здесь нет ни строки. Коэффициенты, раскладки и правила берутся
из тех же модулей `games/*`, которыми играет бот, ставку снимает
`engine.start_round`, выплату считает `engine.finish` — раунды уезжают в ту же
таблицу `rounds`, поэтому игра из приложения видна в истории, в профиле, в ТОП-10
и в RTP админки. Отличается только интерфейс: вместо переписываемого сообщения
Telegram — экран, который сам ничего не решает.

Дайс-игр здесь нет намеренно: кубик, мяч и дартс кидает Telegram в чате, и
подделать эту анимацию в webview значило бы показать игроку не тот бросок, по
которому считаются деньги. Их место — бот.

Что клиент не получает никогда: раскладку мин, плохие двери башни, точку краша и
закрытую карту дилера. Всё это лежит в состоянии раунда на сервере и уезжает
клиенту только вместе с развязкой.
"""

import time

import aiosqlite

import config
import db
from db import fmt
from games import blackjack, coin, crash, engine, mines, roulette, tower

# Ключ игры -> человеческое имя и значок. Порядок — порядок карточек на экране.
CATALOG: tuple[dict, ...] = (
    {'key': 'crash', 'title': 'Краш', 'emoji': '📈',
     'note': 'Множитель растёт, пока не заберёшь'},
    {'key': 'mines', 'title': 'Мины', 'emoji': '💎',
     'note': 'Поле 5×5, мины выбираешь сам'},
    {'key': 'tower', 'title': 'Башня', 'emoji': '🗼',
     'note': 'Три двери на этаж, за одной обрыв'},
    {'key': 'blackjack', 'title': 'Блэкджек', 'emoji': '🃏',
     'note': 'Дилер добирает на мягкой 17'},
    {'key': 'roulette', 'title': 'Рулетка', 'emoji': '🎡',
     'note': 'Европейская, 37 чисел'},
    {'key': 'coin', 'title': 'Монетка', 'emoji': '🪙',
     'note': f'Орёл или решка, ×{coin.MULT:.2f}'},
)

KEYS = tuple(spec['key'] for spec in CATALOG)

# Игры, у которых раунд живёт между запросами: их активный раунд надо
# восстанавливать при входе на экран.
STEPPED = ('mines', 'tower', 'crash', 'blackjack')

# --- краш -------------------------------------------------------------------
#
# В боте множитель тикает в живом сообщении раз в секунду. Здесь тикать нечему:
# HTTP-запрос приходит только когда игрок нажал «Забрать», поэтому множитель —
# функция серверного времени, а точка краша посчитана из сида при старте:
#
#     m(t) = GROWTH ** (t / TICK),  сорвался, если m(t) >= точка краша
#
# Клиент рисует то же самое по своим часам, но с задержкой CRASH_LAG: его цифра
# всегда чуть отстаёт от серверной, и выплата не может оказаться меньше
# показанной. Обратный порядок был бы обманом — игрок видит ×2.00, забирает, а
# сервер уже досчитал до срыва.

CRASH_LAG = 0.25            # на сколько отстаёт цифра на экране, секунды


def crash_multiplier(started_at: float, at: float | None = None) -> float:
    """Множитель краша на момент `at` (по умолчанию — сейчас)."""
    elapsed = max(0.0, (time.time() if at is None else at) - started_at)
    grown = crash.GROWTH ** (elapsed / crash.TICK)
    return min(round(grown, 4), crash.MAX_MULT)


def _crash_point(rnd: engine.Round) -> float:
    """Точка срыва из provably fair потока — та же формула, что в боте."""
    u = rnd.rnd()
    return round(config.RTP / (1 - u), 4) if u < 1 else crash.MAX_MULT


# --- пресеты ставок ---------------------------------------------------------

def bets() -> list[int]:
    """Кнопки ставок — те же границы, что у остальных игр."""
    return [c for c in (10, 20, 50, 100, 250, 500, 1000, 2500)
            if config.MIN_BET_CENTS <= c <= config.MAX_BET_CENTS]


def _bad_bet(bet_cents: int) -> bool:
    return not config.MIN_BET_CENTS <= bet_cents <= config.MAX_BET_CENTS

# --- как раунд выглядит для клиента -----------------------------------------

def _base(rnd: engine.Round, game: str) -> dict:
    return {'game': game, 'round_id': rnd.id, 'bet_cents': rnd.bet_cents,
            'bet': fmt(rnd.bet_cents)}


def _done(rnd: engine.Round, game: str, *, multiplier: float,
          payout_cents: int, view: dict) -> dict:
    """Закрытый раунд: множитель, выплата и всё, что можно показать.

    Итоговые числа кладутся ПОСЛЕ полей вида: у мин и краша во «виде» лежит свой
    множитель — тот, что был на экране до развязки, — и он не должен подменять
    множитель, по которому реально заплатили.
    """
    out = dict(_base(rnd, game))
    out.update(view)
    out.update(status='done', multiplier=round(multiplier, 4),
               payout_cents=payout_cents, win=fmt(payout_cents))
    return out


def _active(rnd: engine.Round, game: str, view: dict) -> dict:
    """Раунд в процессе. Секреты сюда не попадают — только видимое поле."""
    out = dict(_base(rnd, game))
    out.update(view)
    out['status'] = 'active'
    return out


def mines_view(rnd: engine.Round) -> dict:
    """Поле мин без самих мин: клиенту видно только открытое."""
    n, opened = rnd.state['n'], list(rnd.state['opened'])
    now = mines.multiplier(n, len(opened))
    safe_left = mines.CELLS - n - len(opened)
    return {'cells': mines.CELLS, 'side': mines.SIDE, 'n': n, 'opened': opened,
            'multiplier': round(now, 4) if opened else 1.0,
            'cash_cents': engine.payout_cents(rnd.bet_cents, now) if opened else 0,
            'next_multiplier': round(mines.multiplier(n, len(opened) + 1), 4),
            'safe_left': safe_left}


def tower_view(rnd: engine.Round) -> dict:
    level = rnd.state['level']
    now = tower.multiplier(level)
    return {'doors': tower.DOORS, 'floors': tower.FLOORS, 'level': level,
            'multiplier': round(now, 4) if level else 1.0,
            'cash_cents': engine.payout_cents(rnd.bet_cents, now) if level else 0,
            'next_multiplier': round(tower.multiplier(level + 1), 4),
            'ladder': [round(tower.multiplier(f), 4)
                       for f in range(1, tower.FLOORS + 1)]}

def crash_view(rnd: engine.Round) -> dict:
    """Живой краш: клиент получает время старта, а не точку срыва."""
    started = float(rnd.state['at'])
    return {'started_at': started, 'now': time.time(),
            'growth': crash.GROWTH, 'tick': crash.TICK,
            'lag': CRASH_LAG, 'max_multiplier': crash.MAX_MULT,
            'multiplier': crash_multiplier(started)}


def blackjack_view(rnd: engine.Round, deck: list[int], *,
                   reveal: bool = False) -> dict:
    """Стол. Пока раздача идёт, вторая карта дилера клиенту не уезжает."""
    player = blackjack._cards(rnd, deck, 'p')
    dealer = blackjack._cards(rnd, deck, 'd')
    shown = dealer if reveal else dealer[:1]
    balance_note = len(rnd.state['p']) == 2 and not rnd.state['doubled']
    return {
        'player': [blackjack.card(c) for c in player],
        'dealer': [blackjack.card(c) for c in shown],
        'player_total': blackjack.hand_value(player)[0],
        'dealer_total': blackjack.hand_value(shown)[0] if reveal
                        else blackjack.hand_value(dealer[:1])[0],
        'player_soft': blackjack.hand_value(player)[1],
        'hidden': 0 if reveal else max(0, len(dealer) - 1),
        'doubled': bool(rnd.state['doubled']),
        'can_double': balance_note,
        'blackjack': blackjack.is_blackjack(player),
    }


async def _fair(user_id: int) -> dict:
    """Хеш серверного сида и nonce — чтобы честность проверялась и из игры."""
    row = await (await db.conn().execute(
        'SELECT server_seed_hash, client_seed, nonce FROM seeds WHERE user_id = ?',
        (user_id,))).fetchone()
    if row is None:
        return {}
    return {'server_seed_hash': row['server_seed_hash'],
            'client_seed': row['client_seed'], 'nonce': row['nonce']}

# --- разбор ставки на исход -------------------------------------------------

def _coin_side(pick: str | None) -> str | None:
    return pick if pick in coin.SIDES else None


def _roulette_bet(pick: str | None) -> tuple[str, int | None, float] | None:
    """'red' -> ('red', None, 2.0); '17' -> ('straight', 17, 36.0)."""
    if not pick:
        return None
    if pick in roulette.BETS:
        return pick, None, roulette.BETS[pick][1]
    if pick.isdigit() and 0 <= int(pick) < roulette.POCKETS:
        return 'straight', int(pick), roulette.STRAIGHT_MULT
    return None


def _mines_count(pick: str | None) -> int | None:
    if not pick or not pick.isdigit():
        return None
    n = int(pick)
    return n if 1 <= n <= mines.CELLS - 1 else None


async def _round_by_client(user_id: int, game: str,
                           client_id: str) -> aiosqlite.Row | None:
    return await (await db.conn().execute(
        'SELECT * FROM rounds WHERE user_id = ? AND game = ? AND client_id = ?',
        (user_id, game, client_id))).fetchone()


async def history(user_id: int, limit: int = 8) -> list[dict]:
    """Последние раунды игр приложения — общая лента для экрана."""
    marks = ','.join('?' * len(KEYS))
    rows = await (await db.conn().execute(
        f'SELECT id, game, bet_cents, multiplier, payout_cents, status '
        f'FROM rounds WHERE user_id = ? AND game IN ({marks}) '
        f'AND status != "active" ORDER BY id DESC LIMIT ?',
        (user_id, *KEYS, limit))).fetchall()
    titles = {spec['key']: spec for spec in CATALOG}
    return [{'round_id': r['id'], 'game': r['game'],
             'title': titles[r['game']]['title'],
             'emoji': titles[r['game']]['emoji'],
             'bet': fmt(r['bet_cents']), 'bet_cents': r['bet_cents'],
             'multiplier': r['multiplier'] or 0.0,
             'payout_cents': r['payout_cents'] or 0,
             'win': fmt(r['payout_cents'] or 0), 'status': r['status']}
            for r in rows]

# --- старт раунда -----------------------------------------------------------

async def start(user_id: int, game: str, bet_cents: int, client_id: str,
                pick: str | None = None) -> tuple[dict, str]:
    """Начинает раунд. (раунд для клиента, статус).

    Статусы: 'ok', 'repeat', 'busy', 'bad_game', 'bad_pick', 'bad_bet',
    'no_money'. Повторный запрос с тем же `client_id` не снимает ставку второй
    раз: по колонке стоит уникальный индекс, и второй INSERT падает внутри той
    же транзакции, в которой списаны деньги.
    """
    if game not in KEYS:
        return {}, 'bad_game'
    if _bad_bet(bet_cents) or not client_id or len(client_id) > 64:
        return {}, 'bad_bet'

    prev = await _round_by_client(user_id, game, client_id)
    if prev is not None:
        view = await restore(user_id, game)
        return (view, 'repeat') if view else ({}, 'repeat')

    side = number = count = None
    kind = None
    if game == 'coin':
        side = _coin_side(pick)
        if side is None:
            return {}, 'bad_pick'
    elif game == 'roulette':
        parsed = _roulette_bet(pick)
        if parsed is None:
            return {}, 'bad_pick'
        kind, number, _ = parsed
    elif game == 'mines':
        count = _mines_count(pick)
        if count is None:
            return {}, 'bad_pick'

    # Два одновременных раунда одной пошаговой игры — путаница на экране и в
    # деньгах: у игрока остался бы висеть раунд, до которого он не дойдёт.
    if game in STEPPED and await engine.active_round(user_id, game) is not None:
        return {}, 'busy'

    try:
        rnd = await engine.start_round(user_id, game, bet_cents,
                                       client_id=client_id)
    except aiosqlite.IntegrityError:
        view = await restore(user_id, game)
        return (view, 'repeat') if view else ({}, 'repeat')
    if rnd is None:
        return {}, 'no_money'

    return await _open(rnd, game, side=side, kind=kind, number=number,
                       count=count)

async def _open(rnd: engine.Round, game: str, *, side: str | None,
                kind: str | None, number: int | None,
                count: int | None) -> tuple[dict, str]:
    """Разыгрывает только что открытый раунд до первой остановки."""
    if game == 'coin':
        result = 'heads' if rnd.pick(2) == 0 else 'tails'
        rnd.state = {'pick': side, 'result': result}
        mult = coin.MULT if result == side else 0.0
        payout = await engine.finish(rnd, mult)
        if payout is None:
            return {}, 'busy'
        return _done(rnd, game, multiplier=mult, payout_cents=payout,
                     view={'pick': side, 'result': result,
                           'emoji': coin.SIDES[result][0],
                           'name': coin.SIDES[result][1],
                           'won': payout > 0}), 'ok'

    if game == 'roulette':
        spun = rnd.pick(roulette.POCKETS)
        if kind == 'straight':
            won, mult = spun == number, roulette.STRAIGHT_MULT
            label = f'число {number}'
        else:
            label, mult, test = roulette.BETS[kind]
            won = test(spun)
        rnd.state = {'kind': kind, 'number': number, 'result': spun}
        payout = await engine.finish(rnd, mult if won else 0.0)
        if payout is None:
            return {}, 'busy'
        return _done(rnd, game, multiplier=mult if won else 0.0,
                     payout_cents=payout,
                     view={'number': spun, 'color': roulette.color_emoji(spun),
                           'describe': roulette.describe(spun), 'bet_kind': kind,
                           'bet_number': number, 'bet_label': label,
                           'won': payout > 0}), 'ok'

    if game == 'mines':
        rnd.state = {'n': count, 'mines': sorted(rnd.sample(mines.CELLS, count)),
                     'opened': []}
        await engine.save_state(rnd)
        return _active(rnd, game, mines_view(rnd)), 'ok'

    if game == 'tower':
        rnd.state = {'level': 0,
                     'bad': [rnd.pick(tower.DOORS) for _ in range(tower.FLOORS)],
                     'shown': []}
        await engine.save_state(rnd)
        return _active(rnd, game, tower_view(rnd)), 'ok'

    if game == 'crash':
        rnd.state = {'point': _crash_point(rnd), 'at': time.time()}
        await engine.save_state(rnd)
        # Точка ниже единицы — мгновенный краш (те самые 3% преимущества
        # казино). Показывать по нему растущую цифру нельзя: раунд уже мёртв.
        if crash_multiplier(rnd.state['at']) >= rnd.state['point']:
            return await _step_crash(rnd, 'c')
        return _active(rnd, game, crash_view(rnd)), 'ok'

    return await _deal_blackjack(rnd)

async def _deal_blackjack(rnd: engine.Round) -> tuple[dict, str]:
    """Первая раздача. Блэкджек на руках решает раунд сразу."""
    deck = blackjack._deck(rnd)
    # Раздача по кругу: первая и третья карты игроку, вторая и четвёртая дилеру.
    rnd.state = {'p': [0, 2], 'd': [1, 3], 'cur': 4, 'doubled': False}
    player = blackjack._cards(rnd, deck, 'p')
    dealer = blackjack._cards(rnd, deck, 'd')
    if blackjack.is_blackjack(player) or blackjack.is_blackjack(dealer):
        return await _settle_blackjack(rnd, deck)
    await engine.save_state(rnd)
    return _active(rnd, 'blackjack', blackjack_view(rnd, deck)), 'ok'


async def _settle_blackjack(rnd: engine.Round,
                            deck: list[int]) -> tuple[dict, str]:
    """Дилер добирает, руки сравниваются — правилами из games/blackjack.py."""
    blackjack.dealer_draws(rnd, deck)
    player = blackjack._cards(rnd, deck, 'p')
    dealer = blackjack._cards(rnd, deck, 'd')
    mult, code = blackjack.outcome(player, dealer)

    if mult is None:                      # пуш: ставка возвращается целиком
        if not await engine.void(rnd):
            return {}, 'busy'
        view = dict(blackjack_view(rnd, deck, reveal=True), outcome=code,
                    push=True)
        return _done(rnd, 'blackjack', multiplier=1.0,
                     payout_cents=rnd.bet_cents, view=view), 'ok'

    payout = await engine.finish(rnd, mult)
    if payout is None:
        return {}, 'busy'
    view = dict(blackjack_view(rnd, deck, reveal=True), outcome=code, push=False)
    return _done(rnd, 'blackjack', multiplier=mult, payout_cents=payout,
                 view=view), 'ok'

# --- шаги --------------------------------------------------------------------

async def step(user_id: int, game: str, round_id: int,
               action: str) -> tuple[dict, str]:
    """Шаг в открытом раунде. (раунд для клиента, статус).

    Статусы: 'ok', 'gone' (раунд закрыт или чужой), 'bad_game', 'bad_action',
    'no_money' (не хватило на удвоение).

    Раунд ищется по id и по игроку (`engine.load_round`), поэтому чужой раунд
    или раунд из прошлой сессии шагнуть нельзя.
    """
    if game not in STEPPED:
        return {}, 'bad_game'
    rnd = await engine.load_round(round_id, user_id, game)
    if rnd is None:
        return {}, 'gone'

    if game == 'mines':
        return await _step_mines(rnd, action)
    if game == 'tower':
        return await _step_tower(rnd, action)
    if game == 'crash':
        return await _step_crash(rnd, action)
    return await _step_blackjack(rnd, action)


async def _cash(rnd: engine.Round, game: str, mult: float,
                view: dict) -> tuple[dict, str]:
    payout = await engine.finish(rnd, mult)
    if payout is None:
        return {}, 'gone'
    return _done(rnd, game, multiplier=mult, payout_cents=payout,
                 view=dict(view, won=payout > 0)), 'ok'


async def _step_mines(rnd: engine.Round, action: str) -> tuple[dict, str]:
    opened = list(rnd.state['opened'])
    field = set(rnd.state['mines'])

    if action == 'c':
        if not opened:
            return {}, 'bad_action'
        mult = mines.multiplier(rnd.state['n'], len(opened))
        return await _cash(rnd, 'mines', mult,
                           dict(mines_view(rnd), mines=sorted(field), hit=None))

    if not action.isdigit():
        return {}, 'bad_action'
    cell = int(action)
    if not 0 <= cell < mines.CELLS or cell in opened:
        return {}, 'bad_action'

    if cell in field:
        rnd.state['hit'] = cell
        payout = await engine.finish(rnd, 0.0)
        if payout is None:
            return {}, 'gone'
        return _done(rnd, 'mines', multiplier=0.0, payout_cents=0,
                     view=dict(mines_view(rnd), mines=sorted(field), hit=cell,
                               won=False)), 'ok'

    opened.append(cell)
    rnd.state['opened'] = opened
    await engine.save_state(rnd)

    # Все безопасные клетки открыты — забирать больше нечего, максимум взят.
    if len(opened) == mines.CELLS - rnd.state['n']:
        mult = mines.multiplier(rnd.state['n'], len(opened))
        return await _cash(rnd, 'mines', mult,
                           dict(mines_view(rnd), mines=sorted(field), hit=None,
                                cleared=True))
    return _active(rnd, 'mines', mines_view(rnd)), 'ok'

async def _step_tower(rnd: engine.Round, action: str) -> tuple[dict, str]:
    level = rnd.state['level']
    bad = list(rnd.state['bad'])

    if action == 'c':
        if not level:
            return {}, 'bad_action'
        return await _cash(rnd, 'tower', tower.multiplier(level),
                           dict(tower_view(rnd), bad=bad, fell=None))

    if not action.isdigit():
        return {}, 'bad_action'
    door = int(action)
    if not 0 <= door < tower.DOORS:
        return {}, 'bad_action'

    if door == bad[level]:
        rnd.state['fell'] = door
        payout = await engine.finish(rnd, 0.0)
        if payout is None:
            return {}, 'gone'
        return _done(rnd, 'tower', multiplier=0.0, payout_cents=0,
                     view=dict(tower_view(rnd), bad=bad, fell=door,
                               floor=level + 1, won=False)), 'ok'

    rnd.state['level'] = level + 1
    rnd.state['shown'] = list(rnd.state.get('shown', [])) + [door]
    await engine.save_state(rnd)

    # Верхний этаж: выше идти некуда, раунд закрывается сам.
    if rnd.state['level'] >= tower.FLOORS:
        return await _cash(rnd, 'tower', tower.multiplier(tower.FLOORS),
                           dict(tower_view(rnd), bad=bad, fell=None,
                                cleared=True))
    return _active(rnd, 'tower', tower_view(rnd)), 'ok'


async def _step_crash(rnd: engine.Round, action: str) -> tuple[dict, str]:
    """Забрать или узнать, что уже сорвалось. Множитель считает только сервер.

    'c' — забрать, 'p' — только спросить. Второе нужно живому экрану: клиент
    рисует растущую цифру по своим часам, но узнать про срыв может лишь у
    сервера, а спрашивать «забрать» ради этого нельзя.
    """
    if action not in ('c', 'p'):
        return {}, 'bad_action'
    point = float(rnd.state['point'])
    mult = crash_multiplier(float(rnd.state['at']))

    if mult >= point:
        payout = await engine.finish(rnd, 0.0)
        if payout is None:
            return {}, 'gone'
        return _done(rnd, 'crash', multiplier=0.0, payout_cents=0,
                     view=dict(crash_view(rnd), point=point, crashed=True,
                               won=False)), 'ok'

    if action == 'p':
        return _active(rnd, 'crash', crash_view(rnd)), 'ok'

    return await _cash(rnd, 'crash', mult,
                       dict(crash_view(rnd), point=point, crashed=False,
                            taken=mult))

async def _step_blackjack(rnd: engine.Round, action: str) -> tuple[dict, str]:
    deck = blackjack._deck(rnd)

    if action == 's':
        return await _settle_blackjack(rnd, deck)

    if action == 'd':
        if len(rnd.state['p']) != 2 or rnd.state['doubled']:
            return {}, 'bad_action'
        if not await engine.raise_stake(rnd, rnd.bet_cents):
            return {}, 'no_money'
        rnd.state['doubled'] = True
        blackjack._draw(rnd, 'p')            # ровно одна карта и останов
        return await _settle_blackjack(rnd, deck)

    if action != 'h':
        return {}, 'bad_action'

    blackjack._draw(rnd, 'p')
    total = blackjack.hand_value(blackjack._cards(rnd, deck, 'p'))[0]
    if total >= 21:
        # На 21 добирать бессмысленно, на переборе — нечего. Дилер играет сам.
        return await _settle_blackjack(rnd, deck)
    await engine.save_state(rnd)
    return _active(rnd, 'blackjack', blackjack_view(rnd, deck)), 'ok'


# --- возвращение на экран ----------------------------------------------------

async def restore(user_id: int, game: str) -> dict:
    """Активный раунд игры, если он есть. {} — начинать заново.

    Нужно на входе в игру: webview закрывается вместе со свёрнутым Telegram, а
    мины и башня живут между запросами. Заодно здесь добивается краш, который
    сорвался, пока игрока не было: висящий активный раунд иначе не даст начать
    новый.
    """
    if game not in STEPPED:
        return {}
    rnd = await engine.active_round(user_id, game)
    if rnd is None:
        return {}

    if game == 'mines':
        return _active(rnd, game, mines_view(rnd))
    if game == 'tower':
        return _active(rnd, game, tower_view(rnd))
    if game == 'blackjack':
        return _active(rnd, game, blackjack_view(rnd, blackjack._deck(rnd)))

    point = float(rnd.state['point'])
    if crash_multiplier(float(rnd.state['at'])) < point:
        return _active(rnd, game, crash_view(rnd))
    # Раунд сорвался, пока игрока не было: закрываем его и не показываем как
    # живой — иначе экран предложит забрать то, чего уже нет, а начать новый
    # раунд не даст висящий активный.
    await _step_crash(rnd, 'c')
    return {}

async def screen(user_id: int) -> dict:
    """Всё, что нужно экрану игр: каталог, лимиты, незакрытые раунды, история."""
    row = await db.get_user(user_id)
    balance = row['balance_cents'] if row is not None else 0
    active = {}
    for key in STEPPED:
        view = await restore(user_id, key)
        if view:
            active[key] = view
    return {
        'games': [dict(spec) for spec in CATALOG],
        'bets': bets(),
        'min_bet': config.MIN_BET_CENTS, 'max_bet': config.MAX_BET_CENTS,
        'rtp': config.RTP,
        'balance_cents': balance, 'balance': fmt(balance),
        'active': active,
        'history': await history(user_id),
        'fair': await _fair(user_id),
        'rules': rules(),
    }


def rules() -> dict:
    """Короткие правила и числа, которые экран показывает вместо простыни."""
    return {
        'coin': {'multiplier': round(coin.MULT, 2),
                 'sides': [{'key': key, 'emoji': emoji, 'name': name}
                           for key, (emoji, name) in coin.SIDES.items()]},
        'roulette': {'straight': roulette.STRAIGHT_MULT,
                     'pockets': roulette.POCKETS,
                     'red': sorted(roulette.RED),
                     'bets': [{'key': key, 'label': label, 'multiplier': mult}
                              for key, (label, mult, _) in roulette.BETS.items()]},
        'mines': {'cells': mines.CELLS, 'side': mines.SIDE,
                  'choices': [{'n': n, 'first': round(mines.multiplier(n, 1), 2)}
                              for n in mines.MINE_CHOICES]},
        'tower': {'doors': tower.DOORS, 'floors': tower.FLOORS,
                  'ladder': [round(tower.multiplier(f), 2)
                             for f in range(1, tower.FLOORS + 1)]},
        'crash': {'growth': crash.GROWTH, 'tick': crash.TICK,
                  'max_multiplier': crash.MAX_MULT, 'lag': CRASH_LAG},
        'blackjack': {'win': blackjack.WIN_MULT, 'blackjack': blackjack.BJ_MULT,
                      'decks': blackjack.DECKS, 'stand': blackjack.DEALER_STAND},
    }
