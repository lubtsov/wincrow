"""Деньги: формат, разбор суммы, атомарность списания, округление до цента.

Здесь проверяется главное обещание денежного слоя — баланс нельзя увести в
минус и нельзя сыграть дважды на одни деньги. Прежняя версия падала на этом
дважды: проверка `balance > 0` вместо `balance >= bet` и последовательность
`SELECT balance` -> await -> `UPDATE balance = ?` в каждой игре.
"""

import asyncio
import math
from decimal import Decimal

import pytest

import config
import db
import payments
from games import dice_games, dice_sum, mines, roulette, tower
from games import engine
from helpers import TOL, fresh_db, mk_user


# --- формат и разбор --------------------------------------------------------

def test_fmt():
    assert db.fmt(0) == '$0.00'
    assert db.fmt(7) == '$0.07'
    assert db.fmt(1234) == '$12.34'
    assert db.fmt(100000) == '$1000.00'
    assert db.fmt(-50) == '-$0.50'


def test_parse_cents():
    assert db.parse_cents('12.5') == 1250
    assert db.parse_cents('12,5') == 1250      # запятая как разделитель
    assert db.parse_cents(' 0.07 ') == 7
    assert db.parse_cents('1') == 100
    assert db.parse_cents('.5') == 50
    # Лишние знаки отбрасываются вниз, а не округляются вверх: игроку нельзя
    # выдать цент, которого он не внёс.
    assert db.parse_cents('1.999') == 199
    for bad in ('0', '0.001', '-5', 'abc', '', '   ', None, 42,
                '1.2.3', '5 5', '+5', '٣', '9' * 20):
        assert db.parse_cents(bad) is None, repr(bad)


def test_parse_cents_survives_hostile_input():
    """Экспонента и nan — не суммы.

    Decimal принимает и то и другое: '1e999999999' развернулся бы в целое на
    четыреста мегабайт, а сравнение Decimal('nan') с нулём бросает
    InvalidOperation уже после try. И то и другое — падение по одному
    сообщению в чат.
    """
    for bad in ('1e5000', '1E999999999', '-1e9', 'nan', 'inf', '-inf',
                'Infinity', 'snan'):
        assert db.parse_cents(bad) is None, bad


def test_fmt_parse_roundtrip():
    for cents in (1, 7, 99, 100, 1234, 50_000, 1_000_000):
        assert db.parse_cents(db.fmt(cents).lstrip('$')) == cents


def test_amount_str_without_float_artifacts():
    """Сумма для Crypto Pay собирается из целых, иначе получается '5.070000000000001'."""
    assert payments.amount_str(507) == '5.07'
    assert payments.amount_str(100) == '1.00'
    assert payments.amount_str(5) == '0.05'
    assert payments.amount_str(0) == '0.00'
    for cents in range(0, 20_000, 7):
        assert Decimal(payments.amount_str(cents)) * 100 == cents


# --- атомарность ------------------------------------------------------------

async def test_place_bet_atomic():
    """Сто одновременных ставок на баланс, которого хватает на десять."""
    async with fresh_db():
        await mk_user(1, 1000)
        results = await asyncio.gather(*(db.place_bet(1, 100) for _ in range(100)))

        assert sum(results) == 10
        user = await db.get_user(1)
        assert user['balance_cents'] == 0
        assert user['wagered_cents'] == 1000


async def test_place_bet_never_goes_negative():
    """Ставки разного размера вперемешку — баланс всё равно не уходит в минус."""
    async with fresh_db():
        await mk_user(1, 1000)
        sizes = [100, 250, 700, 1000, 50] * 20
        paid = await asyncio.gather(*(db.place_bet(1, s) for s in sizes))

        spent = sum(s for s, ok in zip(sizes, paid) if ok)
        assert spent <= 1000
        assert await db.get_balance(1) == 1000 - spent
        assert await db.get_balance(1) >= 0


async def test_place_bet_rejects_junk():
    async with fresh_db():
        await mk_user(1, 1000)
        assert await db.place_bet(1, 0) is False
        assert await db.place_bet(1, -500) is False
        assert await db.place_bet(1, 1001) is False
        assert await db.get_balance(1) == 1000


async def test_place_bet_blocked_for_banned():
    async with fresh_db():
        await mk_user(1, 1000)
        await db.set_banned(1, True)
        assert await db.place_bet(1, 100) is False
        assert await db.get_balance(1) == 1000


async def test_take_balance_needs_funds():
    async with fresh_db():
        await mk_user(1, 500)
        assert await db.take_balance(1, 600) is False
        assert await db.take_balance(1, 500) is True
        assert await db.get_balance(1) == 0


