"""Ставка на исход телеграмного дайса.

Главное, что проверяется здесь: игры больше не одинаковые. Раньше пять дайсов
из шести были одной и той же дуэлью «твой бросок против броска бота», и выбирать
игроку было нечего. Теперь ставка идёт на конкретный исход — гол, штанга, центр
мишени, три семёрки, — а сравнение двух бросков осталось ровно в одной игре, в
боулинге. Тест на это стоит отдельно: если сравнение вернётся в футбол, он
упадёт.

Дальше — раунд целиком, от кнопки и от команды в чате: списание ставки, бросок,
выплата по коэффициенту и запись всех значений в rounds.state. Значение дайса
приходит от Telegram, поэтому в тестах оно подставляется заготовленным списком:
ровно то, что бот увидел бы в ответе на send_dice, без своей случайности сверху.
"""

import json

import pytest

import config
import db
import keyboards
from games import dice_games, engine
from games.registry import GAMES
from handlers import chat
from helpers import fresh_db, mk_user
from ui import ChatCall

GAMES_LIST = list(dice_games.PICKS.values())
IDS = [g.key for g in GAMES_LIST]
SINGLE = [g for g in GAMES_LIST if not g.duel]
SINGLE_IDS = [g.key for g in SINGLE]


# --- таблица игр ------------------------------------------------------------

def test_all_six_dice_games_are_here():
    """Шесть игр на дайсах, и все они заявлены рабочими в каталоге."""
    assert set(dice_games.PICKS) == {'football', 'basketball', 'darts',
                                     'slots', 'dice', 'bowling'}
    for game in GAMES_LIST:
        spec = GAMES[game.key]
        assert spec.ready and spec.start is not None, game.key
        # Экран выбора исхода — свой, общий экран ставки этим играм не нужен.
        assert spec.own_entry is True, game.key
        assert game.emoji in dice_games.FACES
        assert game.emoji in dice_games.ANIM

@pytest.mark.parametrize('game', GAMES_LIST, ids=IDS)
def test_every_coefficient_is_a_bet_of_its_own(game):
    """Каждый коэффициент — отдельный вариант, на который можно поставить.

    Проверяется по кнопкам, а не по таблице: исход, которого нет на экране,
    поставить нельзя, сколько бы его ни описывали правила. Данные кнопки —
    pb:<игра>:<исход>, то есть переход к выбору суммы именно на этот исход.
    """
    data = {b.callback_data for row in
            dice_games._picks_kb(game).inline_keyboard for b in row}
    if game.grid:
        data |= {b.callback_data for row in
                 dice_games._grid_kb(game).inline_keyboard for b in row}

    for outcome in game.outcomes:
        assert f'pb:{game.key}:{outcome.code}' in data, outcome.code
        # Коэффициент виден игроку до ставки — на самой кнопке или в подписи
        # экрана с сеткой чисел.
        assert (f'×{outcome.mult:.2f}' in outcome.button
                or f'×{outcome.mult:.2f}' in game.grid_button), outcome.code

    assert len({o.code for o in game.outcomes}) == len(game.outcomes)
    assert len(data) >= len(game.outcomes)


@pytest.mark.parametrize('game', GAMES_LIST, ids=IDS)
def test_callback_data_fits_telegram(game):
    """64 байта — предел Telegram на callback_data. Самая длинная кнопка — ±шаг."""
    for outcome in game.outcomes:
        longest = f'pb:{game.key}:{outcome.code}:-{config.BET_STEP_CENTS * 10}'
        assert len(longest.encode()) <= 64, longest


def test_coefficients_match_the_task():
    """Коэффициенты из задания, до цента. Таблица — единственный их источник."""
    expected = {
        'football': {'goal': 1.5, 'miss': 2.3, 'bar': 4.7},
        'basketball': {'goal': 2.3, 'miss': 2.3, 'stuck': 4.7},
        'darts': {'red': 2.3, 'white': 2.3, 'center': 4.7, 'miss': 4.7},
        'slots': {'lemon': 3.5, 'grape': 4.2, 'bar': 5.4, 'seven': 25.0},
        'bowling': {'win': 1.9, 'lose': 1.9, 'draw': 4.7},
        'dice': {'over': 1.9, 'under': 1.9, 'even': 1.9, 'odd': 1.9,
                 'n1': 5.7, 'n2': 5.7, 'n3': 5.7, 'n4': 5.7, 'n5': 5.7,
                 'n6': 5.7},
    }
    for key, mults in expected.items():
        game = dice_games.PICKS[key]
        assert {o.code: o.mult for o in game.outcomes} == mults, key

