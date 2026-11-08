import os
import re
import json
import html
import time
import asyncio
import logging
import aiohttp
from collections import Counter
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

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()
# Одна модель или цепочка через запятую: если первая перегружена (503), бот пробует следующую.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash,gemini-3.5-flash,gemini-3.1-flash-lite").strip()
GEMINI_MODELS = [m.strip() for m in GEMINI_MODEL.split(",") if m.strip()]
LAST_GOOD_MODEL: str | None = None
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/federal_inside").strip()

# Способ обращения к Gemini: auto (сначала interactions, потом запасной generate),
# interactions или generate.
GEMINI_API_MODE = os.getenv("GEMINI_API_MODE", "auto").strip().lower()
# low / minimal ускоряют ответ в режиме interactions. Пусто или "off" отключает параметр.
THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low").strip().lower()
if THINKING_LEVEL in ("", "off", "none"):
    THINKING_LEVEL = None
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "50"))
GEMINI_RETRIES = int(os.getenv("GEMINI_RETRIES", "1"))  # попыток на одну модель/режим


def parse_admin_ids(raw: str) -> set[int]:
    ids = set()
    for part in re.split(r"[,\s;]+", (raw or "").replace('"', "").replace("'", "")):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


# Через запятую или пробел: "123456789, 987654321". Пусто = бот закрыт для всех.
ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))

# Наборы премиум-эмодзи (по приоритету: при совпадении эмодзи берётся из первого набора).
PREMIUM_PACKS = [p.strip() for p in os.getenv(
    "PREMIUM_PACKS",
    "federal_insider_by_emojicreationbot,AdaptiveStatus,tgmacicons",
).split(",") if p.strip()]
MAX_PREMIUM_PER_POST = int(os.getenv("MAX_PREMIUM_PER_POST", "14"))
MAX_PER_EMOJI = int(os.getenv("MAX_PER_EMOJI", "3"))


def norm_emoji(e: str) -> str:
    return e.replace("\ufe0f", "")


# Ручные ID (необязательно), например: {"📍":"5368...","💰":"5369..."}
MANUAL_PREMIUM: dict[str, str] = {}
try:
    MANUAL_PREMIUM = {norm_emoji(k): str(v)
                      for k, v in json.loads(os.getenv("PREMIUM_EMOJI_JSON", "{}") or "{}").items()}
except Exception as e:
    log.warning("PREMIUM_EMOJI_JSON не разобран: %s", e)

DEFAULT_PREMIUM = {"📌": "5368324170671202286"}
# Итоговый словарь «эмодзи без FE0F -> custom_emoji_id». Заполняется при старте из наборов.
PREMIUM_EMOJI: dict[str, str] = {**DEFAULT_PREMIUM, **MANUAL_PREMIUM}

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
MEDIA_GROUP_WAIT = 1.5
MAX_INPUT_CHARS = 12000

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения TELEGRAM_BOT_TOKEN")

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

def build_system_prompt() -> str:
    avail = " ".join(list(PREMIUM_EMOJI.keys())[:150])
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

АРХИТЕКТУРА ПОСТА (выдавать строго в HTML-разметке Telegram; допустимы только теги <b>, <i>, <u>, <a href="...">, <code>, <blockquote>):
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

ЭМОДЗИ (у канала есть фирменные премиум-иконки):
- Пиши эмодзи обычными символами, БЕЗ каких-либо тегов. Бот сам заменит их на премиум-версии.
- Эмодзи с премиум-версией: {avail}
- Выбирай эмодзи строго по смыслу строки и темы поста (деньги, оружие, обыск, суд, локация, документы, транспорт и т.д.). Предпочитай эмодзи из списка выше.
- Не больше одного эмодзи на строку, на весь пост не больше 8, один и тот же эмодзи не чаще двух раз.
- Примеры:
  Заголовок: 📌 <b>НАБУ: ДЕПУТАТ «НАРЕЗАЛ КУШ» НА 5 МЛН $</b>
  Локация: 📍 Киев | 💰 <b>Ущерб: 5 000 000 $</b>
  Правовая рамка: ⚖️ <b>Официальная квалификация:</b>

