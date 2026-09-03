"""HTTP-сервер Mini App: живёт в том же процессе, что и бот.

Отдельной команды запуска нет — `py main.py` поднимает и polling, и этот
сервер: `main.py` зовёт `start()` при старте и `stop()` в `finally`. Отсюда
общий event loop, общее соединение с базой и один объект Bot на всё.

Своей системы пользователей у Mini App нет. Кто пришёл, сервер узнаёт ТОЛЬКО
из initData, подписанной токеном бота (`safe_parse_webapp_init_data`), и
только оттуда берёт user_id. Из JavaScript принимаются ровно две вещи: номер
карточки и id кейса. Баланс, выигрышная карточка, пауза и проверка подписки
считаются здесь и сверяются с базой — подделать результат на клиенте нечем,
потому что клиент его и не сообщает.
"""

import json
import logging
import time
from pathlib import Path

from aiogram import Bot
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

import config
import daily
import db
from db import fmt
from games import storm

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / 'static'

# Bot нужен внутри обработчиков: подписку проверяет он.
BOT_KEY = web.AppKey('bot', Bot)


def _fail(status: int, error: str) -> web.HTTPException:
    """Ошибка как JSON: клиент разбирает один и тот же формат всегда."""
    exc = {401: web.HTTPUnauthorized, 403: web.HTTPForbidden,
           400: web.HTTPBadRequest}.get(status, web.HTTPBadRequest)
    return exc(text=json.dumps({'error': error}, ensure_ascii=False),
               content_type='application/json')

