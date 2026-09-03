"""Ежедневный кейс: пауза, атомарность выдачи и начисления, подписки.

Главное, что здесь проверяется, — деньги. Приз должен приезжать ровно один раз
на кейс, сколько бы кликов, вкладок и перезапусков ни случилось. Всё остальное
(тексты, кнопки, Mini App) — обвязка вокруг этих двух правил:

* один кейс в сутки, пауза считается от открытия предыдущего;
* один кейс — одно открытие и одно начисление.
"""

import asyncio
from types import SimpleNamespace

import pytest

import config
import daily
import db
from helpers import fresh_db, mk_user

PRIZE = config.DAILY_PRIZE_CENTS


class StubBot:
    """Бот, который отвечает на get_chat_member заранее заданным статусом.

    'error' — Telegram отказал: так ведёт себя канал, где бот не админ.
    """

    def __init__(self, statuses: dict | None = None) -> None:
        self.statuses = statuses or {}
        self.calls: list[tuple] = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        status = self.statuses.get(chat_id, 'member')
        if status == 'error':
            raise RuntimeError('member list is inaccessible')
        return SimpleNamespace(status=status, is_member=status == 'member')


# --- выдача и пауза ---------------------------------------------------------

async def test_issue_then_pick_starts_cooldown():
    async with fresh_db():
        uid = await mk_user(1)
        case, result = await db.issue_daily_case(uid)
        assert result == 'issued'
        assert await db.daily_ready_at(uid) == 0        # выдан, но не открыт

        picked, status = await db.pick_daily_case(uid, case['id'], case['win_index'])
        assert status == 'ok'
        assert picked['payout_cents'] == PRIZE
        assert await db.get_balance(uid) == PRIZE

        again, result = await db.issue_daily_case(uid)
        assert (again, result) == (None, 'cooldown')
        assert await db.daily_ready_at(uid) == picked['opened_at'] + config.DAILY_COOLDOWN


async def test_issue_is_idempotent_until_opened():
    """Пока кейс не открыт, повторная выдача отдаёт тот же самый."""
    async with fresh_db():
        uid = await mk_user(2)
        first, _ = await db.issue_daily_case(uid)
        second, result = await db.issue_daily_case(uid)
        assert result == 'open'
        assert second['id'] == first['id']
        rows = await (await db.conn().execute(
            'SELECT COUNT(*) n FROM daily_cases WHERE user_id = ?', (uid,))).fetchone()
        assert rows['n'] == 1


async def test_cooldown_expires():
    async with fresh_db():
        uid = await mk_user(3)
        case, _ = await db.issue_daily_case(uid)
        await db.pick_daily_case(uid, case['id'], 0)
        # Отматываем открытие на сутки назад — пауза должна кончиться.
        await db.conn().execute(
            'UPDATE daily_cases SET opened_at = opened_at - ? WHERE id = ?',
            (config.DAILY_COOLDOWN + 1, case['id']))
        assert await db.daily_ready_at(uid) == 0
        fresh, result = await db.issue_daily_case(uid)
        assert result == 'issued'
        assert fresh['id'] != case['id']

# --- деньги: ровно один раз -------------------------------------------------

async def test_second_pick_pays_nothing():
    """Двойной клик по выигрышной карточке начисляет один раз."""
    async with fresh_db():
        uid = await mk_user(10)
        case, _ = await db.issue_daily_case(uid)
        win = case['win_index']

        first, status = await db.pick_daily_case(uid, case['id'], win)
        assert (status, first['payout_cents']) == ('ok', PRIZE)
        assert await db.get_balance(uid) == PRIZE

        second, status = await db.pick_daily_case(uid, case['id'], win)
        assert status == 'already'
        assert await db.get_balance(uid) == PRIZE        # не удвоилось
        assert second['picked_index'] == win


async def test_empty_card_pays_zero():
    async with fresh_db():
        uid = await mk_user(11)
        case, _ = await db.issue_daily_case(uid)
        empty = next(i for i in range(case['cards']) if i != case['win_index'])
        picked, status = await db.pick_daily_case(uid, case['id'], empty)
        assert status == 'ok'
        assert picked['payout_cents'] == 0
        assert await db.get_balance(uid) == 0


