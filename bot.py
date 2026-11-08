import os
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai

# Переменные окружения подтягиваются автоматически из Render
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

class PostState(StatesGroup):
    waiting_for_input = State()
    preview = State()

# Системный промпт с максимальной юридической защитой и полным словарем
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

АРХИТЕКТУРА ПОСТА (выдавать строго в HTML-разметке Telegram):
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
- Подпись внизу: Federal Insider | Подписаться (ссылкой на канал).

Выдавай ТОЛЬКО готовый текст поста без вступлений, приветствий и пояснений.
"""

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.set_state(PostState.waiting_for_input)
    await message.answer("👋 Привет! Закидывай фактуру, ссылку или фото с описанием, и я упакую пост под стиль **Federal Insider**.")

@dp.message(PostState.waiting_for_input)
async def process_input(message: types.Message, state: FSMContext):
    user_text = message.caption or message.text or ""
    photo_id = message.photo[-1].file_id if message.photo else None

    if not user_text and not photo_id:
        await message.answer("Пришли текст или фото с описанием!")
        return

    msg = await message.answer("⏳ Gemini упаковывает пост в стиль Federal Insider...")

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"{SYSTEM_PROMPT}\n\nИсходные данные:\n{user_text}"
        )
        generated_post = response.text

        await state.update_data(post_text=generated_post, photo_id=photo_id)
        await state.set_state(PostState.preview)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data="publish_now")],
            [InlineKeyboardButton(text="🔄 Перегенерировать", callback_data="regenerate")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ])

        await msg.delete()
        if photo_id:
            await message.answer_photo(photo=photo_id, caption=generated_post, parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer(text=generated_post, parse_mode="HTML", reply_markup=kb)

    except Exception as e:
        await message.answer(f"❌ Ошибка генерации: {e}")

@dp.callback_query(F.data == "publish_now")
async def publish_now(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    post_text = data.get("post_text")
    photo_id = data.get("photo_id")

    try:
        if photo_id:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=post_text, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=post_text, parse_mode="HTML")

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("✅ **Пост успешно опубликован в Federal Insider!**")
        await state.set_state(PostState.waiting_for_input)
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка публикации: {e}")

@dp.callback_query(F.data == "regenerate")
async def regenerate(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("🔄 Перегенерирую пост...")
    await state.set_state(PostState.waiting_for_input)
    await callback.answer()

@dp.callback_query(F.data == "cancel")
async def cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Отменено. Жду новую фактуру.")
    await callback.answer()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
