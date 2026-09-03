"""Отрисовка экранов.

Одна функция вместо разбросанных по коду `edit_message_text` / `send_message`
с копипастой обработки «message is not modified».

Экранов с медиа два: главное меню и каталог игр — гифка + подпись + кнопки
(`render_animation`, гифки заданы в `config.MENU_ANIMATION` и
`config.GAMES_ANIMATION`). Отсюда три правила, которые соблюдают `render` и
`render_animation`:

* сообщение с медиа нельзя превратить в текстовое правкой — Telegram на
  `edit_text` отвечает отказом, поэтому такое сообщение удаляется, а экран
  присылается новым;
* сама гифка между открытиями одного экрана не перезагружается: после первой
  удачной отправки в памяти остаётся `file_id`, и дальше уезжает он, а не
  файл целиком;
* переход между двумя экранами с разными гифками заменяет сообщение, а не
  правит подпись — иначе картинка осталась бы от прошлого экрана.
"""

import logging
from pathlib import Path

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardMarkup,
                           Message)

import config
import db

log = logging.getLogger(__name__)


class ChatCall:
    """Подделка CallbackQuery для запуска игры текстовой командой из чата.

    Игры написаны под клик: берут из события `from_user`, `message`, `bot` и
    квитируют клик через `answer()`. Текстовая команда даёт всё то же, кроме
    тоста — в чате его показать некому. Поэтому:

    * `answer(text, show_alert=True)` — важное сообщение («не хватает на
      ставку», «раунд устарел»), уезжает ответом в чат;
    * `answer('Забрал $1.00')` без alert — декоративная квитанция, которая
      всегда дублируется текстом экрана, и в чате она гасится;
    * `answer()` без текста — просто ack, гасится.

    Так один и тот же код игры обслуживает и приватное меню, и групповой чат.
    """

    def __init__(self, message: Message, data: str = '') -> None:
        self.message = message
        self.from_user = message.from_user
        self.data = data

    @property
    def bot(self):
        return self.message.bot

    async def answer(self, text: str | None = None, show_alert: bool = False,
                     **_kw) -> None:
        if text and show_alert:
            await self.message.reply(text)


def chat_id_of(event) -> int | None:
    """id группы, из которой запускают игру. Личка — None.

    Раунд запоминает чат, чтобы движок знал, владельцу какой группы капает
    процент с проигрыша (db.pay_chat_owner). Годится и для клика, и для
    ChatCall: у обоих чат лежит в `message`. В личке владельца нет, и поле
    остаётся пустым — иначе процент капал бы с игры «в самом себе».
    """
    message = getattr(event, 'message', None)
    chat = getattr(message, 'chat', None)
    if chat is None or chat.type == 'private':
        return None
    return chat.id


def is_private(event) -> bool:
    """Событие из лички? От этого зависят web_app-кнопки в клавиатуре.

    Mini App Telegram открывает только из приватного чата: web_app-кнопка в
    группе отклоняется вместе со всей клавиатурой. Поэтому там, где чат
    определить не удалось, ответ «нет» — потерять кнопку не страшно, уронить
    меню целиком страшно.

    Событием может быть Message (у него чат свой), CallbackQuery или ChatCall
    (у них чат внутри `message`).
    """
    message = getattr(event, 'message', None) or event
    chat = getattr(message, 'chat', None)
    return chat is not None and chat.type == 'private'


async def render(event: Message | CallbackQuery | ChatCall, text: str,
                 kb: InlineKeyboardMarkup | None = None, *,
                 new: bool = False) -> Message | None:
    """Правит сообщение, из которого пришёл callback, либо присылает новое.

    new=True — принудительно новое сообщение (нужно там, где предыдущее
    занято анимацией дайса и править его нельзя).
    """
    if isinstance(event, ChatCall):
        # Править чужое сообщение с командой нечего — экран всегда новый.
        return await event.message.answer(text, reply_markup=kb)
    if isinstance(event, CallbackQuery):
        message = event.message
        if not new and message is not None and _has_media(message):
            # Пришли из главного меню, а оно с гифкой. Текстом такое сообщение
            # уже не станет — убираем и присылаем экран заново.
            if await _delete(message):
                return await message.answer(text, reply_markup=kb)
            # Удалить не дали (чужой чат, сообщение старше двух суток) —
            # правим подпись: гифка сверху останется, но второго меню в чате
            # не появится.
            return await _edit_caption(message, text, kb)
        if not new and message is not None:
            try:
                return await message.edit_text(text, reply_markup=kb)
            except TelegramBadRequest as e:
                # Текст и клавиатура совпали — экран уже такой, это не ошибка.
                if 'not modified' in str(e):
                    return message
                # Сообщение слишком старое для правки — просто пришлём новое.
                log.debug('edit failed, sending new: %s', e)
        if message is not None:
            return await message.answer(text, reply_markup=kb)
        return None
    return await event.answer(text, reply_markup=kb)


# --- меню с гифкой ----------------------------------------------------------

def _has_media(message: Message) -> bool:
    """Есть ли у сообщения медиа. Такое сообщение правится только подписью."""
    return any(getattr(message, attr, None) for attr in
               ('animation', 'photo', 'video', 'document'))


