# -*- coding: utf-8 -*-
"""Калибровка масштаба выплат слота: подбираем SCALE под целевой RTP.

    py -3.10 tools\\calibrate_storm.py 800000

Первый аргумент — сколько спинов гонять, дальше можно перечислить масштабы для
сравнения (по умолчанию берётся текущий `storm.SCALE`). В конце каждой строки
печатается масштаб, при котором отдача сойдётся с `config.RTP` — его и надо
прописать в `games/storm.py`, иначе
`tests/test_storm.py::test_rtp_matches_config` будет падать.
"""
import secrets
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from games import engine, storm  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
SCALES = [float(x) for x in sys.argv[2:]] or [storm.SCALE]

server = secrets.token_hex(32)
client = secrets.token_hex(8)


def measure(scale: float, n: int) -> dict:
    storm.SCALE = scale
    mults, hits, capped, cascades, storms = [], 0, 0, 0, 0
    for nonce in range(1, n + 1):
        rnd = engine.Round(id=0, user_id=0, game=storm.GAME, bet_cents=100,
                           server_seed=server, server_seed_hash='',
                           client_seed=client, nonce=nonce)
        result = storm.play(rnd)
        m = result['multiplier']
        mults.append(m)
        hits += m > 0
        capped += m >= storm.MAX_MULTIPLIER
        cascades += len(result['steps'])
        storms += bool(result['storm_total'])
    mean = statistics.fmean(mults)
    sd = statistics.pstdev(mults)
    return {'scale': scale, 'rtp': mean, 'stderr': sd / n ** 0.5, 'sd': sd,
            'hit': hits / n, 'cap': capped / n, 'cascades': cascades / n,
            'storm': storms / n, 'max': max(mults)}


print('цель RTP =', config.RTP, ' спинов на прогон:', N)
for scale in SCALES:
    r = measure(scale, N)
    print(f"SCALE={r['scale']:.6f}  RTP={r['rtp']:.4f} ±{4 * r['stderr']:.4f}  "
          f"хитрейт={r['hit']:.3f}  каскадов/спин={r['cascades']:.2f}  "
          f"шторм={r['storm']:.3f}  кап={r['cap']:.5f}  max×{r['max']:.1f}  "
          f"sd={r['sd']:.2f}")
    print(f"   -> SCALE для {config.RTP}: {scale * config.RTP / r['rtp']:.6f}")
