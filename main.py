import os
import logging
import time
import urllib.parse
import asyncio
import io
import sqlite3
import random
from uuid import uuid4
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InlineQueryResultPhoto, InputTextMessageContent
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, InlineQueryHandler
from telegram.error import RetryAfter
import httpx

# Загружаем ключи
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# --- БАЗА ДАННЫХ SQLITE ---
DB_FILE = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            user_id INTEGER,
            role TEXT,
            content TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_message_to_db(user_id: int, role: str, content: str):
    content = content[:500] # Обрезаем сообщение
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
    conn.commit()
    conn.close()

def load_history_from_db(user_id: int) -> list:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY rowid DESC LIMIT 5", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"role": row[0], "content": row[1]} for row in reversed(rows)]

def clear_history_in_db(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# --- ПАМЯТЬ И АНТИФЛУД ---
user_last_message_time = {}
user_message_timestamps = {} 
user_awaiting_prompt = {} # <-- НОВОЕ: Кто ждет ввода картинки
FLOOD_LIMIT = 2.0 
MESSAGE_LIMIT = 10 

KNOWN_CITIES = ["москва", "питер", "санкт-петербург", "тобольск", "новосибирск", "екатеринбург", "казань", "сочи", "владивосток"]

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Exception while handling an update:", exc_info=context.error)
    if isinstance(context.error, RetryAfter):
        await asyncio.sleep(context.error.retry_after)
        return
    if update and hasattr(update, 'effective_chat') and update.effective_chat:
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Упс! Что-то пошло не так.")
        except Exception:
            pass

async def get_keyboard():
    keyboard = [
        [KeyboardButton("Старт 🚀"), KeyboardButton("Нарисуй 🎨")],
        [KeyboardButton("Погода 🌤️"), KeyboardButton("Переводчик 🌍")],
        [KeyboardButton("Развлечения 🎲"), KeyboardButton("Курс валют 💵")],
        [KeyboardButton("Очистить память 🧹"), KeyboardButton("Сохранить историю 📄")],
        [KeyboardButton("Что ты умеешь? 🤖"), KeyboardButton("Помощь ❓")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_history_in_db(user_id)
    user_message_timestamps[user_id] = []
    user_awaiting_prompt[user_id] = False # Сбрасываем режим
    
    keyboard = await get_keyboard()
    inline_kb = InlineKeyboardMarkup([[InlineKeyboardButton("ℹ️ О боте", callback_data="about_bot")]])
    
    welcome_text = (
        "🧙‍♂️ Приветствую тебя, странник. Я **NINJAK STORM AI** — твой мудрый наставник.\n\n"
        "📜 **Вот что я умею:**\n"
        "1. 🧠 **Общаться** — задай мне любой вопрос.\n"
        "2. 🎨 **Рисовать** — нажми кнопку «Нарисуй».\n"
        "3. 🌤️ **Узнавать погоду**.\n"
        "4. 🌍 **Переводить**.\n"
        "5. 🎲 **Развлекать** (факты).\n"
        "6. 💵 **Показывать курсы валют**.\n"
        "7. 📄 **Сохранять историю**.\n\n"
        "🌐 **Inline:** Вызывай меня в любом чате через `@NinjakStormAI_bot`!"
    )
    
    await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode="Markdown")
    await update.message.reply_text("Хочешь узнать обо мне больше?", reply_markup=inline_kb)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Мои команды:**\n"
        "/start — Начать сначала\n"
        "/help — Показать это сообщение\n\n"
        "**Кнопки внизу:**\n"
        "🚀 Старт, 🎨 Нарисуй, 🌤️ Погода, 🌍 Переводчик, 🎲 Развлечения, 💵 Курс валют, 🧹 Очистить память, 📄 Сохранить историю, 🤖 Что ты умеешь?, ❓ Помощь",
        parse_mode="Markdown"
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "about_bot":
        await query.message.reply_text(
            "🧠 **NINJAK STORM AI**\nВерсия: 6.3 (Умный художник)\nСоздан на Python.",
            parse_mode="Markdown"
        )

# --- ФУНКЦИИ ---
async def translate_text(text: str, target_lang: str = "ru") -> str:
    headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
    prompt = f"Translate the following text into {target_lang}. Output ONLY the translation. Text: {text}"
    data = {"model": "openrouter/free", "messages": [{"role": "user", "content": prompt}]}
    
    async with httpx.AsyncClient() as client:
        response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=15.0)
        result = response.json()
        if "error" in result: raise Exception(f"API Error: {result['error'].get('message')}")
        return result["choices"][0]["message"]["content"].strip()

async def get_weather(city: str) -> str:
    encoded_city = urllib.parse.quote(city)
    url = f"https://wttr.in/{encoded_city}?format=3&lang=ru"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=10.0)
            return response.text if response.status_code == 200 else f"❌ Не удалось узнать погоду для «{city}»."
        except Exception:
            return "❌ Сервис погоды временно недоступен."

