import os
import re
import json
import html
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

# ======================= НАСТРОЙКИ (переменные окружения) =======================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/federal_inside")
# low / minimal ускоряют ответ. Пустое значение или "off" отключает параметр.
THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low").strip().lower()
if THINKING_LEVEL in ("", "off", "none"):
    THINKING_LEVEL = None
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "60"))
GEMINI_RETRIES = int(os.getenv("GEMINI_RETRIES", "2"))


def parse_admin_ids(raw: str) -> set[int]:
    ids = set()
    for part in re.split(r"[,\s;]+", raw or ""):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


# Через запятую или пробел: "123456789, 987654321". Пусто = бот закрыт для всех.
ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))

# Премиум-эмодзи канала: {"обычный эмодзи": "custom_emoji_id"}.
# Свои ID можно добавить через переменную PREMIUM_EMOJI_JSON, например:
# {"📍":"5368...","💰":"5369...","⚖️":"5370..."}
PREMIUM_EMOJI: dict[str, str] = {"📌": "5368324170671202286"}
try:
    PREMIUM_EMOJI.update({k: str(v) for k, v in json.loads(os.getenv("PREMIUM_EMOJI_JSON", "{}")).items()})
except Exception as e:
    log.warning("PREMIUM_EMOJI_JSON не разобран: %s", e)

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
MEDIA_GROUP_WAIT = 1.5
MAX_INPUT_CHARS = 12000

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

_http_session: aiohttp.ClientSession | None = None
media_buffers: dict[tuple, dict] = {}


class PostState(StatesGroup):
    waiting_for_input = State()
    waiting_for_photo = State()
    waiting_for_emoji = State()
    preview = State()


# ======================= SYSTEM PROMPT =======================

def emoji_tag(emoji: str) -> str:
    """Тег премиум-эмодзи, если ID известен, иначе обычный эмодзи."""
    eid = PREMIUM_EMOJI.get(emoji)
    return f'<tg-emoji emoji-id="{eid}">{emoji}</tg-emoji>' if eid else emoji


def build_system_prompt() -> str:
    emoji_list = "\n".join(
        f'  * {e} → <tg-emoji emoji-id="{i}">{e}</tg-emoji>' for e, i in PREMIUM_EMOJI.items()
    )
    return f"""
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

АРХИТЕКТУРА ПОСТА (выдавать строго в HTML-разметке Telegram; допустимы только теги <b>, <i>, <u>, <a href="...">, <code>, <blockquote>, <tg-emoji>):
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
- Подпись внизу: <a href="{CHANNEL_LINK}">Federal Insider | Подписаться</a>

ПРЕМИУМ-ЭМОДЗИ TELEGRAM:
- В заголовке и важных блоках (локация, ущерб, правовая квалификация) используй кастомные премиум-эмодзи через тег <tg-emoji emoji-id="ID">эмодзи</tg-emoji>.
- Внутри тега ВСЕГДА стоит обычный эмодзи-заменитель.
- Используй ТОЛЬКО эмодзи и ID из этого списка, другие ID выдумывать запрещено:
{emoji_list}
- Эмодзи, которых нет в списке, пиши обычными символами без тега.
- Примеры оформления:
  Заголовок: {emoji_tag("📌")} <b>НАБУ: ДЕПУТАТ «НАРЕЗАЛ КУШ» НА 5 МЛН $</b>
  Локация: {emoji_tag("📍")} Киев | {emoji_tag("💰")} <b>Ущерб: 5 000 000 $</b>
  Правовая рамка: {emoji_tag("⚖️")} <b>Официальная квалификация:</b>
- Не больше одного премиум-эмодзи на строку и не больше 6 на весь пост.

Весь пост должен уместиться примерно в 3500 символов.
Выдавай ТОЛЬКО готовый текст поста без вступлений, приветствий, пояснений и без markdown-обёрток (никаких ``` и **).
"""


SYSTEM_PROMPT = build_system_prompt()


# ======================= ДОСТУП ТОЛЬКО ДЛЯ АДМИНОВ =======================

async def admin_only(handler, event, data):
    user = getattr(event, "from_user", None)
    # /id доступна всем, чтобы можно было узнать свой Telegram ID
    if isinstance(event, types.Message) and (event.text or "").startswith("/id"):
        return await handler(event, data)
    if user is None or user.id not in ADMIN_IDS:
        log.info("Отказано в доступе: %s", getattr(user, "id", None))
        if isinstance(event, types.CallbackQuery):
            await event.answer("⛔ Нет доступа", show_alert=True)
        elif isinstance(event, types.Message):
            await event.answer("⛔ Нет доступа. Свой ID можно узнать командой /id")
        return
    return await handler(event, data)


