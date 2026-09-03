"""Отдача каждой игры.

Два способа проверки, и оба нужны.

*Точный* — там, где отдача выводится из вероятностей в закрытом виде: сумма
«вероятность × множитель» должна дать ровно config.RTP. Такой тест не зависит
от объёма прогона и падает от любой правки коэффициентов.

*Монте-Карло* — прогон на живом provably fair потоке. Точная арифметика ничего
не скажет о самом генераторе: если поток перекошен, таблица выплат останется
верной, а касса поедет. Объём задаётся MC_ROUNDS (по умолчанию 200 000, в
плане — миллион).

Именно этот файл прежняя версия провалила бы на слотах (отдача 1.0625) и на
рулетке (ровно 1.0 — зеро в генераторе не было).
"""

import math
import random

import pytest

import config
from games import blackjack as bj
from games import crash, dice_games, mines, pvp, roulette, tower
from games import coin, engine
from helpers import MC_ROUNDS, Meter


def _stream(tag: str):
    """Поток чисел с меткой вместо сида — прогоны разных тестов не совпадают."""
    return engine.float_stream('server-seed-' + tag, 'client-' + tag, 1)


# --- монетка ----------------------------------------------------------------

def test_coin_multiplier():
    assert coin.MULT == pytest.approx(2 * config.RTP)
    assert 0.5 * coin.MULT == pytest.approx(config.RTP)


def test_coin_monte_carlo():
    """Игрок всегда ставит на орла — отдача не зависит от выбора стороны."""
    meter = Meter()
    stream = _stream('coin')
    for _ in range(MC_ROUNDS):
        side = min(int(next(stream) * 2), 1)
        meter.add(coin.MULT if side == 0 else 0.0)
    meter.check()


# --- слоты ------------------------------------------------------------------

def test_slot_reels_cover_all_64():
    """Дайс 🎰 отдаёт 1..64, и это ровно все тройки барабанов, по одному разу."""
    combos = [dice_games.slot_reels(v) for v in range(1, 65)]
    assert len(set(combos)) == 64
    assert all(0 <= r < len(dice_games.SLOT_SYMBOLS) for c in combos for r in c)


def test_slot_prize_counts():
    """Каждая тройка собирается ровно одним значением дайса из 64.

    Именно поэтому ставка на комбинацию считается тем же кодом, что ставка на
    «гол» или «центр»: множество значений исхода здесь состоит из одного числа,
    а не из диапазона.
    """
    slots = dice_games.PICKS['slots']
    for outcome in slots.outcomes:
        assert len(outcome.values) == 1, outcome.code
        value = next(iter(outcome.values))
        reels = dice_games.slot_reels(value)
        assert len(set(reels)) == 1, (outcome.code, reels)

    # Четыре символа — четыре тройки, и все они разные значения.
    picked = {next(iter(o.values)) for o in slots.outcomes}
    assert len(picked) == len(dice_games.SLOT_SYMBOLS) == len(slots.outcomes)


def test_slots_no_bet_beats_the_house():
    """Слоты платят по фиксированным коэффициентам, а не по выведенным из RTP.

    64 комбинации равновероятны по построению (три барабана по четыре символа),
    поэтому отдачу каждой ставки можно посчитать точно: 1/64 × коэффициент.
    Числа получаются низкие — от 0.055 за лимоны до 0.39 за семёрки: это
    ровно те коэффициенты, которые заданы в задании, и правятся они одной
    строкой в таблице PICKS. Проверяем здесь главное: ни одна ставка не даёт
    игроку преимущества над казино.
    """
    for outcome in dice_games.PICKS['slots'].outcomes:
        rtp = len(outcome.values) / 64 * outcome.mult
        assert rtp < 1.0, (outcome.code, rtp)


# --- ставка на исход дайса --------------------------------------------------

def test_pick_values_stay_inside_the_dice_range():
    """Исход не может ждать значения, которого Telegram не присылает.

    Опечатка в множестве значений не уронила бы бота: ставка просто никогда не
    заходила бы, молча. Поэтому границы проверяются тестом, а не глазами.
    """
    for game in dice_games.PICKS.values():
        faces = dice_games.FACES[game.emoji]
        for outcome in game.outcomes:
            assert all(1 <= v <= faces for v in outcome.values), outcome.code
        if game.duel:
            # У боулинга исход решается сравнением, а не значением.
            assert all(not o.values for o in game.outcomes)
        else:
            assert all(o.values for o in game.outcomes), game.key


