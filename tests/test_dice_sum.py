"""Ставка на сумму двух и трёх кубиков.

Отдача этой игры — единственная в казино, что опирается на равномерность
кубика Telegram, поэтому проверяется дважды: точной арифметикой по всем
исходам свёртки и прогоном на равномерном кубике. Множители нигде не вписаны
руками, и тест обязан падать, если кто-то впишет.

Отдельно — раунд целиком, от текстовой команды до баланса: кубики кидает бот,
и если бросок не ушёл, ставка должна вернуться. Молча съеденная ставка
выглядит как кража.
"""

import json

import pytest

import config
import db
from games import dice_sum, engine
from games.registry import GAMES
from handlers import chat
from helpers import MC_ROUNDS, Meter, fresh_db, mk_user
from ui import ChatCall

TABLES = list(dice_sum.TABLES.values())
IDS = [t.key for t in TABLES]


# --- расклады ---------------------------------------------------------------

def test_counts_are_the_school_numbers():
    """Свёртка обязана дать 36 и 216 исходов — иначе считать нечего."""
    assert sum(dice_sum.counts(2).values()) == 36
    assert sum(dice_sum.counts(3).values()) == 216
    assert dice_sum.counts(2)[7] == 6              # 1+6 … 6+1
    assert dice_sum.counts(2)[2] == 1
    assert dice_sum.counts(3)[10] == 27
    assert dice_sum.counts(3)[3] == 1


@pytest.mark.parametrize('table', TABLES, ids=IDS)
def test_counts_are_symmetric(table):
    """Расклады суммы s и её зеркала совпадают: кубик не различает концов."""
    for s, ways in table.ways.items():
        assert ways == table.ways[table.low + table.high - s], s
    assert min(table.ways) == table.low
    assert max(table.ways) == table.high


# --- отдача -----------------------------------------------------------------

@pytest.mark.parametrize('table', TABLES, ids=IDS)
def test_rtp_exact_for_every_bet(table):
    """Отдача ровно RTP на любой ставке — и внешней, и на точную сумму.

    Множитель выведен из отдачи (RTP × исходы / выигрышные), поэтому равенство
    тождественное. Тест сторожит именно это: стоит вписать «красивую» выплату
    руками — и он падает.
    """
    for kind in ('over', 'under'):
        chance = table.outcomes(kind) / table.total
        assert chance * table.mult(kind) == pytest.approx(config.RTP, abs=1e-12)

    for number in range(table.low, table.high + 1):
        chance = table.ways[number] / table.total
        rtp = chance * table.mult('n', number)
        assert rtp == pytest.approx(config.RTP, abs=1e-12), number


@pytest.mark.parametrize('table', TABLES, ids=IDS)
def test_outer_bets_are_symmetric(table):
    """«Больше» и «меньше» обязаны платить одинаково — иначе одна из них лучше."""
    assert table.outcomes('over') == table.outcomes('under')
    assert table.mult('over') == table.mult('under')


def test_two_dice_seven_is_the_zero():
    """У двух кубиков маржа сидит в целой семёрке, и она там одна.

    Ровно 7 — шесть исходов из 36, на которых проигрывают обе внешние ставки.
    Это полный аналог зеро в рулетке: без него 2.33 не давали бы 97%.
    """
    table = dice_sum.TABLES['dice2']
    assert table.push_outcomes == 6
    assert table.outcomes('over') == table.outcomes('under') == 15
    assert table.wins('over', None, 7) is False
    assert table.wins('under', None, 7) is False


def test_three_dice_split_in_half_without_push():
    """У трёх кубиков граница дробная: 108 на 108, возвращать нечего."""
    table = dice_sum.TABLES['dice3']
    assert table.push_outcomes == 0
    assert table.outcomes('over') == table.outcomes('under') == 108
    assert table.mult('over') == pytest.approx(2 * config.RTP)


@pytest.mark.parametrize('key,picks', [
    ('dice2', [('over', None), ('n', 7), ('n', 2)]),
    ('dice3', [('over', None), ('n', 10), ('n', 3)]),
])
def test_rules_quote_the_computed_multipliers(key, picks):
    """Цифры в правилах игры — те же, что считает код.

    Правила обещают ×2.33 и ×209.52; если множитель поедет, текст врать не
    должен, а поймать это глазами в двух экранах правил нельзя.
    """
    rules = GAMES[key].rules
    table = dice_sum.TABLES[key]
    for kind, number in picks:
        assert f'×{table.mult(kind, number):.2f}' in rules, (kind, number)


