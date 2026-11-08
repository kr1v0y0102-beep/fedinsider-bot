import os
import re
import json
import html
import time
import uuid
import asyncio
import logging
import aiohttp
from collections import Counter
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                           InputMediaPhoto, InputMediaVideo, LinkPreviewOptions)
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

GEMINI_API_MODE = os.getenv("GEMINI_API_MODE", "auto").strip().lower()
THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "low").strip().lower()
if THINKING_LEVEL in ("", "off", "none"):
    THINKING_LEVEL = None
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "50"))
GEMINI_RETRIES = int(os.getenv("GEMINI_RETRIES", "1"))

# Часовой пояс для отложенных постов и файл очереди.
TZ_NAME = os.getenv("TIMEZONE", "Europe/Kyiv").strip()
SCHEDULE_FILE = os.getenv("SCHEDULE_FILE", "scheduled_posts.json").strip()


def load_tz():
    try:
        from zoneinfo import ZoneInfo
        for name in (TZ_NAME, "Europe/Kiev"):
            try:
                return ZoneInfo(name)
            except Exception:
                continue
    except Exception:
        pass
    log.warning("Не удалось загрузить часовой пояс %s (нет tzdata?). Использую UTC+2 без перехода на летнее время.",
                TZ_NAME)
    return timezone(timedelta(hours=2))


TZ = load_tz()


def parse_admin_ids(raw: str) -> set[int]:
    ids = set()
    for part in re.split(r"[,\s;]+", (raw or "").replace('"', "").replace("'", "")):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))

PREMIUM_PACKS = [p.strip() for p in os.getenv(
    "PREMIUM_PACKS",
    "federal_insider_by_emojicreationbot,AdaptiveStatus,tgmacicons",
).split(",") if p.strip()]
MAX_PREMIUM_PER_POST = int(os.getenv("MAX_PREMIUM_PER_POST", "14"))
MAX_PER_EMOJI = int(os.getenv("MAX_PER_EMOJI", "3"))
# Эмодзи, которые НЕ нужно заменять на премиум (например "📍"), если иконка из набора не подходит.
PREMIUM_EXCLUDE_RAW = os.getenv("PREMIUM_EXCLUDE", "")


def norm_emoji(e: str) -> str:
    return e.replace("\ufe0f", "")


MANUAL_PREMIUM: dict[str, str] = {}
try:
    MANUAL_PREMIUM = {norm_emoji(k): str(v)
                      for k, v in json.loads(os.getenv("PREMIUM_EMOJI_JSON", "{}") or "{}").items()}
except Exception as e:
    log.warning("PREMIUM_EMOJI_JSON не разобран: %s", e)

DEFAULT_PREMIUM = {"📌": "5368324170671202286"}
PREMIUM_EMOJI: dict[str, str] = {**DEFAULT_PREMIUM, **MANUAL_PREMIUM}

CAPTION_LIMIT = 1024          # жёсткий лимит подписи к медиа у Telegram
CAPTION_TARGET = 850          # во столько символов просим уложиться Gemini, когда есть медиа
MESSAGE_LIMIT = 4096
MEDIA_GROUP_WAIT = 1.5
MAX_INPUT_CHARS = 12000
MAX_MEDIA = 10

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)  # убирает карточку канала под ссылкой

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения TELEGRAM_BOT_TOKEN")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

_http_session: aiohttp.ClientSession | None = None
media_buffers: dict[tuple, list] = {}


class PostState(StatesGroup):
    waiting_for_input = State()
    waiting_for_photo = State()
    waiting_for_emoji = State()
    waiting_for_time = State()
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

MEDIA_NOTE = f"""

ВАЖНО (приоритет выше лимита 3500 из инструкции): к посту прикреплены фото/видео, а у Telegram жёсткий лимит подписи к медиа. 
ВЕСЬ пост целиком (заголовок, локация, текст, квалификация, панчлайн, подпись) должен быть НЕ ДЛИННЕЕ {CAPTION_TARGET} видимых символов. 
Сожми: завязка 1–2 предложения, схема 2–3 предложения, без длинных цитат и пошаговых описаний. Структуру и фирменный стиль сохрани."""


