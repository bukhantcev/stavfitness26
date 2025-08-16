import asyncio, re, json, io, textwrap
from datetime import datetime, time, timedelta
from typing import Optional, Dict, Any, Tuple, List

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command, CommandObject
from aiogram.enums.parse_mode import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
import os
import inspect

# ---------- CONFIG ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")

# OpenAI client
from openai import OpenAI, PermissionDeniedError

# Проверяем обязательные переменные окружения
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не найден. Заполни .env с OPENAI_API_KEY=sk-...")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Заполни .env с BOT_TOKEN=...")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID не найден. Укажи @username канала или числовой ID, и сделай бота админом.")

# Инициализация клиента OpenAI по новому SDK (ключ возьмётся из окружения)
oai = OpenAI()

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")  # при желании поменяй

DB_PATH = "fitness_bot.db"

# ---------- DB ----------
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS studio (
  id INTEGER PRIMARY KEY CHECK (id=1),
  profile_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY CHECK (id=1),
  daily_time TEXT    -- 'HH:MM'
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT,          -- тип поста (offer, tip, schedule, review, motivation etc)
  text TEXT,
  image_prompt TEXT,
  created_at TEXT
);
"""

DEFAULT_PROFILE = {
    "name": "STAVFITNESS26",
    "address": "ул. Пирогова 15/2, 3 этаж",
    "phone": "+7 988 703-20-14",
    "services": ["пилатес", "стрейчинг", "здоровая спина", "dance аэробика", "силовые тренировки"],
    "tone": "дружелюбно, по делу, без воды, с эмодзи",
    "hashtags": ["#пилатес", "#стрейчинг", "#ставрополь", "#форма", "#здороваяспина", "#тренировка"],
    "offers": [
        "Скидка 10% по флаеру",
        "Пробная тренировка — бесплатно по записи"
    ],
    "brand_words": ["STAVFITNESS26", "сильное тело", "здоровая осанка", "комфортная атмосфера","твоё тело, твоё здоровье, твоя гармония"],
    "image_style": "светлый зал, натуральный свет, динамика, улыбающиеся люди, 3:4, исключить чернокожих и азиатов"
}

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()
        # ensure profile exists
        cur = await db.execute("SELECT profile_json FROM studio WHERE id=1")
        row = await cur.fetchone()
        if not row:
            await db.execute("INSERT INTO studio (id, profile_json) VALUES (1, ?)", (json.dumps(DEFAULT_PROFILE, ensure_ascii=False),))
            await db.commit()

async def get_profile() -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT profile_json FROM studio WHERE id=1")
        row = await cur.fetchone()
        return json.loads(row[0]) if row else DEFAULT_PROFILE

async def set_profile(profile: Dict[str, Any]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE studio SET profile_json=? WHERE id=1", (json.dumps(profile, ensure_ascii=False),))
        await db.commit()

async def get_daily_time() -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT daily_time FROM settings WHERE id=1")
        row = await cur.fetchone()
        return row[0] if row and row[0] else None

async def set_daily_time(hhmm: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM settings WHERE id=1")
        row = await cur.fetchone()
        if row:
            await db.execute("UPDATE settings SET daily_time=? WHERE id=1", (hhmm,))
        else:
            await db.execute("INSERT INTO settings (id, daily_time) VALUES (1, ?)", (hhmm,))
        await db.commit()

async def add_draft(kind: str, text: str, image_prompt: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO drafts (kind, text, image_prompt, created_at) VALUES (?, ?, ?, ?)",
            (kind, text, image_prompt or "", datetime.now().isoformat())
        )
        await db.commit()

async def get_latest_draft() -> Optional[Tuple[int, str, str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, kind, text, image_prompt FROM drafts ORDER BY id DESC LIMIT 1")
        return await cur.fetchone()

# ---------- OPENAI HELPERS ----------
GEN_SYSTEM = """Ты — SMM-редактор фитнес-студии. Пишешь короткие сочные посты для Telegram:
— стиль: дружелюбно, по делу, без воды; 350–700 символов;
— обязательно 1–2 эмодзи в начале абзацев, 3–6 релевантных хештегов в конце;
— явный CTA: записаться/написать в директ/в Телеграм; без “выкатываем” и канцелярита;
— не используй markdown ссылки, только текст; без лишних кавычек и CAPS LOCK.
"""

def build_user_prompt(profile: Dict[str, Any], kind: str, extra: str = "") -> str:
    services = ", ".join(profile["services"])
    hashtags = " ".join(profile["hashtags"])
    offers = "; ".join(profile["offers"])
    brand = ", ".join(profile["brand_words"])
    tone = profile["tone"]
    base = f"""Дано:
- Студия: {profile["name"]}
- Адрес: {profile["address"]}
- Телефон: {profile["phone"]}
- Услуги: {services}
- Офферы: {offers}
- Слова бренда: {brand}
- Тон: {tone}

