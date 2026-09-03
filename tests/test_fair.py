"""Provably fair: воспроизводимость и равномерность потока.

Обещание игроку простое: sha256(server_seed) публикуется до раунда, после
ротации сид раскрывается, и любой прошлый раунд можно пересчитать руками. Эти
тесты проверяют обе половины обещания — что поток однозначно задан тройкой
сидов и что он равномерен.
"""

import hashlib
import hmac

import pytest

import db
from games import engine
from helpers import MC_ROUNDS, fresh_db, mk_user


def _round(nonce: int = 1, server: str = 'srv', client: str = 'cli') -> engine.Round:
    return engine.Round(id=nonce, user_id=1, game='test', bet_cents=100,
                        server_seed=server, server_seed_hash=engine.seed_hash(server),
                        client_seed=client, nonce=nonce)


# --- сиды -------------------------------------------------------------------

def test_seed_hash_is_plain_sha256():
    """Хеш считается так, как игрок сможет проверить у себя."""
    assert engine.seed_hash('abc') == hashlib.sha256(b'abc').hexdigest()
    assert len(engine.seed_hash(engine.new_seed())) == 64


def test_new_seed_is_unique_and_long():
    seeds = {engine.new_seed() for _ in range(100)}
    assert len(seeds) == 100
    assert all(len(s) == 64 for s in seeds)


# --- поток ------------------------------------------------------------------

def _take(count: int, server: str, client: str, nonce: int) -> list[float]:
    stream = engine.float_stream(server, client, nonce)
    return [next(stream) for _ in range(count)]


def test_stream_is_reproducible():
    """Одна тройка сидов — один и тот же поток. Иначе проверять нечего."""
    a = _take(50, 's', 'c', 7)
    b = _take(50, 's', 'c', 7)
    assert a == b
    assert len(set(a)) == 50            # поток идёт вперёд, а не стоит на месте


def test_stream_changes_with_every_input():
    """Смена любого из трёх входов даёт другой поток."""
    base = next(engine.float_stream('s', 'c', 1))
    assert next(engine.float_stream('s2', 'c', 1)) != base
    assert next(engine.float_stream('s', 'c2', 1)) != base
    assert next(engine.float_stream('s', 'c', 2)) != base


def test_stream_stays_in_unit_range():
    """Значение 1.0 сломало бы crash: RTP / (1 - u) — деление на ноль."""
    stream = engine.float_stream('range', 'check', 1)
    for _ in range(100_000):
        u = next(stream)
        assert 0.0 <= u < 1.0


def test_stream_crosses_cursor_boundary():
    """За 32 байта дайджеста поток обязан идти дальше, а не повторяться.

    Из одного HMAC нарезается 8 чисел, дальше растёт cursor. Если бы cursor
    забыли увеличить, каждые восемь значений повторялись бы — и все игры стали
    бы предсказуемыми после восьми бросков.
    """
    stream = engine.float_stream('cursor', 'check', 1)
    values = [next(stream) for _ in range(40)]
    assert len(set(values)) == 40
    assert values[:8] != values[8:16]


def test_stream_is_uniform():
    """Средняя и гистограмма по десяти корзинам.

    Это тот тест, который поймает перекос генератора: таблицы выплат при
    кривом потоке остаются верными, а касса едет.
    """
    bins = [0] * 10
    total = 0.0
    stream = engine.float_stream('uniform', 'check', 1)
    for _ in range(MC_ROUNDS):
        u = next(stream)
        total += u
        bins[min(int(u * 10), 9)] += 1

    mean = total / MC_ROUNDS
    # Стандартная ошибка средней равномерного: 1/sqrt(12n).
    assert abs(mean - 0.5) < 5 * (1 / (12 * MC_ROUNDS)) ** 0.5

    expected = MC_ROUNDS / 10
    sigma = (MC_ROUNDS * 0.1 * 0.9) ** 0.5
    assert max(abs(b - expected) for b in bins) < 5 * sigma


def test_verifiable_by_hand():
    """Ровно та формула, что обещана игроку на экране «Честная игра».

    HMAC_SHA256(server_seed, "client_seed:nonce:cursor"), первые 4 байта
    дайджеста, делённые на 2^32.
    """
    server, client, nonce = 'открытый-сид', 'мой-сид', 42
    digest = hmac.new(server.encode(), f'{client}:{nonce}:0'.encode(),
                      hashlib.sha256).digest()
    by_hand = int.from_bytes(digest[:4], 'big') / 4_294_967_296
    assert next(engine.float_stream(server, client, nonce)) == by_hand


# --- производные от потока --------------------------------------------------

