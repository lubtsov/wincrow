"""Идемпотентность: одно и то же событие не двигает деньги дважды.

Тут собраны все места, где повторный клик, гонка или перезапуск процесса могли
бы заплатить второй раз. Каждый тест — закрытая дыра из аудита прежней версии:

* пополнение зачислялось бесконечно (payment.py:40 не гасил счёт);
* реферальный бонус затирал баланс пригласившего (payment.py:91);
* заявки на вывод несли chat.id админа вместо id заявителя (adminpanel.py:747);
* старое сообщение с игровой клавиатурой работало вечно.
"""

import asyncio

import pytest

import db
from games import engine, pvp
from helpers import fresh_db, mk_user


# --- пополнение -------------------------------------------------------------

async def test_credit_invoice_credits_once():
    """Пять одновременных проверок оплаты — одно зачисление.

    Именно так игрок и жал «Проверить оплату» в прежней версии: счёт не гасился,
    и баланс рос с каждым нажатием.
    """
    async with fresh_db():
        await mk_user(1)
        await db.add_invoice('inv-1', 1, 5000, 'USDT', 'https://t.me/x')

        results = await asyncio.gather(*(db.credit_invoice('inv-1') for _ in range(5)))

        assert sum(1 for r in results if r is not None) == 1
        assert await db.get_balance(1) == 5000
        user = await db.get_user(1)
        assert user['deposited_cents'] == 5000


async def test_credit_invoice_unknown_id():
    async with fresh_db():
        await mk_user(1)
        assert await db.credit_invoice('нет-такого') is None


async def test_credit_invoice_takes_amount_from_our_row():
    """Сумма зачисления — из нашей записи, не из ответа API."""
    async with fresh_db():
        await mk_user(1)
        await db.add_invoice('inv-2', 1, 777, 'USDT', '')
        res = await db.credit_invoice('inv-2')
        assert res == {'user_id': 1, 'amount_cents': 777,
                       'bonus_cents': 0, 'total_cents': 777}


async def test_bonus_burns_on_first_deposit_only():
    """Промокод и ваучер гасятся в той же транзакции, что и зачисление.

    Если гасить раньше — одним промокодом оплачивается десяток счетов; если
    позже — бонус достаётся каждому пополнению.
    """
    async with fresh_db():
        await mk_user(1)
        assert await db.add_promo('HALF', 50, 10) is True
        assert await db.add_voucher('FIVE', 500, 10) is True
        assert await db.redeem_code(1, 'half') == ('promo', 50)     # регистр не важен
        assert await db.redeem_code(1, 'FIVE') == ('voucher', 500)

        assert await db.deposit_bonus_preview(1, 1000) == 1000      # 50% + $5
        # Предпросмотр ничего не гасит.
        assert await db.deposit_bonus_preview(1, 1000) == 1000

        await db.add_invoice('a', 1, 1000, 'USDT', '')
        first = await db.credit_invoice('a')
        assert first['bonus_cents'] == 1000
        assert await db.get_balance(1) == 2000

        await db.add_invoice('b', 1, 1000, 'USDT', '')
        second = await db.credit_invoice('b')
        assert second['bonus_cents'] == 0
        assert await db.get_balance(1) == 3000
        assert await db.deposit_bonus_preview(1, 1000) == 0


async def test_code_usage_limit_holds_under_race():
    """Ваучер на одно применение не расходится на двоих одновременно."""
    async with fresh_db():
        for uid in range(1, 11):
            await mk_user(uid)
        await db.add_voucher('ONCE', 500, 1)

        results = await asyncio.gather(*(db.redeem_code(uid, 'ONCE')
                                         for uid in range(1, 11)))

        assert sum(1 for kind, _ in results if kind == 'voucher') == 1
        assert sum(1 for kind, _ in results if kind == 'exhausted') == 9


async def test_code_once_per_user():
    async with fresh_db():
        await mk_user(1)
        await db.add_promo('WELCOME', 20, 100)
        assert await db.redeem_code(1, 'WELCOME') == ('promo', 20)
        assert await db.redeem_code(1, 'WELCOME') == ('used', 0)
        assert await db.redeem_code(1, 'НЕТУ') == ('not_found', 0)


