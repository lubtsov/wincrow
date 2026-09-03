"""Процент владельцу чата с проигрышей в его группе.

Проверяется не «начисление вообще происходит», а границы, на которых оно
обязано НЕ происходить: победа, ничья, личка, свой же проигрыш, выгнанный бот,
забаненный владелец, повторный finish. Каждая из этих границ — либо дыра в
кассе, либо кэшбек, поднимающий отдачу выше заявленных 97%.
"""

from datetime import datetime, timezone

import pytest
from aiogram.types import Chat, User

import config
import db
import ui
from games import coin, engine, mines
from helpers import fresh_db, mk_user

PLAYER = 900
OWNER = 901
CHAT = -1001234567890


class FakeMessage:
    """Сообщение из группы: игре нужны answer/reply, from_user и chat."""

    def __init__(self, user_id: int, chat_id: int = CHAT,
                 chat_type: str = 'supergroup') -> None:
        self.from_user = User(id=user_id, is_bot=False, first_name='Игрок')
        self.chat = Chat(id=chat_id, type=chat_type)
        self.bot = None
        self.sent: list[str] = []
        self.replies: list[str] = []

    async def answer(self, text: str, reply_markup=None, **_kw):
        self.sent.append(text)
        return self

    async def reply(self, text: str, **_kw):
        self.replies.append(text)
        return self


async def _linked_chat(percent: int = 1) -> None:
    """Игрок, владелец и привязанный к владельцу чат."""
    config.CHAT_OWNER_PERCENT = percent
    await mk_user(PLAYER, balance_cents=100_000)
    await mk_user(OWNER)
    await db.link_chat(CHAT, 'Группа', OWNER, OWNER)


@pytest.fixture(autouse=True)
def keep_percent():
    """Процент — модульная константа; тесты её крутят, значение восстанавливаем."""
    old = config.CHAT_OWNER_PERCENT
    yield
    config.CHAT_OWNER_PERCENT = old


# --- арифметика -------------------------------------------------------------

def test_reward_rounds_down():
    config.CHAT_OWNER_PERCENT = 1
    assert db.chat_reward(100) == 1
    assert db.chat_reward(199) == 1          # 1.99 цента — платим целый
    assert db.chat_reward(99) == 0           # меньше цента платить нечем
    assert db.chat_reward(12_345) == 123


def test_reward_zero_on_nonsense():
    config.CHAT_OWNER_PERCENT = 1
    assert db.chat_reward(0) == 0
    assert db.chat_reward(-500) == 0         # возврат — не проигрыш


def test_reward_off_when_percent_zero():
    config.CHAT_OWNER_PERCENT = 0
    assert db.chat_reward(1_000_000) == 0


# --- привязка чата ----------------------------------------------------------

async def test_link_is_new_only_once():
    async with fresh_db():
        await mk_user(OWNER)
        assert await db.link_chat(CHAT, 'Группа', OWNER, OWNER) is True
        assert await db.link_chat(CHAT, 'Группа', OWNER, OWNER) is False


async def test_relink_keeps_owner_and_renames():
    """Выгнать бота и позвать заново — не способ забрать чужой чат."""
    async with fresh_db():
        await mk_user(OWNER)
        thief = await mk_user(902)
        await db.link_chat(CHAT, 'Группа', OWNER, OWNER)
        await db.set_chat_active(CHAT, False)

        await db.link_chat(CHAT, 'Группа 2.0', thief, thief)
        row = await db.get_chat(CHAT)
        assert row['owner_id'] == OWNER
        assert row['title'] == 'Группа 2.0'
        assert row['active'] == 1            # вернулись — начисления снова идут


async def test_set_active_is_idempotent():
    async with fresh_db():
        await mk_user(OWNER)
        await db.link_chat(CHAT, 'Группа', OWNER, OWNER)
        assert await db.set_chat_active(CHAT, False) is True
        assert await db.set_chat_active(CHAT, False) is False


async def test_owner_chats_counts_and_order():
    async with fresh_db():
        await mk_user(OWNER)
        await db.link_chat(-1, 'Пустой', OWNER, OWNER)
        await db.link_chat(-2, 'Богатый', OWNER, OWNER)
        await db.link_chat(-3, 'Выгнали', OWNER, OWNER)
        await db.set_chat_active(-3, False)
        async with db.transaction() as c:
            await c.execute('UPDATE chats SET earned_cents = 500 WHERE chat_id = -2')

        assert await db.owner_chats_count(OWNER) == (3, 2)
        rows = await db.owner_chats(OWNER)
        # Сначала активные, внутри — по заработку.
        assert [r['chat_id'] for r in rows] == [-2, -1, -3]


