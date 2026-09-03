"""Текстовые команды из чата.

Разбор проверяется как обычная функция, а сцепка с aiogram — прогоном
настоящего Message через обозреватель роутера: фильтр обязан не только сказать
«да», но и положить разобранные части в аргументы хендлера.

Отдельно проверяется, что игра действительно запускается от текстовой команды
и списывает ставку: подделка CallbackQuery (ui.ChatCall) — самое хрупкое место
всей затеи, и падать она должна в тестах, а не в чате.
"""

from datetime import datetime, timezone

import pytest
from aiogram import F, Router
from aiogram.types import Chat, Message, User

import db
import emoji as E
from games import coin, dice_games, dice_sum, mines, roulette
from handlers import chat
from helpers import fresh_db, mk_user


def _msg(text: str, chat_type: str = 'private', user_id: int = 501) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id if chat_type == 'private' else -100, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name='Игрок'),
        text=text)


# --- разбор -----------------------------------------------------------------

def test_parse_game_with_bet_and_param():
    assert chat.parse('мины 0.5 2') == ('game', 'mines', 50, ['2'])


def test_parse_command_with_amount():
    assert chat.parse('деп 5') == ('cmd', 'dep', 500, [])


def test_parse_ignores_case_slash_and_botname():
    assert chat.parse('/Мины@pilot_bot 1') == ('game', 'mines', 100, [])


def test_parse_takes_bet_in_any_position():
    """«монетка орёл 1» и «монетка 1 орёл» — одно и то же."""
    assert chat.parse('монетка орёл 1') == ('game', 'coin', 100, ['орёл'])
    assert chat.parse('монетка 1 орёл') == ('game', 'coin', 100, ['орёл'])


def test_parse_without_bet():
    assert chat.parse('баланс') == ('cmd', 'balance', None, [])
    assert chat.parse('мины') == ('game', 'mines', None, [])


@pytest.mark.parametrize('text', [
    '',
    'привет',
    'мины взрываются красиво сегодня',      # больше трёх слов — это фраза
    'а мины 1',                             # команда только первым словом
])
def test_parse_rejects_not_commands(text):
    assert chat.parse(text) is None


def test_every_alias_points_to_real_game():
    for word, key in chat.GAME_WORDS.items():
        assert key in chat.GAMES, f'{word} -> {key}'


def test_hints_cover_games_with_params():
    """У каждой игры с третьим токеном должна быть подсказка про формат.

    Ожидание собирается из кода, а не перечисляется руками: список дайс-игр
    вырос (слоты, кости, дартс, футбол, баскет, боулинг), и захардкоженный
    набор в тесте отстал от бота, хотя сами подсказки на месте.
    """
    with_params = ({'mines', 'coin', 'roulette', 'crash'}
                   | set(dice_sum.TABLES) | set(dice_games.PICKS))
    assert set(chat.HINTS) == with_params


# --- параметры --------------------------------------------------------------

def test_side_words():
    assert coin.parse_side('Орёл') == 'heads'
    assert coin.parse_side('решка') == 'tails'
    assert coin.parse_side('ребро') is None


def test_roulette_words_and_numbers():
    assert roulette.parse_bet('красное') == ('red', None)
    assert roulette.parse_bet('д2') == ('d2', None)
    assert roulette.parse_bet('0') == ('n', 0)
    assert roulette.parse_bet('36') == ('n', 36)
    assert roulette.parse_bet('37') is None
    assert roulette.parse_bet('зелёное') is None


def test_roulette_words_are_valid_bets():
    for word, kind in roulette.BET_WORDS.items():
        assert kind in roulette.BETS, word


def test_mult_param():
    assert chat._mult_param('2.5') == 2.5
    assert chat._mult_param('×2,5') == 2.5
    assert chat._mult_param('x2') == 2.0
    assert chat._mult_param('высоко') is None


def test_int_param():
    assert chat._int_param('3', 1, 24) == 3
    assert chat._int_param('25', 1, 24) is None
    assert chat._int_param('-1', 1, 24) is None
    assert chat._int_param('три', 1, 24) is None


# --- сцепка с aiogram -------------------------------------------------------

async def test_filter_injects_parsed_parts():
    """Фильтр отдаёт хендлеру kind/key/cents/params, а не только True."""
    router = Router()
    seen: dict = {}

    @router.message(F.text, chat.chat_command)
    async def probe(message: Message, kind: str, key: str, cents, params):
        seen.update(kind=kind, key=key, cents=cents, params=params)

    await router.message.trigger(_msg('мины 0.5 2'))
    assert seen == {'kind': 'game', 'key': 'mines', 'cents': 50,
                    'params': ['2']}


async def test_filter_lets_plain_text_through():
    """Не команда — фильтр молчит, и текст достаётся фолбэку с меню."""
    router = Router()
    called = []

    @router.message(F.text, chat.chat_command)
    async def probe(message: Message, **_kw):
        called.append(1)

    await router.message.trigger(_msg('да ну не может быть'))
    assert not called


# --- игра из чата -----------------------------------------------------------

class FakeMessage:
    """Сообщение с командой: игре от него нужны answer/reply и from_user."""

    def __init__(self, user_id: int) -> None:
        self.from_user = User(id=user_id, is_bot=False, first_name='Игрок')
        self.chat = Chat(id=user_id, type='private')
        self.bot = None
        self.sent: list[str] = []
        self.replies: list[str] = []

    async def answer(self, text: str, reply_markup=None, **_kw):
        self.sent.append(text)
        return self

    async def reply(self, text: str, **_kw):
        self.replies.append(text)
        return self


async def test_mines_plays_from_chat_command():
    async with fresh_db():
        user_id = await mk_user(700, balance_cents=1000)
        await db.set_bet(user_id, 200)

        msg = FakeMessage(user_id)
        await mines.play(chat.ChatCall(msg), user_id, 5)

        # Ставка списана, поле нарисовано, раунд ждёт клика. Значки в тексте —
        # премиальные теги, поэтому сверяемся с подложками (emoji.strip).
        assert await db.get_balance(user_id) == 800
        assert msg.sent and '5 💣' in E.strip(msg.sent[-1])
        assert not msg.replies

        row = await (await db.conn().execute(
            'SELECT status, bet_cents FROM rounds WHERE user_id = ?',
            (user_id,))).fetchone()
        assert row['status'] == 'active'
        assert row['bet_cents'] == 200


async def test_chat_game_reports_lack_of_money_as_reply():
    """Тост в чате показать некому — важное сообщение уезжает ответом."""
    async with fresh_db():
        user_id = await mk_user(701, balance_cents=10)
        await db.set_bet(user_id, 500)

        msg = FakeMessage(user_id)
        await mines.play(chat.ChatCall(msg), user_id, 3)

        assert not msg.sent
        assert msg.replies and 'Не хватает' in msg.replies[0]
        assert await db.get_balance(user_id) == 10