# --- резолверы: что считается выигрышем -------------------------------------

WIN_MAP = {
    # значение дайса -> коды исходов, которые на нём заходят
    'football': {1: {'miss'}, 2: {'miss'}, 3: {'bar'}, 4: {'goal'}, 5: {'goal'}},
    'basketball': {1: {'miss'}, 2: {'miss'}, 3: {'stuck'},
                   4: {'goal'}, 5: {'goal'}},
    'darts': {1: {'miss'}, 2: {'red'}, 3: {'white'}, 4: {'red'}, 5: {'white'},
              6: {'center'}},
    'dice': {1: {'under', 'odd', 'n1'}, 2: {'under', 'even', 'n2'},
             3: {'under', 'odd', 'n3'}, 4: {'over', 'even', 'n4'},
             5: {'over', 'odd', 'n5'}, 6: {'over', 'even', 'n6'}},
}


@pytest.mark.parametrize('key', list(WIN_MAP), ids=list(WIN_MAP))
def test_resolver_hits_exactly_the_documented_values(key):
    """Каждое значение Telegram разобрано, и каждый исход проверен на всех.

    Таблица выше повторяет то, что написано в правилах игры, — и это ровно
    смысл теста: рассуждение про значения дайса зафиксировано отдельно от кода,
    который его применяет. Заодно закрыты и выигрышные, и проигрышные ветки:
    каждый исход прогоняется по всему диапазону, а не только по своим числам.
    """
    game = dice_games.PICKS[key]
    faces = dice_games.FACES[game.emoji]
    assert set(WIN_MAP[key]) == set(range(1, faces + 1))

    for value, winners in WIN_MAP[key].items():
        for outcome in game.outcomes:
            expected = outcome.code in winners
            assert game.wins(outcome, [value]) is expected, (value, outcome.code)


@pytest.mark.parametrize('game', SINGLE, ids=SINGLE_IDS)
def test_every_value_pays_something_and_every_outcome_can_lose(game):
    """На любом броске заходит хотя бы один исход, и любой исход бывает проигрышным.

    Первое — про честность: значения, на котором не выигрывает никто, у
    одиночных игр быть не должно (кроме слотов, где 60 комбинаций из 64
    действительно ничьи). Второе — что ни одна ставка не выигрывает всегда.
    """
    faces = dice_games.FACES[game.emoji]
    for outcome in game.outcomes:
        assert any(not game.wins(outcome, [v]) for v in range(1, faces + 1))
        assert any(game.wins(outcome, [v]) for v in range(1, faces + 1))

    if game.key != 'slots':
        for value in range(1, faces + 1):
            assert any(game.wins(o, [value]) for o in game.outcomes), value

def test_slots_decodes_real_telegram_combinations():
    """Тройка каждого символа — своё единственное значение дайса.

    Значение 🎰 кодирует три барабана по основанию 4, и тройка символа s лежит
    на 21s + 1: BAR — 1, 🍇 — 22, 🍋 — 43, 7️⃣ — 64. Числа не вписаны руками
    ни здесь, ни в коде — считаются оба раза, и сходятся.
    """
    assert dice_games.slot_reels(1) == (0, 0, 0)
    assert dice_games.slot_reels(64) == (3, 3, 3)
    assert dice_games.triple_value(dice_games.SEVEN) == 64
    assert dice_games.slot_line(64) == '7️⃣ 7️⃣ 7️⃣'
    assert dice_games.slot_line(43) == '🍋 🍋 🍋'

    slots = dice_games.PICKS['slots']
    for code, symbol in (('bar', 0), ('grape', 1), ('lemon', 2), ('seven', 3)):
        value = dice_games.triple_value(symbol)
        assert slots.find(code).values == frozenset({value}), code
        assert set(dice_games.slot_reels(value)) == {symbol}

    # Все прочие 60 комбинаций не платят ничего: ставка на «любые три» тут нет.
    wins = sum(1 for v in range(1, 65)
               if any(o.hits(v) for o in slots.outcomes))
    assert wins == 4