Задача: Напиши пост типа "{kind}" для Telegram-канала студии. В конце добавь хештеги: {hashtags}.
Если уместно, вставь явный оффер (но не всегда). Укажи адрес/связь ненавязчиво.
Доп. условия: {extra}
"""
    return base

async def generate_post(profile: Dict[str, Any], kind: str, extra: str = "") -> str:
    prompt = build_user_prompt(profile, kind, extra)
    resp = oai.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": GEN_SYSTEM},
            {"role": "user", "content": prompt}
        ],
        temperature=0.8,
    )
    return resp.choices[0].message.content.strip()

async def generate_image_bytes(image_prompt: str) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Генерирует PNG через OpenAI Images и возвращает (data, error).
    При 403 (нужна Verify Organization) делаем фолбэк без картинки.
    """
    if not image_prompt:
        return None, None
    try:
        img = oai.images.generate(
            model="gpt-image-1",
            prompt=image_prompt,
            size="1024x1024"
        )
        b64 = img.data[0].b64_json
        import base64
        return base64.b64decode(b64), None
    except PermissionDeniedError as e:
        return None, (
            "Нет доступа к модели gpt-image-1: нужна верификация организации на platform.openai.com (Settings → Organization → Verify). "
            "Сделал фолбэк: публикуем без картинки."
        )
    except Exception as e:
        return None, f"Ошибка генерации изображения: {e}"

# ---------- UI ----------
def post_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Утвердить и опубликовать", callback_data="approve")],
        [InlineKeyboardButton(text="🎲 Ещё вариант", callback_data="regen"),
         InlineKeyboardButton(text="✏️ Править", callback_data="edit")],
        [InlineKeyboardButton(text="🖼 Сгенерить картинку", callback_data="image")]
    ])
    return kb

def only_admin(func):
    async def wrapper(event, *args, **kwargs):
        uid = event.from_user.id if isinstance(event, Message) else event.from_user.id
        if uid != ADMIN_ID:
            return await (event.answer if isinstance(event, Message) else event.message.answer)("Доступ только для администратора.")
        # Пропускаем в целевой хэндлер только те kwargs, которые он реально принимает (например, command)
        sig = inspect.signature(func)
        allowed_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return await func(event, *args, **allowed_kwargs)
    return wrapper

# ---------- COMMANDS ----------
@dp.message(Command("start"))
@only_admin
async def start_cmd(m: Message, command: CommandObject):
    await m.answer("Привет! Я SMM‑бот студии. /setup — профиль, /draft — черновик, /schedule HH:MM — автопост, /plan_week — контент‑план, /status.")

@dp.message(Command("setup"))
@only_admin
async def setup_cmd(m: Message, command: CommandObject):
    """
    Пример:
    /setup name=StavFitness; address=ул. Пирогова 15/2, 3 этаж; phone=+7988...; services=пилатес,стрейчинг; hashtags=#пилатес,#стрейчинг; offers=Скидка 10%,Пробная; tone=дружелюбно
    """
    prof = await get_profile()
    if command.args:
        # парсим key=value; key=value; ...
        pairs = [p.strip() for p in command.args.split(";") if p.strip()]
        for pair in pairs:
            if "=" in pair:
                k, v = [x.strip() for x in pair.split("=", 1)]
                if k in ["services", "hashtags", "offers", "brand_words"]:
                    prof[k] = [x.strip() for x in re.split(r"[;,]", v) if x.strip()]
                else:
                    prof[k] = v
    await set_profile(prof)
    pretty = textwrap.dedent(f"""
    Профиль сохранён:
    • name: {prof['name']}
    • address: {prof['address']}
    • phone: {prof['phone']}
    • services: {', '.join(prof['services'])}
    • offers: {', '.join(prof['offers'])}
    • tone: {prof['tone']}
    • hashtags: {' '.join(prof['hashtags'])}
    """).strip()
    await m.answer(pretty)

@dp.message(Command("draft"))
@only_admin
async def draft_cmd(m: Message, command: CommandObject):
    """
    /draft kind=offer|tip|schedule|motivation|review|news; extra=про новую группу по пилатесу
    """
    prof = await get_profile()
    kind = "offer"
    extra = ""
    if command.args:
        # простенький парсер
        parts = [p.strip() for p in command.args.split(";") if p.strip()]
        for part in parts:
            if part.startswith("kind="):
                kind = part.split("=",1)[1].strip()
            elif part.startswith("extra="):
                extra = part.split("=",1)[1].strip()
    text = await generate_post(prof, kind, extra)
    await add_draft(kind, text, image_prompt=None)
    await m.answer(f"<b>Черновик ({kind}):</b>\n\n{text}", reply_markup=post_kb())

