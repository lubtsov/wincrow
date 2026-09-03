"""Слот «Сочный шторм»: механика, отдача и деньги.

Главное здесь — два свойства, без которых слот в казино не имеет права
работать:

* **отдача сходится с заявленной.** Форму таблицы выплат задали руками, а её
  масштаб посчитан Монте-Карло; тест ниже гоняет живой поток движка и проверяет,
  что фактический средний множитель равен `config.RTP`. Правка весов или выплат
  без пересчёта `storm.SCALE` уронит именно этот тест;
* **спин считается один раз.** Ставка снимается в той же транзакции, что и
  запись раунда, а повторный запрос с тем же id спина отдаёт прежний результат
  вместо новой ставки.
"""

import asyncio
import secrets

import pytest

import config
import db
from games import engine, storm
from helpers import MC_ROUNDS, Meter, fresh_db, mk_user


def mk_round(nonce: int, bet_cents: int = 100,
             server: str = 'server-seed', client: str = 'client-seed'):
    """Раунд без базы: только случайность, для проверок чистой математики."""
    return engine.Round(id=0, user_id=0, game=storm.GAME, bet_cents=bet_cents,
                        server_seed=server, server_seed_hash='',
                        client_seed=client, nonce=nonce)


# --- поле и скопления -------------------------------------------------------

def test_grid_shape():
    result = storm.play(mk_round(1))
    assert len(result['grid']) == storm.COLS
    assert all(len(col) == storm.ROWS for col in result['grid'])
    assert storm.COLS * storm.ROWS == 30


def test_cluster_needs_eight():
    """Семь одинаковых — ещё не скопление, восемь — уже да."""
    flat = [(c, r) for c in range(storm.COLS) for r in range(storm.ROWS)]

    def grid_with(cherries: int) -> list[list[str]]:
        # Фон из штормов: они не образуют скоплений, поэтому в поле остаются
        # только черешни, и счёт получается чистым.
        grid = [['x2'] * storm.ROWS for _ in range(storm.COLS)]
        grid = [list(col) for col in grid]
        for col, row in flat[:cherries]:
            grid[col][row] = 'cherry'
        return grid

    assert storm.clusters(grid_with(7)) == []
    found = storm.clusters(grid_with(8))
    assert len(found) == 1
    assert found[0]['symbol'] == 'cherry'
    assert found[0]['count'] == 8
    assert found[0]['win'] == storm.pay('cherry', 8)


def test_pay_tiers_grow():
    for spec in storm.SYMBOLS:
        eight = storm.pay(spec.key, 8)
        ten = storm.pay(spec.key, 10)
        twelve = storm.pay(spec.key, 12)
        assert 0 < eight < ten < twelve
        assert storm.pay(spec.key, storm.MIN_CLUSTER - 1) == 0


def test_storms_never_form_clusters():
    grid = [['x2'] * storm.ROWS for _ in range(storm.COLS)]
    assert storm.clusters(grid) == []

# --- каскады и множители ----------------------------------------------------

def test_collapse_keeps_column_height_and_drops_survivors():
    """После удаления колонка снова полная, а выжившие осели вниз."""
    grid = [[f'{c}-{r}' for r in range(storm.ROWS)] for c in range(storm.COLS)]
    dead = {(0, 0), (0, 1)}                 # верхние две ячейки первой колонки
    out = storm.collapse(grid, dead, mk_round(7))
    assert all(len(col) == storm.ROWS for col in out)
    # Хвост первой колонки — прежние ячейки 2..4, они не сдвинулись друг
    # относительно друга.
    assert out[0][2:] == ['0-2', '0-3', '0-4']
    assert out[1] == grid[1]                # соседние колонки не тронуты


def test_cascade_is_deterministic_from_seed():
    """Один и тот же сид — тот же спин. На этом стоит вся провably fair."""
    first = storm.play(mk_round(42))
    second = storm.play(mk_round(42))
    assert first == second
    assert storm.play(mk_round(43)) != first


def test_storm_total_multiplies_the_win():
    """Штормы умножают выплату, но только если спин вообще что-то заплатил."""
    seen_storm_with_win = False
    for nonce in range(1, 4000):
        result = storm.play(mk_round(nonce))
        if result['storm_total'] and result['base_win'] > 0:
            expected = min(result['base_win'] * result['storm_total'],
                           storm.MAX_MULTIPLIER)
            assert abs(result['multiplier'] - round(expected, 4)) < 1e-6
            seen_storm_with_win = True
        elif not result['storm_total']:
            assert result['multiplier'] == result['base_win']
    assert seen_storm_with_win, 'за 4000 спинов не выпало ни шторма с выигрышем'


def test_multiplier_never_exceeds_cap():
    for nonce in range(1, 2000):
        assert storm.play(mk_round(nonce))['multiplier'] <= storm.MAX_MULTIPLIER


def test_steps_match_wins():
    """Сумма каскадов равна базовому выигрышу — клиенту нечего досчитывать."""
    for nonce in range(1, 300):
        result = storm.play(mk_round(nonce))
        assert abs(sum(s['win'] for s in result['steps'])
                   - result['base_win']) < 1e-6
        for step in result['steps']:
            assert step['clusters']
            assert abs(sum(c['win'] for c in step['clusters'])
                       - step['win']) < 1e-6