async def test_code_names_do_not_collide():
    """Одноимённый ваучер стал бы мёртвым: redeem_code смотрит промокоды первыми."""
    async with fresh_db():
        assert await db.add_promo('DUP', 10, 1) is True
        assert await db.add_voucher('DUP', 500, 1) is False
        assert await db.code_exists('dup') == 'promo'


async def test_vouchers_stack():
    """Два ваучера по $5 дают $10 к пополнению."""
    async with fresh_db():
        await mk_user(1)
        await db.add_voucher('V1', 500, 1)
        await db.add_voucher('V2', 500, 1)
        await db.redeem_code(1, 'V1')
        await db.redeem_code(1, 'V2')
        assert await db.deposit_bonus_preview(1, 100) == 1000


# --- рефералка --------------------------------------------------------------

async def test_referral_reward_is_an_increment():
    """Рефереру начисляется процент, а не записывается баланс донора.

    payment.py:91 читал баланс пополнившего и присваивал его пригласившему:
    богатый реферал обнулял счёт своего реферера.
    """
    async with fresh_db():
        await mk_user(1, 500)                       # реферер, свои $5
        await mk_user(2, 0, referer_id=1)           # его приглашённый

        paid = await db.pay_referral(2, 10_000)
        assert paid == (1, 200)                     # уровень 1 — это 2%
        assert await db.get_balance(1) == 500 + 200
        user = await db.get_user(1)
        assert user['referral_earned_cents'] == 200


async def test_referral_levels_are_monotonic():
    """Уровни считаются порогами по возрастанию.

    В adminpanel.py:52 условие было `20 >= x <= 11` — оно истинно для любого
    x <= 11 и ложно для 12..20, так что вся лестница не работала.
    """
    assert db.referral_level(0) == (1, 2)
    assert db.referral_level(10) == (1, 2)
    assert db.referral_level(11) == (2, 3)
    assert db.referral_level(20) == (2, 3)
    assert db.referral_level(21) == (3, 4)
    assert db.referral_level(41) == (5, 7)
    assert db.referral_level(10_000) == (5, 7)

    percents = [db.referral_level(n)[1] for n in range(0, 60)]
    assert percents == sorted(percents)


async def test_referral_needs_a_referer():
    async with fresh_db():
        await mk_user(1, 0)
        assert await db.pay_referral(1, 10_000) is None


async def test_referer_cannot_be_reassigned():
    """Повторный /start с чужой ссылкой не переписывает реферера."""
    async with fresh_db():
        await mk_user(1)
        await mk_user(2)
        await db.ensure_user(3, 'three', referer_id=1)
        await db.ensure_user(3, 'three', referer_id=2)      # вторая попытка

        user = await db.get_user(3)
        assert user['referer_id'] == 1
        assert (await db.get_user(1))['referrals'] == 1
        assert (await db.get_user(2))['referrals'] == 0


async def test_self_referral_ignored():
    async with fresh_db():
        await db.ensure_user(1, 'one', referer_id=1)
        assert (await db.get_user(1))['referer_id'] is None


# --- выводы -----------------------------------------------------------------

async def test_withdrawal_debits_immediately():
    async with fresh_db():
        await mk_user(1, 1000)
        wd_id = await db.create_withdrawal(1, 600, 'tg:1')
        assert wd_id is not None
        assert await db.get_balance(1) == 400


async def test_withdrawal_needs_funds():
    async with fresh_db():
        await mk_user(1, 500)
        assert await db.create_withdrawal(1, 600, 'tg:1') is None
        assert await db.create_withdrawal(1, 0, 'tg:1') is None
        assert await db.get_balance(1) == 500


