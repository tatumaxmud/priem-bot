# -*- coding: utf-8 -*-
"""
Telegram-бот приёмной комиссии (24/7) с ИИ-ответами по базе знаний и админ-панелью.
Филиал АГТУ в Ташкентской области Республики Узбекистан.

Запуск:  python bot.py
Все настройки берутся из переменных окружения (файл .env). См. README.md.
"""

import os
import re
import sqlite3
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

if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Создайте файл .env (см. .env.example) и укажите токен.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("priem-bot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ------------------------- База знаний -------------------------
KNOWLEDGE = ""
SECTIONS = {}  # {"Заголовок": "текст раздела"}


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

# Кнопка меню -> ключевое слово для поиска раздела в knowledge.md
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


# ------------------------- Хранилище (SQLite) -------------------------
_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        joined TEXT, last_seen TEXT, msg_count INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS questions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        text TEXT, ts TEXT)""")
    return conn


def track(user):
    now = datetime.utcnow().isoformat(timespec="seconds")
    with _db_lock, db() as conn:
        row = conn.execute("SELECT id FROM users WHERE id=?", (user.id,)).fetchone()
        if row:
            conn.execute("UPDATE users SET last_seen=?, msg_count=msg_count+1, username=?, first_name=? WHERE id=?",
                         (now, user.username, user.first_name, user.id))
        else:
            conn.execute("INSERT INTO users(id, username, first_name, joined, last_seen, msg_count) VALUES(?,?,?,?,?,1)",
                         (user.id, user.username, user.first_name, now, now))


def log_question(user_id, text):
    with _db_lock, db() as conn:
        conn.execute("INSERT INTO questions(user_id, text, ts) VALUES(?,?,?)",
                     (user_id, text[:1000], datetime.utcnow().isoformat(timespec="seconds")))


def all_user_ids():
    with _db_lock, db() as conn:
        return [r[0] for r in conn.execute("SELECT id FROM users").fetchall()]


def stats():
    with _db_lock, db() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        msgs = conn.execute("SELECT COALESCE(SUM(msg_count),0) FROM users").fetchone()[0]
        questions = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        last = conn.execute("SELECT text, ts FROM questions ORDER BY id DESC LIMIT 5").fetchall()
    return users, msgs, questions, last


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
    """Возвращает ответ ИИ или None, если ИИ недоступен/ошибка."""
    if not _ai_client:
        return None
    try:
        resp = _ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT.format(kb=KNOWLEDGE)},
                {"role": "user", "content": question},
            ],
            temperature=0.2,
            max_tokens=700,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("Ошибка ИИ: %s", e)
        return None


# Подсказки тема -> ключевое слово раздела (для запасного поиска без ИИ)
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
    """Простой поиск по разделам, если ИИ недоступен."""
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
            "или обратитесь в приёмную комиссию (контакты — кнопка «Контакты»).")


# ------------------------- Клавиатуры -------------------------
def main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    btns = [types.InlineKeyboardButton(label, callback_data=f"sec::{key}") for label, key in MENU]
    kb.add(*btns)
    kb.add(types.InlineKeyboardButton("💬 Задать свой вопрос", callback_data="ask"))
    return kb


def send_long(chat_id, text, **kw):
    """Отправляет длинный текст частями (лимит Telegram 4096)."""
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
           "Примеры: «Когда начинается приём?», «Сколько стоит экономика?», "
           "«Какие экзамены на аквакультуру?»")
    if m.from_user.id in ADMIN_IDS:
        txt += ("\n\n🔐 <b>Команды администратора</b>\n"
                "• /admin — панель администратора\n"
                "• /stats — статистика\n"
                "• /reload — перечитать базу знаний\n"
                "• /broadcast текст — рассылка всем пользователям")
    bot.send_message(m.chat.id, txt)


# ----- Админ -----
def is_admin(uid):
    return uid in ADMIN_IDS


@bot.message_handler(commands=["admin"])
def cmd_admin(m):
    if not is_admin(m.from_user.id):
        return
    u, msgs, q, _ = stats()
    bot.send_message(
        m.chat.id,
        f"🔐 <b>Панель администратора</b>\n\n"
        f"👥 Пользователей: <b>{u}</b>\n"
        f"💬 Сообщений: <b>{msgs}</b>\n"
        f"❓ Вопросов задано: <b>{q}</b>\n\n"
        f"Команды:\n"
        f"• /stats — подробная статистика\n"
        f"• /reload — обновить базу знаний\n"
        f"• /broadcast текст — отправить сообщение всем\n"
        f"ИИ: {'включён ✅ (' + AI_MODEL + ')' if _ai_client else 'выключен ❌ (работает поиск по базе)'}",
    )


@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not is_admin(m.from_user.id):
        return
    u, msgs, q, last = stats()
    txt = f"📊 <b>Статистика</b>\n👥 {u} | 💬 {msgs} | ❓ {q}\n\n<b>Последние вопросы:</b>\n"
    txt += "\n".join(f"• {t}" for t, ts in last) or "—"
    bot.send_message(m.chat.id, txt)


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


# ----- Кнопки -----
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


# ----- Свободные вопросы -----
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