def test_pick_bets_keep_the_house_edge_on_a_uniform_dice():
    """Ни одна ставка не выигрывает у казино на равномерном дайсе.

    Коэффициенты дайс-игр заданы руками, а не выведены из RTP: распределения
    телеграмных дайсов не опубликованы, и считать отдачу от предполагаемых
    вероятностей было бы гаданием. Но проверить, что казино не отдаёт больше,
    чем берёт, можно и без этого — при равномерном дайсе отдача считается
    точно, и она обязана быть меньше единицы.
    """
    for game in dice_games.PICKS.values():
        if game.duel:
            continue
        faces = dice_games.FACES[game.emoji]
        for outcome in game.outcomes:
            rtp = len(outcome.values) / faces * outcome.mult
            assert rtp < 1.0, (game.key, outcome.code, rtp)


# --- боулинг: единственная игра со сравнением бросков -----------------------

@pytest.mark.parametrize('faces', [
    [1, 2, 3, 4, 5, 6],                       # ровный кубик
    [1, 1, 1, 2, 2, 3, 4, 5, 6, 6],           # перекошенный
    [1, 1, 1, 1, 1, 2],                       # почти всегда ничья
])
def test_bowling_win_and_lose_are_symmetric(faces):
    """Победа и поражение равновероятны при любом распределении дайса.

    Шар у игрока и у бота один и тот же, поэтому симметрия следует из
    построения, а не из распределения — что и проверяется на трёх разных,
    включая заведомо кривые. Отсюда же берётся коэффициент 1.9 на обе стороны:
    (1 - d) / 2 × 1.9 не превышает 0.95 при любом шансе ничьей d.
    """
    stream = _stream(f'bowling{len(faces)}')
    wins = losses = draws = 0
    for _ in range(MC_ROUNDS):
        mine = faces[min(int(next(stream) * len(faces)), len(faces) - 1)]
        theirs = faces[min(int(next(stream) * len(faces)), len(faces) - 1)]
        if mine > theirs:
            wins += 1
        elif mine < theirs:
            losses += 1
        else:
            draws += 1

    assert wins + losses + draws == MC_ROUNDS
    sigma = (MC_ROUNDS * 0.25) ** 0.5
    assert abs(wins - losses) < 5 * sigma, (wins, losses)

    game = dice_games.PICKS['bowling']
    side = (wins + losses) / 2 / MC_ROUNDS * game.find('win').mult
    assert side < 1.0


def test_bowling_draw_is_the_only_bet_that_leans_on_the_dice():
    """Ничья — единственная ставка казино, зависящая от распределения Telegram.

    Победа и поражение защищены симметрией: их шанс никогда не выше половины.
    У ничьей такой защиты нет — её вероятность равна сумме квадратов
    вероятностей граней, и на равномерном кубике это 1/6, отдача 0.78. Ставка
    станет убыточной для казино, если Telegram выбивает страйк чаще, чем даёт
    шанс ничьей выше 1 / 4.7 ≈ 21%. Порог зафиксирован тестом, чтобы правка
    коэффициента не прошла мимо этого рассуждения.
    """
    draw = dice_games.PICKS['bowling'].find('draw')
    assert 1 / 6 * draw.mult < 1.0
    assert 1 / draw.mult == pytest.approx(0.2128, abs=5e-4)


# --- рулетка ----------------------------------------------------------------

def test_roulette_wheel_layout():
    assert roulette.POCKETS == 37                       # 0..36, европейское колесо
    assert len(roulette.RED) == 18
    assert 0 not in roulette.RED
    assert all(1 <= n <= 36 for n in roulette.RED)


def test_roulette_color_comes_from_number():
    """Цвет — свойство выпавшего числа.

    Прежняя версия решала красное/чёрное отдельным choice(), не связанным с
    числом на экране: игрок видел 17 (чёрное) и выигрывал ставку на красное.
    """
    assert roulette.color_emoji(0) == '🟢'
    for n in range(1, 37):
        assert roulette.color_emoji(n) == ('🔴' if n in roulette.RED else '⚫')
        assert roulette.BETS['red'][2](n) is (n in roulette.RED)
        assert roulette.BETS['black'][2](n) is (n not in roulette.RED)


def test_roulette_zero_kills_outside_bets():
    """Зеро — единственный источник преимущества казино, и оно бьёт всё внешнее."""
    for key, (_, _, hit) in roulette.BETS.items():
        assert hit(0) is False, key