async def test_claim_withdrawal_once():
    """Двойное нажатие ✅ не отправит монеты дважды.

    claim идёт ДО перевода: transfer дёргает только тот вызов, которому заявка
    вернулась.
    """
    async with fresh_db():
        await mk_user(1, 1000)
        wd_id = await db.create_withdrawal(1, 1000, 'tg:1')

        claims = await asyncio.gather(*(db.claim_withdrawal(wd_id, 99)
                                        for _ in range(5)))

        assert sum(1 for c in claims if c is not None) == 1
        assert await db.get_balance(1) == 0
        row = await db.get_withdrawal(wd_id)
        assert row['status'] == 'paid'
        assert row['admin_id'] == 99


async def test_reject_refunds_once():
    async with fresh_db():
        await mk_user(1, 1000)
        wd_id = await db.create_withdrawal(1, 1000, 'tg:1')

        rejects = await asyncio.gather(*(db.reject_withdrawal(wd_id, 99, 'нет')
                                         for _ in range(5)))

        assert sum(1 for r in rejects if r is not None) == 1
        assert await db.get_balance(1) == 1000
        assert (await db.get_withdrawal(wd_id))['status'] == 'rejected'


async def test_paid_withdrawal_cannot_be_rejected():
    """Выплаченную заявку нельзя «отклонить» и вернуть деньги вторым концом."""
    async with fresh_db():
        await mk_user(1, 1000)
        wd_id = await db.create_withdrawal(1, 1000, 'tg:1')
        assert await db.claim_withdrawal(wd_id, 99) is not None

        assert await db.reject_withdrawal(wd_id, 99) is None
        assert await db.get_balance(1) == 0


async def test_fail_withdrawal_refunds_once():
    """Crypto Pay отказал — деньги возвращаются, но ровно один раз."""
    async with fresh_db():
        await mk_user(1, 1000)
        wd_id = await db.create_withdrawal(1, 1000, 'tg:1')
        await db.claim_withdrawal(wd_id, 99)

        assert await db.fail_withdrawal(wd_id, 'CryptoPay отказал') is True
        assert await db.fail_withdrawal(wd_id, 'повтор') is False
        assert await db.get_balance(1) == 1000
        assert (await db.get_withdrawal(wd_id))['status'] == 'failed'


async def test_note_does_not_move_money():
    """Пометка 'unknown' оставляет заявку выплаченной и денег не возвращает.

    Если связь оборвалась, мы не знаем, ушёл перевод или нет. Возврат на баланс
    заплатил бы игроку дважды; повтор по тому же spend_id безопасен.
    """
    async with fresh_db():
        await mk_user(1, 1000)
        wd_id = await db.create_withdrawal(1, 1000, 'tg:1')
        await db.claim_withdrawal(wd_id, 99)

        assert await db.set_withdrawal_note(wd_id, 'unknown: связь оборвалась') is True
        row = await db.get_withdrawal(wd_id)
        assert row['status'] == 'paid'
        assert row['note'].startswith('unknown:')
        assert await db.get_balance(1) == 0


async def test_withdrawal_id_not_admin_id():
    """Кнопки заявки должны нести id заявки.

    adminpanel.py:747 подставлял в callback_data chat.id админа, и «одобрить»
    применялось к самому админу. Тест фиксирует, что id заявки и id админа —
    разные числа и заявка ищется по своему.
    """
    async with fresh_db():
        await mk_user(555, 1000)
        wd_id = await db.create_withdrawal(555, 1000, 'tg:555')
        assert wd_id != 555
        assert await db.get_withdrawal(555) is None
        assert (await db.get_withdrawal(wd_id))['user_id'] == 555


# --- раунды -----------------------------------------------------------------

async def test_finish_pays_once():
    """Два одновременных «Забрать» дают одно начисление."""
    async with fresh_db():
        await mk_user(1, 1000)
        rnd = await engine.start_round(1, 'crash', 1000)

        payouts = await asyncio.gather(*(engine.finish(rnd, 2.0) for _ in range(5)))

        assert sum(1 for p in payouts if p is not None) == 1
        assert await db.get_balance(1) == 2000