@dp.message(Command("schedule"))
@only_admin
async def schedule_cmd(m: Message, command: CommandObject):
    """
    /schedule 10:00  — ежедневная автопубликация
    /schedule off     — выключить
    """
    if not command.args:
        hhmm = await get_daily_time()
        return await m.answer(f"Текущее расписание: {hhmm or 'нет'}")
    arg = command.args.strip().lower()
    if arg == "off":
        await set_daily_time(None)
        reschedule_daily(None)
        return await m.answer("Ежедневная автопубликация выключена.")
    if not re.match(r"^\d{2}:\d{2}$", arg):
        return await m.answer("Укажи время в формате HH:MM (напр. 10:00)")
    await set_daily_time(arg)
    reschedule_daily(arg)
    await m.answer(f"Готово. Буду публиковать ежедневно в {arg}.")

@dp.message(Command("plan_week"))
@only_admin
async def plan_week_cmd(m: Message, command: CommandObject):
    prof = await get_profile()
    kinds = ["offer", "tip", "schedule", "motivation", "review", "news", "tip"]
    await m.answer("Генерю 7 черновиков на неделю…")
    for k in kinds:
        text = await generate_post(prof, k, "")
        await add_draft(k, text, image_prompt=None)
        await m.answer(f"<b>Черновик ({k}):</b>\n\n{text}", reply_markup=post_kb())

@dp.message(Command("status"))
@only_admin
async def status_cmd(m: Message, command: CommandObject):
    prof = await get_profile()
    hhmm = await get_daily_time()
    await m.answer(
        textwrap.dedent(f"""
        Статус:
        • Канал: {CHANNEL_ID}
        • Автопост: {hhmm or 'выкл'}
        • Студия: {prof['name']} | Тон: {prof['tone']}
        • Хэштеги: {' '.join(prof['hashtags'])}
        """).strip()
    )

# ---------- CALLBACKS ----------
@dp.callback_query(F.data.in_({"approve","regen","edit","image"}))
async def on_cb(q: CallbackQuery):
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Только админ.", show_alert=True)
    draft = await get_latest_draft()
    if not draft:
        return await q.message.answer("Нет черновика.")
    draft_id, kind, text, image_prompt = draft

    # Immediately answer the callback to avoid timeout
    await _safe_cb_answer(q, "⏳ Обрабатываю…")

    if q.data == "approve":
        await publish_to_channel(text, None)
        return await q.message.answer("Опубликовано ✅")

    if q.data == "regen":
        prof = await get_profile()
        new_text = await generate_post(prof, kind, "сделай другой угол и подачу")
        await add_draft(kind, new_text, image_prompt=None)
        return await q.message.answer(f"<b>Черновик ({kind}) — новый вариант:</b>\n\n{new_text}", reply_markup=post_kb())

    if q.data == "edit":
        await q.message.answer("Пришли новый текст одним сообщением. Я опубликую его.")
        dp.message.register(one_shot_publish)
        return

    if q.data == "image":
        prof = await get_profile()
        img_prompt = f"Фитнес-студия {prof['name']}, {prof['image_style']}. Акцент: {', '.join(prof['services'][:2])}"
        data, err = await generate_image_bytes(img_prompt)
        if err:
            await publish_to_channel(text, None)
            return await q.message.answer("Пост без картинки опубликован ✅\n\n" + err)
        if not data:
            return await q.answer("Не удалось сгенерировать изображение", show_alert=True)
        await publish_to_channel(text, data)
        return await q.message.answer("Пост с картинкой опубликован ✅")

async def one_shot_publish(m: Message):
    # снимаем хэндлер сразу после использования
    dp.message.unregister(one_shot_publish)
    await publish_to_channel(m.html_text, None)
    await m.answer("Опубликовано ✅")

# ---------- PUBLISH ----------
async def publish_to_channel(text: str, image_bytes: Optional[bytes]):
    if image_bytes:
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=BufferedInputFile(image_bytes, filename="post.png"),
            caption=text
        )
    else:
        await bot.send_message(chat_id=CHANNEL_ID, text=text)


# ---------- HELPERS ----------
# Helper to safely answer callback queries (avoid late answer errors)
async def _safe_cb_answer(q: CallbackQuery, text: str | None = None, show_alert: bool = False):
    try:
        await q.answer(text or "", show_alert=show_alert)
    except Exception:
        pass

# ---------- SCHEDULER ----------
def reschedule_daily(hhmm: Optional[str]):
    # убираем старую задачу
    for job in scheduler.get_jobs():
        job.remove()
    if not hhmm:
        return
    h, m = map(int, hhmm.split(":"))
    trigger = CronTrigger(hour=h, minute=m)
    scheduler.add_job(func=scheduled_job, trigger=trigger, id="daily_post")

async def scheduled_job():
    prof = await get_profile()
    # ротируем типы постов по кругу
    kinds_cycle = ["offer","tip","schedule","motivation","review","news"]
    kind = kinds_cycle[datetime.now().weekday() % len(kinds_cycle)]
    text = await generate_post(prof, kind, "коротко, для утреннего чтения")
    await publish_to_channel(text, None)

# ---------- ENTRY ----------
async def main():
    await init_db()
    # поднимем планировщик с текущим временем из БД
    hhmm = await get_daily_time()
    reschedule_daily(hhmm)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())