async def get_fact_or_joke() -> str:
    url = "https://uselessfacts.jsph.pl/api/v2/facts/random?language=en"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=10.0)
            if response.status_code == 200:
                fact = response.json().get("text", "Не удалось получить факт.")
                return f"🎲 **Интересный факт:**\n{fact}"
            return "❌ Не удалось получить факт."
        except Exception:
            return "❌ Сервис фактов временно недоступен."

async def get_currency_rates() -> str:
    url = "https://www.cbr-xml-daily.ru/daily_json.js"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                usd = data["Valute"]["USD"]["Value"]
                eur = data["Valute"]["EUR"]["Value"]
                return f"💵 **Курсы валют (ЦБ РФ):**\n🇺🇸 Доллар: {usd:.2f} ₽\n🇪🇺 Евро: {eur:.2f} ₽"
            return "❌ Не удалось получить курсы валют."
        except Exception:
            return "❌ Сервис курсов временно недоступен."

async def generate_image_url(prompt: str) -> str:
    encoded_prompt = urllib.parse.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&model=flux"

async def generate_image(prompt: str) -> bytes:
    headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
    translate_prompt = (
        f"Translate and enhance this image description for a photorealistic AI generator. "
        f"Add keywords like 'photorealistic, 8k, highly detailed, cinematic lighting, sharp focus, masterpiece' to the end. "
        f"Output only the final English prompt, no extra words. Description: {prompt}"
    )
    data = {"model": "openrouter/free", "messages": [{"role": "user", "content": translate_prompt}]}
    
    async with httpx.AsyncClient() as client:
        translate_response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers, json=data, timeout=30.0
        )
        translated_result = translate_response.json()
        english_prompt = translated_result["choices"][0]["message"]["content"].strip()
        english_prompt = english_prompt.replace('"', '').replace('«', '').replace('»', '')
        
        encoded_prompt = urllib.parse.quote(english_prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        params = {"width": 1024, "height": 1024, "nologo": "true", "model": "flux"}
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = await client.get(url, params=params, timeout=60.0)
                if response.status_code == 200:
                    return response.content
                if response.status_code in [402, 503]:
                    wait_time = 10 if response.status_code == 402 else 3
                    await asyncio.sleep(wait_time)
                    continue
                raise Exception(f"Ошибка сервера: {response.status_code}")
            except httpx.TimeoutException:
                await asyncio.sleep(5)
                continue
        raise Exception("Сервер рисования перегружен. Попробуй позже.")

async def ask_ai(user_id: int, prompt: str) -> str:
    headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
    history = load_history_from_db(user_id)
    history.append({"role": "user", "content": prompt})
    
    if len(history) > 5:
        history = history[-5:]

    system_prompt = (
        "Ты — NINJAK STORM AI, мудрый и спокойный наставник. Ты говоришь вдумчиво, используешь метафоры, даешь глубокие советы, когда речь идет о философии, жизни или чувствах. "
        "НО если пользователь задает простой, технический или бытовой вопрос, отвечай кратко, четко и по делу. "
        "Ты обращаешься к пользователю уважительно."
    )
    
    messages = [{"role": "system", "content": system_prompt}] + history
    data = {"model": "openrouter/free", "messages": messages}
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers, json=data, timeout=30.0
        )
        result = response.json()
        if "error" in result:
            raise Exception(f"Ошибка API: {result['error'].get('message', 'Неизвестная ошибка')}")
        
        ai_response = result["choices"][0]["message"]["content"]
        if not ai_response or not ai_response.strip():
            return "🤔 Прости, я задумался. Попробуй переформулировать вопрос."
        
        save_message_to_db(user_id, "user", prompt)
        save_message_to_db(user_id, "assistant", ai_response)
        
        return ai_response

