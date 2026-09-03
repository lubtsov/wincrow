"""Перенос старой базы bebra.db в новую схему.

Запуск:
    python migrate.py            # только показать, что будет сделано
    python migrate.py --apply    # записать

По умолчанию скрипт ничего не пишет: сначала печатает сводку, а база
меняется только с --apply. Повторный запуск безопасен — уже существующие
в новой базе игроки не перезаписываются.

Что переносится: аккаунт (id, ник, бан), реферальные связи и счётчики,
админы, процентные промокоды.

Чего НЕ переносится — и почему.
-------------------------------
Старый бот считал баланс в абстрактных «монетах» 🪙: курса к доллару у них
не было никогда, начислялись они и админкой, и промокодами, и старой
кассой с бесконечным зачислением (payment.py:40 не гасил счёт, так что
любой оплаченный счёт можно было пробить по кругу). Новое казино работает
на реальных деньгах: цент баланса — это цент в USDT, который выводится
через @CryptoBot. Приравнять монету к какому-то числу центов — значит
раздать настоящие деньги по выдуманному курсу, причём в первую очередь
тем, кто крутил дыру в кассе.

Поэтому монетные величины обнуляются:
    users.balance         -> balance_cents = 0
    info.referal_profit   -> referral_earned_cents = 0
    voucher.amount        -> ваучеры не переносятся вообще

Старые балансы не выбрасываются молча: с --apply они выгружаются в
legacy_balances.csv. Кому надо — доначисляется вручную через админку,
осознанно и адресно.

Процентные промокоды переносятся как есть: «+50% к пополнению» не зависит
от единицы измерения.

Старая схема (её создавал прежний main.py на aiogram 2; самого файла в проекте
больше нет — см. раздел «Legacy» в README):
    users (user_id STRING, nickname, balance INTEGER, referals, ..., referer)
    info  (user_id STRING, ..., referal_profit STRING, ban INTEGER, ...)
    promocode (promo, usage_max, usage_actual, percent)
    voucher   (voucher, usage_max, usage_actual, amount)
    admins    (user_id STRING, nickname)

Не переносятся: demo, forms, jackpot, payment_qiwi, payment_youmoney,
payment_bitcoin, payment_crystalpay — они относятся к вырезанному функционалу.
"""

import argparse
import csv
import os
import shutil
import sqlite3
import sys
import time

import config
from db import SCHEMA, norm_code

OLD_DB = 'bebra.db'
DUMP_CSV = 'legacy_balances.csv'