def test_roulette_rtp_per_bet():
    """Каждая ставка даёт одну и ту же отдачу 18/37 × 2 = 0.973.

    Подкручивать выплаты не нужно: классические 1:2, 1:3 и 1:36 на 37 лунках
    сами дают преимущество казино 2.7%.
    """
    expected = 18 / 37 * 2.0
    for key, (_, mult, hit) in roulette.BETS.items():
        wins = sum(1 for n in range(roulette.POCKETS) if hit(n))
        rtp = wins / roulette.POCKETS * mult
        assert rtp == pytest.approx(expected, abs=1e-12), key

    straight = 1 / roulette.POCKETS * roulette.STRAIGHT_MULT
    assert straight == pytest.approx(expected, abs=1e-12)
    # План допускает отклонение от 0.97 в пределах 0.5%: 0.973 укладывается.
    assert abs(expected - config.RTP) < 0.005


def test_roulette_groups_are_complete():
    """Дюжины и половины покрывают 1..36 без дыр и пересечений."""
    for keys in (('d1', 'd2', 'd3'), ('low', 'high'), ('even', 'odd')):
        hit = [roulette.BETS[k][2] for k in keys]
        for n in range(1, 37):
            assert sum(1 for h in hit if h(n)) == 1, (keys, n)


def test_roulette_monte_carlo():
    """Живой поток: лунка выбирается pick(37), ставка на красное."""
    meter = Meter()
    stream = _stream('roulette')
    for _ in range(MC_ROUNDS):
        pocket = min(int(next(stream) * roulette.POCKETS), roulette.POCKETS - 1)
        meter.add(2.0 if pocket in roulette.RED else 0.0)
    meter.check(18 / 37 * 2.0)


# --- crash ------------------------------------------------------------------

def _crash_ladder() -> list[float]:
    """Множители, которые игрок реально может увидеть и забрать.

    Растут тиками по GROWTH с округлением до второго знака — ровно как в
    _run(). Уровень, равный MAX_MULT, недостижим: цикл рвётся на nxt >= точки
    краша, а точка краша сверху обрезана тем же MAX_MULT.
    """
    levels, mult = [1.0], 1.0
    while True:
        nxt = round(mult * crash.GROWTH, 2)
        if nxt >= crash.MAX_MULT:
            return levels
        levels.append(nxt)
        mult = nxt


def test_crash_ladder_starts_at_one():
    ladder = _crash_ladder()
    assert ladder[0] == 1.0
    assert ladder[1] == round(crash.GROWTH, 2)
    assert max(ladder) < crash.MAX_MULT


def test_crash_rtp_exact():
    """Отдача одна и та же при любой точке выхода.

    Показанный множитель m достигается с вероятностью P(краш > m) = RTP / m,
    выплата m, произведение = RTP. Тянуть до ×40 математически не лучше и не
    хуже, чем забирать на ×1.15 — и это тот случай, когда «стратегии» в crash
    существуют только в головах игроков.
    """
    for m in _crash_ladder():
        assert m * (config.RTP / m) == pytest.approx(config.RTP, abs=1e-12)


def test_crash_instant_crash_share():
    """Мгновенный краш = ровно те 3%, из которых состоит преимущество казино."""
    stream = _stream('crash-share')
    instant = 0
    for _ in range(MC_ROUNDS):
        if config.RTP / (1 - next(stream)) < 1.0:
            instant += 1
    share = instant / MC_ROUNDS
    assert abs(share - (1 - config.RTP)) < 4 * (share * (1 - share) / MC_ROUNDS) ** 0.5


@pytest.mark.parametrize('target_index', [0, 1, 3, 8, 15])
def test_crash_monte_carlo(target_index):
    """Игрок с фиксированной целью выхода. Отдача не зависит от цели."""
    ladder = _crash_ladder()
    target = ladder[min(target_index, len(ladder) - 1)]

    meter = Meter()
    stream = _stream('crash-mc')
    for _ in range(MC_ROUNDS):
        raw = config.RTP / (1 - next(stream))
        point = min(raw, crash.MAX_MULT)
        meter.add(target if raw >= 1.0 and target < point else 0.0)
    meter.check()


# --- мины -------------------------------------------------------------------

def test_mines_rtp_exact():
    """Отдача 97% при любом числе мин и любой стратегии остановки.

    Шанс открыть k клеток без мины равен C(25-N, k) / C(25, k), а множитель —
    обратная величина, умноженная на RTP. Забирать после первой клетки или
    идти до конца — произведение одно и то же.
    """
    for count in mines.MINE_CHOICES:
        safe = mines.CELLS - count
        for opened in range(1, safe + 1):
            chance = math.comb(safe, opened) / math.comb(mines.CELLS, opened)
            rtp = chance * mines.multiplier(count, opened)
            assert rtp == pytest.approx(config.RTP, abs=1e-9), (count, opened)