# --- отдача -----------------------------------------------------------------

def test_rtp_matches_config():
    """Фактическая отдача слота сходится с config.RTP.

    Прогон идёт на живом потоке движка, а не на random: проверяется та самая
    случайность, которой играет казино. Допуск считает Meter — у каскадов с
    штормами дисперсия большая, и жёсткие 0.5% на конечном прогоне были бы
    придиркой к шуму, а не проверкой математики.
    """
    server, client = secrets.token_hex(16), secrets.token_hex(8)
    meter = Meter()
    for nonce in range(1, MC_ROUNDS + 1):
        meter.add(storm.play(mk_round(nonce, server=server, client=client))
                  ['multiplier'])
    meter.check()


def test_hit_rate_is_sane():
    """Слот должен платить достаточно часто, иначе играть в него невозможно."""
    wins = sum(1 for nonce in range(1, 4000)
               if storm.play(mk_round(nonce))['multiplier'] > 0)
    assert 0.3 < wins / 4000 < 0.7


# --- деньги -----------------------------------------------------------------

async def test_spin_charges_once_and_pays_out():
    async with fresh_db():
        uid = await mk_user(900, balance_cents=1000)
        result, status = await storm.spin(uid, 100, 'spin-1')
        assert status == 'ok'
        assert result['bet_cents'] == 100

        row = await db.get_user(uid)
        expected = 1000 - 100 + result['payout_cents']
        assert row['balance_cents'] == expected
        assert row['wagered_cents'] == 100
        assert row['won_cents'] == result['payout_cents']
        assert result['payout_cents'] == engine.payout_cents(100, result['multiplier'])


async def test_repeat_request_does_not_charge_again():
    """Тот же id спина — прежний результат, а не новая ставка."""
    async with fresh_db():
        uid = await mk_user(901, balance_cents=1000)
        first, status = await storm.spin(uid, 100, 'same-id')
        assert status == 'ok'
        after_first = await db.get_balance(uid)

        second, status = await storm.spin(uid, 100, 'same-id')
        assert status == 'repeat'
        assert second['round_id'] == first['round_id']
        assert second['multiplier'] == first['multiplier']
        assert second['grid'] == first['grid']          # раскадровка та же
        assert second['replayed'] is True
        assert await db.get_balance(uid) == after_first

async def test_parallel_requests_with_one_id_charge_once():
    """Четыре одновременных запроса с одним id: ставка снимается один раз."""
    async with fresh_db():
        uid = await mk_user(902, balance_cents=1000)
        results = await asyncio.gather(*(storm.spin(uid, 100, 'race')
                                        for _ in range(4)))
        statuses = [status for _, status in results]
        assert statuses.count('ok') == 1

        rounds = await (await db.conn().execute(
            'SELECT COUNT(*) n FROM rounds WHERE user_id = ?', (uid,))).fetchone()
        assert rounds['n'] == 1
        row = await db.get_user(uid)
        assert row['wagered_cents'] == 100
        payout = next(r['payout_cents'] for r, s in results if s == 'ok')
        assert row['balance_cents'] == 1000 - 100 + payout


async def test_no_money_and_bad_bet():
    async with fresh_db():
        uid = await mk_user(903, balance_cents=50)
        assert await storm.spin(uid, 100, 'a') == ({}, 'no_money')
        assert await storm.spin(uid, 1, 'b') == ({}, 'bad_bet')
        assert await storm.spin(uid, config.MAX_BET_CENTS + 10, 'c') == ({}, 'bad_bet')
        assert await storm.spin(uid, 10, '') == ({}, 'bad_bet')
        assert await db.get_balance(uid) == 50          # ни цента не ушло


async def test_round_lands_in_history_and_stats():
    """Спин — обычный раунд: он виден в истории, профиле и статистике казино."""
    async with fresh_db():
        uid = await mk_user(904, balance_cents=1000)
        result, _ = await storm.spin(uid, 100, 'hist-1')

        history = await storm.history(uid)
        assert len(history) == 1
        assert history[0]['round_id'] == result['round_id']
        assert history[0]['payout_cents'] == result['payout_cents']

        assert await db.games_played(uid) == 1
        stats = await db.stats()
        assert stats['rounds'] == 1
        assert stats['wagered'] == 100
        assert stats['paid_to_players'] == result['payout_cents']


async def test_hung_spin_is_refunded_on_restart():
    """Процесс убили между ставкой и результатом — ставка возвращается."""
    async with fresh_db():
        uid = await mk_user(905, balance_cents=1000)
        rnd = await engine.start_round(uid, storm.GAME, 100, client_id='hung')
        assert rnd is not None
        assert await db.get_balance(uid) == 900

        assert await db.reap_active_rounds() == 1
        assert await db.get_balance(uid) == 1000
        row = await db.get_user(uid)
        assert row['wagered_cents'] == 0                 # оборот откатился


async def test_client_id_is_unique_only_within_slot_rounds():
    """Раунды бота client_id не заполняют, и индекс им не мешает."""
    async with fresh_db():
        uid = await mk_user(906, balance_cents=1000)
        first = await engine.start_round(uid, 'coin', 100)
        second = await engine.start_round(uid, 'coin', 100)
        assert first is not None and second is not None
        assert first.id != second.id
