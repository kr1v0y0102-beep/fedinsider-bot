import os
import re
import asyncio
import logging
import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("federal-insider")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
# Модель можно менять через переменную окружения без правки кода
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Ссылка на канал для подписи, например https://t.me/federalinsider
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/your_channel")
# Telegram ID админов через запятую: 123456789,987654321 (пусто = доступ у всех)
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

processed_media_groups: set[str] = set()
_http_session: aiohttp.ClientSession | None = None


class PostState(StatesGroup):
    waiting_for_input = State()
    waiting_for_photo = State()
    preview = State()


SYSTEM_PROMPT = """
Ты — главный редактор и автор Telegram-канала «Federal Insider». 
Твоя задача — переписывать входящую фактуру, материалы сливов и новости в фирменный пост канала.

СТРОГИЕ ПРАВИЛА ЮРИДИЧЕСКОЙ БЕЗОПАСНОСТИ И ЗАКОННОСТИ:
1. Строго соблюдай презумпцию невиновности (ст. 62 Конституции Украины). 
2. Не называй людей преступниками или виновными до вступления приговора суда в законную силу.
3. Используй ТОЛЬКО процессуальные и нейтральные статусы: «фигурант», «подозреваемый», «по версии следствия», «по данным правоохранителей».
4. Никаких прямых личных оскорблений или клеветы — дерзкая подача достигается исключительно за счет фирменного сленга, иронии и динамичного стиля.

ПРАВИЛА СТИЛЯ И ФИРМЕННЫЙ СЛОВАРЬ:
- Язык: русский.
- Тон: хлесткий, оперативный, с сочным силовым и уличным жаргоном.
- Активно используй слова и фразы:
  * Плётка (ствол / пистолет / огнестрельное оружие)
  * Умножили на ноль, наглухо умножили на ноль, жмуранули, забаранил
  * Прихватали ласты, закрутили копыта, запаковали, жестко приняли
  * Сделать дюльгу/делюгу, закосплеили, схематоз, нарезать куш, мамонт, палили, по газам
  * Послал физическое замечание в кабинет, запустил в космос, зарядил пиздюлину
  * Обувать пизду в лапти, ввести в блудняк
  * Выкупили тему, приняли на пон, дали объес, слить по-другому
  * Пошабашила, отслюнявил капусточки/хрустящих, зелень, налик, кэш, подкожный жир
  * ЕРДР открыт, коцало
  * Легавые, погоны, силовики, пацюки, дельцы, гусаки, фигуранты
  * Сверхсуровая баланда, нары, СИЗО, деда Лука, райдел

АРХИТЕКТУРА ПОСТА (выдавать строго в HTML-разметке Telegram, допустимы только теги <b>, <i>, <u>, <a href="...">, <code>, <blockquote>):
- Заголовок: [📌/🆔/FAKE/НАБУ/СХЕМА/ДБР] КАПСОВЫЙ ЗАГОЛОВОК С СУТЬЮ И СУММОЙ $
- Вторая строка: 📍 Локация | 💰 Ущерб / Сумма
- Вводный абзац: Завязка (кто, где и как умножил на ноль схему).
- Блок схемы: Подзаголовок (описание декораций и схематоза).
- Блок механики/цитаты: Подробное пошаговое описание развода (Инструкция была простая как дверь...).
- Финал: Кульминация (как всё пошабашило и как запаковали фигурантов).
- Официальная правовая рамка:
  ⚖️ <b>Официальная квалификация:</b>
  ч. X ст. YY УК Украины (название статьи).
- Панчлайн: Фирменный вывод про баланду, нары и СИЗО.
- Подпись внизу: <a href="__CHANNEL_LINK__">Federal Insider | Подписаться</a>

Весь пост должен уместиться примерно в 3500 символов.
Выдавай ТОЛЬКО готовый текст поста без вступлений, приветствий, пояснений и без markdown-обёрток (никаких ``` и **).
""".replace("__CHANNEL_LINK__", CHANNEL_LINK)


# ---------------------- Доступ только для админов ----------------------