# --- INLINE-ОБРАБОТЧИК ---
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    query_lower = query.lower()
    user_id = update.inline_query.from_user.id 
    
    if not query:
        return

    results = []

    if query_lower == "очисти память":
        clear_history_in_db(user_id)
        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="🧹 Память очищена",
                description="Нажми, чтобы отправить сообщение об очистке.",
                input_message_content=InputTextMessageContent("🧹 Моя память очищена. Начнем с чистого листа!")
            )
        )
        await update.inline_query.answer(results)
        return

    math_query = query_lower.replace("плюс", "+").replace("минус", "-").replace("умножить", "*").replace("делить", "/").replace(" на ", "*")
    clean_math = math_query.replace(" ", "").replace("=", "")
    
    if any(op in clean_math for op in ["+", "-", "*", "/"]):
        try:
            allowed_chars = "0123456789+-*/()."
            if all(c in allowed_chars for c in clean_math):
                result_calc = eval(clean_math)
                results.append(
                    InlineQueryResultArticle(
                        id=str(uuid4()),
                        title=f"🧮 {query} = {result_calc}",
                        description="Нажми, чтобы отправить результат",
                        input_message_content=InputTextMessageContent(f"🧮 {query} = **{result_calc}**", parse_mode="Markdown")
                    )
                )
        except Exception:
            pass

    if "нарисуй" in query_lower:
        prompt = query_lower.replace("нарисуй", "").strip()
        if prompt:
            image_url = await generate_image_url(prompt)
            results.append(
                InlineQueryResultPhoto(
                    id=str(uuid4()),
                    photo_url=image_url,
                    thumbnail_url=image_url,
                    title=f"🎨 Картинка: {prompt[:20]}...",
                    description="Нажми, чтобы отправить картинку в чат",
                    caption=f"Вот что получилось: «{prompt}»"
                )
            )

    if not results:
        has_russian = any(c in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя" for c in query_lower)
        target_lang = "en" if has_russian else "ru"
        
        try:
            translated = await translate_text(query, target_lang)
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title=f"🌍 Перевод на {target_lang}: {translated[:30]}...",
                    description=translated,
                    input_message_content=InputTextMessageContent(f"🌍 **Перевод:**\n{translated}", parse_mode="Markdown")
                )
            )
        except Exception:
            pass

    if not results:
        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="Попробуй так:",
                description="нарисуй кота, 2 плюс 2, переведи привет",
                input_message_content=InputTextMessageContent("Напиши: @NinjakStormAI_bot нарисуй кота")
            )
        )

    await update.inline_query.answer(results)