def test_dice_grid_is_six_numbers():
    """«Угадай число» — отдельный экран из шести кнопок, 1️⃣–6️⃣."""
    game = dice_games.PICKS['dice']
    assert game.grid == tuple(f'n{n}' for n in range(1, 7))
    rows = dice_games._grid_kb(game).inline_keyboard
    assert [len(r) for r in rows] == [3, 3, 1]        # 1-2-3 / 4-5-6 / назад
    assert [b.text for b in rows[0]] == ['1️⃣', '2️⃣', '3️⃣']
    assert [b.text for b in rows[1]] == ['4️⃣', '5️⃣', '6️⃣']
    assert rows[2][0].callback_data == 'pk:dice'


def test_dice_outer_bets_split_the_cube_in_half():
    """Больше/меньше и чёт/нечет делят шесть граней пополам без остатка.

    Проигрышной середины, как «ровно 7» в игре на сумму, у одного кубика нет —
    поэтому и коэффициент у внешних ставок одинаковый.
    """
    game = dice_games.PICKS['dice']
    for pair in (('over', 'under'), ('even', 'odd')):
        a, b = (game.find(c).values for c in pair)
        assert len(a) == len(b) == 3
        assert a | b == {1, 2, 3, 4, 5, 6}
        assert not a & b
    assert game.find('over').values == frozenset({4, 5, 6})

# --- боулинг: PvP остался только здесь --------------------------------------

def test_bowling_is_the_only_comparison_game():
    """Сравнение двух бросков живёт ровно в одной игре.

    Это и есть главная проверка переделки: если сравнение вернётся в футбол
    или дартс, тест упадёт. У одиночных игр бросок один — второго значения
    в раунде просто нет.
    """
    duels = [g.key for g in GAMES_LIST if g.duel]
    assert duels == ['bowling']
    assert dice_games.PICKS['bowling'].dice == 2
    for game in SINGLE:
        assert game.dice == 1, game.key


def test_bowling_resolver_compares_two_throws():
    """Победа, поражение и ничья решаются счётом партии, а не одним значением."""
    game = dice_games.PICKS['bowling']
    win, lose, draw = (game.find(c) for c in ('win', 'lose', 'draw'))

    for mine, theirs, winner in ((6, 1, win), (1, 6, lose), (4, 4, draw),
                                 (1, 1, draw), (6, 6, draw), (3, 2, win),
                                 (2, 3, lose)):
        for outcome in (win, lose, draw):
            assert game.wins(outcome, [mine, theirs]) is (outcome is winner), \
                (mine, theirs, outcome.code)


def test_bowling_draw_is_a_bet_and_not_a_refund():
    """Ничья платит по своему коэффициенту, а не возвращает ставку.

    В прежней версии ничья гасила раунд возвратом, и поставить на неё было
    нельзя. Теперь это такой же исход, как победа: поставил на победу, вышла
    ничья — ставка ушла.
    """
    game = dice_games.PICKS['bowling']
    assert game.find('draw').mult == 4.7
    assert game.wins(game.find('win'), [3, 3]) is False
    assert game.wins(game.find('lose'), [3, 3]) is False
    # Значений у исходов боулинга нет вовсе — иначе резолвер стал бы двойным.
    assert all(not o.values for o in game.outcomes)

# --- правила и чат ----------------------------------------------------------

@pytest.mark.parametrize('game', GAMES_LIST, ids=IDS)
def test_rules_quote_every_coefficient(game):
    """Цифры в правилах — те же, что в таблице выплат.

    Разъехаться им проще всего: коэффициент правится в одном файле, а обещание
    игроку живёт в другом. Поймать это глазами на шести экранах нельзя.
    """
    rules = GAMES[game.key].rules
    for outcome in game.outcomes:
        assert f'×{outcome.mult:.2f}' in rules, (game.key, outcome.code)


@pytest.mark.parametrize('game', GAMES_LIST, ids=IDS)
def test_chat_knows_the_game_and_its_outcomes(game):
    """Играть можно и командой: слово игры, подсказка формата и слова исходов."""
    assert game.key in chat.GAME_WORDS.values(), game.key
    assert game.key in chat.HINTS, game.key
    for outcome in game.outcomes:
        assert outcome.words, outcome.code
        assert dice_games.parse_pick(game.key, outcome.words[0]) == outcome.code
        assert dice_games.parse_pick(game.key, outcome.code) == outcome.code