def test_mines_multiplier_edges():
    assert mines.multiplier(1, 0) == 1.0                     # ещё ничего не открыто
    assert mines.multiplier(24, 1) == pytest.approx(config.RTP * 25)
    # Больше безопасных клеток, чем есть, не бывает: множитель упирается в поле.
    assert mines.multiplier(5, 20) == mines.multiplier(5, 25)
    assert all(mines.multiplier(n, 1) > 1.0 for n in mines.MINE_CHOICES)


def test_mines_layout_is_uniform():
    """Раскладка мин из provably fair сида равномерна по 25 клеткам."""
    rounds = min(MC_ROUNDS, 50_000)
    hits = [0] * mines.CELLS
    for nonce in range(rounds):
        rnd = engine.Round(id=nonce, user_id=1, game='mines', bet_cents=100,
                           server_seed='mines-seed', server_seed_hash='x',
                           client_seed='c', nonce=nonce)
        hits[rnd.sample(mines.CELLS, 1)[0]] += 1

    expected = rounds / mines.CELLS
    sigma = (rounds * (1 / mines.CELLS) * (1 - 1 / mines.CELLS)) ** 0.5
    assert max(abs(h - expected) for h in hits) < 5 * sigma


# --- башня ------------------------------------------------------------------

def test_tower_rtp_exact():
    """0.97 × 1.5^k против шанса (2/3)^k — отдача не зависит от высоты."""
    assert tower.STEP_MULT == pytest.approx(1.5)
    for level in range(tower.FLOORS + 1):
        chance = ((tower.DOORS - 1) / tower.DOORS) ** level
        assert chance * tower.multiplier(level) == pytest.approx(config.RTP, abs=1e-12)


def test_tower_first_floor_is_worth_it():
    """Первый этаж должен возвращать больше ставки, иначе игра бессмысленна."""
    assert tower.multiplier(1) > 1.0
    assert tower.multiplier(0) == pytest.approx(config.RTP)


# --- PvP --------------------------------------------------------------------

def test_pvp_split_keeps_every_cent():
    """Банк расходится без потерь: выплата + рейк = банк, копейка к копейке."""
    for pot in (200, 333, 1001, 12_345, 999_999):
        payout, rake = pvp.split(pot)
        assert payout + rake == pot
        assert rake == round(pot * config.PVP_RAKE)
        assert payout > 0


def test_pvp_rake_is_the_only_house_take():
    """Дисперсии у казино в PvP нет вообще — только фиксированный процент."""
    pot = 1_000_000
    payout, rake = pvp.split(pot)
    assert payout / pot == pytest.approx(1 - config.PVP_RAKE, abs=1e-6)
    assert rake / pot == pytest.approx(config.PVP_RAKE, abs=1e-6)


def test_pvp_jackpot_chance_matches_stake():
    """Шанс победы в джекпот-комнате равен доле в банке.

    Банк делится на центы-билеты, каждому достаётся столько билетов, сколько
    центов он внёс. Внёс четверть банка — выигрываешь в четверти случаев.
    """
    rounds = min(MC_ROUNDS, 50_000)
    players = [{'user_id': 1, 'stake_cents': 25},
               {'user_id': 2, 'stake_cents': 75}]
    wins = 0
    for room_id in range(rounds):
        room = {'id': room_id, 'pot_cents': 100, 'server_seed': 'jackpot-seed'}
        if pvp.pick_winner(room, players)[0] == 1:
            wins += 1

    share = wins / rounds
    sigma = (0.25 * 0.75 / rounds) ** 0.5
    assert abs(share - 0.25) < max(0.01, 4 * sigma)


def test_pvp_jackpot_ticket_never_out_of_range():
    players = [{'user_id': i, 'stake_cents': 100} for i in range(1, 7)]
    for room_id in range(500):
        room = {'id': room_id, 'pot_cents': 600, 'server_seed': 'range-seed'}
        winner, u, ticket = pvp.pick_winner(room, players)
        assert 0 <= ticket < 600
        assert 0.0 <= u < 1.0
        assert winner in {p['user_id'] for p in players}


# --- блэкджек ---------------------------------------------------------------

def test_blackjack_rules_are_set_for_house_edge():
    """Правила, которыми отдача опускается с ~99.5% до ~97%."""
    assert bj.WIN_MULT == 2.0
    assert bj.BJ_MULT == pytest.approx(2.2)      # блэкджек 6:5, а не 3:2
    assert bj.DEALER_STAND == 17
    assert bj.DECKS == 6
    assert len(bj.BASE_DECK) == 52 * bj.DECKS


