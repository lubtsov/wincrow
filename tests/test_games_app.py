"""Игры Mini App: деньги, секреты и повторные запросы.

Экран может нарисовать что угодно, поэтому проверяется не он, а сервер:

* ставка снимается один раз, сколько бы раз клиент ни повторил запрос;
* исход считает сервер, и подсказок для угадывания клиенту не уезжает —
  раскладки мин, плохих дверей, точки краша и закрытой карты дилера в активном
  раунде нет;
* чужой или закрытый раунд шагнуть нельзя.
"""

import json

import config
import db
from games import blackjack, coin, crash, mines, roulette, tower
from helpers import fresh_db, mk_user
from test_webapp import client, init_data
from webapp import games as app_games


async def state_of(round_id: int) -> dict:
    """Состояние раунда прямо из базы — то, чего клиент не видит."""
    row = await (await db.conn().execute(
        'SELECT state FROM rounds WHERE id = ?', (round_id,))).fetchone()
    return json.loads(row['state'] or '{}')


async def post(c, path: str, uid: int, **payload):
    response = await c.post(f'/api/games/{path}',
                            json={'initData': init_data(uid), **payload})
    return await response.json()


async def start(c, uid: int, game: str, *, bet: int = 100, pick=None,
                client_id: str | None = None) -> dict:
    return await post(c, 'play', uid, game=game, bet_cents=bet,
                      client_id=client_id or f'{game}-{uid}-1',
                      pick=None if pick is None else str(pick))


async def step(c, uid: int, game: str, round_id: int, action) -> dict:
    return await post(c, 'step', uid, game=game, round_id=round_id,
                      action=str(action))

# --- доступ ------------------------------------------------------------------

async def test_every_endpoint_needs_a_signature():
    async with fresh_db(), client() as c:
        for path in ('state', 'play', 'step'):
            assert (await c.post(f'/api/games/{path}', json={})).status == 401


async def test_screen_lists_games_without_dice_ones():
    """В приложении только те игры, где бросок считает сервер.

    Дайсы Telegram (кубик, мяч, дартс) кидает клиент Telegram в чате — в
    webview их анимации нет, и показывать другой бросок было бы обманом.
    """
    async with fresh_db(), client() as c:
        uid = await mk_user(800, balance_cents=1000)
        data = await post(c, 'state', uid)
        keys = [g['key'] for g in data['games']]
        assert keys == list(app_games.KEYS)
        for forbidden in ('slots', 'dice', 'darts', 'football', 'dice2', 'duel'):
            assert forbidden not in keys
        assert data['balance_cents'] == 1000
        assert data['min_bet'] == config.MIN_BET_CENTS
        assert data['active'] == {}
        assert data['history'] == []


async def test_bad_bet_and_bad_game_are_refused():
    async with fresh_db(), client() as c:
        uid = await mk_user(801, balance_cents=1000)
        assert (await start(c, uid, 'coin', bet=1, pick='heads'))['status'] == 'bad_bet'
        assert (await start(c, uid, 'dice', pick='1'))['status'] == 'bad_game'
        assert (await start(c, uid, 'coin', pick='ребро'))['status'] == 'bad_pick'
        assert await db.get_balance(uid) == 1000

        # Мусор вместо ставки — это уже формат запроса, а не логика игры.
        bad = await c.post('/api/games/play', json={
            'initData': init_data(801), 'game': 'coin', 'bet_cents': -100,
            'client_id': 'x', 'pick': 'heads'})
        assert bad.status == 400


async def test_no_money():
    async with fresh_db(), client() as c:
        uid = await mk_user(802, balance_cents=50)
        assert (await start(c, uid, 'coin', pick='heads'))['status'] == 'no_money'
        assert await db.get_balance(uid) == 50

# --- монетка и рулетка -------------------------------------------------------

async def test_coin_pays_by_the_bot_multiplier():
    async with fresh_db(), client() as c:
        uid = await mk_user(810, balance_cents=1000)
        data = await start(c, uid, 'coin', pick='heads')
        assert data['status'] == 'ok'
        rnd = data['round']
        assert rnd['status'] == 'done'
        assert rnd['result'] in ('heads', 'tails')

        won = rnd['result'] == 'heads'
        assert rnd['multiplier'] == (round(coin.MULT, 4) if won else 0.0)
        assert rnd['payout_cents'] == (int(100 * coin.MULT) if won else 0)
        assert data['balance_cents'] == 1000 - 100 + rnd['payout_cents']
        assert data['balance_cents'] == await db.get_balance(uid)
        assert len(data['history']) == 1


