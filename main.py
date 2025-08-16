import asyncio, re, json, io, textwrap
from datetime import datetime, time, timedelta
from typing import Optional, Dict, Any, Tuple, List

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand,
)
from aiogram.filters import Command, CommandObject
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.enums.parse_mode import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

import os
import inspect
import logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

# ---------- SAFETY GUARD (NSFW filter for drafts) ----------
NSFW_REGEX = re.compile(r"(пизд|хуй|еб|минет|секс|порно|вагин|пенис|оральн|анал|сосать|куннилинг|феллаци|эрот|нюд)", re.IGNORECASE)

def is_nsfw(text: str) -> bool:
    return bool(NSFW_REGEX.search(text or ""))

# ---------- CONFIG ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")

def _parse_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS")
    if raw:
        ids = set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids
    # backward compatibility with ADMIN_ID
    single = os.getenv("ADMIN_ID")
    if single and single.isdigit():
        return {int(single)}
    return set()

ADMIN_IDS = _parse_admin_ids()

# OpenAI client
from openai import OpenAI, PermissionDeniedError

# Проверяем обязательные переменные окружения
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не найден. Заполни .env с OPENAI_API_KEY=sk-...")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Заполни .env с BOT_TOKEN=...")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID не найден. Укажи @username канала или числовой ID, и сделай бота админом.")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS/ADMIN_ID не задан(ы). Укажи в .env ADMIN_IDS=123,456")

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
    "image_style": "светлый зал, натуральный свет, динамика, улыбающиеся люди, 3:4"
}

async def migrate_db():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("PRAGMA table_info(drafts)")
        cols = [r[1] for r in await cur.fetchall()]
        if "image_bytes" not in cols:
            await db.execute("ALTER TABLE drafts ADD COLUMN image_bytes BLOB")
            await db.commit()

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
    await migrate_db()

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

async def add_draft(kind: str, text: str, image_prompt: Optional[str], image_bytes: Optional[bytes] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO drafts (kind, text, image_prompt, created_at, image_bytes) VALUES (?, ?, ?, ?, ?)",
            (kind, text, image_prompt or "", datetime.now().isoformat(), image_bytes)
        )
        await db.commit()