async def test_only_one_card_wins():
    async with fresh_db():
        uid = await mk_user(12)
        case, _ = await db.issue_daily_case(uid)
        wins = [i for i in range(case['cards']) if i == case['win_index']]
        assert len(wins) == 1
        assert 0 <= case['win_index'] < config.DAILY_CARDS


async def test_parallel_picks_open_case_once():
    """Три одновременных клика по разным карточкам: открытие ровно одно."""
    async with fresh_db():
        uid = await mk_user(13)
        case, _ = await db.issue_daily_case(uid)

        results = await asyncio.gather(*(
            db.pick_daily_case(uid, case['id'], i) for i in range(case['cards'])))
        statuses = [status for _, status in results]
        assert statuses.count('ok') == 1
        assert statuses.count('already') == case['cards'] - 1

        winner = next(row for row, status in results if status == 'ok')
        assert await db.get_balance(uid) == winner['payout_cents']

async def test_pick_rejects_foreign_case_and_bad_index():
    async with fresh_db():
        mine = await mk_user(14)
        other = await mk_user(15)
        case, _ = await db.issue_daily_case(mine)

        assert await db.pick_daily_case(other, case['id'], 0) == (None, 'not_found')
        row, status = await db.pick_daily_case(mine, case['id'], case['cards'])
        assert status == 'bad_index'
        row, status = await db.pick_daily_case(mine, case['id'], -1)
        assert status == 'bad_index'
        assert await db.get_balance(mine) == 0
        # Кейс остался открытым: неудачная попытка его не сожгла.
        assert (await db.open_daily_case(mine))['id'] == case['id']


async def test_prize_does_not_touch_wagered_or_won():
    """Кейс — подарок, а не игра: оборот и won_cents он двигать не должен,
    иначе фактический RTP в статистике поедет на розданных центах."""
    async with fresh_db():
        uid = await mk_user(16, balance_cents=100)
        case, _ = await db.issue_daily_case(uid)
        await db.pick_daily_case(uid, case['id'], case['win_index'])
        row = await db.get_user(uid)
        assert row['balance_cents'] == 100 + PRIZE
        assert row['wagered_cents'] == 0
        assert row['won_cents'] == 0


async def test_win_index_spreads_over_all_cards():
    """Выигрышная карточка не приколочена к одному индексу."""
    async with fresh_db():
        seen = set()
        for uid in range(100, 160):
            await mk_user(uid)
            case, _ = await db.issue_daily_case(uid)
            seen.add(case['win_index'])
            await db.pick_daily_case(uid, case['id'], 0)
        assert seen == set(range(config.DAILY_CARDS))


async def test_daily_stats_counts_only_opened():
    async with fresh_db():
        uid = await mk_user(17)
        case, _ = await db.issue_daily_case(uid)
        assert (await db.daily_stats())['opened'] == 0
        await db.pick_daily_case(uid, case['id'], case['win_index'])
        stats = await db.daily_stats()
        assert stats == {'opened': 1, 'paid': PRIZE, 'players': 1}

# --- обязательные подписки --------------------------------------------------

async def test_channels_crud():
    async with fresh_db():
        assert await db.add_channel(-100, 'wincrow', 'WinCrow', None, 1) is True
        # Повторное добавление — обновление, а не дубль.
        assert await db.add_channel(-100, 'wincrow2', 'Новое имя', None, 1) is False
        rows = await db.list_channels()
        assert len(rows) == 1
        assert rows[0]['username'] == 'wincrow2'
        assert rows[0]['title'] == 'Новое имя'

        await db.mark_channel(-100, True, 'бот не админ')
        assert (await db.list_channels())[0]['broken'] == 1
        # Повторное добавление снимает пометку: канал починили.
        await db.add_channel(-100, 'wincrow2', 'Новое имя', None, 1)
        assert (await db.list_channels())[0]['broken'] == 0

        assert await db.remove_channel(-100) is True
        assert await db.remove_channel(-100) is False
        assert await db.list_channels() == []


