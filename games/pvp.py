"""PvP: дуэль один на один и джекпот-комната.

Здесь казино не играет против игрока вообще. Банк собирается из взносов,
победитель забирает его минус фиксированный рейк config.PVP_RAKE (3%).
Дисперсии у заведения нет: сколько бы ни выиграл победитель, платят это
проигравшие, а не касса.

Деньги комнаты живут в db (pvp_*): взнос списывается тем же условным UPDATE,
что и обычная ставка, розыгрыш запускается ровно один раз (`pvp_lock` меняет
статус open -> playing и проверяет rowcount), выплата идемпотентна, а отмена
возвращает взносы всем участникам и откатывает оборот.

Дуэль бросает Telegram-кубик в чат каждого игрока: свой бросок каждый видит
своими глазами. Джекпот выбирает победителя provably fair — хеш серверного
сида показан в карточке комнаты до розыгрыша, сам сид раскрывается в
результате, и билет пересчитывается вручную.
"""

import asyncio
import html
import json
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

import config
import db
import emoji as E
import keyboards as kb
from db import fmt
from games import engine
from games.registry import GAMES, implement
from ui import render

log = logging.getLogger(__name__)

router = Router(name='pvp')

DUEL_EMOJI = '🎲'
ANIM = 4.0              # ждём анимацию кубика, прежде чем объявлять исход
JACK_MAX = 6            # мест в джекпот-комнате
SPIN_PAUSE = 1.6        # пауза «крутим» перед раскрытием победителя

JOIN_ERRORS = {
    'closed': 'Комната уже закрыта.',
    'full': 'Мест больше нет.',
    'already': 'Ты уже в этой комнате.',
    'stake': 'Взнос не совпал со ставкой комнаты.',
    'no_money': 'Не хватает на взнос.',
}


def split(pot_cents: int) -> tuple[int, int]:
    """Банк -> (выплата победителю, рейк казино).

    Здесь округление к ближайшему, а не вниз, как в выплатах игр, — и по
    той же причине: казино не должно оставаться в минусе. Рейк вниз на
    мелком банке давал бы ноль (дуэль по $0.10 — банк 20 центов, 3% это
    0.6 цента), то есть комната крутилась бы вообще без комиссии.

    Инвариант тут строже, чем процент: payout + rake == pot всегда, из банка
    не выплачивается ни центом больше собранного. Платой за это идёт
    неровный процент на мелких банках — с 20 центов рейк 1 цент, то есть
    фактически 5%, а не 3%. От банка в $1 расхождение исчезает.
    """
    rake = round(pot_cents * config.PVP_RAKE)
    return pot_cents - rake, rake


def _nick(row) -> str:
    name = row['username'] if 'username' in row.keys() else None
    return '@' + html.escape(name) if name else f'ID {row["user_id"]}'


def _chat(player) -> int:
    """Куда писать игроку. У приватного чата id совпадает с user_id."""
    return player['chat_id'] or player['user_id']


# --- рассылка по участникам --------------------------------------------------

async def _notify(bot, players, make_text, make_kb=None) -> None:
    """Обновляет карточку у каждого участника, у каждого — своим текстом.

    Карточка правится по сохранённым chat_id/message_id. Если правка не
    прошла (сообщение удалено, слишком старое), присылаем новое — потерять
    сообщение о выплате хуже, чем показать лишнее.
    """
    for p in players:
        chat_id = _chat(p)
        text = make_text(p['user_id'])
        markup = make_kb(p['user_id']) if make_kb else None
        if p['message_id']:
            try:
                await bot.edit_message_text(chat_id=chat_id,
                                            message_id=p['message_id'],
                                            text=text, reply_markup=markup)
                continue
            except TelegramBadRequest as e:
                if 'not modified' in str(e):
                    continue
                log.debug('pvp edit failed: %s', e)
            except Exception as e:                      # чат недоступен
                log.debug('pvp edit failed: %s', e)
        try:
            await bot.send_message(chat_id, text, reply_markup=markup)
        except Exception as e:
            log.warning('pvp notify %s failed: %s', p['user_id'], e)


def _after_kb(game: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [kb.btn('🔄 Ещё раз', f'pv:{game}')],
        [kb.btn('🎮 Игры', 'games')],
    ])


# --- лобби ------------------------------------------------------------------