# ======================= MIDDLEWARE: ЛОГИ И ДОСТУП =======================

async def log_updates(handler, event, data):
    user = getattr(event, "from_user", None)
    log.info("Апдейт %s от %s: %s", type(event).__name__, getattr(user, "id", None),
             (getattr(event, "text", None) or getattr(event, "data", None) or "")[:60])
    return await handler(event, data)


async def admin_only(handler, event, data):
    user = getattr(event, "from_user", None)
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


def tg_len(s: str) -> int:
    """Длина в UTF-16 единицах, как считает Telegram (эмодзи = 2)."""
    return len(s.encode("utf-16-le")) // 2


def visible_len(text: str) -> int:
    return tg_len(html.unescape(strip_tags(text)))


def cut_to_limit(text: str, limit: int = MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 50]
    pos = cut.rfind("\n\n")
    return cut[:pos] if pos > limit // 2 else cut


async def load_premium_packs() -> str:
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
                if key not in from_packs:
                    from_packs[key] = st.custom_emoji_id
                    new += 1
        report.append(f"✅ {name}: {total} эмодзи, уникальных значков: {new}")

    PREMIUM_EMOJI.clear()
    PREMIUM_EMOJI.update(DEFAULT_PREMIUM)
    PREMIUM_EMOJI.update(from_packs)
    PREMIUM_EMOJI.update(MANUAL_PREMIUM)
    for em in EMOJI_RE.findall(PREMIUM_EXCLUDE_RAW):
        key = norm_emoji(em)
        if key not in MANUAL_PREMIUM:
            PREMIUM_EMOJI.pop(key, None)
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
    if LAST_GOOD_MODEL in GEMINI_MODELS:
        return [LAST_GOOD_MODEL] + [m for m in GEMINI_MODELS if m != LAST_GOOD_MODEL]
    return list(GEMINI_MODELS)


async def generate_gemini_text(prompt: str) -> str:
    """Принимает уже готовый пользовательский промпт (без системного)."""
    global LAST_GOOD_MODEL
    if not GEMINI_API_KEY:
        raise Exception("Не задана переменная окружения GEMINI_API_KEY")

    if GEMINI_API_MODE == "interactions":
        modes = [("interactions", _call_interactions)]
    elif GEMINI_API_MODE == "generate":
        modes = [("generate", _call_generate)]
    else:
        modes = [("interactions", _call_interactions), ("generate", _call_generate)]

    errors = []
    for model in _model_order():
        skip_model = False
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


async def generate_post(fact: str, has_media: bool) -> str:
    """Генерация поста. Если есть медиа, просим уложиться в лимит подписи (1024)."""
    prompt = f"Исходные данные:\n{fact[:MAX_INPUT_CHARS]}"
    if has_media:
        prompt += MEDIA_NOTE
    text = await generate_gemini_text(prompt)

    if has_media and visible_len(text) > CAPTION_LIMIT:
        n = visible_len(text)
        log.info("Пост %d симв. > %d, прошу Gemini сократить", n, CAPTION_LIMIT)
        prompt2 = (f"{prompt}\n\nТвой предыдущий вариант вышел слишком длинным ({n} симв.):\n{text}\n\n"
                   f"Перепиши КОРОЧЕ: не более {CAPTION_TARGET} видимых символов. Сохрани заголовок, "
                   f"локацию/сумму, суть схемы, квалификацию, панчлайн и подпись со ссылкой.")
        try:
            text2 = await generate_gemini_text(prompt2)
            if visible_len(text2) < n:
                text = text2
        except Exception as e:
            log.warning("Сокращение не удалось: %s", e)
    return text


# ======================= МЕДИА =======================