async def test_same_client_id_charges_once():
    async with fresh_db(), client() as c:
        uid = await mk_user(811, balance_cents=1000)
        first = await start(c, uid, 'coin', pick='heads', client_id='same')
        second = await start(c, uid, 'coin', pick='heads', client_id='same')
        assert first['status'] == 'ok' and second['status'] == 'repeat'
        assert await db.get_balance(uid) == first['balance_cents']
        rounds = await (await db.conn().execute(
            'SELECT COUNT(*) n FROM rounds WHERE user_id = ?', (uid,))).fetchone()
        assert rounds['n'] == 1


async def test_roulette_outside_and_straight():
    async with fresh_db(), client() as c:
        uid = await mk_user(812, balance_cents=10_000)
        data = await start(c, uid, 'roulette', pick='red', client_id='rl-1')
        rnd = data['round']
        assert 0 <= rnd['number'] < roulette.POCKETS
        red = rnd['number'] in roulette.RED
        assert rnd['multiplier'] == (2.0 if red else 0.0)
        assert rnd['payout_cents'] == (200 if red else 0)

        data = await start(c, uid, 'roulette', pick='17', client_id='rl-2')
        rnd = data['round']
        hit = rnd['number'] == 17
        assert rnd['bet_label'] == 'число 17'
        assert rnd['multiplier'] == (roulette.STRAIGHT_MULT if hit else 0.0)
        assert await db.get_balance(uid) == data['balance_cents']

# --- мины --------------------------------------------------------------------

async def test_mines_hides_the_field_until_it_is_over():
    async with fresh_db(), client() as c:
        uid = await mk_user(820, balance_cents=1000)
        data = await start(c, uid, 'mines', pick=3, client_id='mn-1')
        rnd = data['round']
        assert rnd['status'] == 'active' and rnd['n'] == 3
        assert 'mines' not in rnd          # раскладка клиенту не уезжает
        assert rnd['opened'] == [] and rnd['cash_cents'] == 0
        assert await db.get_balance(uid) == 900

        field = set((await state_of(rnd['round_id']))['mines'])
        safe = next(i for i in range(mines.CELLS) if i not in field)
        after = await step(c, uid, 'mines', rnd['round_id'], safe)
        opened = after['round']
        assert opened['status'] == 'active'
        assert opened['opened'] == [safe]
        assert opened['multiplier'] == round(mines.multiplier(3, 1), 4)
        assert opened['cash_cents'] > 100   # первая клетка уже выше ставки

        taken = await step(c, uid, 'mines', rnd['round_id'], 'c')
        assert taken['round']['status'] == 'done'
        assert taken['round']['payout_cents'] == opened['cash_cents']
        assert sorted(taken['round']['mines']) == sorted(field)  # теперь можно
        assert await db.get_balance(uid) == 900 + opened['cash_cents']


async def test_mines_bomb_ends_the_round():
    async with fresh_db(), client() as c:
        uid = await mk_user(821, balance_cents=1000)
        rnd = (await start(c, uid, 'mines', pick=5, client_id='mn-2'))['round']
        bomb = (await state_of(rnd['round_id']))['mines'][0]

        data = await step(c, uid, 'mines', rnd['round_id'], bomb)
        assert data['round']['status'] == 'done'
        assert data['round']['hit'] == bomb
        assert data['round']['payout_cents'] == 0
        assert await db.get_balance(uid) == 900

        # Раунд закрыт: второй шаг по нему уже никуда не ведёт.
        assert (await step(c, uid, 'mines', rnd['round_id'], 1))['status'] == 'gone'


async def test_second_field_is_refused_while_one_is_open():
    async with fresh_db(), client() as c:
        uid = await mk_user(822, balance_cents=1000)
        await start(c, uid, 'mines', pick=3, client_id='mn-3')
        second = await start(c, uid, 'mines', pick=3, client_id='mn-4')
        assert second['status'] == 'busy'
        assert await db.get_balance(uid) == 900     # вторая ставка не снята

# --- башня -------------------------------------------------------------------

async def test_tower_climbs_and_pays():
    async with fresh_db(), client() as c:
        uid = await mk_user(830, balance_cents=1000)
        rnd = (await start(c, uid, 'tower', client_id='tw-1'))['round']
        assert rnd['level'] == 0 and 'bad' not in rnd

        bad = (await state_of(rnd['round_id']))['bad']
        good = next(d for d in range(tower.DOORS) if d != bad[0])
        up = await step(c, uid, 'tower', rnd['round_id'], good)
        assert up['round']['level'] == 1
        assert up['round']['multiplier'] == round(tower.multiplier(1), 4)

        taken = await step(c, uid, 'tower', rnd['round_id'], 'c')
        assert taken['round']['status'] == 'done'
        assert taken['round']['payout_cents'] == up['round']['cash_cents']
        assert taken['round']['bad'] == bad


async def test_tower_fall_takes_the_bet():
    async with fresh_db(), client() as c:
        uid = await mk_user(831, balance_cents=1000)
        rnd = (await start(c, uid, 'tower', client_id='tw-2'))['round']
        bad = (await state_of(rnd['round_id']))['bad']

        data = await step(c, uid, 'tower', rnd['round_id'], bad[0])
        assert data['round']['status'] == 'done'
        assert data['round']['fell'] == bad[0]
        assert data['round']['payout_cents'] == 0
        assert await db.get_balance(uid) == 900