def test_parse_pick_rejects_words_from_other_games():
    assert dice_games.parse_pick('football', 'ГОЛ') == 'goal'
    assert dice_games.parse_pick('darts', 'центр') == 'center'
    assert dice_games.parse_pick('slots', 'семёрки') == 'seven'
    assert dice_games.parse_pick('bowling', 'ничья') == 'draw'
    assert dice_games.parse_pick('dice', '4') == 'n4'
    # Число вне кубика и чужое слово ставкой не становятся.
    assert dice_games.parse_pick('dice', '7') is None
    assert dice_games.parse_pick('football', 'центр') is None
    assert dice_games.parse_pick('dice2', 'гол') is None


def test_bot_assembles_with_the_dice_router():
    """Бот поднимается, и кнопки новых игр кому-то адресованы.

    Роутер без регистрации — самая тихая поломка из возможных: экран рисуется,
    кнопки нажимаются, и ничего не происходит.
    """
    import handlers

    assert dice_games.router in handlers.routers
    names = [r.name for r in handlers.routers]
    # Текстовые команды идут последними, иначе они съедают ввод суммы.
    assert names[-1] == 'chat'
    assert names.index('dice_games') < names.index('chat')

# --- раунд целиком ----------------------------------------------------------

class FakeDice:
    """Ответ Telegram на send_dice: игре от него нужно одно — значение."""

    def __init__(self, value: int) -> None:
        self.dice = type('D', (), {'value': value})()


class FakeMessage:
    """Сообщение игрока. Дайсы «кидает» заготовленный список значений.

    Своей случайности в раунде нет вообще, поэтому подставить значение — это и
    есть полная имитация броска: бот видит ровно то же поле в ответе на
    send_dice.
    """

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
        'SELECT game, status, bet_cents, payout_cents, state FROM rounds '
        'WHERE user_id = ? ORDER BY id DESC LIMIT 1', (user_id,))).fetchone()


@pytest.fixture
def instant_dice(monkeypatch):
    """Анимацию ждать незачем: значение известно из ответа на send_dice."""
    for emoji in dice_games.ANIM:
        monkeypatch.setitem(dice_games.ANIM, emoji, 0.0)

async def test_win_pays_bet_times_coefficient(instant_dice):
    """Пример из задания: футбол, штанга, ставка 100, ×4.7 — выигрыш 470."""
    async with fresh_db():
        user_id = await mk_user(810, balance_cents=1000)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id, rolls=[3])          # 3 — мяч в штангу
        await dice_games.play(ChatCall(msg), user_id, 'football', 'bar')

        assert msg.thrown == ['⚽']                    # один бросок, не два
        assert await db.get_balance(user_id) == 1000 - 100 + 470

        row = await _round_row(user_id)
        assert row['game'] == 'football'
        assert row['status'] == 'won'
        assert row['payout_cents'] == 470
        # Ставка и бросок лежат в базе: трактовку значения можно перепроверить.
        assert json.loads(row['state']) == {'pick': 'bar', 'values': [3]}


async def test_losing_outcome_takes_the_stake(instant_dice):
    """Не тот исход — ставка не возвращается, и в тексте сказано, что выпало."""
    async with fresh_db():
        user_id = await mk_user(811, balance_cents=1000)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id, rolls=[4])          # 4 — гол, а ставка на штангу
        await dice_games.play(ChatCall(msg), user_id, 'football', 'bar')

        assert await db.get_balance(user_id) == 900
        row = await _round_row(user_id)
        assert row['status'] == 'lost'
        assert row['payout_cents'] == 0
        assert 'Гол' in msg.sent[-1]