def extract_media(items: list[types.Message]) -> list[dict]:
    media = []
    for m in sorted(items, key=lambda x: x.message_id):
        if m.photo:
            media.append({"type": "photo", "id": m.photo[-1].file_id})
        elif m.video:
            media.append({"type": "video", "id": m.video.file_id})
        elif m.animation:
            media.append({"type": "animation", "id": m.animation.file_id})
    if len(media) > 1:  # гифки нельзя класть в альбом
        media = [x for x in media if x["type"] != "animation"]
    return media[:MAX_MEDIA]


def media_label(media: list[dict]) -> str:
    c = Counter(x["type"] for x in media)
    names = {"photo": "фото", "video": "видео", "animation": "гиф"}
    return ", ".join(f"{n} {names[t]}" for t, n in c.items()) or "без медиа"


async def collect_group(message: types.Message) -> list[types.Message] | None:
    """Для альбома возвращает все сообщения (только в первом вызове), для остальных частей None."""
    gid = message.media_group_id
    if not gid:
        return [message]
    key = (message.chat.id, gid)
    buf = media_buffers.get(key)
    if buf is not None:
        buf.append(message)
        return None
    media_buffers[key] = [message]
    await asyncio.sleep(MEDIA_GROUP_WAIT)
    return sorted(media_buffers.pop(key), key=lambda m: m.message_id)


# ======================= ОТПРАВКА ПОСТА =======================

