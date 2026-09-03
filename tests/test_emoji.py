"""Премиальные значки.

Проверяется не внешний вид, а три обещания модуля: подложка внутри тега —
ровно один обычный эмодзи (иначе Telegram отвечает CUSTOM_EMOJI_INVALID),
PREMIUM_EMOJI=0 честно выключает разметку, а strip() возвращает текст к
подложкам. Последнее важно самим тестам: они сверяют тексты с эмодзи.
"""

import re

import config
import emoji as E


# --- таблица ----------------------------------------------------------------

def test_ids_are_numeric():
    """id значка — длинное число; опечатка в нём ломает отправку целиком."""
    ids = list(E.EMOJI_IDS.values()) + list(E._EXTRA_IDS.values())
    ids += [E.CASHIER_ID, E.CRASH_ID, E.STATS_ID]
    assert ids
    for emoji_id in ids:
        assert emoji_id.isdigit(), emoji_id


def test_fallbacks_are_single_emoji():
    """Подложка — один эмодзи, не ASCII и не два символа подряд.

    ASCII-подложку («$», буква) Telegram не принимает вообще, а два эмодзи в
    одном теге показываются только первым.
    """
    for fallback in list(E.EMOJI_IDS) + list(E._EXTRA_IDS):
        assert fallback, 'пустая подложка'
        assert not fallback.isascii(), fallback
        # Селекторы вариации и знак объединения к длине не считаются.
        core = [c for c in fallback if c not in '️‍']
        assert len(core) == 1, fallback


def test_extra_ids_do_not_shadow_table():
    """Подложки с общей картинкой живут отдельно — иначе tag() выберет не то."""
    assert not set(E._EXTRA_IDS) & set(E.EMOJI_IDS)


# --- сборка тега ------------------------------------------------------------

def test_emoji_wraps_fallback(monkeypatch):
    # Флаг ставим явно: прогон с PREMIUM_EMOJI=0 не должен ломать этот тест.
    monkeypatch.setattr(config, 'PREMIUM_EMOJI', True)
    out = E.emoji('123', '💎')
    assert out == '<tg-emoji emoji-id="123">💎</tg-emoji>'
    assert E.strip(out) == '💎'


def test_emoji_off_returns_plain(monkeypatch):
    monkeypatch.setattr(config, 'PREMIUM_EMOJI', False)
    assert E.emoji('123', '💎') == '💎'
    assert E.tag('💎') == '💎'


def test_constants_carry_their_fallback():
    """Константа обязана содержать свой эмодзи: с ним её видит не-премиум."""
    pairs = [(E.OK, '✅'), (E.FAIL, '❌'), (E.MINE, '💣'), (E.GEM, '💎'),
             (E.CASHIER, '💸'), (E.CASHIER_IN, '⬆️'), (E.CASHIER_OUT, '⬇️'),
             (E.CRASH, '📈'), (E.STATS, '📊'), (E.STATS_TOP, '🏆')]
    for value, fallback in pairs:
        assert E.strip(value) == fallback, value


def test_trophy_is_stats_top():
    """Игры берут кубок через TROPHY — псевдоним, а не второй id."""
    assert E.TROPHY == E.STATS_TOP


# --- tag() ------------------------------------------------------------------

def test_tag_lifts_known_emoji():
    assert E.tag('🎲') == E.DICE
    assert E.tag('💸') == E.CASHIER


def test_tag_passes_unknown_through():
    """Незаказанный значок возвращается как есть — вызывающему всё равно."""
    assert E.tag('🎟') == '🎟'
    assert E.tag('') == ''


# --- strip() ----------------------------------------------------------------

def test_strip_leaves_fallbacks():
    text = f'{E.OK} готово, {E.FAIL} нет'
    assert E.strip(text) == '✅ готово, ❌ нет'


def test_strip_handles_plain_and_empty():
    assert E.strip('просто текст') == 'просто текст'
    assert E.strip('') == ''
    assert E.strip(None) == ''


def test_strip_clears_all_tags():
    """После strip() разметки значков не остаётся ни в одной константе."""
    text = ' '.join([E.BACK, E.MONEY, E.TROPHY, E.CARD_BACK, E.MEDALS[0]])
    assert '<tg-emoji' not in E.strip(text)
    assert not re.search(r'</?tg-emoji', E.strip(text))
