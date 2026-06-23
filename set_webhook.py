# -*- coding: utf-8 -*-
"""
Регистрация (или удаление) webhook у Telegram.

Запускать ОДИН РАЗ из консоли PythonAnywhere после настройки веб-приложения:

    python set_webhook.py

Перед запуском задайте переменные окружения (или впишите в .env):
    BOT_TOKEN         — токен бота
    WEBHOOK_SECRET    — тот же секрет, что и в flask_app.py
    PA_USERNAME       — ваш логин на PythonAnywhere (для адреса сайта)
или сразу полный адрес:
    WEBHOOK_URL=https://<логин>.pythonanywhere.com/<секрет>

Чтобы удалить webhook (например, чтобы вернуться к polling):
    python set_webhook.py delete
"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import telebot

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SECRET = os.getenv("WEBHOOK_SECRET", "secret").strip()
USERNAME = os.getenv("PA_USERNAME", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()

if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN.")

if not WEBHOOK_URL:
    if not USERNAME:
        raise SystemExit("Задайте WEBHOOK_URL или PA_USERNAME (логин PythonAnywhere).")
    WEBHOOK_URL = f"https://{USERNAME}.pythonanywhere.com/{SECRET}"

bot = telebot.TeleBot(BOT_TOKEN)

bot.remove_webhook()

if len(sys.argv) > 1 and sys.argv[1] == "delete":
    print("Webhook удалён. Теперь можно использовать polling (python bot.py).")
else:
    bot.set_webhook(url=WEBHOOK_URL)
    print("Webhook установлен на:", WEBHOOK_URL)
    print("Проверка:", bot.get_webhook_info())