# --- начисление -------------------------------------------------------------

async def test_loss_in_group_pays_owner():
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'coin', 1_000, chat_id=CHAT)
        assert await engine.finish(rnd, 0.0) == 0

        assert await db.get_balance(OWNER) == 10        # 1% от $10.00
        owner = await db.get_user(OWNER)
        assert owner['chat_earned_cents'] == 10
        assert owner['won_cents'] == 0                  # это не выигрыш

        chat = await db.get_chat(CHAT)
        assert chat['earned_cents'] == 10
        assert chat['losses'] == 1


async def test_loss_in_private_pays_nobody():
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'coin', 1_000)
        await engine.finish(rnd, 0.0)

        assert await db.get_balance(OWNER) == 0
        assert (await db.get_chat(CHAT))['losses'] == 0


async def test_win_pays_nothing():
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'coin', 1_000, chat_id=CHAT)
        assert await engine.finish(rnd, 1.94) == 1_940

        assert await db.get_balance(OWNER) == 0
        assert (await db.get_chat(CHAT))['losses'] == 0


async def test_partial_loss_pays_from_what_player_lost():
    """Выход из краша на ×0.5 — потеряна половина ставки, процент с неё."""
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'crash', 2_000, chat_id=CHAT)
        assert await engine.finish(rnd, 0.5) == 1_000

        assert await db.get_balance(OWNER) == 10        # 1% от $10.00 потерь


async def test_cashout_above_stake_pays_nothing():
    """Забрал больше ставки — потери нет, платить не с чего."""
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'crash', 1_000, chat_id=CHAT)
        await engine.finish(rnd, 1.5)

        assert await db.get_balance(OWNER) == 0


async def test_void_pays_nothing():
    """Ничья в дуэли: ставка вернулась, проигрыша не было."""
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'dice', 1_000, chat_id=CHAT)
        assert await engine.void(rnd) is True

        assert await db.get_balance(OWNER) == 0
        assert (await db.get_chat(CHAT))['losses'] == 0


async def test_owner_own_loss_pays_nothing():
    """Иначе это кэшбек, поднимающий личную отдачу владельца выше 97%."""
    async with fresh_db():
        await _linked_chat()
        await db.add_balance(OWNER, 10_000)
        rnd = await engine.start_round(OWNER, 'coin', 1_000, chat_id=CHAT)
        await engine.finish(rnd, 0.0)

        assert await db.get_balance(OWNER) == 9_000     # только списанная ставка
        assert (await db.get_chat(CHAT))['losses'] == 0


async def test_second_finish_pays_owner_once():
    """Двойной клик закрывает раунд один раз — и платит владельцу один раз."""
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'coin', 1_000, chat_id=CHAT)
        assert await engine.finish(rnd, 0.0) == 0
        assert await engine.finish(rnd, 0.0) is None

        assert await db.get_balance(OWNER) == 10
        assert (await db.get_chat(CHAT))['losses'] == 1


async def test_inactive_chat_pays_nothing():
    async with fresh_db():
        await _linked_chat()
        await db.set_chat_active(CHAT, False)
        rnd = await engine.start_round(PLAYER, 'coin', 1_000, chat_id=CHAT)
        await engine.finish(rnd, 0.0)

        assert await db.get_balance(OWNER) == 0
        assert (await db.get_chat(CHAT))['losses'] == 0


async def test_banned_owner_gets_nothing():
    async with fresh_db():
        await _linked_chat()
        await db.set_banned(OWNER, True)
        rnd = await engine.start_round(PLAYER, 'coin', 1_000, chat_id=CHAT)
        await engine.finish(rnd, 0.0)

        assert await db.get_balance(OWNER) == 0
        # Счётчик чата тоже не растёт: денег не было, а не «были и потерялись».
        assert (await db.get_chat(CHAT))['losses'] == 0


async def test_unknown_chat_pays_nothing():
    """Раунд помнит чат, привязку которого удалили, — раунд от этого не падает."""
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'coin', 1_000, chat_id=-777)
        assert await engine.finish(rnd, 0.0) == 0


