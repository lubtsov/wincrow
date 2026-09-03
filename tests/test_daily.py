"""Ежедневный кейс: пауза, атомарность выдачи и начисления, подписки, серия.

Главное, что здесь проверяется, — деньги. Приз должен приезжать ровно один раз
на кейс, сколько бы кликов, вкладок и перезапусков ни случилось. Всё остальное
(тексты, кнопки, Mini App) — обвязка вокруг этих двух правил:

* один кейс в сутки, пауза считается от открытия предыдущего;
* один кейс — одно открытие и одно начисление.

Третье правило — серия: каждая угаданная карточка подряд добавляет цент к призу
следующего кейса, а пустая карточка или опоздание гасят огонёк. Проверяется и
то, что приз фиксируется в момент выдачи: иначе его можно было бы раздуть,
подождав с открытием.
"""

import asyncio
from types import SimpleNamespace

import pytest

import config
import daily
import db
from helpers import fresh_db, mk_user

PRIZE = config.DAILY_PRIZE_CENTS
STEP = config.DAILY_STREAK_STEP_CENTS


async def shift_case_back(case_id: int, seconds: int) -> None:
    """Отматывает открытие кейса назад: «прошло столько-то времени»."""
    await db.conn().execute(
        'UPDATE daily_cases SET opened_at = opened_at - ? WHERE id = ?',
        (seconds, case_id))


async def play_day(uid: int, *, win: bool = True):
    """День серии: кейс выдан, карточка открыта, сутки прошли.

    Возвращает открытый кейс. `win=False` — игрок ткнул пустую карточку, то есть
    серия должна оборваться.
    """
    case, result = await db.issue_daily_case(uid)
    assert result == 'issued', result
    index = case['win_index'] if win else next(
        i for i in range(case['cards']) if i != case['win_index'])
    picked, status = await db.pick_daily_case(uid, case['id'], index)
    assert status == 'ok'
    await shift_case_back(picked['id'], config.DAILY_COOLDOWN + 1)
    return picked


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

# --- серия ------------------------------------------------------------------

def test_streak_prize_grows_by_one_step_a_day():
    """День 1 — базовый приз, каждый следующий на шаг больше."""
    assert db.streak_prize(1) == PRIZE
    assert db.streak_prize(2) == PRIZE + STEP
    assert db.streak_prize(3) == PRIZE + 2 * STEP          # 3 дня -> $0.07
    assert db.streak_prize(0) == PRIZE                     # серии ещё нет
    # Выше предела приз замирает: это защита кассы, а не подарок за верность.
    cap = config.DAILY_STREAK_MAX_DAYS
    assert db.streak_prize(cap) == db.streak_prize(cap + 50)


async def test_three_days_in_a_row_pay_five_six_seven():
    async with fresh_db():
        uid = await mk_user(40)
        payouts = [(await play_day(uid))['payout_cents'] for _ in range(3)]
        assert payouts == [PRIZE, PRIZE + STEP, PRIZE + 2 * STEP]
        assert await db.get_balance(uid) == sum(payouts)

        streak = await db.daily_streak(uid)
        assert streak['streak'] == 3
        assert streak['day'] == 4                          # следующий — четвёртый
        assert streak['prize_cents'] == PRIZE + 3 * STEP
        assert streak['expires_at'] > db.now()


async def test_empty_card_burns_the_streak():
    """Не угадал — огонёк потух, и следующий кейс снова базовый."""
    async with fresh_db():
        uid = await mk_user(41)
        await play_day(uid)
        await play_day(uid)
        assert (await db.daily_streak(uid))['prize_cents'] == PRIZE + 2 * STEP

        third = await play_day(uid, win=False)
        assert third['payout_cents'] == 0
        assert third['prize_cents'] == PRIZE + 2 * STEP    # в кейсе лежало $0.07

        streak = await db.daily_streak(uid)
        assert streak['streak'] == 0
        assert streak['day'] == 1
        assert streak['prize_cents'] == PRIZE
        assert streak['expires_at'] == 0

        fresh, _ = await db.issue_daily_case(uid)
        assert fresh['streak'] == 1
        assert fresh['prize_cents'] == PRIZE