dp.message.outer_middleware(admin_only)
dp.callback_query.outer_middleware(admin_only)


# ======================= ОБРАБОТКА HTML И ЭМОДЗИ =======================

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a",
                "code", "pre", "tg-spoiler", "tg-emoji", "blockquote", "span"}
TAG_RE = re.compile(r"(<[^<>]*>)")
TAG_NAME_RE = re.compile(r"^<\s*/?\s*([a-zA-Z][a-zA-Z0-9-]*)")
TG_EMOJI_RE = re.compile(r'<tg-emoji\s+emoji-id="(\d+)"\s*>(.*?)</tg-emoji>', re.S)
BARE_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")


def clean_model_output(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    return text.strip()


def sanitize_html(text: str) -> str:
    """Оставляет только теги, которые принимает Telegram, и экранирует лишние < > &."""
    out = []
    for token in TAG_RE.split(text):
        if not token:
            continue
        if token.startswith("<") and token.endswith(">"):
            m = TAG_NAME_RE.match(token)
            if m and m.group(1).lower() in ALLOWED_TAGS:
                out.append(token)
            # неизвестные теги отбрасываем
        else:
            token = BARE_AMP_RE.sub("&amp;", token).replace("<", "&lt;").replace(">", "&gt;")
            out.append(token)
    return "".join(out)


def fix_premium_emoji(text: str) -> str:
    """Убирает выдуманные ID и оборачивает известные эмодзи в <tg-emoji>."""
    known_ids = set(PREMIUM_EMOJI.values())
    text = TG_EMOJI_RE.sub(lambda m: m.group(0) if m.group(1) in known_ids else m.group(2), text)

    parts = re.split(r"(<tg-emoji.*?</tg-emoji>)", text, flags=re.S)
    for idx, part in enumerate(parts):
        if part.startswith("<tg-emoji"):
            continue
        # не трогаем содержимое тегов (атрибуты)
        sub = TAG_RE.split(part)
        for j, seg in enumerate(sub):
            if seg.startswith("<") and seg.endswith(">"):
                continue
            for emoji, eid in PREMIUM_EMOJI.items():
                base = emoji.replace("\ufe0f", "")
                seg = re.sub(re.escape(base) + "\ufe0f?",
                             f'<tg-emoji emoji-id="{eid}">{emoji}</tg-emoji>', seg)
            sub[j] = seg
        parts[idx] = "".join(sub)
    return "".join(parts)


def finalize_post(text: str) -> str:
    return fix_premium_emoji(sanitize_html(clean_model_output(text)))


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def drop_custom_emoji(text: str) -> str:
    return TG_EMOJI_RE.sub(r"\2", text)


def cut_to_limit(text: str, limit: int = MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 50]
    pos = cut.rfind("\n\n")
    return cut[:pos] if pos > limit // 2 else cut


# ======================= GEMINI (Interactions API) =======================

async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def generate_gemini_text(prompt_text: str) -> str:
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
        "Api-Revision": "2026-05-20",
    }
    payload = {
        "model": GEMINI_MODEL,
        "input": f"{SYSTEM_PROMPT}\n\nИсходные данные:\n{prompt_text[:MAX_INPUT_CHARS]}",
    }
    if THINKING_LEVEL:
        payload["generation_config"] = {"thinking_level": THINKING_LEVEL}

    session = await get_session()
    last_error = None
    attempt = 0
    while attempt < GEMINI_RETRIES:
        attempt += 1
        try:
            async with session.post(url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=GEMINI_TIMEOUT)) as resp:
                data = await resp.json(content_type=None)
                code = resp.status
        except asyncio.TimeoutError:
            last_error = f"Gemini не ответил за {GEMINI_TIMEOUT} сек."
            log.warning(last_error)
            continue
        except aiohttp.ClientError as e:
            last_error = f"Сетевая ошибка: {e}"
            log.warning(last_error)
            await asyncio.sleep(2)
            continue

        if code in (429, 500, 503):
            last_error = f"API Error {code}: {data}"
            log.warning(last_error)
            await asyncio.sleep(3 * attempt)
            continue

        if code == 400 and "generation_config" in payload and \
                any(w in str(data).lower() for w in ("generation_config", "thinking")):
            log.warning("API не принял generation_config, повторяю без него: %s", data)
            payload.pop("generation_config", None)
            attempt -= 1  # такой повтор не считается попыткой
            continue

        if code != 200:
            raise Exception(f"API Error {code}: {data}")

        status = data.get("status")
        if status and status != "completed":
            raise Exception(f"Генерация не завершена (status: {status}): "
                            f"{data.get('error') or data.get('errors')}")

        texts = []
        for step in data.get("steps") or []:
            if step.get("type") == "model_output":
                for block in step.get("content") or []:
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
        text = "".join(texts).strip()
        if not text:
            raise Exception("Пустой ответ от Gemini. Нажми «Перегенерировать».")
        return finalize_post(text)

    raise Exception(last_error or "Не удалось получить ответ от Gemini")