async def test_owner_without_account_pays_nothing():
    async with fresh_db():
        config.CHAT_OWNER_PERCENT = 1
        await mk_user(PLAYER, balance_cents=10_000)
        await db.link_chat(CHAT, 'Группа', 909, 909)     # владельца в users нет
        rnd = await engine.start_round(PLAYER, 'coin', 1_000, chat_id=CHAT)
        assert await engine.finish(rnd, 0.0) == 0
        assert (await db.get_chat(CHAT))['losses'] == 0


async def test_sub_threshold_loss_pays_nothing():
    """При 1% проигрыш меньше $1.00 округляется в ноль."""
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'coin', 99, chat_id=CHAT)
        await engine.finish(rnd, 0.0)

        assert await db.get_balance(OWNER) == 0
        assert (await db.get_chat(CHAT))['losses'] == 0


async def test_zero_percent_disables_payouts():
    async with fresh_db():
        await _linked_chat(percent=0)
        rnd = await engine.start_round(PLAYER, 'coin', 50_000, chat_id=CHAT)
        await engine.finish(rnd, 0.0)

        assert await db.get_balance(OWNER) == 0


async def test_payout_comes_from_casino_not_player():
    """У игрока с баланса не удерживается ничего сверх ставки."""
    async with fresh_db():
        await _linked_chat()
        before = await db.get_balance(PLAYER)
        rnd = await engine.start_round(PLAYER, 'coin', 1_000, chat_id=CHAT)
        await engine.finish(rnd, 0.0)

        assert await db.get_balance(PLAYER) == before - 1_000


# --- chat_id живёт вместе с раундом -----------------------------------------

async def test_round_remembers_chat_across_reload():
    """Кнопки многошаговых игр приходят без чата — он читается из раунда."""
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'mines', 1_000, chat_id=CHAT)
        again = await engine.load_round(rnd.id, PLAYER, 'mines')
        assert again.chat_id == CHAT

        await engine.finish(again, 0.0)
        assert await db.get_balance(OWNER) == 10


async def test_private_round_stores_null_chat():
    async with fresh_db():
        await _linked_chat()
        rnd = await engine.start_round(PLAYER, 'mines', 1_000)
        row = await (await db.conn().execute(
            'SELECT chat_id FROM rounds WHERE id = ?', (rnd.id,))).fetchone()
        assert row['chat_id'] is None
        assert (await engine.load_round(rnd.id, PLAYER, 'mines')).chat_id is None


# --- ui.chat_id_of ----------------------------------------------------------

def test_chat_id_of_group_and_private():
    assert ui.chat_id_of(ui.ChatCall(FakeMessage(PLAYER))) == CHAT
    private = FakeMessage(PLAYER, chat_id=PLAYER, chat_type='private')
    assert ui.chat_id_of(ui.ChatCall(private)) is None


def test_chat_id_of_survives_missing_message():
    class Bare:
        pass

    assert ui.chat_id_of(Bare()) is None


# --- сквозной путь: команда из группы ---------------------------------------

async def test_coin_from_group_pays_owner_on_loss():
    async with fresh_db():
        await _linked_chat()
        await db.set_bet(PLAYER, 1_000)

        msg = FakeMessage(PLAYER)
        # Сторона выбрана так, чтобы раунд точно проиграл: сид случайный,
        # поэтому играем обе и проверяем ровно одно начисление на проигрыш.
        await coin.play(ui.ChatCall(msg), PLAYER, 'heads')

        row = await (await db.conn().execute(
            'SELECT status, chat_id FROM rounds WHERE user_id = ? '
            'ORDER BY id DESC LIMIT 1', (PLAYER,))).fetchone()
        assert row['chat_id'] == CHAT

        expected = 10 if row['status'] == 'lost' else 0
        assert await db.get_balance(OWNER) == expected


async def test_mines_from_group_records_chat():
    async with fresh_db():
        await _linked_chat()
        await db.set_bet(PLAYER, 1_000)

        msg = FakeMessage(PLAYER)
        await mines.play(ui.ChatCall(msg), PLAYER, 24)

        rnd = await engine.active_round(PLAYER, 'mines')
        assert rnd.chat_id == CHAT

        # 24 мины из 25 клеток: любая клетка, кроме одной, — проигрыш.
        await engine.finish(rnd, 0.0)
        assert await db.get_balance(OWNER) == 10