async def test_late_player_starts_the_streak_over():
    """Опоздал за кейсом — серия сгорает, даже если карточку угадал."""
    async with fresh_db():
        uid = await mk_user(42)
        first = await play_day(uid)
        await play_day(uid)
        assert (await db.daily_streak(uid))['streak'] == 2

        # Ещё сутки простоя: срок серии (пауза плюс запас) вышел.
        await shift_case_back((await db.last_daily_case(uid))['id'],
                              config.DAILY_STREAK_GRACE + 2)
        streak = await db.daily_streak(uid)
        assert streak['streak'] == 0
        assert streak['prize_cents'] == PRIZE
        assert first['prize_cents'] == PRIZE               # первый день не тронут


async def test_prize_is_fixed_when_the_case_is_issued():
    """Приз считается при выдаче: подождать с открытием и получить больше нельзя."""
    async with fresh_db():
        uid = await mk_user(43)
        await play_day(uid)
        case, _ = await db.issue_daily_case(uid)
        assert (case['streak'], case['prize_cents']) == (2, PRIZE + STEP)

        # Игрок ушёл на неделю и вернулся к тому же выданному кейсу.
        streak = await db.daily_streak(uid)
        assert streak['day'] == 2 and streak['prize_cents'] == PRIZE + STEP
        assert streak['expires_at'] == 0        # кейс на руках, гореть нечему

        picked, status = await db.pick_daily_case(uid, case['id'],
                                                 case['win_index'])
        assert (status, picked['payout_cents']) == ('ok', PRIZE + STEP)


async def test_streak_survives_within_the_grace_window():
    """Пришёл позже суток, но в пределах запаса — серия жива."""
    async with fresh_db():
        uid = await mk_user(44)
        await play_day(uid)
        await shift_case_back((await db.last_daily_case(uid))['id'],
                              config.DAILY_STREAK_GRACE - 60)
        assert (await db.daily_streak(uid))['streak'] == 1
        case, _ = await db.issue_daily_case(uid)
        assert case['streak'] == 2


async def test_state_shows_streak_for_every_screen():
    async with fresh_db():
        uid = await mk_user(45)
        bot = StubBot()

        st = await daily.state(bot, uid)                   # кейсов ещё не было
        assert (st['streak'], st['streak_day']) == (0, 1)
        assert st['prize_cents'] == PRIZE
        assert st['next_prize_cents'] == PRIZE + STEP

        await play_day(uid)
        await play_day(uid)
        st = await daily.state(bot, uid)                   # кейс снова положен
        assert st['status'] == 'ready'
        assert (st['streak'], st['streak_day']) == (2, 3)
        assert st['prize_cents'] == PRIZE + 2 * STEP       # 3 дня подряд -> $0.07
        assert st['next_prize_cents'] == PRIZE + 3 * STEP
        assert st['streak_seconds_left'] > 0

        case, _ = await db.issue_daily_case(uid)
        await db.pick_daily_case(uid, case['id'], case['win_index'])
        st = await daily.state(bot, uid)                   # пауза после открытия
        assert st['status'] == 'cooldown'
        assert (st['streak'], st['streak_day']) == (3, 4)
        assert st['prize_cents'] == PRIZE + 3 * STEP       # столько будет завтра


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
        st = await daily.state(StubBot({-1005: 'member'}), uid)
        ready = screen._ready_text(st, await db.get_user(uid))
        assert '$0.05' in ready
        assert 'День <b>1</b>' in ready and '$0.06' in ready   # что будет дальше

        picked, _ = await db.pick_daily_case(uid, case['id'], case['win_index'])
        result = screen._result_text(picked, await db.get_balance(uid),
                                     await db.daily_streak(uid))
        assert '$0.05' in result and '$10.05' in result
        assert 'Серия <b>1</b>' in result and '$0.06' in result

        st = await daily.state(StubBot({-1005: 'member'}), uid)
        cooldown = screen._cooldown_text(st)
        assert 'Следующий через' in cooldown
        assert 'Серия <b>1</b>' in cooldown


async def test_bot_screen_says_when_the_streak_burns():
    """Пустая карточка: экран пишет про сгоревшую серию, а не про рост."""
    from handlers import daily as screen

    async with fresh_db():
        uid = await mk_user(31, balance_cents=100)
        await play_day(uid)
        case, _ = await db.issue_daily_case(uid)
        empty = next(i for i in range(case['cards']) if i != case['win_index'])
        picked, _ = await db.pick_daily_case(uid, case['id'], empty)

        result = screen._result_text(picked, await db.get_balance(uid),
                                     await db.daily_streak(uid))
        assert 'сгорела' in result and '$0.05' in result
        assert 'Серия <b>' not in result

        st = await daily.state(StubBot(), uid)
        assert 'сгорела' in screen._cooldown_text(st)