# --- разбор из чата ---------------------------------------------------------

def test_parse_pick_words_and_numbers():
    assert dice_sum.parse_pick('dice2', 'Больше') == ('over', None)
    assert dice_sum.parse_pick('dice2', 'м') == ('under', None)
    assert dice_sum.parse_pick('dice2', '7') == ('n', 7)
    assert dice_sum.parse_pick('dice3', '18') == ('n', 18)
    # Сумма вне диапазона своей игры — не ставка: 13 бывает только на трёх.
    assert dice_sum.parse_pick('dice2', '13') is None
    assert dice_sum.parse_pick('dice3', '13') == ('n', 13)
    assert dice_sum.parse_pick('dice2', 'орёл') is None
    assert dice_sum.parse_pick('slots', 'больше') is None


def test_chat_words_lead_to_both_tables():
    """В чате обе игры должны быть достижимы — иначе кнопки есть, команд нет."""
    assert chat.GAME_WORDS['кубики'] == 'dice2'
    assert chat.GAME_WORDS['кубики3'] == 'dice3'
    # Дуэльный «кубик» остаётся дуэлью: слово занято до этой игры.
    assert chat.GAME_WORDS['кубик'] == 'dice'
    assert set(dice_sum.TABLES) <= set(chat.HINTS)


def test_valid_rejects_sums_outside_the_table():
    table = dice_sum.TABLES['dice2']
    assert table.valid('over', None) is True
    assert table.valid('n', 12) is True
    assert table.valid('n', 13) is False
    assert table.valid('n', None) is False
    assert table.valid('grid', None) is False


# --- Монте-Карло ------------------------------------------------------------

def _roll_stream(tag: str, dice: int):
    """Поток сумм: равномерный кубик 1–6 на provably fair числах движка."""
    stream = engine.float_stream('server-' + tag, 'client-' + tag, 1)
    while True:
        yield sum(min(int(next(stream) * dice_sum.FACES), dice_sum.FACES - 1) + 1
                  for _ in range(dice))


@pytest.mark.parametrize('key,kind,number', [
    ('dice2', 'over', None),
    ('dice2', 'under', None),
    ('dice2', 'n', 7),
    ('dice3', 'over', None),
    ('dice3', 'n', 11),
])
def test_monte_carlo_on_uniform_dice(key, kind, number):
    """Прогон на живом потоке движка: таблица выплат сходится с реальностью.

    Ровное распределение здесь — допущение про кубик Telegram, и тест
    проверяет ровно то, что от него зависит: если кубик равномерен, касса
    получает свои 3% и не больше. Перекос самого Telegram так не поймать —
    об этом честно сказано в правилах игры.
    """
    table = dice_sum.TABLES[key]
    mult = table.mult(kind, number)
    rolls = _roll_stream(f'{key}-{kind}{number}', table.dice)

    meter = Meter()
    for _ in range(min(MC_ROUNDS, 200_000)):
        # Ничьей в этой игре нет: ровно 7 — проигрыш обеих внешних ставок,
        # поэтому в счётчик идут все раунды без исключения.
        meter.add(mult if table.wins(kind, number, next(rolls)) else 0.0)
    meter.check()


# --- раунд из чата ----------------------------------------------------------

class FakeDice:
    """Ответ на send_dice: игре от него нужно одно — значение кубика."""

    def __init__(self, value: int) -> None:
        self.dice = type('D', (), {'value': value})()


class FakeMessage:
    """Сообщение с командой. Кубики «кидает» список заготовленных значений."""

    def __init__(self, user_id: int, rolls: list[int] | None = None,
                 dice_fails: bool = False) -> None:
        self.from_user = type('U', (), {'id': user_id})()
        self.chat = type('C', (), {'id': user_id, 'type': 'private'})()
        self.bot = None
        self.sent: list[str] = []
        self.replies: list[str] = []
        self.thrown: list[str] = []
        self._rolls = list(rolls or [])
        self._fails = dice_fails

    async def answer(self, text: str, reply_markup=None, **_kw):
        self.sent.append(text)
        return self

    async def reply(self, text: str, **_kw):
        self.replies.append(text)
        return self

    async def answer_dice(self, emoji: str = '🎲', **_kw):
        if self._fails:
            raise RuntimeError('dice is disabled in this chat')
        self.thrown.append(emoji)
        return FakeDice(self._rolls.pop(0))


