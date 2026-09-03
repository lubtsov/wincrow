# -*- coding: utf-8 -*-
"""Разовая диагностика боевого Mini App: доходят ли запросы до API.

Подпись собирается тем же токеном, что лежит в config, и уезжает на публичный
адрес. Токен нигде не печатается. Пользователь в базе создаётся один — тестовый.

    py -3.10 tools\probe_live.py
"""
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1 else config.WEBAPP_URL).rstrip('/')
UID = 900000001


def init_data(user_id: int) -> str:
    fields = {
        'auth_date': str(int(time.time())),
        'query_id': 'AAHdF6IQAAAAAN0XohDhrOrc',
        'user': json.dumps({'id': user_id, 'first_name': 'Проверка',
                            'username': 'probe'},
                           separators=(',', ':'), ensure_ascii=False),
    }
    check = '\n'.join(f'{k}={fields[k]}' for k in sorted(fields))
    secret = hmac.new(b'WebAppData', config.TOKEN.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


def post(path: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        BASE + path, data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            text = response.read().decode('utf-8', 'replace')
            print(f'{path}: {response.status} {text[:220]}')
    except urllib.error.HTTPError as e:
        print(f'{path}: {e.code} {e.read().decode("utf-8", "replace")[:220]}')
    except Exception as e:
        print(f'{path}: не дошло — {e}')


print('адрес:', BASE)
raw = init_data(UID)
post('/api/state', {'initData': raw})
post('/api/slots/state', {'initData': raw})
post('/api/profile', {'initData': raw})
post('/api/state', {})                      # без подписи — обязан быть 401