@pytest.mark.parametrize('key,code,value,mult', [
    ('football', 'goal', 5, 1.5),
    ('football', 'miss', 1, 2.3),
    ('basketball', 'goal', 4, 2.3),
    ('basketball', 'stuck', 3, 4.7),
    ('darts', 'red', 2, 2.3),
    ('darts', 'white', 5, 2.3),
    ('darts', 'center', 6, 4.7),
    ('darts', 'miss', 1, 4.7),
    ('dice', 'over', 6, 1.9),
    ('dice', 'under', 1, 1.9),
    ('dice', 'even', 2, 1.9),
    ('dice', 'odd', 3, 1.9),
    ('dice', 'n4', 4, 5.7),
    ('slots', 'seven', 64, 25.0),
    ('slots', 'lemon', 43, 3.5),
])
async def test_every_winning_outcome_pays_its_own_coefficient(
        instant_dice, key, code, value, mult):
    """Все выигрышные ветки одиночных игр — по одной на каждый коэффициент."""
    async with fresh_db():
        user_id = await mk_user(820, balance_cents=10_000)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id, rolls=[value])
        await dice_games.play(ChatCall(msg), user_id, key, code)

        payout = engine.payout_cents(100, mult)
        assert payout == round(100 * mult)
        assert await db.get_balance(user_id) == 10_000 - 100 + payout
        assert (await _round_row(user_id))['status'] == 'won'

@pytest.mark.parametrize('key,code,value', [
    ('football', 'goal', 3),          # штанга вместо гола
    ('football', 'miss', 4),
    ('football', 'bar', 2),
    ('basketball', 'goal', 1),
    ('basketball', 'miss', 5),
    ('basketball', 'stuck', 4),
    ('darts', 'red', 3),              # соседнее кольцо, другой цвет
    ('darts', 'white', 4),
    ('darts', 'center', 5),           # почти центр — это ещё не центр
    ('darts', 'miss', 2),
    ('dice', 'over', 3),              # граница: 3 — это уже «меньше»
    ('dice', 'under', 4),             # граница: 4 — это уже «больше»
    ('dice', 'even', 5),
    ('dice', 'odd', 6),
    ('dice', 'n4', 5),
    ('slots', 'seven', 63),           # на единицу мимо джекпота
    ('slots', 'lemon', 42),
    ('slots', 'bar', 2),
])
async def test_every_losing_outcome_keeps_the_stake(instant_dice, key, code, value):
    """Проигрышные ветки, включая граничные значения у каждой игры."""
    async with fresh_db():
        user_id = await mk_user(830, balance_cents=10_000)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id, rolls=[value])
        await dice_games.play(ChatCall(msg), user_id, key, code)

        assert await db.get_balance(user_id) == 9_900
        row = await _round_row(user_id)
        assert row['status'] == 'lost'
        assert row['payout_cents'] == 0


async def test_stake_is_debited_before_the_throw(instant_dice):
    """Ставка списывается одним шагом с созданием раунда, до броска.

    Порядок важен: пока раунда нет, кликом из старого сообщения можно было бы
    кинуть дайс дважды за одну ставку.
    """
    async with fresh_db():
        user_id = await mk_user(840, balance_cents=500)
        await db.set_bet(user_id, 250)

        msg = FakeMessage(user_id, rolls=[1])
        await dice_games.play(ChatCall(msg), user_id, 'darts', 'miss')

        row = await _round_row(user_id)
        assert row['bet_cents'] == 250
        # 500 - 250 списаны, выплата 250 × 4.7 = 1175 зачислена.
        assert await db.get_balance(user_id) == 500 - 250 + 1_175


async def test_not_enough_money_never_throws_the_dice(instant_dice):
    """Не хватает на ставку — ни раунда, ни броска, и игроку сказано почему."""
    async with fresh_db():
        user_id = await mk_user(841, balance_cents=50)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id, rolls=[6])
        await dice_games.play(ChatCall(msg), user_id, 'dice', 'n6')

        assert await db.get_balance(user_id) == 50
        assert await _round_row(user_id) is None
        assert not msg.thrown
        assert msg.replies and 'Не хватает' in msg.replies[0]

async def test_repeat_bet_starts_a_fresh_round(instant_dice):
    """«Ещё раз» — это новый раунд на ту же ставку и тот же исход.

    Кнопка повтора несёт исход в callback_data, поэтому повторяется именно та
    ставка, а не «что там сейчас выбрано».
    """
    async with fresh_db():
        user_id = await mk_user(850, balance_cents=1000)
        await db.set_bet(user_id, 100)

        call = ChatCall(FakeMessage(user_id, rolls=[6]))
        await dice_games.play(call, user_id, 'dice', 'over')
        again = ChatCall(FakeMessage(user_id, rolls=[1]))
        await dice_games.play(again, user_id, 'dice', 'over')

        # 1000 - 100 + 190 (выиграл) - 100 (проиграл) = 990
        assert await db.get_balance(user_id) == 990
        rows = await (await db.conn().execute(
            'SELECT status, nonce FROM rounds WHERE user_id = ? ORDER BY id',
            (user_id,))).fetchall()
        assert [r['status'] for r in rows] == ['won', 'lost']
        assert len({r['nonce'] for r in rows}) == 2

        repeat = keyboards.pick_again('dice', 'over')
        assert repeat.inline_keyboard[0][0].callback_data == 'pp:dice:over'