async def _delete(message: Message) -> bool:
    """Убирает сообщение. Не дали — False, звать это должно быть безопасно.

    Ловим весь TelegramAPIError, а не только BadRequest: в группе бот может
    оказаться без прав на удаление, и это TelegramForbiddenError. Ни то ни
    другое не повод не показать игроку экран.
    """
    try:
        await message.delete()
        return True
    except TelegramAPIError as e:
        log.debug('удалить сообщение не вышло: %s', e)
        return False


async def _edit_caption(message: Message, text: str,
                        kb: InlineKeyboardMarkup | None) -> Message | None:
    try:
        return await message.edit_caption(caption=text, reply_markup=kb)
    except TelegramBadRequest as e:
        if 'not modified' in str(e):
            return message
        log.debug('правка подписи не прошла: %s', e)
        return None


# file_id гифки после первой удачной отправки, ключ — путь к файлу. Telegram
# принимает его вместо файла, и экран перестаёт стоить мегабайты трафика на
# каждое открытие. Кеш живёт в памяти процесса: id привязан к боту, а не к
# чату, и переживать перезапуск ему незачем — после рестарта он наберётся
# заново. Словарь, а не одна переменная: гифок две (меню и каталог игр), и
# один общий id подсунул бы на экран картинку от соседнего экрана.
_animation_ids: dict[Path, str] = {}


async def _send_animation(message: Message, text: str,
                          kb: InlineKeyboardMarkup | None,
                          path: Path) -> Message:
    """Присылает гифку с подписью. Не вышло — присылает обычный текст.

    Подпись может быть пустой — экран каталога игр так и сделан, гифка плюс
    кнопки. Но текстовое сообщение без текста Telegram не принимает, поэтому в
    аварийном пути пустая подпись заменяется названием казино.
    """
    fallback = text or f'{config.CASINO_NAME}'
    cached = _animation_ids.get(path)
    if cached:
        try:
            return await message.answer_animation(cached, caption=text,
                                                  reply_markup=kb)
        except TelegramBadRequest as e:
            log.debug('file_id гифки не принят, грузим файл: %s', e)
            _animation_ids.pop(path, None)

    if not path.is_file():
        log.warning('нет файла %s — экран уедет текстом', path)
        return await message.answer(fallback, reply_markup=kb)

    try:
        sent = await message.answer_animation(FSInputFile(path), caption=text,
                                              reply_markup=kb)
    except TelegramAPIError as e:
        # Любой отказ по картинке — не повод не показать экран. Гифка
        # украшение, баланс и кнопки — нет.
        log.warning('гифка %s не отправилась (%s) — экран уедет текстом',
                    path.name, e)
        return await message.answer(fallback, reply_markup=kb)

    if sent.animation is not None:
        _animation_ids[path] = sent.animation.file_id
    return sent


def _shows(message: Message, path: Path) -> bool:
    """На экране уже эта самая гифка? Тогда достаточно правки подписи.

    Сравниваем file_id с кешем, а не просто проверяем наличие анимации: у меню
    и каталога игр гифки разные, и правка подписи вместо замены сообщения
    оставила бы игрока с картинкой от предыдущего экрана. Пустой кеш (первый
    показ после рестарта) даёт False — сообщение заменится, это безопасный
    вариант ответа.
    """
    animation = getattr(message, 'animation', None)
    if animation is None:
        return False
    return _animation_ids.get(path) == animation.file_id


async def render_animation(event: Message | CallbackQuery | ChatCall, text: str,
                           kb: InlineKeyboardMarkup | None = None, *,
                           animation: Path | None = None) -> Message | None:
    """То же, что render, но экран уезжает гифкой с подписью.

    animation — какую гифку показать; по умолчанию гифка главного меню.

    Возврат в меню из самого меню не перезагружает файл: гифка уже на месте,
    правится только подпись. Переход между экранами с разными гифками
    заменяет сообщение — иначе картинка осталась бы от прошлого экрана.
    """
    path = animation or config.MENU_ANIMATION

    if isinstance(event, ChatCall):
        return await _send_animation(event.message, text, kb, path)
    if not isinstance(event, CallbackQuery):
        return await _send_animation(event, text, kb, path)

    message = event.message
    if message is None:
        return None

    if _shows(message, path):
        edited = await _edit_caption(message, text, kb)
        if edited is not None:
            return edited

    # Экран был текстовым, с другой гифкой (или подпись править не дали) —
    # заменяем сообщение.
    await _delete(message)
    return await _send_animation(message, text, kb, path)



async def notify_admins(bot, text: str, kb: InlineKeyboardMarkup | None = None,
                        *, exclude: tuple[int, ...] = ()) -> int:
    """Рассылает служебное сообщение владельцу и всем админам.

    Заблокировавший бота админ не должен ломать доставку остальным, поэтому
    каждая отправка обёрнута отдельно. Отдаёт число реально доставленных.
    """
    ids = {config.OWNER_ID}
    ids.update(row['user_id'] for row in await db.list_admins())
    ids.difference_update(exclude)

    sent = 0
    for admin_id in sorted(ids):
        try:
            await bot.send_message(admin_id, text, reply_markup=kb)
            sent += 1
        except Exception as e:
            log.warning('админ %s недоступен: %s', admin_id, e)
    return sent
