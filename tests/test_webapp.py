"""Mini App: подпись initData и серверная сторона кейса.

Проверяется то, ради чего сервер вообще существует, — клиенту верить нельзя:

* без подписи Telegram запрос не проходит вовсе;
* подпись чужим токеном и правка user внутри строки ломают проверку;
* где лежит приз, клиент не узнаёт до открытия;
* деньги начисляются один раз, сколько бы запросов ни пришло.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import shutil
import subprocess
import time
import urllib.parse

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
import db
import webapp
from helpers import fresh_db, mk_user
from test_daily import PRIZE, StubBot, play_day
from webapp.server import STATIC

TOKEN = '123456:TEST-TOKEN-FOR-SIGNATURE'


def init_data(user_id: int, *, token: str = TOKEN,
              auth_date: int | None = None, tamper: bool = False) -> str:
    """Собирает initData так же, как это делает Telegram."""
    fields = {
        'auth_date': str(auth_date if auth_date is not None else int(time.time())),
        'query_id': 'AAHdF6IQAAAAAN0XohDhrOrc',
        'user': json.dumps({'id': user_id, 'first_name': 'Тест',
                            'username': f'user{user_id}'},
                           separators=(',', ':'), ensure_ascii=False),
    }
    check = '\n'.join(f'{k}={fields[k]}' for k in sorted(fields))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        # Подменяем id уже после подписи — ровно то, что сделал бы клиент.
        fields['user'] = fields['user'].replace(str(user_id), str(user_id + 1), 1)
    return urllib.parse.urlencode(fields)


@contextlib.asynccontextmanager
async def client(bot=None):
    old_token = config.TOKEN
    config.TOKEN = TOKEN
    test_client = TestClient(TestServer(webapp.build(bot or StubBot())))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()
        config.TOKEN = old_token

# --- подпись ----------------------------------------------------------------

async def test_no_init_data_is_unauthorized():
    async with fresh_db(), client() as c:
        assert (await c.post('/api/state', json={})).status == 401


async def test_tampered_user_is_unauthorized():
    """Подменённый после подписи user_id — самая очевидная атака."""
    async with fresh_db(), client() as c:
        response = await c.post('/api/state',
                                json={'initData': init_data(500, tamper=True)})
        assert response.status == 401
        assert (await response.json())['error'] == 'bad-init-data'


async def test_foreign_token_is_unauthorized():
    async with fresh_db(), client() as c:
        raw = init_data(501, token='999:SOMEONE-ELSES-TOKEN')
        assert (await c.post('/api/state', json={'initData': raw})).status == 401


async def test_expired_init_data_is_unauthorized():
    async with fresh_db(), client() as c:
        stale = int(time.time()) - config.WEBAPP_INITDATA_TTL - 60
        response = await c.post('/api/state',
                                json={'initData': init_data(502, auth_date=stale)})
        assert response.status == 401
        assert (await response.json())['error'] == 'init-data-expired'


async def test_init_data_in_authorization_header():
    """Современные Mini App отправляют подпись заголовком — принимаем и так."""
    async with fresh_db(), client() as c:
        await mk_user(503, balance_cents=250)
        response = await c.post(
            '/api/state', json={},
            headers={'Authorization': 'tma ' + init_data(503)})
        assert response.status == 200
        assert (await response.json())['balance'] == '$2.50'


async def test_banned_user_is_forbidden():
    async with fresh_db(), client() as c:
        uid = await mk_user(504)
        await db.set_banned(uid, True)
        response = await c.post('/api/state', json={'initData': init_data(uid)})
        assert response.status == 403
        assert (await response.json())['error'] == 'banned'

# --- кейс через Mini App ----------------------------------------------------

async def test_open_does_not_leak_winning_card():
    """Пока кейс не открыт, из ответа сервера выигрышную карточку не узнать."""
    async with fresh_db(), client() as c:
        await mk_user(600)
        data = await (await c.post('/api/case/open',
                                   json={'initData': init_data(600)})).json()
        assert data['status'] == 'open'
        assert data['reveal'] is None
        assert 'win' not in data and 'win_index' not in data
        assert isinstance(data['case_id'], int)


async def test_pick_credits_prize_once():
    async with fresh_db(), client() as c:
        uid = await mk_user(601, balance_cents=100)
        raw = init_data(601)
        opened = await (await c.post('/api/case/open', json={'initData': raw})).json()

        # Клиент выигрышного индекса не знает — в тесте берём его из базы.
        case = await db.open_daily_case(uid)
        payload = {'initData': raw, 'case_id': opened['case_id'],
                   'index': case['win_index']}

        first = await (await c.post('/api/case/pick', json=payload)).json()
        assert first['pick'] == 'ok'
        assert first['reveal']['payout_cents'] == PRIZE
        assert first['reveal']['win'] == case['win_index']
        assert first['balance_cents'] == 100 + PRIZE
        assert first['status'] == 'cooldown'
        assert first['seconds_left'] > 0

        second = await (await c.post('/api/case/pick', json=payload)).json()
        assert second['pick'] == 'already'
        assert second['balance_cents'] == 100 + PRIZE
        assert await db.get_balance(uid) == 100 + PRIZE


async def test_parallel_picks_credit_once():
    """Две вкладки Mini App нажали одновременно — приз всё равно один."""
    async with fresh_db(), client() as c:
        uid = await mk_user(602)
        raw = init_data(602)
        opened = await (await c.post('/api/case/open', json={'initData': raw})).json()
        case = await db.open_daily_case(uid)
        payload = {'initData': raw, 'case_id': opened['case_id'],
                   'index': case['win_index']}

        responses = await asyncio.gather(*(c.post('/api/case/pick', json=payload)
                                          for _ in range(4)))
        picks = [(await r.json())['pick'] for r in responses]
        assert picks.count('ok') == 1
        assert await db.get_balance(uid) == PRIZE


async def test_state_carries_the_streak():
    """Серию считает сервер: клиент получает счёт, день и обе суммы готовыми."""
    async with fresh_db(), client() as c:
        uid = await mk_user(610)
        raw = init_data(610)

        first = await (await c.post('/api/state', json={'initData': raw})).json()
        assert (first['streak'], first['streak_day']) == (0, 1)
        assert first['prize'] == '$0.05' and first['next_prize'] == '$0.06'

        # Два угаданных дня подряд: третий кейс должен стоить $0.07.
        for _ in range(2):
            await play_day(uid)
        third = await (await c.post('/api/state', json={'initData': raw})).json()
        assert (third['streak'], third['streak_day']) == (2, 3)
        assert third['prize'] == '$0.07' and third['next_prize'] == '$0.08'
        assert third['streak_seconds_left'] > 0
        assert third['streak_max_days'] == config.DAILY_STREAK_MAX_DAYS

        # Открыли третий на пустую карточку — огонёк гаснет прямо в ответе.
        opened = await (await c.post('/api/case/open', json={'initData': raw})).json()
        case = await db.open_daily_case(uid)
        empty = next(i for i in range(case['cards']) if i != case['win_index'])
        after = await (await c.post('/api/case/pick', json={
            'initData': raw, 'case_id': opened['case_id'], 'index': empty})).json()
        assert after['streak'] == 0
        assert after['prize'] == '$0.05'


async def test_pick_grows_the_streak_and_the_prize():
    async with fresh_db(), client() as c:
        uid = await mk_user(611, balance_cents=100)
        raw = init_data(611)
        await play_day(uid)

        opened = await (await c.post('/api/case/open', json={'initData': raw})).json()
        case = await db.open_daily_case(uid)
        assert case['prize_cents'] == PRIZE + config.DAILY_STREAK_STEP_CENTS

        data = await (await c.post('/api/case/pick', json={
            'initData': raw, 'case_id': opened['case_id'],
            'index': case['win_index']})).json()
        assert data['reveal']['payout_cents'] == case['prize_cents']
        assert data['streak'] == 2
        assert data['prize'] == '$0.07'          # столько будет в следующем

async def test_pick_requires_subscription():
    async with fresh_db():
        uid = await mk_user(603)
        await db.add_channel(-1010, 'chan', 'Канал', None, 1)
        async with client(StubBot({-1010: 'member'})) as c:
            raw = init_data(603)
            opened = await (await c.post('/api/case/open',
                                         json={'initData': raw})).json()
            case_id = opened['case_id']

        # Игрок отписался между выдачей и открытием.
        async with client(StubBot({-1010: 'left'})) as c:
            data = await (await c.post('/api/case/pick', json={
                'initData': init_data(603), 'case_id': case_id, 'index': 0})).json()
            assert data['pick'] == 'subscribe'
            assert data['status'] == 'subscribe'
            assert [ch['chat_id'] for ch in data['channels']] == [-1010]
            assert await db.get_balance(uid) == 0
            # Кейс не сожжён: подписался — открывай.
            assert (await db.open_daily_case(uid))['id'] == case_id


async def test_pick_validates_payload():
    async with fresh_db(), client() as c:
        await mk_user(604)
        raw = init_data(604)
        opened = await (await c.post('/api/case/open', json={'initData': raw})).json()
        for bad in ({'index': '0', 'case_id': opened['case_id']},
                    {'index': True, 'case_id': opened['case_id']},
                    {'index': 0, 'case_id': None},
                    {'index': 0}):
            response = await c.post('/api/case/pick',
                                    json={'initData': raw, **bad})
            assert response.status == 400, bad


async def test_unknown_case_id_is_not_found():
    async with fresh_db(), client() as c:
        await mk_user(605)
        data = await (await c.post('/api/case/pick', json={
            'initData': init_data(605), 'case_id': 999_999, 'index': 0})).json()
        assert data['pick'] == 'not_found'


# --- слоты ------------------------------------------------------------------

async def test_slots_state_needs_signature():
    async with fresh_db(), client() as c:
        assert (await c.post('/api/slots/state', json={})).status == 401
        assert (await c.post('/api/slots/spin', json={})).status == 401
        assert (await c.post('/api/profile', json={})).status == 401


async def test_slots_state_gives_paytable_and_bets():
    async with fresh_db(), client() as c:
        await mk_user(700, balance_cents=500)
        data = await (await c.post('/api/slots/state',
                                   json={'initData': init_data(700)})).json()
        assert (data['cols'], data['rows']) == (6, 5)
        assert data['cluster'] == 8
        assert data['balance'] == '$5.00'
        assert len(data['paytable']) == 8
        assert all(len(s['pays']) == 3 for s in data['paytable'])
        assert data['bets'] and min(data['bets']) >= data['min_bet']
        assert data['history'] == []


async def test_spin_charges_and_returns_result():
    async with fresh_db(), client() as c:
        uid = await mk_user(701, balance_cents=1000)
        data = await (await c.post('/api/slots/spin', json={
            'initData': init_data(701), 'bet_cents': 100,
            'spin_id': 'web-1'})).json()

        assert data['status'] == 'ok'
        spin = data['spin']
        assert len(spin['grid']) == 6 and len(spin['grid'][0]) == 5
        assert spin['bet_cents'] == 100
        assert data['balance_cents'] == 1000 - 100 + spin['payout_cents']
        assert data['balance_cents'] == await db.get_balance(uid)
        assert len(data['history']) == 1
        # Провably fair: сид уезжает клиенту, чтобы спин можно было пересчитать.
        assert len(spin['fair']['server_seed_hash']) == 64


async def test_same_spin_id_over_http_charges_once():
    async with fresh_db(), client() as c:
        uid = await mk_user(702, balance_cents=1000)
        payload = {'initData': init_data(702), 'bet_cents': 100,
                   'spin_id': 'web-repeat'}
        first = await (await c.post('/api/slots/spin', json=payload)).json()
        second = await (await c.post('/api/slots/spin', json=payload)).json()

        assert first['status'] == 'ok'
        assert second['status'] == 'repeat'
        assert second['spin']['round_id'] == first['spin']['round_id']
        assert second['balance_cents'] == first['balance_cents']
        assert await db.get_balance(uid) == first['balance_cents']

async def test_client_cannot_forge_the_bet():
    """Ставку клиент выбирает, но не назначает: границы проверяет сервер."""
    async with fresh_db(), client() as c:
        uid = await mk_user(703, balance_cents=1000)
        raw = init_data(703)
        for bad in ('100', 10.5, True, -100, None):
            response = await c.post('/api/slots/spin', json={
                'initData': raw, 'bet_cents': bad, 'spin_id': 'forge'})
            assert response.status == 400, bad

        # Целое, но вне лимитов — уже логика игры, а не формат запроса.
        data = await (await c.post('/api/slots/spin', json={
            'initData': raw, 'bet_cents': 1, 'spin_id': 'forge-2'})).json()
        assert data['status'] == 'bad_bet'
        data = await (await c.post('/api/slots/spin', json={
            'initData': raw, 'bet_cents': 900_000, 'spin_id': 'forge-3'})).json()
        assert data['status'] == 'bad_bet'
        assert await db.get_balance(uid) == 1000


async def test_spin_needs_money():
    async with fresh_db(), client() as c:
        uid = await mk_user(704, balance_cents=50)
        data = await (await c.post('/api/slots/spin', json={
            'initData': init_data(704), 'bet_cents': 100,
            'spin_id': 'poor'})).json()
        assert data['status'] == 'no_money'
        assert data['spin'] is None
        assert await db.get_balance(uid) == 50


# --- профиль ----------------------------------------------------------------

async def test_profile_shows_the_same_numbers_as_the_bot():
    async with fresh_db(), client() as c:
        uid = await mk_user(705, balance_cents=1000)
        await c.post('/api/slots/spin', json={
            'initData': init_data(705), 'bet_cents': 100, 'spin_id': 'p-1'})
        case, _ = await db.issue_daily_case(uid)
        await db.pick_daily_case(uid, case['id'], case['win_index'])

        data = await (await c.post('/api/profile',
                                   json={'initData': init_data(705)})).json()
        row = await db.get_user(uid)
        assert data['id'] == uid
        assert data['balance_cents'] == row['balance_cents']
        assert data['played'] == 1
        assert data['wagered'] == '$1.00'
        assert data['cases'] == {'opened': 1, 'paid': '$0.05',
                                 'streak': 1, 'next_prize': '$0.06'}
        assert data['fair']['nonce'] == 1


# --- страница ---------------------------------------------------------------

async def test_page_and_static_are_served():
    """Каждый файл, на который ссылается страница, сервер отдаёт.

    Список путей берётся из самой разметки, а не переписывается сюда руками:
    новый скрипт в index.html проверяется сам, а забытый роняет тест здесь, а
    не пустым экраном у игрока.
    """
    async with fresh_db(), client() as c:
        page = await c.get('/')
        assert page.status == 200
        body = await page.text()
        assert 'Ежедневный кейс' in body
        assert 'telegram-web-app.js' in body

        paths = re.findall(r'(?:src|href)="(/static/[^"]+)"', body)
        assert len(paths) >= 5, paths
        for path in paths:
            assert (await c.get(path)).status == 200, path

        health = await (await c.get('/health')).json()
        assert health['ok'] is True


@pytest.mark.skipif(shutil.which('node') is None,
                    reason='нет node — синтаксис JS проверить нечем')
def test_js_files_parse():
    """`node --check` по каждому скрипту страницы.

    Питоновские тесты гоняют серверную сторону, а экран собирается в браузере:
    одна лишняя скобка в конце файла молча ломает вкладку целиком, и ни
    compileall, ни остальные тесты этого не видят. Проверка стоит секунду, а
    поломка выглядит как «приложение не работает» без единой ошибки в логе.
    """
    scripts = sorted(STATIC.glob('*.js'))
    assert scripts, 'в webapp/static нет ни одного скрипта'
    for path in scripts:
        done = subprocess.run(['node', '--check', str(path)],
                              capture_output=True, text=True)
        assert done.returncode == 0, f'{path.name}: {done.stderr.strip()}'