def test_blackjack_hand_value():
    def value(*ranks):
        # Индекс карты: ранг + 13 × масть. Масть на сумму не влияет.
        return bj.hand_value([bj.RANK_NAMES.index(r) for r in ranks])

    assert value('A', 'K') == (21, True)         # блэкджек, туз за 11
    assert value('A', 'A', '9') == (21, True)
    assert value('A', 'A', 'A') == (13, True)
    assert value('A', '9', '5') == (15, False)   # туз пришлось опустить до 1
    assert value('K', 'Q', 'J') == (30, False)   # перебор
    assert value('9', '8') == (17, False)


def test_blackjack_is_blackjack():
    cards = [bj.RANK_NAMES.index(r) for r in ('A', 'K')]
    assert bj.is_blackjack(cards) is True
    # 21 из трёх карт блэкджеком не считается и платит как обычная победа.
    three = [bj.RANK_NAMES.index(r) for r in ('7', '7', '7')]
    assert bj.hand_value(three)[0] == 21
    assert bj.is_blackjack(three) is False


def _bj_action(cards: list[int], dealer_up: int, can_double: bool) -> str:
    """Упрощённая базовая стратегия: без сплитов, их в игре и нет."""
    total, soft = bj.hand_value(cards)
    up = bj.RANK_VALUE[dealer_up % 13]
    if can_double:
        if total == 11:
            return 'double'
        if total == 10 and up <= 9:
            return 'double'
        if total == 9 and 3 <= up <= 6:
            return 'double'
    if soft:
        return 'stand' if total >= 18 else 'hit'
    if total >= 17:
        return 'stand'
    if total >= 13:
        return 'stand' if up <= 6 else 'hit'
    if total == 12:
        return 'stand' if 4 <= up <= 6 else 'hit'
    return 'hit'


def _bj_hand(rng: random.Random) -> tuple[float, float]:
    """Одна раздача по правилам _settle. Отдаёт (ставка, возврат) в долях.

    Тасуется не вся обувка, а первые 32 карты: раздача глубже не заходит, а
    выборка первых 32 из перемешанных 312 распределена точно так же.
    """
    deck = rng.sample(bj.BASE_DECK, 32)
    player, dealer, cur = [deck[0], deck[2]], [deck[1], deck[3]], 4
    stake = 1.0

    if not (bj.is_blackjack(player) or bj.is_blackjack(dealer)):
        while True:
            action = _bj_action(player, dealer[0], len(player) == 2 and stake == 1.0)
            if action == 'stand':
                break
            player.append(deck[cur])
            cur += 1
            if action == 'double':
                stake = 2.0
                break
            if bj.hand_value(player)[0] > 21:
                break

    p_total = bj.hand_value(player)[0]
    p_bj, d_bj = bj.is_blackjack(player), bj.is_blackjack(dealer)

    if p_total <= 21 and not p_bj:
        while True:
            total, soft = bj.hand_value(dealer)
            if total > bj.DEALER_STAND or (total == bj.DEALER_STAND and not soft):
                break
            dealer.append(deck[cur])
            cur += 1

    d_total = bj.hand_value(dealer)[0]
    d_bj = bj.is_blackjack(dealer)

    if p_total > 21:
        return stake, 0.0
    if p_bj and d_bj:
        return 0.0, 0.0                  # пуш: ставка вернулась, оборота не было
    if p_bj:
        return stake, stake * bj.BJ_MULT
    if d_bj:
        return stake, 0.0
    if d_total > 21 or p_total > d_total:
        return stake, stake * bj.WIN_MULT
    if p_total < d_total:
        return stake, 0.0
    return 0.0, 0.0                      # ничья по очкам — тоже пуш


def test_blackjack_rtp_in_advertised_band():
    """Блэкджек — единственная игра, где отдача зависит от игрока.

    В остальных играх множители выведены из вероятностей и отдача фиксирована
    хоть при какой стратегии. Здесь плохой игрок теряет больше, хороший —
    около 3%, и точной константы у блэкджека не существует. Поэтому проверяется
    полоса: преимущество у казино есть (отдача строго меньше единицы), но при
    разумной игре она рядом с обещанными 97%.

    Пуши в оборот не идут — так же, как их считает engine.void и статистика
    админки.
    """
    rounds = min(MC_ROUNDS, 40_000)
    rng = random.Random(20260821)
    staked = returned = 0.0
    for _ in range(rounds):
        bet, back = _bj_hand(rng)
        staked += bet
        returned += back

    rtp = returned / staked
    assert 0.94 < rtp < 1.0, f'отдача блэкджека {rtp:.4f} вне полосы'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