def get_preview_keyboard(has_media: bool):
    rows = [
        [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data="publish_now")],
        [InlineKeyboardButton(text="⏰ Отложить", callback_data="schedule")],
        [InlineKeyboardButton(text="🖼️ Заменить медиа" if has_media else "🖼️ Добавить фото/видео",
                              callback_data="add_media")],
        [InlineKeyboardButton(text="🔄 Перегенерировать", callback_data="regenerate")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _input_media(media: list[dict], caption: str | None, mode: str | None):
    items = []
    for i, m in enumerate(media):
        kw = {"media": m["id"]}
        if i == 0 and caption:
            kw["caption"] = caption
            kw["parse_mode"] = mode
        items.append(InputMediaPhoto(**kw) if m["type"] == "photo" else InputMediaVideo(**kw))
    return items


async def _send_media(chat_id, media: list[dict], caption: str | None = None,
                      mode: str | None = None, reply_markup=None):
    """Отправляет медиа (одно или альбом). Подпись только к первому элементу."""
    if len(media) == 1:
        m = media[0]
        kw = dict(chat_id=chat_id, caption=caption, reply_markup=reply_markup)
        if caption:
            kw["parse_mode"] = mode
        if m["type"] == "photo":
            await bot.send_photo(photo=m["id"], **kw)
        elif m["type"] == "video":
            await bot.send_video(video=m["id"], **kw)
        else:
            await bot.send_animation(animation=m["id"], **kw)
        return
    await bot.send_media_group(chat_id=chat_id, media=_input_media(media, caption, mode))
    if reply_markup:  # к альбому кнопки не прикрепить, шлём отдельным сообщением
        await bot.send_message(chat_id=chat_id, text="👆 Превью поста. Что делаем?",
                               reply_markup=reply_markup)


async def send_post(chat_id, text: str, media: list[dict] | None = None, reply_markup=None):
    """
    Медиа (фото/видео/альбом) + подпись одним постом, если текст влезает в 1024.
    Если не влезает: медиа, затем текст отдельным сообщением.
    Запасные варианты разметки: HTML с премиум-эмодзи -> HTML без них -> чистый текст.
    Превью ссылок отключено (нет карточки канала).
    """
    media = media or []
    separate_text = bool(media) and visible_len(text) > CAPTION_LIMIT

    if separate_text:
        await _send_media(chat_id, media)

    variants = [(text, "HTML")]
    no_emoji = drop_custom_emoji(text)
    if no_emoji != text:
        variants.append((no_emoji, "HTML"))
    variants.append((html.unescape(strip_tags(text)), None))

    last_error = None
    for body, mode in variants:
        try:
            if media and not separate_text:
                await _send_media(chat_id, media, caption=body, mode=mode, reply_markup=reply_markup)
            else:
                await bot.send_message(chat_id=chat_id, text=cut_to_limit(body), parse_mode=mode,
                                       reply_markup=reply_markup, link_preview_options=NO_PREVIEW)
            return
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "parse entities" in msg or "emoji" in msg or "entity" in msg:
                log.warning("Ошибка разметки, пробую упрощённый вариант: %s", e)
                last_error = e
                continue
            raise
    raise last_error


async def show_preview(chat_id, post: str, media: list[dict]):
    if media and visible_len(post) > CAPTION_LIMIT:
        await bot.send_message(
            chat_id,
            f"⚠️ Текст ({visible_len(post)} симв.) длиннее лимита подписи Telegram ({CAPTION_LIMIT}). "
            f"Он уйдёт отдельным сообщением после медиа. Чтобы был один пост, нажми «🔄 Перегенерировать».")
    await send_post(chat_id, post, media, get_preview_keyboard(bool(media)))


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
    texts = []
    for m in sorted(items, key=lambda x: x.message_id):
        t = extract_text(m)
        if t and t not in texts:
            texts.append(t)
    media = extract_media(items)

    fact = "\n\n".join(texts).strip()
    source = forward_source(first)

    if not fact:
        await first.answer("В сообщении нет текста. Пришли текст или фото/видео с описанием (фактурой) в подписи.")
        return
    if source:
        fact = f"Источник пересылки: {source}\n\n{fact}"

    msg = await first.answer("⏳ Gemini упаковывает пост в стиль Federal Insider...")
    try:
        post = await generate_post(fact, bool(media))
        await state.update_data(post_text=post, media=media, original_input=fact)
        await state.set_state(PostState.preview)
        try:
            await msg.delete()
        except Exception:
            pass
        await show_preview(first.chat.id, post, media)
    except Exception as e:
        log.exception("Ошибка генерации")
        await first.answer(f"❌ Ошибка генерации: {e}")


# ======================= ОТЛОЖЕННЫЕ ПОСТЫ =======================

SCHEDULED: list[dict] = []
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def save_schedule():
    try:
        d = os.path.dirname(SCHEDULE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = SCHEDULE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(SCHEDULED, f, ensure_ascii=False)
        os.replace(tmp, SCHEDULE_FILE)
    except Exception as e:
        log.error("Не удалось сохранить очередь: %s", e)


def load_schedule():
    try:
        if os.path.exists(SCHEDULE_FILE):
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                SCHEDULED[:] = json.load(f)
        log.info("Отложенных постов в очереди: %d", len(SCHEDULED))
    except Exception as e:
        log.error("Не удалось прочитать очередь: %s", e)


def fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


def fmt_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d} д.")
    if h:
        parts.append(f"{h} ч.")
    if m or not parts:
        parts.append(f"{m} мин.")
    return " ".join(parts)


def add_scheduled(chat_id: int, text: str, media: list[dict], dt: datetime) -> dict:
    item = {"id": uuid.uuid4().hex[:8], "chat_id": chat_id, "text": text,
            "media": media or [], "at": dt.timestamp(), "tries": 0}
    SCHEDULED.append(item)
    save_schedule()
    return item


def parse_user_datetime(raw: str) -> datetime | None:
    """Форматы: 18:30 | 25.12 18:30 | 25.12.2026 18:30 | завтра 18:30 | сегодня 18:30 | послезавтра 18:30"""
    s = raw.strip().lower()
    m = re.fullmatch(
        r"(?:(сегодня|завтра|послезавтра)\s+)?(?:(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\s+)?"
        r"(\d{1,2})[:.](\d{2})", s)
    if not m:
        return None
    word, d, mo, y, hh, mm = m.groups()
    hh, mm = int(hh), int(mm)
    if hh > 23 or mm > 59:
        return None
    now = datetime.now(TZ)
    try:
        if word:
            base = now.date() + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[word])
            dt = datetime(base.year, base.month, base.day, hh, mm, tzinfo=TZ)
        elif d:
            year = int(y) if y else now.year
            if year < 100:
                year += 2000
            dt = datetime(year, int(mo), int(d), hh, mm, tzinfo=TZ)
            if not y and dt < now:
                dt = datetime(year + 1, int(mo), int(d), hh, mm, tzinfo=TZ)
        else:
            dt = datetime(now.year, now.month, now.day, hh, mm, tzinfo=TZ)
            if dt <= now:
                dt += timedelta(days=1)
    except ValueError:
        return None
    return dt if dt > now + timedelta(seconds=10) else None