async def test_void_refunds_once():
    """Ничья в дуэли: ставка возвращается, оборот откатывается."""
    async with fresh_db():
        await mk_user(1, 1000)
        rnd = await engine.start_round(1, 'dice', 500)

        results = await asyncio.gather(*(engine.void(rnd) for _ in range(5)))

        assert sum(results) == 1
        user = await db.get_user(1)
        assert user['balance_cents'] == 1000
        assert user['wagered_cents'] == 0        # оборота не было
        assert user['won_cents'] == 0


async def test_void_after_finish_does_nothing():
    async with fresh_db():
        await mk_user(1, 1000)
        rnd = await engine.start_round(1, 'coin', 100)
        assert await engine.finish(rnd, 0.0) == 0
        assert await engine.void(rnd) is False
        assert await db.get_balance(1) == 900


async def test_lost_round_pays_nothing():
    async with fresh_db():
        await mk_user(1, 1000)
        rnd = await engine.start_round(1, 'coin', 100)
        assert await engine.finish(rnd, 0.0) == 0
        row = await (await db.conn().execute(
            'SELECT status, payout_cents FROM rounds WHERE id = ?', (rnd.id,))).fetchone()
        assert row['status'] == 'lost'
        assert row['payout_cents'] == 0


async def test_stale_keyboard_cannot_be_replayed():
    """Кнопка из старого сообщения не применяется к новой ставке.

    Это главный эксплойт прежней версии: ставка читалась из БД в момент клика,
    поэтому её поднимали в новом сообщении и жали кнопку в старом. Теперь в
    callback_data едет round_id, а раунд закрыт.
    """
    async with fresh_db():
        await mk_user(1, 10_000)
        await mk_user(2, 10_000)

        rnd = await engine.start_round(1, 'mines', 100)
        assert await engine.load_round(rnd.id, 1, 'mines') is not None

        await engine.finish(rnd, 0.0)
        assert await engine.load_round(rnd.id, 1, 'mines') is None      # закрыт

        fresh = await engine.start_round(1, 'mines', 5000)
        assert await engine.load_round(fresh.id, 2, 'mines') is None     # чужой
        assert await engine.load_round(fresh.id, 1, 'tower') is None     # не та игра
        assert await engine.load_round(999_999, 1, 'mines') is None      # нет такого


async def test_save_state_ignores_closed_round():
    """Дописать состояние в закрытый раунд нельзя — иначе поле мин «переедет»."""
    async with fresh_db():
        await mk_user(1, 1000)
        rnd = await engine.start_round(1, 'mines', 100, {'mines': [1, 2, 3]})
        await engine.finish(rnd, 0.0)

        rnd.state = {'mines': []}
        await engine.save_state(rnd)
        row = await (await db.conn().execute(
            'SELECT state FROM rounds WHERE id = ?', (rnd.id,))).fetchone()
        assert '1' in row['state']


async def test_reap_active_rounds_after_restart():
    """Перезапуск посреди crash-раунда: ставка возвращается один раз.

    Цикл тиков живёт в памяти процесса, поэтому раунд, переживший перезапуск,
    больше никогда не завершится сам.
    """
    async with fresh_db():
        await mk_user(1, 1000)
        rnd = await engine.start_round(1, 'crash', 400)
        assert await db.get_balance(1) == 600

        assert await db.reap_active_rounds() == 1
        assert await db.get_balance(1) == 1000
        assert await db.reap_active_rounds() == 0          # повтор ничего не платит

        user = await db.get_user(1)
        assert user['wagered_cents'] == 0
        row = await (await db.conn().execute(
            'SELECT status FROM rounds WHERE id = ?', (rnd.id,))).fetchone()
        assert row['status'] == 'void'


async def test_reap_leaves_other_games_alone():
    """Мины и башня живут в базе и после перезапуска — их добивает сам игрок."""
    async with fresh_db():
        await mk_user(1, 1000)
        await engine.start_round(1, 'mines', 100)
        assert await db.reap_active_rounds() == 0
        assert await db.get_balance(1) == 900


# --- PvP --------------------------------------------------------------------