# --- краш --------------------------------------------------------------------

async def test_crash_hides_the_point_and_pays_by_server_clock():
    async with fresh_db(), client() as c:
        uid = await mk_user(840, balance_cents=1000)
        rnd = (await start(c, uid, 'crash', client_id='cr-1'))['round']
        point = (await state_of(rnd['round_id']))['point']

        if point <= 1.0:
            # Мгновенный краш — те самые 3% преимущества казино. Растущую цифру
            # по такому раунду не показывают: он закрыт сразу.
            assert rnd['status'] == 'done' and rnd['crashed'] is True
            assert rnd['payout_cents'] == 0
            assert await db.get_balance(uid) == 900
            return

        assert rnd['status'] == 'active'
        assert 'point' not in rnd            # точку срыва клиент не узнает
        assert rnd['growth'] == crash.GROWTH and rnd['lag'] == app_games.CRASH_LAG

        # «Спросить» не забирает: раунд остаётся открытым.
        peek = await step(c, uid, 'crash', rnd['round_id'], 'p')
        assert peek['round']['status'] == 'active'
        assert await db.get_balance(uid) == 900

        taken = await step(c, uid, 'crash', rnd['round_id'], 'c')
        got = taken['round']
        assert got['status'] == 'done'
        assert got['crashed'] is False
        assert got['multiplier'] >= 1.0
        assert got['payout_cents'] == await db.get_balance(uid) - 900

async def test_crash_that_burst_while_away_is_closed_on_return():
    """Игрок закрыл приложение и не забрал — раунд добивается сам."""
    async with fresh_db(), client() as c:
        uid = await mk_user(841, balance_cents=1000)
        rnd = (await start(c, uid, 'crash', client_id='cr-2'))['round']

        # Отматываем старт на минуту назад: с ×1.15 в секунду это заведомо
        # выше любой точки срыва (максимум системы — ×50).
        await db.conn().execute(
            'UPDATE rounds SET state = ? WHERE id = ?',
            (json.dumps({'point': (await state_of(rnd['round_id']))['point'],
                         'at': db.now() - 60}), rnd['round_id']))

        screen = await post(c, 'state', uid)
        assert 'crash' not in screen['active']
        assert screen['balance_cents'] == 900
        row = await (await db.conn().execute(
            'SELECT status, payout_cents FROM rounds WHERE id = ?',
            (rnd['round_id'],))).fetchone()
        assert (row['status'], row['payout_cents']) == ('lost', 0)


# --- блэкджек ----------------------------------------------------------------

async def test_blackjack_hides_the_hole_card():
    async with fresh_db(), client() as c:
        uid = await mk_user(850, balance_cents=1000)
        rnd = (await start(c, uid, 'blackjack', client_id='bj-1'))['round']

        if rnd['status'] == 'active':
            assert len(rnd['dealer']) == 1 and rnd['hidden'] == 1
            assert len(rnd['player']) == 2
            data = await step(c, uid, 'blackjack', rnd['round_id'], 's')
            done = data['round']
        else:
            done = rnd                       # блэкджек с раздачи

        assert done['status'] == 'done'
        assert done['hidden'] == 0 and len(done['dealer']) >= 2
        assert done['outcome'] in ('bust', 'push_bj', 'player_bj', 'dealer_bj',
                                  'dealer_bust', 'win', 'lose', 'push')
        assert await db.get_balance(uid) == data['balance_cents'] \
            if rnd['status'] == 'active' else True


async def test_blackjack_double_takes_the_second_bet():
    async with fresh_db(), client() as c:
        uid = await mk_user(851, balance_cents=1000)
        rnd = (await start(c, uid, 'blackjack', client_id='bj-2'))['round']
        if rnd['status'] != 'active' or not rnd['can_double']:
            return                           # раздача решилась сразу — не наш случай

        data = await step(c, uid, 'blackjack', rnd['round_id'], 'd')
        done = data['round']
        assert done['status'] == 'done'
        assert done['bet_cents'] == 200      # ставка удвоена
        assert done['doubled'] is True
        assert len(done['player']) == 3      # ровно одна карта и останов
        assert await db.get_balance(uid) == data['balance_cents']


async def test_foreign_round_cannot_be_stepped():
    async with fresh_db(), client() as c:
        mine = await mk_user(860, balance_cents=1000)
        other = await mk_user(861, balance_cents=1000)
        rnd = (await start(c, mine, 'mines', pick=3, client_id='mn-9'))['round']
        assert (await step(c, other, 'mines', rnd['round_id'], 0))['status'] == 'gone'
        assert await db.get_balance(other) == 1000