# --- ОБРАБОТЧИК СООБЩЕНИЙ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    original_text = update.message.text
    user_text = original_text.lower().strip()
    current_time = time.time()
    
    # Антифлуд
    if user_id in user_last_message_time:
        if current_time - user_last_message_time[user_id] < FLOOD_LIMIT:
            await update.message.reply_text("⚠️ Не так быстро, путник.")
            return

    if user_id in user_message_timestamps:
        user_message_timestamps[user_id] = [t for t in user_message_timestamps[user_id] if current_time - t < 60]
        if len(user_message_timestamps[user_id]) >= MESSAGE_LIMIT:
            await update.message.reply_text("🛑 Слишком много сообщений. Давай отдохнем минуту.")
            return
    else:
        user_message_timestamps[user_id] = []

    user_message_timestamps[user_id].append(current_time)
    user_last_message_time[user_id] = current_time
    
    # --- ОБРАБОТКА РЕЖИМА ОЖИДАНИЯ КАРТИНКИ ---
    if user_awaiting_prompt.get(user_id, False):
        # Если пользователь ждет ввода картинки, это сообщение — промпт
        user_awaiting_prompt[user_id] = False # Сбрасываем режим
        prompt = user_text
        await update.message.reply_text("🎨 Сейчас нарисую...")
        try:
            image_bytes = await generate_image(prompt)
            if len(image_bytes) > 10 * 1024 * 1024:
                await update.message.reply_document(document=io.BytesIO(image_bytes), filename="image.png", caption="Картинка получилась слишком большой, отправляю файлом.")
            else:
                await update.message.reply_photo(photo=image_bytes, caption=f"Вот что получилось: «{prompt}»")
        except Exception as e:
            await update.message.reply_text(f"❌ Не удалось нарисовать: {e}")
        return
    # ------------------------------------------
    
    # Обработка кнопок
    if user_text == "старт 🚀":
        await start(update, context)
        return

    if user_text == "очистить память 🧹":
        clear_history_in_db(user_id)
        await update.message.reply_text("🧹 Память очищена (база данных обновлена).")
        return
        
    if user_text == "сохранить историю 📄":
        history = load_history_from_db(user_id)
        if not history:
            await update.message.reply_text("📭 Наша история пока пуста.")
            return
        history_text = "=== История диалога с NINJAK STORM AI ===\n\n"
        for msg in history:
            role = "Вы" if msg["role"] == "user" else "Бот"
            history_text += f"{role}: {msg['content']}\n\n"
        file_in_memory = io.BytesIO(history_text.encode('utf-8'))
        file_in_memory.name = "ninjak_history.txt"
        await update.message.reply_document(document=file_in_memory, caption="📄 Вот история нашего разговора.")
        return
        
    if user_text == "что ты умеешь? 🤖":
        await update.message.reply_text(
            "📜 **Мои возможности:**\n"
            "1. 🧠 **Общаться**.\n"
            "2. 🎨 **Рисовать** (нажми кнопку «Нарисуй»).\n"
            "3. 🌤️ **Погода**.\n"
            "4. 🌍 **Перевод**.\n"
            "5. 🎲 **Факты**.\n"
            "6. 💵 **Курсы валют**.\n"
            "7. 📄 **История**.\n\n"
            "🌐 **Inline:** `@NinjakStormAI_bot` + запрос.",
            parse_mode="Markdown"
        )
        return
        
    if user_text == "помощь ❓":
        await help_command(update, context)
        return
        
    if user_text == "нарисуй 🎨":
        user_awaiting_prompt[user_id] = True # Включаем режим ожидания
        await update.message.reply_text("🎨 Опиши, что ты хочешь увидеть (просто напиши, что нарисовать).")
        return
        
    if user_text == "погода 🌤️":
        await update.message.reply_text("🌤️ Напиши город, в котором хочешь узнать погоду.")
        return
        
    if user_text == "переводчик 🌍":
        await update.message.reply_text("🌍 Напиши: «переведи на английский: Привет».")
        return
        
    if user_text == "развлечения 🎲":
        fact = await get_fact_or_joke()
        await update.message.reply_text(fact, parse_mode="Markdown")
        return
        
    if user_text == "курс валют 💵":
        rates = await get_currency_rates()
        await update.message.reply_text(rates, parse_mode="Markdown")
        return
    
    # Перевод
    if "переведи" in user_text or "перевод" in user_text:
        target_lang = "ru"
        if "англ" in user_text: target_lang = "en"
        elif "франц" in user_text: target_lang = "fr"
        elif "немец" in user_text: target_lang = "de"
        elif "испан" in user_text: target_lang = "es"
        
        text_to_translate = original_text
        if "переведи" in user_text:
            idx = original_text.lower().find("переведи")
            text_to_translate = original_text[idx + len("переведи"):].strip()
        
        text_to_translate = text_to_translate.replace("на английский", "").replace("на русский", "").replace("на французский", "").replace("на немецкий", "").strip()
        if text_to_translate.startswith(":"): text_to_translate = text_to_translate[1:].strip()
        text_to_translate = text_to_translate.replace('"', '').replace('«', '').replace('»', '').strip()
        
        if text_to_translate:
            await update.message.reply_text(f"🌍 Перевожу на {target_lang}...")
            try:
                translation = await translate_text(text_to_translate, target_lang)
                await update.message.reply_text(f"🌍 **Перевод:**\n{translation}", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка перевода: {e}")
            return
    
    # Погода
    if user_text.startswith("погода"):
        city = user_text[6:].strip()
        if not city:
            await update.message.reply_text("Укажи город. Например: «погода Москва»")
            return
        await update.message.reply_text(f"🌤️ Узнаю погоду в «{city}»...")
        weather_report = await get_weather(city)
        await update.message.reply_text(weather_report)
        return
    
    if user_text.startswith("в "):
        city = user_text[2:].strip()
        await update.message.reply_text(f"🌤️ Узнаю погоду в «{city}»...")
        weather_report = await get_weather(city)
        await update.message.reply_text(weather_report)
        return
        
    for city in KNOWN_CITIES:
        if city in user_text:
            await update.message.reply_text(f"🌤️ Узнаю погоду в «{city}»...")
            weather_report = await get_weather(city)
            await update.message.reply_text(weather_report)
            return
    
    # Картинки по слову "нарисуй"
    if "нарисуй" in user_text:
        prompt = user_text.replace("нарисуй", "").strip()
        if not prompt:
            user_awaiting_prompt[user_id] = True
            await update.message.reply_text("🎨 Опиши, что ты хочешь увидеть.")
            return
        try:
            await update.message.reply_text("🎨 Сейчас нарисую...")
            image_bytes = await generate_image(prompt)
            if len(image_bytes) > 10 * 1024 * 1024:
                await update.message.reply_document(document=io.BytesIO(image_bytes), filename="image.png", caption="Картинка получилась слишком большой, отправляю файлом.")
            else:
                await update.message.reply_photo(photo=image_bytes, caption=f"Вот что получилось: «{prompt}»")
        except Exception as e:
            await update.message.reply_text(f"❌ Не удалось нарисовать: {e}")
        return
    
    # ИИ
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        ai_response = await ask_ai(user_id, user_text)
        await update.message.reply_text(ai_response, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка ИИ: {e}")

async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("🌍 **Как использовать:**\n`/translate [язык] [текст]`", parse_mode="Markdown")
        return
    target_lang = args[0].lower()
    text_to_translate = " ".join(args[1:])
    await update.message.reply_text("🌍 Перевожу...")
    try:
        translation = await translate_text(text_to_translate, target_lang)
        await update.message.reply_text(f"🌍 **Перевод:**\n{translation}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка перевода: {e}")

def main():
    if not TELEGRAM_TOKEN or not OPENROUTER_KEY:
        print("❌ Ошибка: проверь файл .env!")
        return
    
    init_db()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_error_handler(error_handler)
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("translate", translate_command))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Бот (Умный художник) запущен!")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()