@pytest.mark.parametrize('mine,theirs,code,status', [
    (6, 1, 'win', 'won'),
    (6, 1, 'lose', 'lost'),
    (1, 6, 'lose', 'won'),
    (4, 4, 'draw', 'won'),
    (4, 4, 'win', 'lost'),
])
async def test_bowling_round_stays_pvp(instant_dice, mine, theirs, code, status):
    """Боулинг кидает два шара и сравнивает их — механика оставлена намеренно."""
    async with fresh_db():
        user_id = await mk_user(860, balance_cents=1000)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id, rolls=[mine, theirs])
        await dice_games.play(ChatCall(msg), user_id, 'bowling', code)

        assert msg.thrown == ['🎳', '🎳']
        row = await _round_row(user_id)
        assert row['status'] == status
        assert json.loads(row['state']) == {'pick': code,
                                            'values': [mine, theirs]}
        mult = dice_games.PICKS['bowling'].find(code).mult
        won = engine.payout_cents(100, mult) if status == 'won' else 0
        assert await db.get_balance(user_id) == 900 + won
        # Счёт партии показан игроку — иначе сравнение нельзя проверить.
        assert f'{mine} : {theirs}' in msg.sent[-1]

async def test_dice_that_did_not_fly_returns_the_stake(instant_dice):
    """Бросок не ушёл — раунда не было. Ставка возвращается, и об этом говорят.

    Молча съеденная ставка выглядит как кража, а запретить дайсы в группе
    может любой администратор чата.
    """
    async with fresh_db():
        user_id = await mk_user(870, balance_cents=1000)
        await db.set_bet(user_id, 250)

        msg = FakeMessage(user_id, dice_fails=True)
        await dice_games.play(ChatCall(msg), user_id, 'slots', 'seven')

        assert await db.get_balance(user_id) == 1000
        assert (await _round_row(user_id))['status'] == 'void'
        assert 'вернулась' in msg.sent[-1]


async def test_unknown_outcome_never_takes_money(instant_dice):
    """Исхода нет в игре — денег не берём и дайс не кидаем."""
    async with fresh_db():
        user_id = await mk_user(871, balance_cents=1000)
        await db.set_bet(user_id, 100)

        msg = FakeMessage(user_id, rolls=[6])
        await dice_games.play(ChatCall(msg), user_id, 'football', 'center')
        await dice_games.play(ChatCall(msg), user_id, 'нет-такой-игры', 'goal')

        assert await db.get_balance(user_id) == 1000
        assert await _round_row(user_id) is None
        assert not msg.thrown


async def test_chat_command_plays_the_round(instant_dice):
    """«футбол 1 штанга» из чата доезжает до раунда, а не до экрана выбора."""
    async with fresh_db():
        user_id = await mk_user(880, balance_cents=1000)
        msg = FakeMessage(user_id, rolls=[3])
        user = await db.get_user(user_id)

        await chat._play_game(msg, user, None, 'football', 100, ['штанга'])

        assert msg.thrown == ['⚽']
        row = await _round_row(user_id)
        assert row['status'] == 'won'
        assert row['bet_cents'] == 100
        assert not msg.replies


async def test_chat_command_explains_a_wrong_outcome(instant_dice):
    """Непонятное слово — подсказка формата, а не списанная ставка."""
    async with fresh_db():
        user_id = await mk_user(881, balance_cents=1000)
        msg = FakeMessage(user_id, rolls=[3])
        user = await db.get_user(user_id)

        await chat._play_game(msg, user, None, 'football', 100, ['офсайд'])

        assert not msg.thrown
        assert await _round_row(user_id) is None
        assert msg.replies and 'штанга' in msg.replies[0]
        # Ставка при этом всё равно сохранилась — её просили сменить явно.
        assert (await db.get_user(user_id))['bet_cents'] == 100


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