Весь пост должен уместиться примерно в 3500 символов.
Выдавай ТОЛЬКО готовый текст поста без вступлений, приветствий, пояснений и без markdown-обёрток (никаких ``` и **).
"""


SYSTEM_PROMPT = build_system_prompt()


# ======================= MIDDLEWARE: ЛОГИ И ДОСТУП =======================

async def log_updates(handler, event, data):
    user = getattr(event, "from_user", None)
    log.info("Апдейт %s от %s: %s", type(event).__name__, getattr(user, "id", None),
             (getattr(event, "text", None) or getattr(event, "data", None) or "")[:60])
    return await handler(event, data)


async def admin_only(handler, event, data):
    user = getattr(event, "from_user", None)
    # /id доступна всем, чтобы можно было узнать свой Telegram ID
    if isinstance(event, types.Message) and (event.text or "").split("@")[0].strip() == "/id":
        return await handler(event, data)
    if user is None or user.id not in ADMIN_IDS:
        log.info("Отказано в доступе: %s", getattr(user, "id", None))
        if isinstance(event, types.CallbackQuery):
            await event.answer("⛔ Нет доступа", show_alert=True)
        elif isinstance(event, types.Message):
            await event.answer("⛔ Нет доступа. Свой ID можно узнать командой /id")
        return
    return await handler(event, data)


# log_updates регистрируется первым, чтобы видеть и отклонённые сообщения
dp.message.outer_middleware(log_updates)
dp.callback_query.outer_middleware(log_updates)
dp.message.outer_middleware(admin_only)
dp.callback_query.outer_middleware(admin_only)


@dp.errors()
async def on_error(event: types.ErrorEvent):
    log.exception("Необработанная ошибка в хендлере: %s", event.exception)


# ======================= ОБРАБОТКА HTML И ЭМОДЗИ =======================

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a",
                "code", "pre", "tg-spoiler", "tg-emoji", "blockquote", "span"}
TAG_RE = re.compile(r"(<[^<>]*>)")
TAG_NAME_RE = re.compile(r"^<\s*/?\s*([a-zA-Z][a-zA-Z0-9-]*)")
TG_EMOJI_RE = re.compile(r'<tg-emoji\s+emoji-id="(\d+)"\s*>(.*?)</tg-emoji>', re.S)
BARE_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")

# Один эмодзи: флаг, кейкап, либо символ + вариации/оттенок/ZWJ-последовательности
EMOJI_RE = re.compile(
    r"(?:[\U0001F1E6-\U0001F1FF]{2}"
    r"|[0-9#*]\ufe0f?\u20e3"
    r"|(?:[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u23FF\u24C2\u25A0-\u25FF"
    r"\u2600-\u27BF\u2900-\u297F\u2B00-\u2BFF\u3030\u303D\u3297\u3299"
    r"\u00A9\u00AE\u203C\u2049\u2122\u2139]"
    r"[\ufe0f\U0001F3FB-\U0001F3FF]?"
    r"(?:\u200d[\U0001F000-\U0001FAFF\u2600-\u27BF\u2640\u2642\u2B00-\u2BFF]\ufe0f?)*))"
)


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
        else:
            token = BARE_AMP_RE.sub("&amp;", token).replace("<", "&lt;").replace(">", "&gt;")
            out.append(token)
    return "".join(out)


def apply_premium(text: str) -> str:
    """
    Заменяет обычные эмодзи в тексте на премиум (<tg-emoji>), если для них есть ID
    в наборах канала. Выдуманные моделью ID убираются. Есть лимиты на пост и на один эмодзи.
    """
    known = set(PREMIUM_EMOJI.values())
    text = TG_EMOJI_RE.sub(lambda m: m.group(0) if m.group(1) in known else m.group(2), text)

    used: Counter = Counter()
    total = len(TG_EMOJI_RE.findall(text))

    def repl(m):
        nonlocal total
        em = m.group(0)
        key = norm_emoji(em)
        eid = PREMIUM_EMOJI.get(key)
        if not eid or total >= MAX_PREMIUM_PER_POST or used[key] >= MAX_PER_EMOJI:
            return em
        total += 1
        used[key] += 1
        return f'<tg-emoji emoji-id="{eid}">{em}</tg-emoji>'

    parts = re.split(r"(<tg-emoji.*?</tg-emoji>)", text, flags=re.S)
    for idx, part in enumerate(parts):
        if part.startswith("<tg-emoji"):
            continue
        sub = TAG_RE.split(part)
        for j, seg in enumerate(sub):
            if seg.startswith("<") and seg.endswith(">"):
                continue
            sub[j] = EMOJI_RE.sub(repl, seg)
        parts[idx] = "".join(sub)
    return "".join(parts)


def finalize_post(text: str) -> str:
    return apply_premium(sanitize_html(clean_model_output(text)))


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


async def load_premium_packs() -> str:
    """Загружает наборы кастомных эмодзи и строит словарь «эмодзи -> ID». Возвращает отчёт."""
    global SYSTEM_PROMPT
    from_packs: dict[str, str] = {}
    report = []
    for name in PREMIUM_PACKS:
        try:
            ss = await asyncio.wait_for(bot.get_sticker_set(name), timeout=25)
        except Exception as e:
            report.append(f"❌ {name}: {e}")
            continue
        total = new = 0
        for st in ss.stickers:
            if not getattr(st, "custom_emoji_id", None):
                continue
            total += 1
            for em in EMOJI_RE.findall(st.emoji or ""):
                key = norm_emoji(em)
                if key not in from_packs:  # приоритет у более раннего набора
                    from_packs[key] = st.custom_emoji_id
                    new += 1
        report.append(f"✅ {name}: {total} эмодзи, уникальных значков: {new}")

    PREMIUM_EMOJI.clear()
    PREMIUM_EMOJI.update(DEFAULT_PREMIUM)
    PREMIUM_EMOJI.update(from_packs)
    PREMIUM_EMOJI.update(MANUAL_PREMIUM)
    SYSTEM_PROMPT = build_system_prompt()
    report.append(f"Всего значков для замены: {len(PREMIUM_EMOJI)}")
    for line in report:
        log.info(line)
    return "\n".join(report)


# ======================= GEMINI =======================

class GeminiError(Exception):
    def __init__(self, msg: str, retry: bool = False):
        super().__init__(msg)
        self.retry = retry


async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def _post_json(url: str, headers: dict, payload: dict, timeout: int):
    session = await get_session()
    async with session.post(url, json=payload, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        raw = await resp.text()
        code = resp.status
    try:
        data = json.loads(raw)
    except Exception:
        data = {"raw": raw[:500]}
    return code, data


async def _call_interactions(prompt: str, use_system: bool, timeout: int, model: str) -> str:
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json",
               "Api-Revision": "2026-05-20"}
    payload = {"model": model,
               "input": (f"{SYSTEM_PROMPT}\n\n" if use_system else "") + prompt}
    if THINKING_LEVEL:
        payload["generation_config"] = {"thinking_level": THINKING_LEVEL}

    for _ in range(2):
        code, data = await _post_json(url, headers, payload, timeout)
        if code == 400 and "generation_config" in payload and \
                any(w in str(data).lower() for w in ("generation_config", "thinking")):
            log.warning("interactions: параметр thinking не принят, повторяю без него: %s", data)
            payload.pop("generation_config", None)
            continue
        break

    if code != 200:
        raise GeminiError(f"interactions HTTP {code}: {str(data)[:300]}",
                          retry=code in (429, 500, 503))
    status = data.get("status")
    if status and status != "completed":
        raise GeminiError(f"interactions status={status}: {data.get('error') or data.get('errors')}")
    texts = []
    for step in data.get("steps") or []:
        if step.get("type") == "model_output":
            for block in step.get("content") or []:
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
    text = "".join(texts).strip()
    if not text:
        raise GeminiError("interactions: пустой ответ")
    return text


async def _call_generate(prompt: str, use_system: bool, timeout: int, model: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if use_system:
        payload["systemInstruction"] = {"parts": [{"text": SYSTEM_PROMPT}]}

    code, data = await _post_json(url, headers, payload, timeout)
    if code != 200:
        raise GeminiError(f"generate HTTP {code}: {str(data)[:300]}", retry=code in (429, 500, 503))
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "неизвестно")
        raise GeminiError(f"generate: нет ответа (блокировка: {reason})")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise GeminiError(f"generate: пустой ответ (finishReason: {candidates[0].get('finishReason')})")
    return text


def _model_order() -> list[str]:
    """Сначала последняя успешная модель, затем остальные по порядку из GEMINI_MODEL."""
    if LAST_GOOD_MODEL in GEMINI_MODELS:
        return [LAST_GOOD_MODEL] + [m for m in GEMINI_MODELS if m != LAST_GOOD_MODEL]
    return list(GEMINI_MODELS)


async def generate_gemini_text(prompt_text: str) -> str:
    global LAST_GOOD_MODEL
    if not GEMINI_API_KEY:
        raise Exception("Не задана переменная окружения GEMINI_API_KEY")

    prompt = f"Исходные данные:\n{prompt_text[:MAX_INPUT_CHARS]}"
    if GEMINI_API_MODE == "interactions":
        modes = [("interactions", _call_interactions)]
    elif GEMINI_API_MODE == "generate":
        modes = [("generate", _call_generate)]
    else:
        modes = [("interactions", _call_interactions), ("generate", _call_generate)]

    errors = []
    for model in _model_order():
        skip_model = False  # модель перегружена или молчит: идём к следующей
        for mode_name, fn in modes:
            if skip_model:
                break
            for attempt in range(GEMINI_RETRIES):
                t0 = time.monotonic()
                try:
                    text = await fn(prompt, True, GEMINI_TIMEOUT, model)
                    LAST_GOOD_MODEL = model
                    log.info("Gemini %s/%s ответил за %.1f сек.", model, mode_name, time.monotonic() - t0)
                    return finalize_post(text)
                except asyncio.TimeoutError:
                    msg = f"{model}/{mode_name}: нет ответа за {GEMINI_TIMEOUT} сек."
                    log.warning(msg)
                    errors.append(msg)
                    skip_model = True
                    break
                except aiohttp.ClientError as e:
                    msg = f"{model}/{mode_name}: сетевая ошибка {e}"
                    log.warning(msg)
                    errors.append(msg)
                    skip_model = True
                    break
                except GeminiError as e:
                    log.warning("Gemini %s: %s", model, e)
                    if e.retry and attempt + 1 < GEMINI_RETRIES:
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    errors.append(f"{model}/{mode_name}: {str(e)[:160]}")
                    if e.retry:
                        skip_model = True
                    break
    raise Exception((" | ".join(errors[-4:]))[:700] or "Не удалось получить ответ от Gemini")


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
    Подпись к фото <= 1024 символов, длиннее: фото и текст двумя сообщениями.
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
        "/testgemini — проверить связь с Gemini\n"
        "/packs — перезагрузить наборы премиум-эмодзи\n"
        "/emojiid — узнать ID премиум-эмодзи",
        parse_mode="HTML",
    )


@dp.message(Command("packs"))
async def packs_cmd(message: types.Message):
    msg = await message.answer("🔄 Загружаю наборы эмодзи...")
    report = await load_premium_packs()
    await msg.edit_text(html.escape(report))


@dp.message(Command("testgemini"))
async def testgemini_cmd(message: types.Message):
    # /testgemini  или  /testgemini gemini-3.5-flash,gemini-3.1-flash-lite
    parts = (message.text or "").split(maxsplit=1)
    models = [m.strip() for m in parts[1].split(",") if m.strip()] if len(parts) > 1 else list(GEMINI_MODELS)
    lines = [f"Цепочка моделей: <code>{html.escape(', '.join(GEMINI_MODELS))}</code>",
             f"Режим: <code>{GEMINI_API_MODE}</code>, таймаут: {GEMINI_TIMEOUT} сек."]
    msg = await message.answer("🔬 Проверяю связь с Gemini...")

    async def show(tail: str = ""):
        try:
            await msg.edit_text("\n".join(lines) + (f"\n\n{tail}" if tail else ""), parse_mode="HTML")
        except Exception:
            pass

    # 1) Доступность API и список моделей
    names: list[str] = []
    await show("⏳ Список моделей...")
    t0 = time.monotonic()
    try:
        session = await get_session()
        async with session.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                               headers={"x-goog-api-key": GEMINI_API_KEY},
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            raw, code = await resp.text(), resp.status
        if code == 200:
            names = [m.get("name", "").replace("models/", "") for m in json.loads(raw).get("models", [])]
            flash = [n for n in names if "flash" in n and not any(x in n for x in ("image", "tts"))]
            lines.append(f"✅ API доступен ({time.monotonic() - t0:.1f} c).")
            lines.append("Flash-модели: " + html.escape(", ".join(flash[:30])))
        else:
            lines.append(f"❌ Список моделей: HTTP {code}: {html.escape(raw[:200])}")
    except asyncio.TimeoutError:
        lines.append("⏱ Список моделей: нет ответа за 20 сек. (сеть до Google или ключ)")
    except Exception as e:
        lines.append(f"❌ Список моделей: {html.escape(str(e)[:200])}")

    # 2) Короткий запрос к каждой модели цепочки
    working = []
    for model in models:
        mark = " (нет в списке!)" if names and model not in names else ""
        for mode, fn in (("interactions", _call_interactions), ("generate", _call_generate)):
            await show(f"⏳ {model} / {mode}...")
            t0 = time.monotonic()
            try:
                await fn("Ответь одним словом: ок", False, 30, model)
                lines.append(f"✅ {model}{mark} / {mode}: {time.monotonic() - t0:.1f} c")
                working.append((model, mode, fn))
                break
            except asyncio.TimeoutError:
                lines.append(f"⏱ {model} / {mode}: нет ответа за 30 сек.")
                break
            except GeminiError as e:
                lines.append(f"❌ {model} / {mode}: {html.escape(str(e)[:160])}")
                if e.retry:  # перегрузка: второй способ на этой же модели не поможет
                    break
            except Exception as e:
                lines.append(f"❌ {model} / {mode}: {html.escape(str(e)[:160])}")

    # 3) Боевой промпт на первой рабочей модели
    if working:
        model, mode, fn = working[0]
        await show(f"⏳ Боевой промпт: {model} / {mode}...")
        t0 = time.monotonic()
        try:
            text = await fn("Исходные данные:\nНАБУ задержало чиновника в Киеве за взятку 50 тыс. долларов.",
                            True, GEMINI_TIMEOUT, model)
            lines.append(f"✅ Боевой промпт ({model}): {time.monotonic() - t0:.1f} c, {len(text)} симв.")
        except asyncio.TimeoutError:
            lines.append(f"⏱ Боевой промпт ({model}): нет ответа за {GEMINI_TIMEOUT} сек.")
        except Exception as e:
            lines.append(f"❌ Боевой промпт ({model}): {html.escape(str(e)[:200])}")
    else:
        lines.append("⚠️ Ни одна модель не ответила. Попробуй /testgemini gemini-flash-latest")
    await show("Готово.")


@dp.message(Command("emojiid"))
async def emojiid_cmd(message: types.Message, state: FSMContext):
    await state.set_state(PostState.waiting_for_emoji)
    await message.answer("Отправь сообщение с премиум-эмодзи, и я покажу их ID.")


@dp.message(PostState.waiting_for_emoji)
async def emojiid_receive(message: types.Message, state: FSMContext):
    entities = (message.entities or []) + (message.caption_entities or [])
    text = message.text or message.caption or ""
    raw = text.encode("utf-16-le")
    lines = []
    for e in entities:
        if e.type == "custom_emoji":
            # offset/length в Telegram считаются в UTF-16
            sym = raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le")
            lines.append(f"{sym} → <code>{e.custom_emoji_id}</code>")
    await state.set_state(PostState.waiting_for_input)
    if lines:
        await message.answer("\n".join(lines) + "\n\nМожно добавить в PREMIUM_EMOJI_JSON.",
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
    # Веб-сервер поднимаем первым, чтобы Render сразу видел открытый порт
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start()
    log.info("Веб-сервер запущен на порту %s", os.getenv("PORT", 10000))

    log.info("Настройки: модель=%s, режим=%s, thinking=%s, таймаут=%s, админов=%d, "
             "GEMINI_API_KEY=%s, CHANNEL_ID=%s",
             GEMINI_MODEL, GEMINI_API_MODE, THINKING_LEVEL, GEMINI_TIMEOUT, len(ADMIN_IDS),
             "есть" if GEMINI_API_KEY else "НЕ ЗАДАН", CHANNEL_ID or "НЕ ЗАДАН")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст: бот закрыт для всех. Свой ID можно узнать командой /id")

    try:
        me = await bot.get_me()
        log.info("Бот авторизован: @%s (id %s)", me.username, me.id)
    except Exception as e:
        log.error("Не удалось авторизоваться в Telegram, проверь TELEGRAM_BOT_TOKEN: %s", e)
        await asyncio.Event().wait()  # не падаем, чтобы ошибка осталась в логах

    try:
        await load_premium_packs()
    except Exception as e:
        log.warning("Наборы эмодзи не загружены: %s", e)

    keep_task = asyncio.create_task(keep_alive())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Polling запущен, жду сообщения")
        await dp.start_polling(bot)
    finally:
        keep_task.cancel()
        if _http_session and not _http_session.closed:
            await _http_session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