async def _auth(request: web.Request) -> tuple[dict, object]:
    """(тело запроса, пользователь из initData). Иначе — 401/403.

    initData едет либо заголовком `Authorization: tma <...>` (так делают
    современные Mini App), либо полем `initData` в теле — принимаем оба.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    auth = request.headers.get('Authorization', '')
    raw = auth[4:].strip() if auth.startswith('tma ') else ''
    raw = raw or str(body.get('initData') or '')
    if not raw:
        raise _fail(401, 'no-init-data')

    try:
        parsed = safe_parse_webapp_init_data(config.TOKEN, raw)
    except ValueError:
        # Подпись не сходится: либо чужой токен, либо строку правили руками.
        raise _fail(401, 'bad-init-data')
    if parsed.user is None:
        raise _fail(401, 'no-user')

    # Подпись Telegram не истекает сама, поэтому срок ставим мы: иначе один раз
    # перехваченная строка работала бы вечно.
    if config.WEBAPP_INITDATA_TTL:
        age = time.time() - parsed.auth_date.timestamp()
        if age > config.WEBAPP_INITDATA_TTL:
            raise _fail(401, 'init-data-expired')

    await db.ensure_user(parsed.user.id, parsed.user.username)
    row = await db.get_user(parsed.user.id)
    if row is not None and row['banned']:
        raise _fail(403, 'banned')
    return body, parsed.user

def _reveal(case) -> dict | None:
    """Раскрытый кейс. Только для уже открытого: у закрытого win_index —
    секрет, и в JSON он не попадает ни при каких условиях."""
    if case is None or case['status'] != 'done':
        return None
    return {'picked': case['picked_index'], 'win': case['win_index'],
            'cards': case['cards'], 'payout_cents': case['payout_cents'],
            'payout': fmt(case['payout_cents']),
            'prize': fmt(case['prize_cents'])}


async def _snapshot(user_id: int, st: dict) -> dict:
    """Всё состояние экрана одним объектом — клиент только рисует его."""
    row = await db.get_user(user_id)
    balance = row['balance_cents'] if row is not None else 0
    return {
        'casino': config.CASINO_NAME,
        'balance_cents': balance,
        'balance': fmt(balance),
        'status': st['status'],
        'cards': st['cards'],
        'prize': fmt(st['prize_cents']),
        'prize_cents': st['prize_cents'],
        'cooldown': st['cooldown'],
        'seconds_left': st['seconds_left'],
        'case_id': st['case']['id'] if st['case'] is not None else None,
        'channels': [daily.as_dict(r) for r in st['missing']],
        'broken': len(st['broken']),
        'reveal': _reveal(st['last']) if st['status'] == 'cooldown' else None,
    }


async def api_state(request: web.Request) -> web.Response:
    _, user = await _auth(request)
    st = await daily.state(request.app[BOT_KEY], user.id)
    return web.json_response(await _snapshot(user.id, st))


async def api_check(request: web.Request) -> web.Response:
    """Кнопка «Проверить подписку» — тот же ответ, что и у /api/state."""
    return await api_state(request)


async def api_open(request: web.Request) -> web.Response:
    """Выдача кейса. Идемпотентна: два запроса подряд дадут один кейс."""
    _, user = await _auth(request)
    st, result = await daily.issue(request.app[BOT_KEY], user.id)
    data = await _snapshot(user.id, st)
    data['issue'] = result
    return web.json_response(data)

async def api_pick(request: web.Request) -> web.Response:
    """Открытие карточки. Единственная операция с деньгами в Mini App.

    Из клиента приходят только номер карточки и id кейса. Совпал ли номер с
    выигрышным, решает база (`db.pick_daily_case`) — там же и начисление, в
    одной транзакции с отметкой «открыт». Поэтому два одновременных запроса,
    две вкладки и перезапуск бота заплатят ровно один раз.
    """
    body, user = await _auth(request)
    bot = request.app[BOT_KEY]

    index, case_id = body.get('index'), body.get('case_id')
    if not isinstance(index, int) or not isinstance(case_id, int) \
            or isinstance(index, bool) or isinstance(case_id, bool):
        raise _fail(400, 'bad-request')

    st = await daily.state(bot, user.id)
    if st['status'] == 'subscribe':
        data = await _snapshot(user.id, st)
        data['pick'] = 'subscribe'
        return web.json_response(data)

    case, result = await db.pick_daily_case(user.id, case_id, index)
    fresh = await daily.state(bot, user.id)
    data = await _snapshot(user.id, fresh)
    data['pick'] = result
    if case is not None:
        data['reveal'] = _reveal(case)
    log.info('Mini App: кейс #%s игрока %s -> %s (%s)',
             case_id, user.id, result, case['payout_cents'] if case else 0)
    return web.json_response(data)


# --- слоты «Сочный шторм» ---------------------------------------------------
#
# Спин целиком считает сервер (games/storm.py) и записывает его в ту же таблицу
# rounds, что и игры в боте. Клиент присылает две вещи: сумму ставки и свой
# уникальный id спина — по нему повторный запрос отдаёт прежний результат, а не
# снимает ставку второй раз.


async def api_slots(request: web.Request) -> web.Response:
    """Всё, что нужно экрану слота до первого спина."""
    _, user = await _auth(request)
    row = await db.get_user(user.id)
    balance = row['balance_cents'] if row is not None else 0
    return web.json_response({
        'title': storm.TITLE,
        'cols': storm.COLS, 'rows': storm.ROWS,
        'cluster': storm.MIN_CLUSTER,
        'paytable': storm.paytable(),
        'bets': storm.bets(),
        'min_bet': config.MIN_BET_CENTS, 'max_bet': config.MAX_BET_CENTS,
        'rtp': config.RTP, 'max_multiplier': storm.MAX_MULTIPLIER,
        'balance_cents': balance, 'balance': fmt(balance),
        'history': await storm.history(user.id, 8),
    })


async def api_spin(request: web.Request) -> web.Response:
    body, user = await _auth(request)
    bet, spin_id = body.get('bet_cents'), body.get('spin_id')
    # Ноль и минус — не «ставка вне лимитов», а мусор в запросе: такую ставку
    # игрок в интерфейсе выбрать не может, поэтому 400, а не игровой статус.
    if not isinstance(bet, int) or isinstance(bet, bool) or bet <= 0 \
            or not isinstance(spin_id, str) or not spin_id:
        raise _fail(400, 'bad-request')

    result, status = await storm.spin(user.id, bet, spin_id)
    row = await db.get_user(user.id)
    balance = row['balance_cents'] if row is not None else 0
    if status == 'ok':
        log.info('слот: игрок %s ставка %s -> ×%s (%s)', user.id, bet,
                 result['multiplier'], result['payout_cents'])
    return web.json_response({
        'status': status,
        'spin': result or None,
        'balance_cents': balance, 'balance': fmt(balance),
        'history': await storm.history(user.id, 8),
    })


async def api_profile(request: web.Request) -> web.Response:
    """Профиль: те же цифры, что в боте, без единого запроса к Telegram."""
    _, user = await _auth(request)
    row = await db.get_user(user.id)
    if row is None:
        raise _fail(401, 'no-user')
    level, percent = db.referral_level(row['referrals'])
    seed = await (await db.conn().execute(
        'SELECT server_seed_hash, client_seed, nonce FROM seeds '
        'WHERE user_id = ?', (user.id,))).fetchone()
    cases = await db.daily_stats(user.id)
    return web.json_response({
        'id': user.id,
        'name': user.first_name or user.username or str(user.id),
        'username': user.username,
        'balance_cents': row['balance_cents'], 'balance': fmt(row['balance_cents']),
        'played': await db.games_played(user.id),
        'wagered': fmt(row['wagered_cents']), 'won': fmt(row['won_cents']),
        'net': fmt(row['won_cents'] - row['wagered_cents']),
        'deposited': fmt(row['deposited_cents']),
        'referrals': row['referrals'], 'level': level, 'percent': percent,
        'referral_earned': fmt(row['referral_earned_cents']),
        'chat_earned': fmt(row['chat_earned_cents']),
        'cases': {'opened': cases['opened'], 'paid': fmt(cases['paid'])},
        'fair': None if seed is None else {
            'hash': seed['server_seed_hash'], 'client_seed': seed['client_seed'],
            'nonce': seed['nonce']},
        'rtp': config.RTP,
    })


# --- статика ----------------------------------------------------------------

async def page(request: web.Request) -> web.Response:
    """Сама страница Mini App. Без кеша: версия в Telegram живёт долго."""
    body = (STATIC / 'index.html').read_text(encoding='utf-8')
    return web.Response(text=body, content_type='text/html',
                        headers={'Cache-Control': 'no-store'})


async def health(_: web.Request) -> web.Response:
    return web.json_response({'ok': True, 'casino': config.CASINO_NAME})

@web.middleware
async def errors(request: web.Request, handler) -> web.Response:
    """Любая необработанная ошибка — JSON, а не HTML-страница aiohttp.

    Mini App разбирает ответы как JSON, и HTML в ответе выглядел бы для него
    как «сервер сломался молча». Заодно ошибка не роняет сервер целиком.
    """
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        log.exception('Mini App: ошибка в %s', request.path)
        return web.json_response({'error': 'server-error'}, status=500)


def build(bot: Bot) -> web.Application:
    app = web.Application(middlewares=[errors])
    app[BOT_KEY] = bot
    app.router.add_get('/', page)
    app.router.add_get('/health', health)
    app.router.add_post('/api/state', api_state)
    app.router.add_post('/api/subscription/check', api_check)
    app.router.add_post('/api/case/open', api_open)
    app.router.add_post('/api/case/pick', api_pick)
    app.router.add_post('/api/slots/state', api_slots)
    app.router.add_post('/api/slots/spin', api_spin)
    app.router.add_post('/api/profile', api_profile)
    app.router.add_static('/static/', STATIC, name='static')
    return app


async def start(bot: Bot) -> web.AppRunner | None:
    """Поднимает сервер в текущем event loop. None — Mini App выключен."""
    if not config.WEBAPP_ENABLED:
        log.info('Mini App выключен (WEBAPP_ENABLED=0)')
        return None

    runner = web.AppRunner(build(bot), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBAPP_HOST, config.WEBAPP_PORT)
    await site.start()
    log.info('Mini App слушает http://%s:%s', config.WEBAPP_HOST,
             config.WEBAPP_PORT)
    if config.WEBAPP_URL:
        log.info('Mini App открывается по %s', config.WEBAPP_URL)
    else:
        log.warning('WEBAPP_URL пуст: кнопки Mini App в боте не будет, кейс '
                    'открывается кнопками внутри бота. Задай публичный '
                    'https-адрес (домен или туннель), чтобы включить витрину.')
    return runner


async def stop(runner: web.AppRunner | None) -> None:
    """Гасит сервер: дожидается открытых запросов и закрывает сокет."""
    if runner is not None:
        await runner.cleanup()
        log.info('Mini App остановлен')