def date_keyboard():
    now = datetime.now(TZ)
    rows, row = [], []
    for i in range(8):
        d = (now + timedelta(days=i)).date()
        label = "Сегодня" if i == 0 else "Завтра" if i == 1 else WEEKDAYS[d.weekday()]
        row.append(InlineKeyboardButton(text=f"{label} {d:%d.%m}", callback_data=f"sch:d:{d:%Y%m%d}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⏱ Через 1 ч", callback_data="sch:in:60"),
                 InlineKeyboardButton(text="⏱ Через 3 ч", callback_data="sch:in:180")])
    rows.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="sch:manual")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="sch:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hours_keyboard(d):
    now = datetime.now(TZ)
    start = now.hour if d == now.date() else 0
    btns = [InlineKeyboardButton(text=f"{h:02d}", callback_data=f"sch:h:{d:%Y%m%d}:{h:02d}")
            for h in range(start, 24)]
    rows = [btns[i:i + 6] for i in range(0, len(btns), 6)]
    rows.append([InlineKeyboardButton(text="◀️ К датам", callback_data="sch:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def minutes_keyboard(d, hour: int):
    now = datetime.now(TZ)
    mins = list(range(0, 60, 5))
    if d == now.date() and hour == now.hour:
        mins = [x for x in mins if x > now.minute]
    btns = [InlineKeyboardButton(text=f"{hour:02d}:{x:02d}",
                                 callback_data=f"sch:m:{d:%Y%m%d}:{hour:02d}:{x:02d}") for x in mins]
    rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    rows.append([InlineKeyboardButton(text="◀️ К часам", callback_data=f"sch:d:{d:%Y%m%d}")])
    return InlineKeyboardMarkup(inline_keyboard=rows), bool(mins)


async def notify(chat_id, text: str):
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", link_preview_options=NO_PREVIEW)
    except Exception as e:
        log.warning("Не удалось уведомить %s: %s", chat_id, e)


def short_title(text: str, n: int = 90) -> str:
    t = html.unescape(strip_tags(text)).replace("\n", " ").strip()
    return html.escape(t[:n] + ("…" if len(t) > n else ""))


async def publish_due():
    now = time.time()
    due = sorted([p for p in SCHEDULED if p["at"] <= now], key=lambda x: x["at"])
    for p in due:
        if p not in SCHEDULED:
            continue
        SCHEDULED.remove(p)  # сразу убираем, чтобы не отправить дважды
        save_schedule()
        try:
            await send_post(CHANNEL_ID, p["text"], p.get("media"))
            late = time.time() - p["at"]
            note = f" (с опозданием {fmt_delta(late)})" if late > 120 else ""
            log.info("Отложенный пост %s опубликован%s", p["id"], note)
            await notify(p["chat_id"], f"✅ <b>Отложенный пост опубликован</b>{note}\n{short_title(p['text'])}")
        except Exception as e:
            log.exception("Ошибка публикации отложенного поста %s", p["id"])
            p["tries"] = p.get("tries", 0) + 1
            if p["tries"] < 3:
                p["at"] = time.time() + 60
                SCHEDULED.append(p)
                save_schedule()
                await notify(p["chat_id"], f"⚠️ Не вышло опубликовать отложенный пост ({html.escape(str(e)[:200])}). "
                                           f"Повторю через минуту.")
            else:
                await notify(p["chat_id"], f"❌ Отложенный пост не опубликован после 3 попыток: "
                                           f"{html.escape(str(e)[:200])}\n{short_title(p['text'])}")


async def scheduler_loop():
    log.info("Планировщик запущен")
    while True:
        try:
            await publish_due()
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(2)


# ======================= ХЕНДЛЕРЫ =======================

@dp.message(Command("id"))
async def id_cmd(message: types.Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(PostState.waiting_for_input)
    await message.answer(
        "👋 Привет! Закидывай фактуру: текст, ссылку, фото/видео с описанием или просто "
        "<b>перешли пост</b> (в том числе альбом) из любого канала, и я упакую его под стиль "
        "<b>Federal Insider</b>. Фото и видео сохранятся как есть, поменяется только текст.\n\n"
        "/queue — отложенные посты\n"
        "/testgemini — проверить связь с Gemini\n"
        "/packs — перезагрузить наборы премиум-эмодзи\n"
        "/emojiid — узнать ID премиум-эмодзи",
        parse_mode="HTML",
    )


@dp.message(Command("queue"))
async def queue_cmd(message: types.Message):
    if not SCHEDULED:
        await message.answer("Очередь пуста. Отложить пост можно кнопкой «⏰ Отложить» в превью.")
        return
    for p in sorted(SCHEDULED, key=lambda x: x["at"]):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👁 Показать", callback_data=f"q:show:{p['id']}"),
            InlineKeyboardButton(text="⚡ Сейчас", callback_data=f"q:now:{p['id']}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"q:del:{p['id']}"),
        ]])
        await message.answer(
            f"⏰ <b>{fmt_dt(p['at'])}</b> ({html.escape(TZ_NAME)}), через {fmt_delta(p['at'] - time.time())}\n"
            f"📎 {media_label(p.get('media') or [])}\n{short_title(p['text'])}",
            parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("q:"))
async def queue_cb(callback: types.CallbackQuery):
    _, act, pid = callback.data.split(":", 2)
    item = next((p for p in SCHEDULED if p["id"] == pid), None)
    if not item:
        await callback.answer("Этот пост уже опубликован или удалён", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if act == "show":
        await callback.answer()
        await send_post(callback.message.chat.id, item["text"], item.get("media"))
    elif act == "del":
        SCHEDULED.remove(item)
        save_schedule()
        await callback.answer("Удалено")
        try:
            await callback.message.edit_text("🗑 Отложенный пост удалён.")
        except Exception:
            pass
    elif act == "now":
        SCHEDULED.remove(item)
        save_schedule()
        await callback.answer("Публикую...")
        try:
            await send_post(CHANNEL_ID, item["text"], item.get("media"))
            await callback.message.edit_text("✅ Опубликовано сразу.")
        except Exception as e:
            SCHEDULED.append(item)
            save_schedule()
            await callback.message.answer(f"❌ Ошибка публикации: {e}")


@dp.message(Command("packs"))
async def packs_cmd(message: types.Message):
    msg = await message.answer("🔄 Загружаю наборы эмодзи...")
    report = await load_premium_packs()
    await msg.edit_text(html.escape(report))


@dp.message(Command("testgemini"))
async def testgemini_cmd(message: types.Message):
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
                if e.retry:
                    break
            except Exception as e:
                lines.append(f"❌ {model} / {mode}: {html.escape(str(e)[:160])}")

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
            sym = raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le")
            lines.append(f"{sym} → <code>{e.custom_emoji_id}</code>")
    await state.set_state(PostState.waiting_for_input)
    if lines:
        await message.answer("\n".join(lines) + "\n\nМожно добавить в PREMIUM_EMOJI_JSON.",
                             parse_mode="HTML")
    else:
        await message.answer("Премиум-эмодзи в сообщении не найдено.")


@dp.message(PostState.waiting_for_photo, F.photo | F.video | F.animation)
async def receive_media(message: types.Message, state: FSMContext):
    items = await collect_group(message)
    if items is None:
        return
    media = extract_media(items)
    if not media:
        await message.answer("Не нашёл фото или видео. Пришли ещё раз.")
        return
    data = await state.get_data()
    await state.update_data(media=media)
    await state.set_state(PostState.preview)
    await message.answer(f"✅ Медиа прикреплены: {media_label(media)}")
    await show_preview(message.chat.id, data.get("post_text", ""), media)


@dp.message(PostState.waiting_for_photo)
async def waiting_photo_wrong_type(message: types.Message):
    if (message.text or "").startswith("/"):
        return
    await message.answer("Нужно фото, видео или альбом. Пришли медиа или нажми /start для сброса.")


@dp.message(PostState.waiting_for_time, F.text)
async def receive_time(message: types.Message, state: FSMContext):
    if (message.text or "").startswith("/"):
        return
    dt = parse_user_datetime(message.text)
    if not dt:
        await message.answer(
            "Не понял время или оно уже прошло. Форматы:\n"
            "<code>18:30</code> — сегодня (или завтра, если уже прошло)\n"
            "<code>25.12 18:30</code>\n<code>25.12.2026 18:30</code>\n"
            "<code>завтра 09:00</code>\n\nИли /start для отмены.", parse_mode="HTML")
        return
    await finish_schedule(message.chat.id, message.chat.id, state, dt, message)


@dp.message(StateFilter(None, PostState.waiting_for_input, PostState.preview))
async def process_input(message: types.Message, state: FSMContext):
    if (message.text or "").startswith("/"):
        return
    items = await collect_group(message)
    if items is None:
        return
    await handle_materials(items[0], state, items)


@dp.callback_query(PostState.preview, F.data == "add_media")
async def ask_for_media(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.waiting_for_photo)
    await callback.message.answer("📸 Пришли фото, видео или альбом (до 10). "
                                  "Они заменят текущие медиа, текст останется прежним:")
    await callback.answer()


async def finish_schedule(chat_id: int, admin_chat: int, state: FSMContext, dt: datetime,
                          reply_to: types.Message):
    data = await state.get_data()
    post_text, media = data.get("post_text"), data.get("media") or []
    if not post_text:
        await reply_to.answer("Нет текста для публикации. Пришли материал заново.")
        return
    item = add_scheduled(admin_chat, post_text, media, dt)
    await state.clear()
    await state.set_state(PostState.waiting_for_input)
    await reply_to.answer(
        f"⏰ <b>Пост запланирован</b>\n📅 {fmt_dt(item['at'])} ({html.escape(TZ_NAME)})\n"
        f"⏳ через {fmt_delta(item['at'] - time.time())}\n\nСписок и удаление: /queue\n"
        f"Жду новую фактуру.", parse_mode="HTML")


@dp.callback_query(PostState.preview, F.data == "schedule")
async def schedule_menu(callback: types.CallbackQuery):
    await callback.answer()
    now = datetime.now(TZ)
    await callback.message.answer(
        f"⏰ Когда опубликовать? Сейчас {now:%d.%m %H:%M} ({html.escape(TZ_NAME)}). Выбери дату:",
        reply_markup=date_keyboard())


@dp.callback_query(PostState.preview, F.data.startswith("sch:"))
async def schedule_cb(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    act = parts[1]
    now = datetime.now(TZ)
    try:
        if act == "back":
            await callback.message.delete()
            await callback.answer()
        elif act == "menu":
            await callback.message.edit_text("Выбери дату:", reply_markup=date_keyboard())
            await callback.answer()
        elif act == "d":
            d = datetime.strptime(parts[2], "%Y%m%d").date()
            await callback.message.edit_text(f"📅 {d:%d.%m.%Y}. Выбери час:", reply_markup=hours_keyboard(d))
            await callback.answer()
        elif act == "h":
            d = datetime.strptime(parts[2], "%Y%m%d").date()
            kb, ok = minutes_keyboard(d, int(parts[3]))
            if not ok:
                await callback.answer("Этот час уже заканчивается, выбери следующий", show_alert=True)
                return
            await callback.message.edit_text(f"📅 {d:%d.%m.%Y}, {int(parts[3]):02d}:__. Выбери минуты:",
                                             reply_markup=kb)
            await callback.answer()
        elif act == "m":
            dt = datetime.strptime(parts[2] + parts[3] + parts[4], "%Y%m%d%H%M").replace(tzinfo=TZ)
            if dt <= now + timedelta(seconds=10):
                await callback.answer("Это время уже прошло", show_alert=True)
                return
            await callback.answer()
            await finish_schedule(callback.message.chat.id, callback.message.chat.id, state, dt, callback.message)
        elif act == "in":
            dt = now + timedelta(minutes=int(parts[2]))
            await callback.answer()
            await finish_schedule(callback.message.chat.id, callback.message.chat.id, state, dt, callback.message)
        elif act == "manual":
            await state.set_state(PostState.waiting_for_time)
            await callback.message.edit_text(
                f"✍️ Напиши дату и время ({html.escape(TZ_NAME)}):\n"
                "<code>18:30</code> — сегодня (или завтра, если уже прошло)\n"
                "<code>25.12 18:30</code>\n<code>25.12.2026 18:30</code>\n<code>завтра 09:00</code>\n\n"
                "/start — отмена", parse_mode="HTML")
            await callback.answer()
        else:
            await callback.answer()
    except TelegramBadRequest as e:
        log.warning("schedule_cb: %s", e)
        await callback.answer()


@dp.callback_query(PostState.preview, F.data == "publish_now")
async def publish_now(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    post_text, media = data.get("post_text"), data.get("media") or []
    if not post_text:
        await callback.answer("Нет текста для публикации", show_alert=True)
        return

    await state.set_state(PostState.waiting_for_input)  # защита от двойного клика
    try:
        await send_post(CHANNEL_ID, post_text, media)
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
    original, media = data.get("original_input", ""), data.get("media") or []
    msg = await callback.message.answer("🔄 Перегенерирую пост...")
    try:
        post = await generate_post(original, bool(media))
        await state.update_data(post_text=post)
        await state.set_state(PostState.preview)
        try:
            await msg.delete()
        except Exception:
            pass
        await show_preview(callback.message.chat.id, post, media)
    except Exception as e:
        log.exception("Ошибка перегенерации")
        await callback.message.answer(f"❌ Ошибка генерации: {e}")


@dp.callback_query(F.data == "cancel")
async def cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(PostState.waiting_for_input)
    await callback.message.answer("Отменено. Жду новую фактуру.")
    await callback.answer()


@dp.callback_query(F.data.in_({"publish_now", "regenerate", "add_media", "schedule"}) | F.data.startswith("sch:"))
async def stale_callback(callback: types.CallbackQuery):
    await callback.answer("Сессия устарела. Пришли материал заново.", show_alert=True)


# ======================= ВЕБ-СЕРВЕР ДЛЯ RENDER =======================

async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def keep_alive():
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
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start()
    log.info("Веб-сервер запущен на порту %s", os.getenv("PORT", 10000))

    log.info("Настройки: модель=%s, режим=%s, thinking=%s, таймаут=%s, админов=%d, "
             "GEMINI_API_KEY=%s, CHANNEL_ID=%s, TZ=%s, очередь=%s",
             GEMINI_MODEL, GEMINI_API_MODE, THINKING_LEVEL, GEMINI_TIMEOUT, len(ADMIN_IDS),
             "есть" if GEMINI_API_KEY else "НЕ ЗАДАН", CHANNEL_ID or "НЕ ЗАДАН", TZ_NAME, SCHEDULE_FILE)
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст: бот закрыт для всех. Свой ID можно узнать командой /id")

    try:
        me = await bot.get_me()
        log.info("Бот авторизован: @%s (id %s)", me.username, me.id)
    except Exception as e:
        log.error("Не удалось авторизоваться в Telegram, проверь TELEGRAM_BOT_TOKEN: %s", e)
        await asyncio.Event().wait()

    try:
        await load_premium_packs()
    except Exception as e:
        log.warning("Наборы эмодзи не загружены: %s", e)

    load_schedule()
    keep_task = asyncio.create_task(keep_alive())
    sched_task = asyncio.create_task(scheduler_loop())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Polling запущен, жду сообщения")
        await dp.start_polling(bot)
    finally:
        keep_task.cancel()
        sched_task.cancel()
        if _http_session and not _http_session.closed:
            await _http_session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