async def admin_only_middleware(handler, event, data):
    user = getattr(event, "from_user", None)
    if ADMIN_IDS and (user is None or user.id not in ADMIN_IDS):
        if isinstance(event, types.CallbackQuery):
            await event.answer("⛔ Нет доступа", show_alert=True)
        return
    return await handler(event, data)


dp.message.outer_middleware(admin_only_middleware)
dp.callback_query.outer_middleware(admin_only_middleware)


# ---------------------- Вспомогательные функции ----------------------

def get_preview_keyboard(has_photo: bool):
    buttons = []
    if has_photo:
        buttons.append([InlineKeyboardButton(text="🚀 Опубликовать с фото", callback_data="publish_now")])
    else:
        buttons.append([InlineKeyboardButton(text="🚀 Опубликовать только текст", callback_data="publish_now")])
        buttons.append([InlineKeyboardButton(text="🖼️ Добавить фото", callback_data="add_photo")])

    buttons.append([InlineKeyboardButton(text="🔄 Перегенерировать", callback_data="regenerate")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def clean_model_output(text: str) -> str:
    """Убирает markdown-обёртки, которые Gemini иногда добавляет вокруг HTML."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90))
    return _http_session


async def generate_gemini_text(prompt_text: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {"role": "user", "parts": [{"text": f"Исходные данные:\n{prompt_text}"}]}
        ],
        "generationConfig": {"temperature": 0.9},
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
            for c in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ],
    }

    session = await get_session()
    last_error = None
    for attempt in range(3):
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status in (429, 500, 503):
                    last_error = f"API Error {resp.status}: {data}"
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if resp.status != 200:
                    raise Exception(f"API Error {resp.status}: {data}")
        except asyncio.TimeoutError:
            last_error = "Таймаут запроса к Gemini"
            await asyncio.sleep(2)
            continue

        candidates = data.get("candidates") or []
        if not candidates:
            reason = data.get("promptFeedback", {}).get("blockReason", "неизвестно")
            raise Exception(f"Gemini не вернул ответ (причина блокировки: {reason}). Попробуй переформулировать фактуру.")

        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if not text:
            raise Exception(f"Пустой ответ от Gemini (finishReason: {candidate.get('finishReason')}). Нажми «Перегенерировать».")
        return clean_model_output(text)

    raise Exception(last_error or "Не удалось получить ответ от Gemini")


async def send_post(chat_id, text: str, photo_id: str | None = None, reply_markup=None, prefix: str = ""):
    """
    Отправляет пост с учётом лимитов Telegram:
    - подпись к фото максимум 1024 символа (длиннее — фото и текст отдельными сообщениями);
    - при ошибке HTML-разметки повторяет отправку без неё.
    """
    full_text = (prefix + text)[:MESSAGE_LIMIT]

    async def _send(parse_mode):
        body = full_text if parse_mode else strip_tags(full_text)
        if photo_id and len(body) <= CAPTION_LIMIT:
            await bot.send_photo(chat_id=chat_id, photo=photo_id, caption=body,
                                 parse_mode=parse_mode, reply_markup=reply_markup)
        elif photo_id:
            await bot.send_photo(chat_id=chat_id, photo=photo_id)
            await bot.send_message(chat_id=chat_id, text=body,
                                   parse_mode=parse_mode, reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=body,
                                   parse_mode=parse_mode, reply_markup=reply_markup)

    try:
        await _send("HTML")
    except TelegramBadRequest as e:
        if "parse entities" in str(e).lower():
            log.warning("HTML parse error, отправляю без разметки: %s", e)
            await _send(None)
        else:
            raise


# ---------------------- Хендлеры ----------------------

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.set_state(PostState.waiting_for_input)
    await message.answer(
        "👋 Привет! Закидывай фактуру, ссылку или фото с описанием, и я упакую пост под стиль <b>Federal Insider</b>.",
        parse_mode="HTML",
    )


@dp.message(StateFilter(None, PostState.waiting_for_input), ~F.text.startswith("/"))
async def process_input(message: types.Message, state: FSMContext):
    if message.media_group_id:
        if message.media_group_id in processed_media_groups:
            return
        processed_media_groups.add(message.media_group_id)
        if len(processed_media_groups) > 500:
            processed_media_groups.clear()

    user_text = (message.caption or message.text or "").strip()
    photo_id = message.photo[-1].file_id if message.photo else None

    if not user_text:
        await message.answer("Пришли текст или фото вместе с описанием (фактурой) в подписи!")
        return

    msg = await message.answer("⏳ Gemini упаковывает пост в стиль Federal Insider...")

    try:
        generated_post = await generate_gemini_text(user_text)

        await state.update_data(post_text=generated_post, photo_id=photo_id, original_input=user_text)
        await state.set_state(PostState.preview)

        await msg.delete()
        await send_post(message.chat.id, generated_post, photo_id, get_preview_keyboard(bool(photo_id)))

    except Exception as e:
        log.exception("Ошибка генерации")
        await message.answer(f"❌ Ошибка генерации: {e}")


@dp.callback_query(PostState.preview, F.data == "add_photo")
async def ask_for_photo(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.waiting_for_photo)
    await callback.message.answer("📸 Пришли изображение, которое нужно прикрепить к этому посту:")
    await callback.answer()


@dp.message(PostState.waiting_for_photo, F.photo)
async def receive_photo(message: types.Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    data = await state.get_data()
    post_text = data.get("post_text", "")

    await state.update_data(photo_id=photo_id)
    await state.set_state(PostState.preview)

    await send_post(
        message.chat.id, post_text, photo_id, get_preview_keyboard(True),
        prefix="✅ Фото прикреплено! Вот итоговый превью:\n\n",
    )


@dp.message(PostState.waiting_for_photo, ~F.text.startswith("/"))
async def waiting_photo_wrong_type(message: types.Message):
    await message.answer("Нужно именно изображение. Пришли фото или нажми /start для сброса.")


@dp.callback_query(PostState.preview, F.data == "publish_now")
async def publish_now(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    post_text = data.get("post_text")
    photo_id = data.get("photo_id")

    if not post_text:
        await callback.answer("Нет текста для публикации", show_alert=True)
        return

    # Сразу меняем состояние, чтобы двойной клик не опубликовал пост дважды
    await state.set_state(PostState.waiting_for_input)

    try:
        await send_post(CHANNEL_ID, post_text, photo_id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("✅ <b>Пост успешно опубликован в Federal Insider!</b>", parse_mode="HTML")
    except Exception as e:
        log.exception("Ошибка публикации")
        await state.set_state(PostState.preview)
        await callback.message.answer(f"❌ Ошибка публикации: {e}")
    await callback.answer()


@dp.callback_query(PostState.preview, F.data == "regenerate")
async def regenerate(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    original_input = data.get("original_input", "")
    photo_id = data.get("photo_id")

    msg = await callback.message.answer("🔄 Перегенерирую пост...")

    try:
        generated_post = await generate_gemini_text(original_input)
        await state.update_data(post_text=generated_post)
        await state.set_state(PostState.preview)

        await msg.delete()
        await send_post(callback.message.chat.id, generated_post, photo_id, get_preview_keyboard(bool(photo_id)))
    except Exception as e:
        log.exception("Ошибка перегенерации")
        await callback.message.answer(f"❌ Ошибка генерации: {e}")


@dp.callback_query(F.data == "cancel")
async def cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(PostState.waiting_for_input)
    await callback.message.answer("Отменено. Жду новую фактуру.")
    await callback.answer()


@dp.callback_query(F.data.in_({"publish_now", "regenerate", "add_photo"}))
async def stale_callback(callback: types.CallbackQuery):
    """Кнопка от старого превью (например, после перезапуска бота)."""
    await callback.answer("Сессия устарела. Пришли материал заново.", show_alert=True)


# ---------------------- Веб-сервер для хостинга ----------------------

async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def main():
    if not all([TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, CHANNEL_ID]):
        raise RuntimeError("Задай TELEGRAM_BOT_TOKEN, GEMINI_API_KEY и CHANNEL_ID в переменных окружения")

    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    log.info("Старт бота, модель Gemini: %s", GEMINI_MODEL)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if _http_session and not _http_session.closed:
            await _http_session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