async def test_start_round_atomic():
    """То же самое через движок: раунд открывается только вместе со списанием.

    Отдельно проверяется nonce — два раунда с одинаковым nonce дали бы
    одинаковый исход, и это была бы дыра пострашнее лишней ставки.
    """
    async with fresh_db():
        await mk_user(1, 1000)
        rounds = await asyncio.gather(
            *(engine.start_round(1, 'coin', 100) for _ in range(100)))

        opened = [r for r in rounds if r is not None]
        assert len(opened) == 10
        assert len({r.id for r in opened}) == 10
        assert len({r.nonce for r in opened}) == 10
        assert await db.get_balance(1) == 0


async def test_raise_stake_needs_funds():
    """Удвоение в блэкджеке — то же условное списание, что и ставка."""
    async with fresh_db():
        await mk_user(1, 150)
        rnd = await engine.start_round(1, 'blackjack', 100)
        assert rnd is not None
        assert await engine.raise_stake(rnd, 100) is False   # осталось 50
        assert rnd.bet_cents == 100
        assert await engine.raise_stake(rnd, 50) is True
        assert rnd.bet_cents == 150
        assert await db.get_balance(1) == 0


# --- округление до цента ----------------------------------------------------
#
# Множители дробные, ставка целая, мельче цента платить нечем. Значит выплату
# надо к центу приводить — и ровно здесь прежняя версия плана ошибалась,
# предлагая round(). При ставке $1 половина цента правда незаметна, но
# минимальная ставка теперь $0.10, то есть всего десять центов, и половина
# цента — это 5% ставки В ОБЕ СТОРОНЫ. Вверх — за счёт казино.
#
# engine.payout_cents округляет ВНИЗ. Тесты ниже проверяют два обещания:
# фактическая отдача никогда не выше заявленной, а недобор не превышает
# одного цента с выплаты.


def _strategies() -> list[tuple[str, list[tuple[float, float]]]]:
    """(название, [(вероятность, множитель), …]) для каждой стратегии игрока.

    Проигрышные исходы опущены: они платят ноль и до, и после округления.
    Стратегия — это не игра, а конкретный выбор: «забрать на 3 этаже»,
    «поставить на дюжину». Каждая обязана держать отдачу сама по себе,
    иначе одна из них становится дыркой в кассе.

    Здесь только игры с выведенной из RTP выплатой. Дайс-игры на исход
    (футбол, слоты, кости и прочие) живут в _fixed_odds: их коэффициенты
    заданы руками, отдача у них своя, и требовать от них config.RTP нечестно.
    """
    out: list[tuple[str, list[tuple[float, float]]]] = []

    for level in range(1, tower.FLOORS + 1):
        chance = ((tower.DOORS - 1) / tower.DOORS) ** level
        out.append((f'башня, {level} этаж', [(chance, tower.multiplier(level))]))

    for count in mines.MINE_CHOICES:
        safe = mines.CELLS - count
        for opened in range(1, safe + 1):
            chance = math.comb(safe, opened) / math.comb(mines.CELLS, opened)
            out.append((f'мины {count} 💣, {opened} клеток',
                        [(chance, mines.multiplier(count, opened))]))

    for key, (_, mult, hit) in roulette.BETS.items():
        wins = sum(1 for n in range(roulette.POCKETS) if hit(n))
        out.append((f'рулетка {key}', [(wins / roulette.POCKETS, mult)]))

    # Краш: точка выхода задаётся заранее, дойти до ×m шанс RTP/m.
    for mult in (1.01, 1.1, 1.35, 1.5, 2.0, 3.33, 10.0, 47.5):
        out.append((f'краш ×{mult}', [(config.RTP / mult, mult)]))

    # Ставка на сумму кубиков: самые крупные множители в казино (×209.52 за
    # сумму 3), а на них округление вниз обязано держаться так же, как на ×1.5.
    for table in dice_sum.TABLES.values():
        out.append((f'{table.key} больше',
                    [(table.outcomes('over') / table.total, table.mult('over'))]))
        for number in range(table.low, table.high + 1):
            out.append((f'{table.key} сумма {number}',
                        [(table.ways[number] / table.total,
                          table.mult('n', number))]))

    return out


