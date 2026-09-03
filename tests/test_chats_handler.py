"""Привязка чата к владельцу: my_chat_member и экран «Мои чаты».

Хендлер вызывается настоящим ChatMemberUpdated — это единственное место, где
бот узнаёт, что его добавили в группу, и ошибка здесь означает молча
неработающую половину рефералки.
"""

from datetime import datetime, timezone

import pytest
from aiogram.types import (Chat, ChatMemberLeft, ChatMemberMember,
                           ChatMemberOwner, ChatMemberUpdated, User)

import config
import db
from handlers import chats
from helpers import fresh_db, mk_user

BOT = User(id=42, is_bot=True, first_name='Casino')
CHAT = -1009876543210


class FakeBot:
    """Бот для хендлера: список админов чата и отправка приветствия."""

    def __init__(self, admins=(), fail_send: bool = False) -> None:
        self.admins = list(admins)
        self.fail_send = fail_send
        self.sent: list[tuple[int, str]] = []

    async def get_chat_administrators(self, chat_id: int):
        if self.admins == 'boom':
            raise RuntimeError('нет прав')
        return self.admins

    async def send_message(self, chat_id: int, text: str, **_kw):
        if self.fail_send:
            raise RuntimeError('писать в чат нельзя')
        self.sent.append((chat_id, text))


def _event(new_status, adder: User, chat_type: str = 'supergroup',
           title: str = 'Группа') -> ChatMemberUpdated:
    return ChatMemberUpdated(
        chat=Chat(id=CHAT, type=chat_type, title=title),
        from_user=adder,
        date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberLeft(user=BOT),
        new_chat_member=new_status)


def _joined(adder: User, **kw) -> ChatMemberUpdated:
    return _event(ChatMemberMember(user=BOT), adder, **kw)


def _left(adder: User, **kw) -> ChatMemberUpdated:
    return _event(ChatMemberLeft(user=BOT), adder, **kw)


@pytest.fixture(autouse=True)
def keep_percent():
    old = config.CHAT_OWNER_PERCENT
    yield
    config.CHAT_OWNER_PERCENT = old


# --- кто становится получателем ---------------------------------------------

async def test_creator_gets_the_chat_not_the_adder():
    """Добавить бота может любой участник — платим создателю группы."""
    async with fresh_db():
        creator = User(id=801, is_bot=False, first_name='Создатель')
        adder = User(id=802, is_bot=False, first_name='Участник')
        bot = FakeBot([ChatMemberOwner(user=creator, is_anonymous=False)])

        await chats.bot_in_chat(_joined(adder).as_(bot))

        row = await db.get_chat(CHAT)
        assert row['owner_id'] == 801
        assert row['added_by'] == 802
        assert row['title'] == 'Группа'
        assert row['active'] == 1
        # Создателя могло не быть в базе — теперь он там есть, иначе платить
        # было бы некому.
        assert await db.get_user(801) is not None


async def test_adder_is_fallback_when_creator_unknown():
    """Список админов не отдался — привязку не теряем, платим позвавшему."""
    async with fresh_db():
        adder = User(id=803, is_bot=False, first_name='Участник')
        bot = FakeBot('boom')

        await chats.bot_in_chat(_joined(adder).as_(bot))

        assert (await db.get_chat(CHAT))['owner_id'] == 803


async def test_bot_creator_is_ignored():
    """Создатель-бот (бывает после миграции) получателем быть не может."""
    async with fresh_db():
        adder = User(id=804, is_bot=False, first_name='Участник')
        bot = FakeBot([ChatMemberOwner(user=BOT, is_anonymous=False)])

        await chats.bot_in_chat(_joined(adder).as_(bot))

        assert (await db.get_chat(CHAT))['owner_id'] == 804


# --- приветствие ------------------------------------------------------------

async def test_greeting_only_on_first_link():
    async with fresh_db():
        adder = User(id=805, is_bot=False, first_name='Участник')
        bot = FakeBot()

        await chats.bot_in_chat(_joined(adder).as_(bot))
        assert len(bot.sent) == 1
        assert str(config.CHAT_OWNER_PERCENT) + '%' in bot.sent[0][1]

        await chats.bot_in_chat(_joined(adder).as_(bot))
        assert len(bot.sent) == 1           # вернулись в знакомый чат — молчим


async def test_link_survives_muted_bot():
    """Права писать в чат может не быть — привязка обязана остаться."""
    async with fresh_db():
        adder = User(id=806, is_bot=False, first_name='Участник')
        bot = FakeBot(fail_send=True)

        await chats.bot_in_chat(_joined(adder).as_(bot))

        assert await db.get_chat(CHAT) is not None


# --- уход из чата -----------------------------------------------------------

async def test_leaving_stops_payouts_and_keeps_earnings():
    async with fresh_db():
        owner = await mk_user(807)
        await db.link_chat(CHAT, 'Группа', owner, owner)
        async with db.transaction() as c:
            await c.execute('UPDATE chats SET earned_cents = 250 WHERE chat_id = ?',
                            (CHAT,))

        bot = FakeBot()
        await chats.bot_in_chat(_left(User(id=807, is_bot=False,
                                          first_name='Владелец')).as_(bot))

        row = await db.get_chat(CHAT)
        assert row['active'] == 0
        assert row['earned_cents'] == 250


async def test_leaving_unknown_chat_is_harmless():
    async with fresh_db():
        bot = FakeBot()
        await chats.bot_in_chat(_left(User(id=808, is_bot=False,
                                          first_name='Кто-то')).as_(bot))
        assert await db.get_chat(CHAT) is None


async def test_return_after_kick_reactivates():
    async with fresh_db():
        owner = User(id=809, is_bot=False, first_name='Владелец')
        bot = FakeBot([ChatMemberOwner(user=owner, is_anonymous=False)])

        await chats.bot_in_chat(_joined(owner).as_(bot))
        await chats.bot_in_chat(_left(owner).as_(bot))
        assert (await db.get_chat(CHAT))['active'] == 0

        await chats.bot_in_chat(_joined(owner).as_(bot))
        assert (await db.get_chat(CHAT))['active'] == 1


# --- экран «Мои чаты» -------------------------------------------------------

async def test_chats_screen_lists_chats_with_marks():
    async with fresh_db():
        config.CHAT_OWNER_PERCENT = 1
        owner = await mk_user(810)
        await db.link_chat(-11, 'Живая', owner, owner)
        await db.link_chat(-12, 'Мёртвая', owner, owner)
        await db.set_chat_active(-12, False)

        text = await chats.chats_text(await db.get_user(owner))
        assert 'Живая' in text and 'Мёртвая' in text
        assert '✅' in text and '✖️' in text
        assert '$1.00' in text          # порог, ниже которого выплата нулевая


async def test_chats_screen_without_chats_explains_how():
    async with fresh_db():
        owner = await mk_user(811)
        text = await chats.chats_text(await db.get_user(owner))
        assert 'ни одного чата' in text


async def test_chats_screen_escapes_title():
    """Название группы приходит от пользователя — в HTML его нельзя вставлять сырым."""
    async with fresh_db():
        owner = await mk_user(812)
        await db.link_chat(-13, '<b>злой</b>', owner, owner)

        text = await chats.chats_text(await db.get_user(owner))
        assert '&lt;b&gt;злой&lt;/b&gt;' in text


async def test_chats_screen_falls_back_to_id():
    async with fresh_db():
        owner = await mk_user(813)
        await db.link_chat(-14, None, owner, owner)

        assert 'ID -14' in await chats.chats_text(await db.get_user(owner))
