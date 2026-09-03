"""Клавиатуры: ссылки, которые Telegram обязан принять.

Одна битая url-кнопка роняет всю клавиатуру целиком, поэтому ссылки собираются
только из того, что действительно похоже на юзернейм, а не из любого текста в
SUPPORT_NAME.
"""

import pytest

import config
import keyboards as kb


@pytest.fixture(autouse=True)
def keep_support():
    old = config.SUPPORT_NAME
    old_url = config.WEBAPP_URL
    yield
    config.SUPPORT_NAME = old
    config.WEBAPP_URL = old_url


def _urls(markup) -> list[str]:
    return [b.url for row in markup.inline_keyboard for b in row if b.url]


def _webapps(markup) -> list[str]:
    return [b.web_app.url for row in markup.inline_keyboard for b in row
            if b.web_app]


def _datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row
            if b.callback_data]


# --- ссылка на поддержку ----------------------------------------------------

def test_support_url_from_username():
    config.SUPPORT_NAME = '@polot_fratr'
    assert config.support_url() == 'https://t.me/polot_fratr'


def test_support_url_without_at():
    config.SUPPORT_NAME = 'polot_fratr'
    assert config.support_url() == 'https://t.me/polot_fratr'


@pytest.mark.parametrize('name', [
    '', 'ab', 'пиши в чат', '+79001234567', 'два слова', 'a' * 40, 'bad-name',
])
def test_support_url_none_when_not_a_username(name):
    config.SUPPORT_NAME = name
    assert config.support_url() is None


def test_support_button_in_menus():
    config.SUPPORT_NAME = '@polot_fratr'
    assert 'https://t.me/polot_fratr' in _urls(kb.main_menu(False, 'pilot_bot'))


def test_no_support_button_when_not_a_username():
    config.SUPPORT_NAME = 'пиши в чат'
    assert _urls(kb.main_menu(False, None)) == []


# --- меню без «Помощи» ------------------------------------------------------

def test_main_menu_has_no_help_section():
    """Раздела «Помощь» больше нет, а профиль и баланс из него — есть."""
    datas = _datas(kb.main_menu(False, 'pilot_bot'))
    assert 'faq' not in datas
    assert 'profile' in datas
    assert 'balance' in datas


def test_games_menu_keeps_fair_and_chat_commands():
    """Экраны из снесённой «Помощи» должны остаться достижимыми кнопкой."""
    datas = _datas(kb.groups_menu())
    assert 'fair' in datas
    assert 'chatcmd' in datas


def test_promo_code_reachable_from_balance():
    assert 'code' in _datas(kb.balance_menu())


# --- чаты и рефералка -------------------------------------------------------

def test_refs_menu_leads_to_chats():
    assert 'mychats' in _datas(kb.refs_menu('pilot_bot'))


def test_add_to_chat_link_is_startgroup():
    assert _urls(kb.chats_menu('pilot_bot')) == \
        ['https://t.me/pilot_bot?startgroup=true']


def test_no_add_button_without_bot_username():
    """Имя бота не узнали — кнопки нет: мёртвая ссылка хуже её отсутствия."""
    assert _urls(kb.chats_menu(None)) == []
    assert 'mychats' in _datas(kb.refs_menu(None))


def test_every_menu_has_a_way_back():
    for markup in (kb.refs_menu('pilot_bot'), kb.chats_menu('pilot_bot'),
                   kb.groups_menu(), kb.chat_help_menu()):
        assert 'menu' in _datas(markup)


# --- ежедневный кейс --------------------------------------------------------

def test_case_button_without_webapp_is_a_callback():
    """Mini App не настроен — кнопка кейса всё равно есть, ведёт внутрь бота."""
    config.WEBAPP_URL = ''
    menu = kb.main_menu(False, 'pilot_bot', True)
    assert 'case' in _datas(menu)
    assert _webapps(menu) == []


def test_webapp_button_only_in_private():
    """web_app-кнопку Telegram принимает только в личке.

    В группе она отклонила бы всю клавиатуру целиком, поэтому там на её месте
    обычный callback — кейс открывается экраном бота.
    """
    config.WEBAPP_URL = 'https://case.example.org'
    private = kb.main_menu(False, 'pilot_bot', True)
    group = kb.main_menu(False, 'pilot_bot', False)
    assert _webapps(private) == ['https://case.example.org#case',
                                 'https://case.example.org#slots']
    assert _webapps(group) == []
    assert 'case' in _datas(group)


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_menu_has_its_own_button_for_slots():
    """Слот живёт только в приложении, поэтому вход в него — отдельная кнопка.

    Кнопка кейса ведёт на кейс, эта — на слоты: экран уезжает якорем в адресе,
    и игроку не приходится каждый раз искать нужную вкладку.
    """
    config.WEBAPP_URL = 'https://case.example.org'
    menu = kb.main_menu(False, 'pilot_bot', True)
    assert kb.SLOTS_LABEL in _labels(menu)
    assert 'https://case.example.org#slots' in _webapps(menu)

    # Приложения нет — кнопки нет вовсе: внутри бота этого слота не существует.
    config.WEBAPP_URL = ''
    assert kb.SLOTS_LABEL not in _labels(kb.main_menu(False, 'pilot_bot', True))


def test_bottom_keyboard_opens_slots():
    config.WEBAPP_URL = 'https://case.example.org'
    button = kb.app_keyboard().keyboard[0][0]
    assert button.text == kb.APP_LABEL
    assert button.web_app.url == 'https://case.example.org#slots'

    config.WEBAPP_URL = ''
    assert kb.app_keyboard() is None


def test_screen_url_needs_both_domain_and_screen():
    config.WEBAPP_URL = 'https://case.example.org'
    assert config.webapp_screen_url('case') == 'https://case.example.org#case'
    assert config.webapp_screen_url() == 'https://case.example.org'
    config.WEBAPP_URL = ''
    assert config.webapp_screen_url('slots') == ''


def test_case_cards_carry_case_id():
    """В callback_data едет id кейса: клик из старого сообщения не должен
    применяться к новой выдаче."""
    datas = _datas(kb.case_cards(42, 3))
    assert datas[:3] == ['case:pick:42:0', 'case:pick:42:1', 'case:pick:42:2']


def test_opened_case_cards_are_inert_and_revealed():
    case = {'cards': 3, 'win_index': 1, 'picked_index': 2, 'prize_cents': 5}
    markup = kb.case_result(case)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels[:3] == ['$0.00', '$0.05', '👉 $0.00']
    assert set(_datas(markup)[:3]) == {'nop'}


def test_subscribe_screen_has_check_button():
    channels = [{'chat_id': -1, 'title': 'Канал', 'url': 'https://t.me/chan',
                 'broken': False}]
    markup = kb.case_subscribe(channels, True)
    assert _urls(markup) == ['https://t.me/chan']
    assert 'case:check' in _datas(markup)


def test_admin_menu_has_channels_section():
    assert 'admin:chan' in _datas(kb.admin_menu())