def _fixed_odds() -> list[tuple[str, list[tuple[float, float]]]]:
    """Дайс-игры на исход: ставка на «гол», «центр», «три семёрки» и прочее.

    Коэффициенты у них вписаны руками, а не выведены из отдачи, поэтому в
    _strategies им нельзя: тест на config.RTP они провалили бы по определению.
    Округление до цента обязано работать и здесь — это и проверяется отдельно.

    Вероятность считается от равномерного дайса. Для округления она нужна
    только как вес, само допущение о равномерности тут ни на что не влияет.
    """
    out: list[tuple[str, list[tuple[float, float]]]] = []
    for game in dice_games.PICKS.values():
        if game.duel:
            # Боулинг: победа и поражение по (1 - 1/6) / 2, ничья 1/6.
            out.append((f'{game.key} победа', [(5 / 12, game.find('win').mult)]))
            out.append((f'{game.key} ничья', [(1 / 6, game.find('draw').mult)]))
            continue
        faces = dice_games.FACES[game.emoji]
        for outcome in game.outcomes:
            out.append((f'{game.key} {outcome.code}',
                        [(len(outcome.values) / faces, outcome.mult)]))
    return out


def _nominal(outcomes) -> float:
    """Отдача до округления — то, что обещано игроку."""
    return sum(p * m for p, m in outcomes)


def _actual(outcomes, bet: int) -> float:
    """Отдача с фактическими центами на балансе."""
    return sum(p * engine.payout_cents(bet, m) for p, m in outcomes) / bet


def test_payout_cents_never_pays_more_than_exact():
    """Целая часть от точного значения — ни центом больше."""
    for bet in (10, 11, 17, 100, 333, 50_000):
        for mult in (1.01, 1.4533, 1.5, 1.94, 2.0, 2.4691, 8.0, 25.0, 47.5):
            exact = Decimal(bet) * Decimal(str(mult))
            paid = engine.payout_cents(bet, mult)
            assert paid <= exact, f'{bet}×{mult}: {paid} > {exact}'
            assert exact - paid < 1, f'{bet}×{mult}: потеряно {exact - paid}'


def test_payout_cents_exact_on_whole_values():
    """Там, где выплата и так целая, округление не должно ничего съесть.

    100 * 0.29 == 28.999999999999996 — без запаса на ошибку float честные
    29 центов превратились бы в 28 на ровном месте. Крупные суммы проверяются
    отдельно: шаг double растёт вместе с числом, и запас обязан расти с ним.
    """
    assert engine.payout_cents(10, 1.5) == 15
    assert engine.payout_cents(100, 2.0) == 200
    assert engine.payout_cents(33, 3.0) == 99
    assert engine.payout_cents(100, 0.29) == 29
    assert engine.payout_cents(50_000, 1.94) == 97_000
    assert engine.payout_cents(50_000, 24.86) == 1_243_000
    assert engine.payout_cents(config.MAX_BET_CENTS, 50.0) == 2_500_000
    for bet in range(1, 200):
        assert engine.payout_cents(bet, 2.0) == bet * 2
        assert engine.payout_cents(bet, 1.0) == bet
    # Целая выплата остаётся целой на всём диапазоне ставок, а не только
    # на мелких: bet * mult считается через float и на больших числах
    # промахивается мимо целого.
    for bet in (10, 137, 1_000, 9_999, config.MAX_BET_CENTS):
        for mult in (1.0, 2.0, 4.0, 20.0, 36.0):
            assert engine.payout_cents(bet, mult) == bet * int(mult)


def test_payout_cents_ignores_nonsense():
    for bet, mult in ((0, 2.0), (-100, 2.0), (100, 0.0), (100, -1.0)):
        assert engine.payout_cents(bet, mult) == 0


def test_round_would_have_leaked_at_min_bet():
    """Тот самый эксплойт, из-за которого выплата округляется вниз.

    Краш, выход на ×1.35 ставкой $0.10: точная выплата 13.5 цента, round()
    даёт 14, шанс дойти — 0.97/1.35. Отдача 100.6%, то есть минимальными
    ставками кассу можно было бы доить в плюс без всякого везения.
    Башня на первом этаже (×1.455 -> round 15 центов) даёт ровно 100%.
    """
    bet = config.MIN_BET_CENTS

    crash_mult = 1.35
    chance = config.RTP / crash_mult
    assert chance * round(bet * crash_mult) / bet > 1.0
    assert chance * engine.payout_cents(bet, crash_mult) / bet <= config.RTP

    tower_mult = tower.multiplier(1)
    chance = (tower.DOORS - 1) / tower.DOORS
    assert chance * round(bet * tower_mult) / bet >= 1.0
    assert chance * engine.payout_cents(bet, tower_mult) / bet <= config.RTP


