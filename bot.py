# -*- coding: utf-8 -*-
"""
Telegram-бот приёмной комиссии (24/7) с ИИ-ответами по базе знаний и админ-панелью.
Филиал АГТУ в Ташкентской области Республики Узбекистан.

Хранилище статистики:
  - по умолчанию — локальный файл SQLite (bot.db). На бесплатном Render он
    обнуляется при перезапуске.
  - если задана переменная окружения DATABASE_URL (PostgreSQL, например Neon),
    статистика хранится в ней и НЕ теряется при перезапуске.

Запуск:  python bot.py   (или run_render.py на Render)
Настройки берутся из переменных окружения (файл .env). См. README.md.
"""

import os
import re
import logging
import threading
from datetime import datetime

import telebot
from telebot import types

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ------------------------- Конфигурация -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "").strip()) if x.isdigit()
}
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1").strip()
AI_MODEL = os.getenv("AI_MODEL", "llama-3.3-70b-versatile").strip()
KNOWLEDGE_FILE = os.getenv("KNOWLEDGE_FILE", "knowledge.md").strip()
DB_FILE = os.getenv("DB_FILE", "bot.db").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Создайте файл .env (см. .env.example) и укажите токен.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("priem-bot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ------------------------- База знаний -------------------------
KNOWLEDGE = ""
SECTIONS = {}


def load_knowledge():
    """Читает knowledge.md и разбивает на разделы по заголовкам '## '."""
    global KNOWLEDGE, SECTIONS
    path = KNOWLEDGE_FILE if os.path.isabs(KNOWLEDGE_FILE) else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), KNOWLEDGE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            KNOWLEDGE = f.read()
    except FileNotFoundError:
        KNOWLEDGE = ""
        log.warning("Файл базы знаний не найден: %s", path)
        return
    SECTIONS = {}
    cur_title, cur_lines = None, []
    for line in KNOWLEDGE.splitlines():
        m = re.match(r"^##\s+(.*)", line)
        if m:
            if cur_title:
                SECTIONS[cur_title] = "\n".join(cur_lines).strip()
            cur_title, cur_lines = m.group(1).strip(), []
        elif cur_title:
            cur_lines.append(line)
    if cur_title:
        SECTIONS[cur_title] = "\n".join(cur_lines).strip()
    log.info("База знаний загружена: %d разделов, %d символов", len(SECTIONS), len(KNOWLEDGE))


load_knowledge()

MENU = [
    ("📅 Сроки приёма", "Сроки приёма"),
    ("💰 Стоимость обучения", "Стоимость обучения"),
    ("🎓 Направления подготовки", "Направления подготовки"),
    ("📊 План приёма и места", "План приёма"),
    ("📝 Вступительные испытания", "Вступительные испытания"),
    ("📄 Документы", "Необходимые документы"),
    ("✅ Зачисление", "Конкурс и зачисление"),
    ("📞 Контакты", "Контакты"),
]


def find_section(keyword):
    for title, text in SECTIONS.items():
        if keyword.lower() in title.lower():
            return title, text
    return None, None


# ------------------------- Хранилище (SQLite или PostgreSQL) -------------------------
_db_lock = threading.Lock()
USE_PG = DATABASE_URL.startswith("postgres")
PH = "%s" if USE_PG else "?"  # стиль плейсхолдеров

if USE_PG:
    import psycopg2
    log.info("Хранилище: PostgreSQL (постоянное)")
else:
    import sqlite3
    log.info("Хранилище: SQLite (%s)", DB_FILE)


def get_conn():
    if USE_PG:
        # URL уже содержит sslmode=require; передаём строку целиком без доп. kwargs,
        # иначе psycopg2 неверно разбирает URI (берёт имя пользователя за хост).
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(DB_FILE)