async def test_no_channels_means_no_gate():
    async with fresh_db():
        uid = await mk_user(20)
        st = await daily.state(StubBot(), uid)
        assert st['status'] == 'ready'
        assert st['missing'] == []


async def test_missing_subscription_blocks_case():
    async with fresh_db():
        uid = await mk_user(21)
        await db.add_channel(-1001, 'chan', 'Канал', None, 1)
        bot = StubBot({-1001: 'left'})

        st, result = await daily.issue(bot, uid)
        assert result == 'subscribe'
        assert [r['chat_id'] for r in st['missing']] == [-1001]
        assert await db.open_daily_case(uid) is None      # кейс не выдан

        # Подписался — тот же вызов выдаёт кейс.
        st, result = await daily.issue(StubBot({-1001: 'member'}), uid)
        assert result == 'issued'
        assert st['case'] is not None

@pytest.mark.parametrize('status,subscribed', [
    ('member', True), ('administrator', True), ('creator', True),
    ('left', False), ('kicked', False),
])
async def test_member_statuses(status, subscribed):
    async with fresh_db():
        uid = await mk_user(22)
        await db.add_channel(-1002, 'chan', 'Канал', None, 1)
        missing, broken = await daily.check_channels(StubBot({-1002: status}), uid)
        assert bool(missing) is not subscribed
        assert broken == []


async def test_unverifiable_channel_does_not_block_but_is_flagged():
    """Канал, который бот не может проверить, кейс не блокирует.

    Игрок в такой поломке не виноват и починить её не может, а «подпишись» на
    канал, где он уже подписан, выглядит как сломанный бот. Зато канал
    помечается broken — админ видит это в панели.
    """
    async with fresh_db():
        uid = await mk_user(23)
        await db.add_channel(-1003, None, 'Закрытый', None, 1)
        bot = StubBot({-1003: 'error'})

        st, result = await daily.issue(bot, uid)
        assert result == 'issued'
        assert st['missing'] == []
        assert [r['chat_id'] for r in st['broken']] == [-1003]
        assert (await db.list_channels())[0]['broken'] == 1

        # Канал заработал — пометка снимается сама, без админа.
        await daily.check_channels(StubBot({-1003: 'member'}), uid)
        assert (await db.list_channels())[0]['broken'] == 0


async def test_state_skips_telegram_while_on_cooldown():
    """На паузе каналы не дёргаются: экран всё равно покажет таймер."""
    async with fresh_db():
        uid = await mk_user(24)
        await db.add_channel(-1004, 'chan', 'Канал', None, 1)
        bot = StubBot({-1004: 'member'})
        case, _ = await db.issue_daily_case(uid)
        await db.pick_daily_case(uid, case['id'], 0)

        bot.calls.clear()
        st = await daily.state(bot, uid)
        assert st['status'] == 'cooldown'
        assert bot.calls == []


def test_left_text():
    assert daily.left_text(24 * 3600) == '24 ч 00 мин'
    assert daily.left_text(3661) == '1 ч 01 мин'
    assert daily.left_text(75) == '1 мин 15 с'
    assert daily.left_text(-5) == '0 с'


# --- экран в боте -----------------------------------------------------------

async def test_bot_screen_texts_build_on_real_rows():
    """Тексты экрана собираются на настоящих строках базы, а не на словарях."""
    from handlers import daily as screen

    async with fresh_db():
        uid = await mk_user(30, balance_cents=1000)
        await db.add_channel(-1005, 'chan', 'Канал подписки', None, 1)

        st = await daily.state(StubBot({-1005: 'left'}), uid)
        assert st['status'] == 'subscribe'
        assert 'Канал подписки' in screen._subscribe_text(st)

        case, _ = await db.issue_daily_case(uid)
        assert '$0.05' in screen._ready_text(await db.get_user(uid))

        picked, _ = await db.pick_daily_case(uid, case['id'], case['win_index'])
        result = screen._result_text(picked, await db.get_balance(uid))
        assert '$0.05' in result and '$10.05' in result

        st = await daily.state(StubBot({-1005: 'member'}), uid)
        assert 'Следующий через' in screen._cooldown_text(st)