async def get_latest_draft() -> Optional[Tuple[int, str, str, str, Optional[bytes]]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, kind, text, image_prompt, image_bytes FROM drafts ORDER BY id DESC LIMIT 1")
        return await cur.fetchone()

async def set_draft_image(draft_id: int, image_bytes: Optional[bytes], image_prompt: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if image_bytes is None:
            await db.execute("UPDATE drafts SET image_bytes=NULL WHERE id=?", (draft_id,))
        else:
            await db.execute("UPDATE drafts SET image_bytes=?, image_prompt=? WHERE id=?", (image_bytes, image_prompt or "", draft_id))
        await db.commit()

# ---------- OPENAI HELPERS ----------
GEN_SYSTEM = """Ты — SMM-редактор фитнес-студии. Пишешь короткие сочные посты для Telegram:
— стиль: дружелюбно, по делу, без воды; 350–700 символов;
— обязательно 1–2 эмодзи в начале абзацев, 3–6 релевантных хештегов в конце;
— явный CTA: записаться/написать в директ/в Телеграм; без “выкатываем” и канцелярита;
— не используй markdown ссылки, только текст; без лишних кавычек и CAPS LOCK.
- обязательно используй слоган: STAVFITNESS26 - твоё тело, твоё здоровье, твоя гармония
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
def post_kb(has_image: bool = False):
    rows = [
        [InlineKeyboardButton(text="✅ Утвердить и опубликовать", callback_data="approve")],
        [InlineKeyboardButton(text="🎲 Ещё вариант текста", callback_data="regen"),
         InlineKeyboardButton(text="✏️ Править", callback_data="edit")]
    ]
    if has_image:
        rows.append([
            InlineKeyboardButton(text="🖼 Ещё картинка", callback_data="regen_image"),
            InlineKeyboardButton(text="🗑 Удалить картинку", callback_data="remove_image"),
        ])
    else:
        rows.append([InlineKeyboardButton(text="🖼 Сгенерировать картинку", callback_data="image")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сделать черновик"), KeyboardButton(text="Сделать черновик с картинкой")],
            [KeyboardButton(text="План на неделю"), KeyboardButton(text="Статус")],
            [KeyboardButton(text="Автопост выкл/вкл")],
        ],
        resize_keyboard=True,
    )

async def setup_bot_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск бота"),
        BotCommand(command="menu", description="Показать меню"),
        BotCommand(command="setup", description="Настроить профиль"),
        BotCommand(command="draft", description="Сделать черновик"),
        BotCommand(command="plan_week", description="План на неделю"),
        BotCommand(command="schedule", description="Автопост ежедневно"),
        BotCommand(command="status", description="Статус"),
    ])

def only_admin(func):
    async def wrapper(event, *args, **kwargs):
        uid = event.from_user.id if isinstance(event, Message) else event.from_user.id
        if uid not in ADMIN_IDS:
            return await (event.answer if isinstance(event, Message) else event.message.answer)("Доступ только для администраторов.")
        # Пропускаем в целевой хэндлер только те kwargs, которые он реально принимает (например, command)
        sig = inspect.signature(func)
        allowed_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return await func(event, *args, **allowed_kwargs)
    return wrapper

# ---------- COMMANDS ----------
@dp.message(Command("start"))
@only_admin
async def start_cmd(m: Message, command: CommandObject):
    await m.answer(
        "Привет! Я SMM‑бот студии. /setup — профиль, /draft — черновик, /schedule HH:MM — автопост, /plan_week — контент‑план, /status.",
        reply_markup=main_menu_kb(),
    )

@dp.message(Command("menu"))
@only_admin
async def menu_cmd(m: Message):
    await m.answer("Меню:", reply_markup=main_menu_kb())

@dp.message(F.text == "Сделать черновик")
@only_admin
async def _mk_draft(m: Message):
    await draft_cmd(m, CommandObject(command="draft", args="kind=offer"))

@dp.message(F.text == "Сделать черновик с картинкой")
@only_admin
async def _mk_draft_with_img(m: Message):
    prof = await get_profile()
    text = await generate_post(prof, "offer", "")
    await add_draft("offer", text, image_prompt=None, image_bytes=None)
    await m.answer(f"<b>Черновик (offer):</b>\n\n{text}", reply_markup=post_kb(False))

@dp.message(F.text == "План на неделю")
@only_admin
async def _plan_week_btn(m: Message):
    await plan_week_cmd(m, CommandObject(command="plan_week", args=None))

@dp.message(F.text == "Статус")
@only_admin
async def _status_btn(m: Message):
    await status_cmd(m, CommandObject(command="status", args=None))

@dp.message(F.text == "Автопост выкл/вкл")
@only_admin
async def _toggle_autopost(m: Message):
    cur = await get_daily_time()
    if cur:
        await set_daily_time(None); reschedule_daily(None)
        await m.answer("Ежедневная автопубликация: выключена")
    else:
        time_str = "10:00"
        await set_daily_time(time_str); reschedule_daily(time_str)
        await m.answer(f"Ежедневная автопубликация: включена ({time_str})")


# ---------- Natural language draft handlers ----------
@dp.message(F.text.regexp(r"^черновик\s+(.+)$", flags=re.IGNORECASE))
@only_admin
async def nl_draft_ru(m: Message):
    theme = m.text.split(None, 1)[1].strip()
    if is_nsfw(theme):
        return await m.answer(
            "Не могу сгенерировать такой текст. Давай сформулируем по-спортивному: например, ‘растяжка приводящих мышц’, ‘наклон вперёд в бабочке’, ‘складка’."
        )
    prof = await get_profile()
    # Привязываем тему к тексту поста и сохраняем её для картинки
    extra = f"Тема поста: {theme}. Отрази тему в тексте."
    text = await generate_post(prof, "tip", extra)
    await add_draft("tip", text, image_prompt=theme, image_bytes=None)
    await m.answer(f"<b>Черновик (tip):</b>\n\n{text}", reply_markup=post_kb(False))


@dp.message(F.text.regexp(r"^draft\s+(.+)$", flags=re.IGNORECASE))
@only_admin
async def nl_draft_en(m: Message):
    theme = m.text.split(None, 1)[1].strip()
    if is_nsfw(theme):
        return await m.answer(
            "I can’t generate explicit content. Please rephrase in a sports/fitness way, e.g., ‘adductor stretch’, ‘seated butterfly forward fold’, ‘hamstring fold’."
        )
    prof = await get_profile()
    extra = f"Post theme: {theme}. Reflect the theme in the text."
    text = await generate_post(prof, "tip", extra)
    await add_draft("tip", text, image_prompt=theme, image_bytes=None)
    await m.answer(f"<b>Draft (tip):</b>\n\n{text}", reply_markup=post_kb(False))

# ---------- Natural language ANY-TEXT → draft ----------
@dp.message(
    F.text &
    F.text.regexp(r"^(?!/).+") &
    ~F.text.in_({
        "Сделать черновик",
        "Сделать черновик с картинкой",
        "План на неделю",
        "Статус",
        "Автопост выкл/вкл",
    })
)
@only_admin
async def nl_draft_any(m: Message):
    theme = m.text.strip()
    if is_nsfw(theme):
        return await m.answer(
            "Не могу сгенерировать такой текст. Перефразируй по‑спортивному (например: ‘растяжка приводящих’, ‘наклон в бабочке’, ‘складка’)."
        )
    prof = await get_profile()
    extra = f"Тема поста: {theme}. Отрази тему в тексте."
    text = await generate_post(prof, "tip", extra)
    await add_draft("tip", text, image_prompt=theme, image_bytes=None)
    await m.answer(f"<b>Черновик (tip):</b>\n\n{text}", reply_markup=post_kb(False))

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
    # NSFW guard for theme/extra
    theme = extra if extra else ""
    if 'theme' in locals() and theme and is_nsfw(theme):
        return await m.answer("Тема содержит неприемлемые выражения. Перефразируй в спортивных терминах (например: ‘растяжка приводящих’, ‘наклон в бабочке’, ‘складка’).")
    text = await generate_post(prof, kind, extra)
    await add_draft(kind, text, image_prompt=(extra or None), image_bytes=None)
    await m.answer(f"<b>Черновик ({kind}):</b>\n\n{text}", reply_markup=post_kb(False))

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
        await add_draft(k, text, image_prompt=None, image_bytes=None)
        await m.answer(f"<b>Черновик ({k}):</b>\n\n{text}", reply_markup=post_kb(False))

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
@dp.callback_query(F.data.in_({"approve","regen","edit","image","regen_image","remove_image"}))
async def on_cb(q: CallbackQuery):
    if q.from_user.id not in ADMIN_IDS:
        return await q.answer("Только админ.", show_alert=True)
    draft = await get_latest_draft()
    if not draft:
        return await q.message.answer("Нет черновика.")
    draft_id, kind, text, image_prompt, image_bytes = draft

    # Immediately answer the callback to avoid timeout
    await _safe_cb_answer(q, "⏳ Обрабатываю…")

    if q.data == "approve":
        await publish_to_channel(text, image_bytes if image_bytes else None)
        return await q.message.answer("Опубликовано ✅")

    if q.data == "regen":
        prof = await get_profile()
        new_text = await generate_post(prof, kind, "сделай другой угол и подачу")
        await add_draft(kind, new_text, image_prompt=None, image_bytes=None)
        return await q.message.answer(f"<b>Черновик ({kind}) — новый вариант:</b>\n\n{new_text}", reply_markup=post_kb(False))

    if q.data == "edit":
        await q.message.answer("Пришли новый текст одним сообщением. Я опубликую его.")
        dp.message.register(one_shot_publish)
        return

    if q.data == "image":
        prof = await get_profile()
        theme_text = f" Тема: {image_prompt}." if image_prompt else ""
        img_prompt = (
            f"Фитнес-студия {prof['name']}. Стиль: {prof['image_style']}. "
            f"Акцент: {', '.join(prof['services'][:2])}." + theme_text
        )
        if image_prompt and is_nsfw(image_prompt):
            return await q.message.answer("Тема черновика содержит неприемлемые формулировки для изображения. Перефразируй, и попробуем снова.")
        data, err = await generate_image_bytes(img_prompt)
        if err:
            return await q.message.answer("Не удалось добавить картинку:\n\n" + err)
        if not data:
            return await q.message.answer("Не удалось сгенерировать изображение")
        await set_draft_image(draft_id, data, img_prompt)
        return await q.message.answer_photo(photo=BufferedInputFile(data, filename="preview.png"), caption=text, reply_markup=post_kb(True))

    if q.data == "regen_image":
        prof = await get_profile()
        theme_text = f" Тема: {image_prompt}." if image_prompt else ""
        img_prompt = (
            f"Фитнес-студия {prof['name']}. Стиль: {prof['image_style']}. "
            f"Акцент: {', '.join(prof['services'][:2])}." + theme_text
        )
        if image_prompt and is_nsfw(image_prompt):
            return await q.message.answer("Тема черновика содержит неприемлемые формулировки для изображения. Перефразируй, и попробуем снова.")
        data, err = await generate_image_bytes(img_prompt)
        if err or not data:
            return await q.message.answer("Не удалось обновить картинку." + (f"\n\n{err}" if err else ""))
        await set_draft_image(draft_id, data, img_prompt)
        return await q.message.answer_photo(photo=BufferedInputFile(data, filename="preview.png"), caption=text, reply_markup=post_kb(True))

    if q.data == "remove_image":
        await set_draft_image(draft_id, None)
        return await q.message.answer(f"<b>Черновик ({kind}) без картинки:</b>\n\n{text}", reply_markup=post_kb(False))

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

# Middleware to print user info for every update
class LogUserIdMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            user = None
            if isinstance(event, Message) and event.from_user:
                user = event.from_user
            elif isinstance(event, CallbackQuery) and event.from_user:
                user = event.from_user
            if user is not None:
                print(
                    f"[USER] id={user.id} name={user.full_name} username=@{user.username}",
                    flush=True,
                )
                logging.info(
                    "USER id=%s name=%s username=@%s",
                    user.id,
                    user.full_name,
                    user.username,
                )
        except Exception as e:
            logging.debug("LogUserIdMiddleware error: %s", e)
        return await handler(event, data)

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
    await setup_bot_commands()
    dp.update.middleware(LogUserIdMiddleware())
    dp.message.middleware(LogUserIdMiddleware())
    dp.callback_query.middleware(LogUserIdMiddleware())
    # поднимем планировщик с текущим временем из БД
    hhmm = await get_daily_time()
    reschedule_daily(hhmm)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())