def init_db():
    serial = "SERIAL" if USE_PG else "INTEGER"
    bigint = "BIGINT" if USE_PG else "INTEGER"
    with _db_lock:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"""CREATE TABLE IF NOT EXISTS users(
                id {bigint} PRIMARY KEY, username TEXT, first_name TEXT,
                joined TEXT, last_seen TEXT, msg_count INTEGER DEFAULT 0)""")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS questions(
                id {serial} PRIMARY KEY, user_id {bigint}, text TEXT, ts TEXT)""")
            conn.commit()
        finally:
            conn.close()


def track(user):
    now = datetime.utcnow().isoformat(timespec="seconds")
    q = (f"INSERT INTO users(id, username, first_name, joined, last_seen, msg_count) "
         f"VALUES({PH},{PH},{PH},{PH},{PH},1) "
         f"ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, "
         f"msg_count=users.msg_count+1, username=excluded.username, "
         f"first_name=excluded.first_name")
    with _db_lock:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(q, (user.id, user.username, user.first_name, now, now))
            conn.commit()
        except Exception as e:
            log.error("track error: %s", e)
        finally:
            conn.close()


def log_question(user_id, text):
    now = datetime.utcnow().isoformat(timespec="seconds")
    with _db_lock:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"INSERT INTO questions(user_id, text, ts) VALUES({PH},{PH},{PH})",
                        (user_id, text[:1000], now))
            conn.commit()
        except Exception as e:
            log.error("log_question error: %s", e)
        finally:
            conn.close()


def all_user_ids():
    with _db_lock:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM users")
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()


def stats():
    with _db_lock:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM users")
            users = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(SUM(msg_count),0) FROM users")
            msgs = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM questions")
            questions = cur.fetchone()[0]
            cur.execute("SELECT text, ts FROM questions ORDER BY id DESC LIMIT 5")
            last = cur.fetchall()
            return users, msgs, questions, last
        finally:
            conn.close()


def list_users(limit=50):
    with _db_lock:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT first_name, username, msg_count, joined "
                        f"FROM users ORDER BY msg_count DESC LIMIT {int(limit)}")
            return cur.fetchall()
        finally:
            conn.close()


init_db()

# ------------------------- ИИ -------------------------
SYSTEM_PROMPT = (
    "Ты — вежливый помощник приёмной комиссии Филиала АГТУ в Ташкентской области "
    "Республики Узбекистан. Отвечай на вопросы абитуриентов кратко, дружелюбно и по делу, "
    "ТОЛЬКО на основании приведённой ниже базы знаний. Если в базе нет ответа — честно скажи, "
    "что точной информации нет, и предложи обратиться в приёмную комиссию (контакты есть в базе). "
    "Не выдумывай факты, цифры, даты и стоимость. Отвечай на русском языке.\n\n"
    "=== БАЗА ЗНАНИЙ ===\n{kb}"
)

_ai_client = None
if AI_API_KEY:
    try:
        from openai import OpenAI
        _ai_client = OpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
        log.info("ИИ подключён: %s (%s)", AI_MODEL, AI_BASE_URL)
    except Exception as e:
        log.warning("Не удалось инициализировать ИИ-клиент: %s", e)


def ai_answer(question):
    if not _ai_client:
        return None
    try:
        resp = _ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT.format(kb=KNOWLEDGE)},
                {"role": "user", "content": question},
            ],
            temperature=0.2, max_tokens=700,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("Ошибка ИИ: %s", e)
        return None


INTENT_HINTS = [
    (("зачисл", "конкурс", "рейтинг", "договор"), "Конкурс и зачисление"),
    (("документ", "паспорт", "заявлен", "фото", "подать", "подач", "сдать докум"), "Необходимые документы"),
    (("стоим", "цена", "стоит", "сколько", "оплат", "сум"), "Стоимость обучения"),
    (("срок", "когда", "дата", "число", "начало"), "Сроки приёма"),
    (("экзам", "испыт", "балл", "тест", "биолог", "матем", "русск", "собесед"), "Вступительные испытания"),
    (("направлен", "специальн", "профил", "програм", "факультет"), "Направления подготовки"),
    (("грант", "бюджет", "место", "мест", "план приёма", "контракт"), "План приёма"),
    (("контакт", "телефон", "адрес", "почта", "e-mail", "email", "сайт", "связ", "позвон"), "Контакты"),
    (("магистрат",), "Направления подготовки"),
    (("дополнит", "ноябр"), "Дополнительный приём"),
]


def keyword_fallback(question):
    q = question.lower()
    for triggers, key in INTENT_HINTS:
        if any(t in q for t in triggers):
            title, text = find_section(key)
            if text:
                return f"<b>{title}</b>\n{text}"
    words = [w[:5] for w in re.findall(r"\w+", q) if len(w) > 3]
    best, score = None, 0
    for title, text in SECTIONS.items():
        hay = (title + " " + text).lower()
        s = sum(1 for stem in words if stem in hay)
        if s > score:
            best, score = (title, text), s
    if best and score:
        return f"<b>{best[0]}</b>\n{best[1]}"
    return ("Я не нашёл точного ответа в базе. Воспользуйтесь меню /start "
            "или обратитесь в приёмную комиссию (кнопка «Контакты»).")


# ------------------------- Клавиатуры -------------------------
def main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    btns = [types.InlineKeyboardButton(label, callback_data=f"sec::{key}") for label, key in MENU]
    kb.add(*btns)
    kb.add(types.InlineKeyboardButton("💬 Задать свой вопрос", callback_data="ask"))
    return kb


def send_long(chat_id, text, **kw):
    for i in range(0, len(text), 4000):
        bot.send_message(chat_id, text[i:i + 4000], **kw)


# ------------------------- Хендлеры -------------------------
@bot.message_handler(commands=["start", "menu"])
def cmd_start(m):
    track(m.from_user)
    bot.send_message(
        m.chat.id,
        "👋 Здравствуйте! Я бот приёмной кампании <b>2026/2027</b>\n"
        "Филиала АГТУ в Ташкентской области Республики Узбекистан.\n\n"
        "Выберите раздел или задайте свой вопрос — я отвечу по официальной информации о приёме.",
        reply_markup=main_menu(),
    )


@bot.message_handler(commands=["help"])
def cmd_help(m):
    track(m.from_user)
    txt = ("ℹ️ <b>Как пользоваться</b>\n"
           "• /start — главное меню с разделами\n"
           "• Просто напишите вопрос своими словами — я отвечу.\n\n"
           "Примеры: «Когда начинается приём?», «Сколько стоит экономика?»")
    if m.from_user.id in ADMIN_IDS:
        txt += ("\n\n🔐 <b>Команды администратора</b>\n"
                "• /admin — панель администратора\n"
                "• /stats — статистика\n"
                "• /users — список пользователей\n"
                "• /reload — перечитать базу знаний\n"
                "• /broadcast текст — рассылка всем пользователям")
    bot.send_message(m.chat.id, txt)


def is_admin(uid):
    return uid in ADMIN_IDS


@bot.message_handler(commands=["admin"])
def cmd_admin(m):
    if not is_admin(m.from_user.id):
        return
    u, msgs, q, _ = stats()
    storage = "PostgreSQL (постоянно)" if USE_PG else "SQLite (сбрасывается при перезапуске)"
    bot.send_message(
        m.chat.id,
        f"🔐 <b>Панель администратора</b>\n\n"
        f"👥 Пользователей: <b>{u}</b>\n"
        f"💬 Сообщений: <b>{msgs}</b>\n"
        f"❓ Вопросов задано: <b>{q}</b>\n\n"
        f"Команды: /stats, /users, /broadcast, /reload\n"
        f"ИИ: {'включён ✅' if _ai_client else 'выключен ❌ (поиск по базе)'}\n"
        f"Хранилище: {storage}",
    )


@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not is_admin(m.from_user.id):
        return
    u, msgs, q, last = stats()
    txt = f"📊 <b>Статистика</b>\n👥 {u} | 💬 {msgs} | ❓ {q}\n\n<b>Последние вопросы:</b>\n"
    txt += "\n".join(f"• {t}" for t, ts in last) or "—"
    bot.send_message(m.chat.id, txt)


@bot.message_handler(commands=["users"])
def cmd_users(m):
    if not is_admin(m.from_user.id):
        return
    rows = list_users(50)
    if not rows:
        bot.send_message(m.chat.id, "Пока нет пользователей.")
        return
    lines = ["👥 <b>Пользователи бота</b> (топ по активности):", ""]
    for i, (first, username, cnt, joined) in enumerate(rows, 1):
        name = first or "—"
        uname = f" (@{username})" if username else ""
        day = (joined or "")[:10]
        lines.append(f"{i}. {name}{uname} — {cnt} сообщ.{' · с ' + day if day else ''}")
    send_long(m.chat.id, "\n".join(lines))


@bot.message_handler(commands=["reload"])
def cmd_reload(m):
    if not is_admin(m.from_user.id):
        return
    load_knowledge()
    bot.send_message(m.chat.id, f"♻️ База знаний перечитана: {len(SECTIONS)} разделов.")


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(m):
    if not is_admin(m.from_user.id):
        return
    text = m.text.partition(" ")[2].strip()
    if not text:
        bot.send_message(m.chat.id, "Использование: /broadcast ваш текст сообщения")
        return
    ids = all_user_ids()
    sent = failed = 0
    for uid in ids:
        try:
            bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
    bot.send_message(m.chat.id, f"📣 Рассылка завершена. Отправлено: {sent}, не доставлено: {failed}.")


@bot.callback_query_handler(func=lambda c: True)
def on_callback(c):
    track(c.from_user)
    if c.data == "ask":
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "✍️ Напишите ваш вопрос одним сообщением.")
        return
    if c.data.startswith("sec::"):
        key = c.data.split("::", 1)[1]
        title, text = find_section(key)
        bot.answer_callback_query(c.id)
        if text:
            send_long(c.message.chat.id, f"<b>{title}</b>\n{text}")
        else:
            bot.send_message(c.message.chat.id, "Раздел пока не заполнен.")


@bot.message_handler(content_types=["text"])
def on_text(m):
    track(m.from_user)
    log_question(m.from_user.id, m.text)
    bot.send_chat_action(m.chat.id, "typing")
    answer = ai_answer(m.text) or keyword_fallback(m.text)
    send_long(m.chat.id, answer)


if __name__ == "__main__":
    log.info("Бот запущен. Админы: %s", ADMIN_IDS or "не заданы")
    bot.infinity_polling(skip_pending=True, timeout=30)