async def _round_row(user_id: int):
    return await (await db.conn().execute(
        'SELECT status, bet_cents, payout_cents, state FROM rounds '
        'WHERE user_id = ? ORDER BY id DESC LIMIT 1', (user_id,))).fetchone()


@pytest.fixture
def instant_dice(monkeypatch):
    """Анимацию ждать незачем: значение приходит сразу в ответе на send_dice."""
    monkeypatch.setitem(dice_sum.ANIM, dice_sum.CUBE, 0.0)


async def test_win_pays_exactly_what_the_table_promises(instant_dice):
    async with fresh_db():
        user_id = await mk_user(720, balance_cents=1000)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id, rolls=[6, 6])
        await dice_sum.play(ChatCall(msg), user_id, 'dice2', 'over')

        mult = dice_sum.TABLES['dice2'].mult('over')
        payout = engine.payout_cents(100, mult)
        assert msg.thrown == [dice_sum.CUBE, dice_sum.CUBE]
        assert await db.get_balance(user_id) == 1000 - 100 + payout

        row = await _round_row(user_id)
        assert row['status'] == 'won'
        assert row['payout_cents'] == payout
        # Все броски раунда лежат в state: без них допущение о равномерности
        # кубика проверить по базе нельзя.
        assert json.loads(row['state']) == {
            'pick': 'over', 'number': None, 'dice': [6, 6], 'sum': 12}


async def test_seven_takes_both_outer_bets(instant_dice):
    """Зеро двух кубиков: ставка уходит, и игроку сказано, почему."""
    async with fresh_db():
        user_id = await mk_user(721, balance_cents=1000)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id, rolls=[3, 4])
        await dice_sum.play(ChatCall(msg), user_id, 'dice2', 'over')

        assert await db.get_balance(user_id) == 900
        assert (await _round_row(user_id))['status'] == 'lost'
        assert 'Ровно 7' in msg.sent[-1]


async def test_exact_sum_pays_the_long_odds(instant_dice):
    async with fresh_db():
        user_id = await mk_user(722, balance_cents=1000)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id, rolls=[6, 6])
        await dice_sum.play(ChatCall(msg), user_id, 'dice2', 'n', 12)

        payout = engine.payout_cents(100, dice_sum.TABLES['dice2'].mult('n', 12))
        assert payout == 3492                      # 0.97 × 36 = ×34.92
        assert await db.get_balance(user_id) == 900 + payout


async def test_dice_that_did_not_fly_returns_the_stake(instant_dice):
    """Бросок не ушёл — раунда не было. Ставка возвращается, и об этом говорят."""
    async with fresh_db():
        user_id = await mk_user(723, balance_cents=1000)
        await db.set_bet(user_id, 250)

        msg = FakeMessage(user_id, dice_fails=True)
        await dice_sum.play(ChatCall(msg), user_id, 'dice3', 'under')

        assert await db.get_balance(user_id) == 1000
        assert (await _round_row(user_id))['status'] == 'void'
        assert 'вернулась' in msg.sent[-1]


async def test_impossible_sum_never_takes_money(instant_dice):
    """13 на двух кубиках не выпадет никогда — ставку принимать нельзя."""
    async with fresh_db():
        user_id = await mk_user(724, balance_cents=1000)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id)
        await dice_sum.play(ChatCall(msg), user_id, 'dice2', 'n', 13)

        assert await db.get_balance(user_id) == 1000
        assert await _round_row(user_id) is None
        assert msg.replies and 'от 2 до 12' in msg.replies[0]
        assert not msg.thrown


async def test_chat_command_plays_the_round(instant_dice):
    """«кубики 1 больше» из чата доезжает до раунда, а не до экрана выбора."""
    async with fresh_db():
        user_id = await mk_user(725, balance_cents=1000)
        msg = FakeMessage(user_id, rolls=[5, 5])
        user = await db.get_user(user_id)

        await chat._play_game(msg, user, None, 'dice2', 100, ['больше'])

        assert msg.thrown == [dice_sum.CUBE, dice_sum.CUBE]
        assert (await _round_row(user_id))['status'] == 'won'
        assert not msg.replies


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