# ======================= ОТПРАВКА ПОСТА =======================

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


async def send_post(chat_id, text: str, photo_id: str | None = None,
                    reply_markup=None, prefix: str = ""):
    """
    Отправка с учётом лимитов Telegram и запасными вариантами разметки:
    1) полный HTML с премиум-эмодзи; 2) HTML без премиум-эмодзи; 3) чистый текст.
    Подпись к фото ≤ 1024 символов, длиннее — фото и текст двумя сообщениями.
    """
    full = prefix + text
    visible_len = len(html.unescape(strip_tags(full)))
    long_mode = bool(photo_id) and visible_len > CAPTION_LIMIT

    if long_mode:
        await bot.send_photo(chat_id=chat_id, photo=photo_id)

    variants = [(full, "HTML")]
    no_emoji = drop_custom_emoji(full)
    if no_emoji != full:
        variants.append((no_emoji, "HTML"))
    variants.append((html.unescape(strip_tags(full)), None))

    last_error = None
    for body, mode in variants:
        body = cut_to_limit(body)
        try:
            if photo_id and not long_mode:
                await bot.send_photo(chat_id=chat_id, photo=photo_id, caption=body,
                                     parse_mode=mode, reply_markup=reply_markup)
            else:
                await bot.send_message(chat_id=chat_id, text=body,
                                       parse_mode=mode, reply_markup=reply_markup)
            return
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "parse entities" in msg or "emoji" in msg or "entity" in msg:
                log.warning("Ошибка разметки, пробую упрощённый вариант: %s", e)
                last_error = e
                continue
            raise
    raise last_error


# ======================= СБОР ФАКТУРЫ (в т.ч. пересылки) =======================