def _lobby_kb(game: str, rooms, bet_cents: int) -> InlineKeyboardMarkup:
    rows = []
    for r in rooms:
        if game == 'duel':
            label = f'⚔️ {fmt(r["stake_cents"])} · {_nick(r)}'
        else:
            label = (f'🏆 банк {fmt(r["pot_cents"])} · '
                     f'{r["players"]}/{JACK_MAX} игроков')
        rows.append([kb.btn(label, f'pvj:{r["id"]}')])
    rows.append([kb.btn(f'➕ Своя комната на {fmt(bet_cents)}', f'pvn:{game}')])
    rows.append([kb.btn('🔄 Обновить', f'pv:{game}'),
                 kb.btn('💰 Ставка', f'bet:{game}:ask')])
    rows.append([kb.btn('📖 Правила', f'rules:{game}'),
                 kb.btn('⬅️ К играм', 'grp:pvp')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _lobby(call: CallbackQuery, game: str) -> None:
    spec = GAMES[game]
    user_id = call.from_user.id

    # Ленивая уборка: открытая комната держит чужие деньги, висеть вечно ей нельзя.
    await db.pvp_expire()

    mine = await db.pvp_my_room(game, user_id)
    if mine is not None:
        await _card(call, mine['id'], user_id)
        return

    bet = await db.get_bet(user_id)
    balance = await db.get_balance(user_id)
    rooms = await db.pvp_open_rooms(game)

    if game == 'duel':
        head = ('Создай дуэль на свою ставку или зайди в чужую. Оба кидают '
                f'{DUEL_EMOJI} — у каждого в своём чате, — кто выше, забирает '
                f'банк минус {config.PVP_RAKE * 100:.0f}%. Ничья — взносы '
                f'возвращаются.')
    else:
        head = (f'Скидываемся в общий банк, шанс победы равен твоей доле. '
                f'Мест в комнате — {JACK_MAX}, крутить можно с двух игроков. '
                f'Победитель забирает банк минус {config.PVP_RAKE * 100:.0f}%.')

    body = ('\nОткрытых комнат нет — создай свою, её увидят остальные.'
            if not rooms else f'\nОткрытых комнат: <b>{len(rooms)}</b>.')

    await render(call,
                 f'{spec.emoji} <b>{spec.title}</b>\n\n{head}\n{body}\n\n'
                 f'Твоя ставка: <b>{fmt(bet)}</b>\n'
                 f'Баланс: <b>{fmt(balance)}</b>',
                 _lobby_kb(game, rooms, bet))


# --- карточка комнаты -------------------------------------------------------

def _card_text(room, players) -> str:
    spec = GAMES[room['game']]
    pot = room['pot_cents']
    duel = room['game'] == 'duel'

    # В дуэли второй игрок обязан внести столько же, поэтому банк известен
    # заранее — показывать половину было бы обманом ожиданий.
    shown_pot = room['stake_cents'] * 2 if duel else pot
    payout, rake = split(shown_pot)

    text = f'{spec.emoji} <b>{spec.title} #{room["id"]}</b>\n\n'
    if duel:
        text += f'Взнос: <b>{fmt(room["stake_cents"])}</b> с каждого\n'
    text += (f'Банк: <b>{fmt(shown_pot)}</b>\n'
             f'Победителю: <b>{fmt(payout)}</b> (комиссия {fmt(rake)})\n\n')

    lines = []
    for p in players:
        share = p['stake_cents'] / pot * 100 if pot else 0
        line = f'• {_nick(p)} — {fmt(p["stake_cents"])}'
        if not duel:
            line += f' · {share:.1f}%'
        lines.append(line)
    text += 'Участники:\n' + '\n'.join(lines) + '\n\n'

    if duel:
        text += ('Ждём соперника. Как только кто-то зайдёт, кубики бросятся '
                 'сами — каждому в свой чат.')
    else:
        text += (f'Мест: {len(players)}/{JACK_MAX}. '
                 f'Крутить может любой участник, начиная со второго игрока.\n\n'
                 f'Хеш сида, по которому вытянется билет:\n'
                 f'<code>{room["server_seed_hash"]}</code>')
    return text


def _card_kb(room, players, viewer_id: int) -> InlineKeyboardMarkup:
    room_id = room['id']
    rows = []
    if room['game'] == 'jackpot' and len(players) >= 2:
        rows.append([kb.btn('🎲 Крутить', f'pvs:{room_id}')])
    if room['game'] == 'jackpot':
        rows.append([kb.btn('➕ Докинуть', f'pvj:{room_id}')])
    rows.append([kb.btn('🔄 Обновить', f'pvr:{room_id}')])
    if room['creator_id'] == viewer_id:
        rows.append([kb.btn('❌ Распустить и вернуть взносы', f'pvx:{room_id}')])
    else:
        rows.append([kb.btn('🚪 Выйти и забрать взнос', f'pvo:{room_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _card(event, room_id: int, viewer_id: int):
    room = await db.pvp_room(room_id)
    if room is None:
        return await render(event, 'Комната не найдена.', kb.back_to('games'))
    players = await db.pvp_players(room_id)
    return await render(event, _card_text(room, players),
                        _card_kb(room, players, viewer_id))


# --- вход в игру ------------------------------------------------------------

@implement('duel')
async def start_duel(call: CallbackQuery, user, state) -> None:
    await _lobby(call, 'duel')


@implement('jackpot')
async def start_jackpot(call: CallbackQuery, user, state) -> None:
    await _lobby(call, 'jackpot')


@router.callback_query(F.data.startswith('pv:'))
async def cb_lobby(call: CallbackQuery, user) -> None:
    game = call.data.split(':', 1)[1]
    if game not in ('duel', 'jackpot'):
        await call.answer('Такой комнаты нет.', show_alert=True)
        return
    await call.answer()
    await _lobby(call, game)


@router.callback_query(F.data.startswith('pvr:'))
async def cb_refresh(call: CallbackQuery, user) -> None:
    await call.answer()
    await _card(call, int(call.data.split(':', 1)[1]), call.from_user.id)


# --- создание --------------------------------------------------------------

@router.callback_query(F.data.startswith('pvn:'))
async def cb_new(call: CallbackQuery, user) -> None:
    game = call.data.split(':', 1)[1]
    if game not in ('duel', 'jackpot'):
        await call.answer('Такой комнаты нет.', show_alert=True)
        return

    user_id = call.from_user.id
    if await db.pvp_my_room(game, user_id) is not None:
        await call.answer('У тебя уже есть комната в этой игре.', show_alert=True)
        await _lobby(call, game)
        return

    stake = await db.get_bet(user_id)
    seed = engine.new_seed()
    room_id = await db.pvp_create(game, user_id, stake, seed, engine.seed_hash(seed))
    if room_id is None:
        await call.answer(f'Не хватает на взнос {fmt(stake)}.', show_alert=True)
        return

    await call.answer('Комната создана')
    msg = await _card(call, room_id, user_id)
    if msg is not None:
        # Запоминаем карточку, чтобы дописать в неё результат розыгрыша.
        await db.pvp_set_card(room_id, user_id, msg.chat.id, msg.message_id)


# --- вход в комнату ---------------------------------------------------------

@router.callback_query(F.data.startswith('pvj:'))
async def cb_join(call: CallbackQuery, user) -> None:
    room_id = int(call.data.split(':', 1)[1])
    user_id = call.from_user.id

    room = await db.pvp_room(room_id)
    if room is None:
        await call.answer('Комната не найдена.', show_alert=True)
        return
    if room['status'] != 'open':
        await call.answer('Комната уже закрыта.', show_alert=True)
        await _lobby(call, room['game'])
        return

    if room['game'] == 'duel':
        if room['creator_id'] == user_id:
            await call.answer('Это твоя комната, ждём соперника.', show_alert=True)
            return
        stake, params = room['stake_cents'], dict(max_players=2,
                                                  fixed_stake=True, topup=False)
    else:
        stake, params = await db.get_bet(user_id), dict(max_players=JACK_MAX,
                                                        fixed_stake=False, topup=True)

    status = await db.pvp_join(room_id, user_id, stake,
                              chat_id=call.message.chat.id,
                              message_id=call.message.message_id, **params)
    if status != 'ok':
        await call.answer(JOIN_ERRORS.get(status, 'Не вышло.'), show_alert=True)
        return

    if room['game'] == 'duel':
        await call.answer(f'Взнос {fmt(stake)} внесён')
        await _run_duel(call, room_id)
        return

    # Джекпот: обновляем карточку всем, чтобы новые доли были видны сразу.
    await call.answer(f'Докинул {fmt(stake)}')
    fresh = await db.pvp_room(room_id)
    players = await db.pvp_players(room_id)
    text = _card_text(fresh, players)
    await _notify(call.bot, players, lambda uid: text,
                  lambda uid: _card_kb(fresh, players, uid))


@router.callback_query(F.data.startswith('pvo:'))
async def cb_leave(call: CallbackQuery, user) -> None:
    room_id = int(call.data.split(':', 1)[1])
    if not await db.pvp_leave(room_id, call.from_user.id):
        await call.answer('Выйти уже нельзя.', show_alert=True)
        return
    room = await db.pvp_room(room_id)
    await call.answer('Взнос вернулся на баланс')
    await _lobby(call, room['game'])

    players = await db.pvp_players(room_id)
    if players:
        fresh = await db.pvp_room(room_id)
        text = _card_text(fresh, players)
        await _notify(call.bot, players, lambda uid: text,
                      lambda uid: _card_kb(fresh, players, uid))


@router.callback_query(F.data.startswith('pvx:'))
async def cb_cancel(call: CallbackQuery, user) -> None:
    room_id = int(call.data.split(':', 1)[1])
    room = await db.pvp_room(room_id)
    if room is None:
        await call.answer('Комната не найдена.', show_alert=True)
        return

    players = await db.pvp_players(room_id)
    if not await db.pvp_cancel(room_id, by_user=call.from_user.id):
        await call.answer('Распустить уже нельзя — розыгрыш начался.',
                          show_alert=True)
        return

    await call.answer('Комната распущена, взносы вернулись')
    others = [p for p in players if p['user_id'] != call.from_user.id]
    if others:
        await _notify(call.bot, others,
                      lambda uid: (f'{E.FAIL} Комната #{room_id} распущена '
                                   f'создателем.\n'
                                   f'Взнос вернулся на баланс.'),
                      lambda uid: _after_kb(room['game']))
    await _lobby(call, room['game'])


# --- дуэль ------------------------------------------------------------------

async def _roll(bot, chat_id: int) -> int | None:
    try:
        msg = await bot.send_dice(chat_id=chat_id, emoji=DUEL_EMOJI)
        return msg.dice.value
    except Exception as e:
        log.warning('pvp dice to %s failed: %s', chat_id, e)
        return None


async def _run_duel(call: CallbackQuery, room_id: int) -> None:
    """Оба броска, сравнение, выплата. Точка входа одна — pvp_lock."""
    room = await db.pvp_lock(room_id, 2)
    if room is None:
        # Комнату успели распустить: взнос вернулся вместе с отменой.
        await call.answer('Комната закрылась, взнос вернулся.', show_alert=True)
        await _lobby(call, 'duel')
        return

    players = await db.pvp_players(room_id)
    a, b = players[0], players[1]
    bot = call.bot

    names = {a['user_id']: _nick(a), b['user_id']: _nick(b)}
    other = {a['user_id']: b['user_id'], b['user_id']: a['user_id']}
    await _notify(bot, players,
                  lambda uid: (f'{E.SWORDS} <b>Дуэль #{room_id}</b>\n\n'
                               f'Соперник: {names[other[uid]]}\n'
                               f'Банк: <b>{fmt(room["pot_cents"])}</b>\n\n'
                               f'Кидаем кубики.'))

    rolls = {}
    for p in players:
        value = await _roll(bot, _chat(p))
        if value is None:
            await db.pvp_cancel(room_id, expect='playing')
            await _notify(bot, players,
                          lambda uid: (f'{E.SWORDS} Дуэль отменена: не удалось '
                                       'бросить кубик одному из игроков. Взносы '
                                       'вернулись на баланс.'),
                          lambda uid: _after_kb('duel'))
            return
        rolls[p['user_id']] = value

    await asyncio.sleep(ANIM)

    va, vb = rolls[a['user_id']], rolls[b['user_id']]
    score = {a['user_id']: va, b['user_id']: vb}

    if va == vb:
        if not await db.pvp_cancel(room_id, expect='playing'):
            return
        await _notify(bot, players,
                      lambda uid: (f'{E.DRAW} <b>Ничья {va} : {vb}</b>\n\n'
                                   f'Дуэль #{room_id} не состоялась, взнос '
                                   f'{fmt(room["stake_cents"])} вернулся на '
                                   f'баланс. Комиссию тоже не берём.'),
                      lambda uid: _after_kb('duel'))
        return

    winner = a if va > vb else b
    payout, rake = split(room['pot_cents'])
    result = json.dumps({'rolls': {str(k): v for k, v in rolls.items()},
                         'emoji': DUEL_EMOJI})
    if not await db.pvp_finish(room_id, winner['user_id'], payout, rake, result):
        return

    def text(uid: int) -> str:
        mine, theirs = score[uid], score[other[uid]]
        head = (f'{E.TROPHY} <b>{mine} : {theirs}</b> — забираешь банк.'
                if uid == winner['user_id']
                else f'{E.FAIL} <b>{mine} : {theirs}</b> — '
                     f'{names[winner["user_id"]]} выше.')
        money = (f'Банк {fmt(room["pot_cents"])} − комиссия {fmt(rake)} = '
                 f'<b>{fmt(payout)}</b>' if uid == winner['user_id']
                 else f'Взнос {fmt(room["stake_cents"])} ушёл в банк соперника.')
        return f'{E.SWORDS} <b>Дуэль #{room_id}</b>\n\n{head}\n{money}'

    await _notify(bot, players, text, lambda uid: _after_kb('duel'))


# --- джекпот ----------------------------------------------------------------

def pick_winner(room, players) -> tuple[int, float, int]:
    """(user_id победителя, u, номер билета).

    Банк делится на центы-билеты, каждому игроку достаётся столько билетов,
    сколько центов он внёс. Билет вытягивается из provably fair потока по
    сиду комнаты, так что доля в банке и есть шанс победы.
    """
    pot = room['pot_cents']
    u = next(engine.float_stream(room['server_seed'], f'room{room["id"]}', pot))
    ticket = min(int(u * pot), pot - 1)
    running = 0
    for p in players:
        running += p['stake_cents']
        if ticket < running:
            return p['user_id'], u, ticket
    return players[-1]['user_id'], u, ticket


@router.callback_query(F.data.startswith('pvs:'))
async def cb_spin(call: CallbackQuery, user) -> None:
    room_id = int(call.data.split(':', 1)[1])
    room = await db.pvp_room(room_id)
    if room is None or room['game'] != 'jackpot':
        await call.answer('Комната не найдена.', show_alert=True)
        return

    players = await db.pvp_players(room_id)
    if call.from_user.id not in {p['user_id'] for p in players}:
        await call.answer('Крутить может только участник комнаты.', show_alert=True)
        return
    if len(players) < 2:
        await call.answer('Нужен хотя бы второй игрок.', show_alert=True)
        return

    locked = await db.pvp_lock(room_id, 2)
    if locked is None:
        await call.answer('Розыгрыш уже идёт.', show_alert=True)
        return

    await call.answer()
    await _run_jackpot(call.bot, locked)


async def _run_jackpot(bot, room) -> None:
    room_id = room['id']
    players = await db.pvp_players(room_id)
    pot = room['pot_cents']

    await _notify(bot, players,
                  lambda uid: (f'{E.TROPHY} <b>Джекпот #{room_id}</b>\n\n'
                               f'Банк: <b>{fmt(pot)}</b>\n'
                               f'Игроков: {len(players)}\n\n'
                               f'Крутим…'))
    await asyncio.sleep(SPIN_PAUSE)

    winner_id, u, ticket = pick_winner(room, players)
    payout, rake = split(pot)
    result = json.dumps({'seed': room['server_seed'], 'u': u, 'ticket': ticket,
                         'pot': pot,
                         'shares': {str(p['user_id']): p['stake_cents']
                                    for p in players}})
    if not await db.pvp_finish(room_id, winner_id, payout, rake, result):
        return

    names = {p['user_id']: _nick(p) for p in players}
    shares = '\n'.join(
        (f'{E.TROPHY} ' if p['user_id'] == winner_id else '• ') +
        f'{names[p["user_id"]]} — {fmt(p["stake_cents"])} · '
        f'{p["stake_cents"] / pot * 100:.1f}%'
        for p in players)

    def text(uid: int) -> str:
        head = (f'{E.TROPHY} <b>Ты забираешь банк!</b>' if uid == winner_id
                else f'{E.FAIL} <b>Забрал {names[winner_id]}.</b>')
        return (f'{E.TROPHY} <b>Джекпот #{room_id}</b>\n\n{head}\n\n'
                f'{shares}\n\n'
                f'Банк {fmt(pot)} − комиссия {fmt(rake)} = <b>{fmt(payout)}</b>\n\n'
                f'Проверка: билет <code>{ticket}</code> из {pot}, '
                f'u = <code>{u:.10f}</code>\n'
                f'Сид: <code>{room["server_seed"]}</code>')

    await _notify(bot, players, text, lambda uid: _after_kb('jackpot'))