def as_coins(value) -> float:
    """Старый баланс мог лежать строкой — приводим руками, мусор считаем нулём."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def as_int(value) -> int | None:
    """user_id в старой базе объявлен STRING, поэтому приводим руками."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == 'none':
        return None
    if text.lstrip('-').isdigit():
        return int(text)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description='Миграция bebra.db в новую схему')
    ap.add_argument('--apply', action='store_true', help='записать изменения')
    ap.add_argument('--old', default=OLD_DB)
    ap.add_argument('--new', default=config.DB_PATH)
    ap.add_argument('--dump', default=DUMP_CSV,
                    help=f'куда выгрузить старые монетные балансы '
                         f'(по умолчанию {DUMP_CSV})')
    args = ap.parse_args()

    if not os.path.exists(args.old):
        print(f'Старой базы {args.old} нет — миграция не нужна, '
              f'новая база создастся при первом запуске бота.')
        return 0

    old = sqlite3.connect(args.old)
    old.row_factory = sqlite3.Row
    oc = old.cursor()

    if not table_exists(oc, 'users'):
        print(f'В {args.old} нет таблицы users. Нечего переносить.')
        return 1

    # --- читаем старое -------------------------------------------------------

    bans: dict[int, int] = {}
    profits: dict[int, float] = {}
    if table_exists(oc, 'info'):
        for row in oc.execute('SELECT * FROM info').fetchall():
            uid = as_int(row['user_id'])
            if uid is None:
                continue
            keys = row.keys()
            if 'ban' in keys and row['ban']:
                bans[uid] = 1
            if 'referal_profit' in keys:
                profits[uid] = as_coins(row['referal_profit'])

    users: list[dict] = []
    skipped_ids = 0
    for row in oc.execute('SELECT * FROM users').fetchall():
        uid = as_int(row['user_id'])
        if uid is None:
            skipped_ids += 1
            continue
        keys = row.keys()
        nickname = (row['nickname'] or '') if 'nickname' in keys else ''
        users.append({
            'user_id': uid,
            'username': str(nickname).lstrip('@').strip() or None,
            'referrals': int(row['referals'] or 0) if 'referals' in keys else 0,
            'referer_id': as_int(row['referer']) if 'referer' in keys else None,
            'banned': bans.get(uid, 0),
            # Монеты — только для выгрузки, в новую базу не идут.
            'legacy_coins': as_coins(row['balance'] if 'balance' in keys else 0),
            'legacy_ref_coins': profits.get(uid, 0.0),
        })

    known = {u['user_id'] for u in users}
    # Реферер, которого нет в users, — мусор: внешний ключ повесить некуда.
    lost_referers = 0
    for u in users:
        if u['referer_id'] is not None and u['referer_id'] not in known:
            u['referer_id'] = None
            lost_referers += 1
        if u['referer_id'] == u['user_id']:
            u['referer_id'] = None

    promos = []
    if table_exists(oc, 'promocode'):
        for row in oc.execute('SELECT * FROM promocode').fetchall():
            code = norm_code(str(row['promo'] or ''))
            if code:
                promos.append((code, int(row['percent'] or 0),
                               int(row['usage_max'] or 0),
                               int(row['usage_actual'] or 0)))

    # Ваучеры — фиксированная сумма В МОНЕТАХ, переводить её в центы нечем.
    dropped_vouchers = 0
    if table_exists(oc, 'voucher'):
        dropped_vouchers = oc.execute(
            'SELECT COUNT(*) FROM voucher').fetchone()[0]

    admins = []
    if table_exists(oc, 'admins'):
        for row in oc.execute('SELECT * FROM admins').fetchall():
            uid = as_int(row['user_id'])
            if uid is not None and uid != config.OWNER_ID:
                nick = row['nickname'] if 'nickname' in row.keys() else None
                admins.append((uid, str(nick or '').lstrip('@') or None))

    old.close()

    with_coins = [u for u in users if u['legacy_coins'] > 0]
    total_coins = sum(u['legacy_coins'] for u in users)
    total_ref_coins = sum(u['legacy_ref_coins'] for u in users)

    # --- сводка --------------------------------------------------------------

    print(f'Источник:   {args.old}')
    print(f'Приёмник:   {args.new}')
    print()
    print('Переносится:')
    print(f'  игроков:            {len(users)}')
    if skipped_ids:
        print(f'    с нечитаемым id:  {skipped_ids} (пропущены)')
    print(f'  забанено:           {len(bans)}')
    print(f'  рефереров потеряно: {lost_referers} (нет такого игрока в users)')
    print(f'  промокодов (%):     {len(promos)}')
    print(f'  админов:            {len(admins)} (владелец {config.OWNER_ID} '
          f'добавляется кодом, не таблицей)')
    print()
    print('Обнуляется — это монеты, курса к доллару у них нет:')
    print(f'  балансы:            {total_coins:.0f} 🪙 у {len(with_coins)} '
          f'игроков -> у всех $0.00')
    print(f'  реф. заработок:     {total_ref_coins:.0f} 🪙 -> $0.00')
    print(f'  ваучеров отброшено: {dropped_vouchers} (сумма была в монетах)')
    print()
    print(f'Старые балансы уйдут в {args.dump} — доначислить нужным игрокам '
          f'можно вручную через админку.')
    print()
    print('Не переносятся: demo, forms, jackpot, payment_qiwi, '
          'payment_youmoney, payment_bitcoin, payment_crystalpay.')

    if not args.apply:
        print()
        print('Это был прогон без записи. Если всё сходится — запусти')
        print('    python migrate.py --apply')
        return 0

    # --- запись --------------------------------------------------------------

    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = f'{args.old}.{stamp}.bak'
    shutil.copy2(args.old, backup)
    print(f'\nРезервная копия старой базы: {backup}')
    if os.path.exists(args.new):
        new_backup = f'{args.new}.{stamp}.bak'
        shutil.copy2(args.new, new_backup)
        print(f'Резервная копия новой базы: {new_backup}')

    # Выгрузка до записи: если дальше что-то упадёт, монеты уже сохранены.
    with open(args.dump, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(['user_id', 'username', 'legacy_coins', 'legacy_ref_coins'])
        for u in sorted(users, key=lambda x: -x['legacy_coins']):
            if u['legacy_coins'] or u['legacy_ref_coins']:
                w.writerow([u['user_id'], u['username'] or '',
                            f'{u["legacy_coins"]:.0f}',
                            f'{u["legacy_ref_coins"]:.0f}'])
    print(f'Старые балансы выгружены: {args.dump}')

    new = sqlite3.connect(args.new)
    new.executescript(SCHEMA)
    nc = new.cursor()
    now = int(time.time())

    inserted = existed = 0
    for u in users:
        # INSERT OR IGNORE, а не REPLACE: если игрок уже играет в новом боте,
        # его настоящий баланс важнее нулей из миграции.
        nc.execute(
            'INSERT OR IGNORE INTO users (user_id, username, balance_cents, '
            'bet_cents, banned, referer_id, referrals, referral_earned_cents, '
            'created_at) VALUES (?, ?, 0, ?, ?, ?, ?, 0, ?)',
            (u['user_id'], u['username'], config.MIN_BET_CENTS, u['banned'],
             u['referer_id'], u['referrals'], now))
        if nc.rowcount == 1:
            inserted += 1
        else:
            existed += 1

    for code, percent, umax, uact in promos:
        nc.execute(
            'INSERT OR IGNORE INTO promocodes (code, percent, usage_max, '
            'usage_actual, created_at) VALUES (?, ?, ?, ?, ?)',
            (code, percent, umax, uact, now))

    for uid, nick in admins:
        nc.execute(
            'INSERT OR IGNORE INTO admins (user_id, username, added_at) '
            'VALUES (?, ?, ?)', (uid, nick, now))

    new.commit()

    check = nc.execute(
        'SELECT COUNT(*) n, COALESCE(SUM(balance_cents), 0) s FROM users'
    ).fetchone()
    new.close()

    print()
    print(f'Перенесено:  {inserted}')
    print(f'Уже были:    {existed} (не тронуты)')
    print(f'Итого в {args.new}: {check[0]} игроков, '
          f'${check[1] / 100:.2f} на балансах')
    return 0


if __name__ == '__main__':
    sys.exit(main())