async def test_pvp_cancel_refunds_every_player_once():
    async with fresh_db():
        await mk_user(1, 1000)
        await mk_user(2, 1000)
        room_id = await db.pvp_create('duel', 1, 300, 'seed', 'hash')
        assert await db.pvp_join(room_id, 2, 300, max_players=2,
                                 fixed_stake=True, topup=False) == 'ok'
        assert await db.get_balance(1) == 700
        assert await db.get_balance(2) == 700

        assert await db.pvp_cancel(room_id) is True
        assert await db.pvp_cancel(room_id) is False      # повтор не платит

        assert await db.get_balance(1) == 1000
        assert await db.get_balance(2) == 1000
        assert (await db.get_user(1))['wagered_cents'] == 0


async def test_pvp_room_has_no_extra_seats():
    """Третий игрок не влезет в дуэль, даже если нажмёт одновременно со вторым."""
    async with fresh_db():
        for uid in (1, 2, 3):
            await mk_user(uid, 1000)
        room_id = await db.pvp_create('duel', 1, 300, 'seed', 'hash')

        results = await asyncio.gather(*(
            db.pvp_join(room_id, uid, 300, max_players=2,
                        fixed_stake=True, topup=False) for uid in (2, 3)))

        assert sorted(results) == ['full', 'ok']
        room = await db.pvp_room(room_id)
        assert room['pot_cents'] == 600
        assert len(await db.pvp_players(room_id)) == 2
        # Тому, кто не влез, деньги не тронули.
        assert sorted([await db.get_balance(2), await db.get_balance(3)]) == [700, 1000]


async def test_pvp_lock_starts_the_round_once():
    async with fresh_db():
        await mk_user(1, 1000)
        await mk_user(2, 1000)
        room_id = await db.pvp_create('duel', 1, 300, 'seed', 'hash')
        await db.pvp_join(room_id, 2, 300, max_players=2,
                          fixed_stake=True, topup=False)

        locks = await asyncio.gather(*(db.pvp_lock(room_id, 2) for _ in range(5)))
        assert sum(1 for lock in locks if lock is not None) == 1


async def test_pvp_lock_needs_a_second_player():
    async with fresh_db():
        await mk_user(1, 1000)
        room_id = await db.pvp_create('duel', 1, 300, 'seed', 'hash')
        assert await db.pvp_lock(room_id, 2) is None
        assert (await db.pvp_room(room_id))['status'] == 'open'


async def test_pvp_finish_pays_winner_once():
    async with fresh_db():
        await mk_user(1, 1000)
        await mk_user(2, 1000)
        room_id = await db.pvp_create('duel', 1, 300, 'seed', 'hash')
        await db.pvp_join(room_id, 2, 300, max_players=2,
                          fixed_stake=True, topup=False)
        await db.pvp_lock(room_id, 2)

        payout, rake = pvp.split(600)
        results = await asyncio.gather(*(
            db.pvp_finish(room_id, 1, payout, rake, '{}') for _ in range(5)))

        assert sum(results) == 1
        assert await db.get_balance(1) == 700 + payout
        assert await db.get_balance(2) == 700
        room = await db.pvp_room(room_id)
        assert room['status'] == 'done'
        assert room['rake_cents'] == rake
        # Касса забрала ровно рейк, банк сошёлся до цента.
        assert room['payout_cents'] + room['rake_cents'] == room['pot_cents']


async def test_pvp_leave_returns_only_own_stake():
    async with fresh_db():
        await mk_user(1, 1000)
        await mk_user(2, 1000)
        room_id = await db.pvp_create('jackpot', 1, 300, 'seed', 'hash')
        await db.pvp_join(room_id, 2, 500, max_players=6,
                          fixed_stake=False, topup=True)

        assert await db.pvp_leave(room_id, 2) is True
        assert await db.pvp_leave(room_id, 2) is False     # уже вышел
        assert await db.pvp_leave(room_id, 1) is False      # создателю нельзя

        assert await db.get_balance(2) == 1000
        assert (await db.pvp_room(room_id))['pot_cents'] == 300


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