def forward_source(message: types.Message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    t = getattr(origin, "type", None)
    if t == "channel":
        return getattr(origin.chat, "title", None) or "канал"
    if t == "chat":
        return getattr(origin.sender_chat, "title", None) or "чат"
    if t == "user":
        u = origin.sender_user
        return " ".join(x for x in (u.first_name, u.last_name) if x) or "пользователь"
    if t == "hidden_user":
        return origin.sender_user_name
    return "неизвестный источник"


def extract_text(message: types.Message) -> str:
    """Текст или подпись + скрытые ссылки (text_link), которых нет в самом тексте."""
    text = (message.text or message.caption or "").strip()
    entities = message.entities or message.caption_entities or []
    links = []
    for ent in entities:
        if ent.type == "text_link" and ent.url and ent.url not in text and ent.url not in links:
            links.append(ent.url)
    if links:
        text += "\n\nСсылки из поста: " + ", ".join(links)
    return text.strip()


async def handle_materials(first: types.Message, state: FSMContext, items: list[types.Message]):
    texts, photo_id = [], None
    for m in items:
        t = extract_text(m)
        if t and t not in texts:
            texts.append(t)
        if photo_id is None and m.photo:
            photo_id = m.photo[-1].file_id

    fact = "\n\n".join(texts).strip()
    source = forward_source(first)

    if not fact:
        await first.answer("В сообщении нет текста. Пришли текст или фото с описанием (фактурой) в подписи.")
        return
    if source:
        fact = f"Источник пересылки: {source}\n\n{fact}"

    msg = await first.answer("⏳ Gemini упаковывает пост в стиль Federal Insider...")
    try:
        post = await generate_gemini_text(fact)
        await state.update_data(post_text=post, photo_id=photo_id, original_input=fact)
        await state.set_state(PostState.preview)
        try:
            await msg.delete()
        except Exception:
            pass
        await send_post(first.chat.id, post, photo_id, get_preview_keyboard(bool(photo_id)))
    except Exception as e:
        log.exception("Ошибка генерации")
        await first.answer(f"❌ Ошибка генерации: {e}")


# ======================= ХЕНДЛЕРЫ =======================

@dp.message(Command("id"))
async def id_cmd(message: types.Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(PostState.waiting_for_input)
    await message.answer(
        "👋 Привет! Закидывай фактуру: текст, ссылку, фото с описанием или просто "
        "<b>перешли пост</b> из любого канала, и я упакую его под стиль <b>Federal Insider</b>.\n\n"
        "/emojiid — узнать ID премиум-эмодзи",
        parse_mode="HTML",
    )


@dp.message(Command("emojiid"))
async def emojiid_cmd(message: types.Message, state: FSMContext):
    await state.set_state(PostState.waiting_for_emoji)
    await message.answer("Отправь сообщение с премиум-эмодзи, и я покажу их ID.")


@dp.message(PostState.waiting_for_emoji)
async def emojiid_receive(message: types.Message, state: FSMContext):
    entities = (message.entities or []) + (message.caption_entities or [])
    ids = [e.custom_emoji_id for e in entities if e.type == "custom_emoji"]
    text = message.text or message.caption or ""
    lines = []
    for e in entities:
        if e.type == "custom_emoji":
            # entity.offset/length считаются в UTF-16, поэтому берём срез через него
            raw = text.encode("utf-16-le")
            sym = raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le")
            lines.append(f"{sym} → <code>{e.custom_emoji_id}</code>")
    await state.set_state(PostState.waiting_for_input)
    if ids:
        await message.answer("\n".join(lines) + "\n\nДобавь их в переменную PREMIUM_EMOJI_JSON.",
                             parse_mode="HTML")
    else:
        await message.answer("Премиум-эмодзи в сообщении не найдено.")


@dp.message(PostState.waiting_for_photo, F.photo)
async def receive_photo(message: types.Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    data = await state.get_data()
    await state.update_data(photo_id=photo_id)
    await state.set_state(PostState.preview)
    await send_post(message.chat.id, data.get("post_text", ""), photo_id,
                    get_preview_keyboard(True),
                    prefix="✅ Фото прикреплено! Вот итоговый превью:\n\n")


@dp.message(PostState.waiting_for_photo)
async def waiting_photo_wrong_type(message: types.Message):
    if (message.text or "").startswith("/"):
        return
    await message.answer("Нужно именно изображение. Пришли фото или нажми /start для сброса.")


@dp.message(StateFilter(None, PostState.waiting_for_input, PostState.preview))
async def process_input(message: types.Message, state: FSMContext):
    if (message.text or "").startswith("/"):
        return

    gid = message.media_group_id
    if gid:
        key = (message.chat.id, gid)
        buf = media_buffers.get(key)
        if buf is not None:
            buf["items"].append(message)
            return
        media_buffers[key] = {"items": [message]}
        await asyncio.sleep(MEDIA_GROUP_WAIT)  # ждём остальные части альбома
        items = media_buffers.pop(key)["items"]
        await handle_materials(message, state, items)
    else:
        await handle_materials(message, state, [message])


@dp.callback_query(PostState.preview, F.data == "add_photo")
async def ask_for_photo(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.waiting_for_photo)
    await callback.message.answer("📸 Пришли изображение, которое нужно прикрепить к этому посту:")
    await callback.answer()


@dp.callback_query(PostState.preview, F.data == "publish_now")
async def publish_now(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    post_text, photo_id = data.get("post_text"), data.get("photo_id")
    if not post_text:
        await callback.answer("Нет текста для публикации", show_alert=True)
        return

    await state.set_state(PostState.waiting_for_input)  # защита от двойного клика
    try:
        await send_post(CHANNEL_ID, post_text, photo_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("✅ <b>Пост успешно опубликован в Federal Insider!</b>",
                                      parse_mode="HTML")
    except Exception as e:
        log.exception("Ошибка публикации")
        await state.set_state(PostState.preview)
        await callback.message.answer(f"❌ Ошибка публикации: {e}")
    await callback.answer()


@dp.callback_query(PostState.preview, F.data == "regenerate")
async def regenerate(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    original, photo_id = data.get("original_input", ""), data.get("photo_id")
    msg = await callback.message.answer("🔄 Перегенерирую пост...")
    try:
        post = await generate_gemini_text(original)
        await state.update_data(post_text=post)
        await state.set_state(PostState.preview)
        try:
            await msg.delete()
        except Exception:
            pass
        await send_post(callback.message.chat.id, post, photo_id, get_preview_keyboard(bool(photo_id)))
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
    await callback.answer("Сессия устарела. Пришли материал заново.", show_alert=True)


# ======================= ВЕБ-СЕРВЕР ДЛЯ RENDER =======================

async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def keep_alive():
    """Render Free засыпает без входящего трафика, поэтому пингуем свой же URL."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        await asyncio.sleep(600)
        try:
            session = await get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)):
                pass
        except Exception as e:
            log.warning("keep_alive: %s", e)


async def main():
    if not all([TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, CHANNEL_ID]):
        raise RuntimeError("Задай TELEGRAM_BOT_TOKEN, GEMINI_API_KEY и CHANNEL_ID")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст: бот закрыт для всех. Свой ID можно узнать командой /id")

    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start()

    keep_task = asyncio.create_task(keep_alive())
    log.info("Старт бота. Модель: %s, thinking: %s, админов: %d",
             GEMINI_MODEL, THINKING_LEVEL, len(ADMIN_IDS))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        keep_task.cancel()
        if _http_session and not _http_session.closed:
            await _http_session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