def test_actual_rtp_never_above_nominal():
    """Главное обещание: округление работает только в пользу казино.

    Проверяется на всех размерах ставки, а не только на минимальной: дыра
    от round() вылезала именно на конкретных сочетаниях ставки и множителя.
    """
    bets = [config.MIN_BET_CENTS, config.MIN_BET_CENTS + 1, 17, 33, 100,
            999, 1_000, 12_345, config.MAX_BET_CENTS]
    for name, outcomes in _strategies():
        nominal = _nominal(outcomes)
        for bet in bets:
            actual = _actual(outcomes, bet)
            assert actual <= nominal + 1e-12, f'{name}, ставка {bet}: {actual}'


def test_rounding_deficit_is_under_one_cent_per_payout():
    """Недобор ограничен центом с выплаты — иначе это уже не округление.

    На минимальной ставке цент — это 10%, поэтому граница считается честно
    через вероятность выигрыша, а не берётся с потолка.
    """
    for name, outcomes in _strategies():
        p_win = sum(p for p, _ in outcomes)
        for bet in (config.MIN_BET_CENTS, 100, 1_000):
            deficit = _nominal(outcomes) - _actual(outcomes, bet)
            # Границы с допуском 1e-12: там, где округление не съедает ничего,
            # разность двух float-сумм гуляет в последнем бите.
            assert -1e-12 <= deficit <= p_win / bet + 1e-12, \
                f'{name}, {bet}: {deficit}'


def test_min_bet_still_pays_something_on_a_win():
    """Выигрыш ставкой $0.10 не должен обнуляться округлением."""
    for name, outcomes in _strategies():
        for _, mult in outcomes:
            assert engine.payout_cents(config.MIN_BET_CENTS, mult) > 0, name


def test_rtp_holds_at_normal_bets():
    """От $10 округление в отдаче не видно вообще — расхождение меньше 0.1%.

    Порог 1/bet — арифметическая граница потери цента, а не подогнанное число.
    """
    for name, outcomes in _strategies():
        for bet in (1_000, 10_000, config.MAX_BET_CENTS):
            actual = _actual(outcomes, bet)
            assert abs(actual - _nominal(outcomes)) < 1 / bet, f'{name}, {bet}'
            assert abs(actual - config.RTP) < TOL, f'{name}, {bet}: {actual}'


def test_fixed_odds_rounding_favours_the_house():
    """Округление в дайс-играх на исход работает так же — вниз, в пользу кассы.

    Отдачи config.RTP от них не требуется: коэффициенты заданы руками. А вот
    правило «ни центом больше обещанного, и недобор не больше цента с выплаты»
    общее для всего казино, и оно здесь проверяется.
    """
    bets = [config.MIN_BET_CENTS, config.MIN_BET_CENTS + 1, 17, 100, 999,
            12_345, config.MAX_BET_CENTS]
    for name, outcomes in _fixed_odds():
        nominal = _nominal(outcomes)
        p_win = sum(p for p, _ in outcomes)
        for bet in bets:
            actual = _actual(outcomes, bet)
            assert actual <= nominal + 1e-12, f'{name}, ставка {bet}: {actual}'
            deficit = nominal - actual
            assert -1e-12 <= deficit <= p_win / bet + 1e-12, \
                f'{name}, {bet}: {deficit}'


def test_fixed_odds_min_bet_still_pays():
    """Минимальная ставка $0.10 на любой исход не должна обнуляться в ноль."""
    for name, outcomes in _fixed_odds():
        for _, mult in outcomes:
            assert engine.payout_cents(config.MIN_BET_CENTS, mult) > 0, name


def test_bet_limits_are_whole_cents():
    """Лимиты и шаг — целые центы, иначе на кнопках вылезет дробь."""
    for value in (config.MIN_BET_CENTS, config.BET_STEP_CENTS,
                  config.MAX_BET_CENTS, config.MIN_DEPOSIT_CENTS,
                  config.MAX_DEPOSIT_CENTS, config.MIN_WITHDRAWAL_CENTS):
        assert isinstance(value, int) and value > 0
    assert config.MIN_BET_CENTS <= config.MAX_BET_CENTS
    assert config.MIN_DEPOSIT_CENTS <= config.MAX_DEPOSIT_CENTS
    # Шаг кнопок не крупнее самой ставки, иначе «−шаг» уводит ниже минимума
    # с первого нажатия.
    assert config.BET_STEP_CENTS <= config.MIN_BET_CENTS
    # Вывод не дешевле минимальной ставки: иначе заявка стоит казино дороже,
    # чем сумма в ней.
    assert config.MIN_WITHDRAWAL_CENTS >= config.MIN_BET_CENTS


def test_rtp_is_house_edge():
    """Отдача меньше единицы. Прежние слоты давали 1.0625 — казино в минусе."""
    assert 0.9 <= config.RTP < 1.0
    assert 0.0 < config.PVP_RAKE < 0.1


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