def test_pick_stays_in_bounds():
    """pick(n) не должен отдать n — иначе рулетка выдала бы лунку 37."""
    rnd = _round()
    for n in (2, 3, 6, 25, 37, 64):
        for _ in range(2000):
            value = rnd.pick(n)
            assert 0 <= value < n


def test_pick_is_uniform_on_37():
    """Лунки рулетки. Перекос здесь — это перекос кассы."""
    rounds = min(MC_ROUNDS, 100_000)
    rnd = _round()
    hits = [0] * 37
    for _ in range(rounds):
        hits[rnd.pick(37)] += 1

    expected = rounds / 37
    sigma = (rounds * (1 / 37) * (1 - 1 / 37)) ** 0.5
    assert max(abs(h - expected) for h in hits) < 5 * sigma


def test_shuffle_is_a_permutation():
    """Тасовка колоды не имеет права терять или дублировать карты."""
    deck = list(range(52 * 6))
    for nonce in range(20):
        out = _round(nonce).shuffle(deck)
        assert sorted(out) == deck
        assert out != deck                      # и всё же перемешана


def test_shuffle_does_not_touch_the_original():
    deck = [1, 2, 3, 4, 5]
    _round().shuffle(deck)
    assert deck == [1, 2, 3, 4, 5]


def test_sample_returns_distinct_cells():
    """Раскладка мин: k различных клеток, две мины в одной клетке невозможны."""
    for k in (1, 3, 5, 10, 15, 24):
        cells = _round(k).sample(25, k)
        assert len(cells) == k
        assert len(set(cells)) == k
        assert all(0 <= c < 25 for c in cells)


def test_layouts_differ_between_rounds():
    """Разный nonce — разная раскладка. Одинаковая означала бы, что поле
    можно выучить и открывать по памяти."""
    layouts = {tuple(sorted(_round(n).sample(25, 5))) for n in range(200)}
    assert len(layouts) > 190


def test_round_replays_identically():
    """Раунд восстанавливается после клика: поток каждый раз начинается с нуля.

    На этом держится блэкджек — колода тасуется заново при каждом апдейте, и
    порядок карт обязан совпасть с раздачей.
    """
    first, second = _round(9), _round(9)
    assert [first.rnd() for _ in range(10)] == [second.rnd() for _ in range(10)]

    a, b = _round(9), _round(9)
    assert a.shuffle(list(range(52))) == b.shuffle(list(range(52)))


# --- ротация в базе ---------------------------------------------------------

async def test_rotate_reveals_previous_seed():
    """После ротации прежний сид раскрыт, и его хеш совпадает с опубликованным."""
    async with fresh_db():
        await mk_user(1, 1000)
        rnd = await engine.start_round(1, 'coin', 100)
        assert rnd is not None
        published = rnd.server_seed_hash

        revealed = await engine.rotate_seed(1)
        assert engine.seed_hash(revealed) == published
        assert revealed == rnd.server_seed

        row = await (await db.conn().execute(
            'SELECT * FROM seeds WHERE user_id = 1')).fetchone()
        assert row['server_seed'] != revealed        # новый сид уже другой
        assert row['prev_server_seed'] == revealed
        assert row['nonce'] == 0                     # счётчик обнулён


async def test_round_stores_hash_not_seed():
    """В rounds лежит только хеш: раскрытый сид до ротации — это подсказка."""
    async with fresh_db():
        await mk_user(1, 1000)
        rnd = await engine.start_round(1, 'coin', 100)
        row = await (await db.conn().execute(
            'SELECT * FROM rounds WHERE id = ?', (rnd.id,))).fetchone()

        assert row['server_seed_hash'] == engine.seed_hash(rnd.server_seed)
        assert 'server_seed' not in row.keys()
        assert row['nonce'] == rnd.nonce
        assert row['client_seed'] == rnd.client_seed


async def test_nonce_grows_with_every_round():
    """Один и тот же nonce у двух раундов дал бы одинаковый исход."""
    async with fresh_db():
        await mk_user(1, 10_000)
        nonces = []
        for _ in range(20):
            rnd = await engine.start_round(1, 'coin', 100)
            nonces.append(rnd.nonce)
        assert nonces == list(range(1, 21))


async def test_client_seed_is_the_players_choice():
    async with fresh_db():
        await mk_user(1, 1000)
        await engine.set_client_seed(1, 'мой-любимый-сид')
        rnd = await engine.start_round(1, 'coin', 100)
        assert rnd.client_seed == 'мой-любимый-сид'

        # Длинный сид обрезается, а не роняет INSERT.
        await engine.set_client_seed(1, 'x' * 200)
        rnd = await engine.start_round(1, 'coin', 100)
        assert len(rnd.client_seed) == 64


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
