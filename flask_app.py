# -*- coding: utf-8 -*-
"""
Webhook-версия бота для БЕСПЛАТНОГО PythonAnywhere.

PythonAnywhere (бесплатный тариф) не позволяет держать постоянно работающий
процесс (polling), но даёт всегда доступное веб-приложение. Поэтому здесь бот
принимает сообщения через webhook: Telegram сам присылает обновления на адрес
вашего сайта https://<логин>.pythonanywhere.com/<секрет>.

Файл импортирует обработчики из bot.py (там же — база знаний, админ-команды,
поиск по базе). На бесплатном тарифе ИИ-ответы недоступны (api.groq.com вне
белого списка), бот отвечает через меню и поиск по базе — это работает.

В настройках веб-приложения PythonAnywhere WSGI-файл должен импортировать
переменную `application` из этого модуля.
"""

import os
import telebot
from flask import Flask, request, abort

import bot as b  # импорт регистрирует все обработчики на b.bot

app = Flask(__name__)
application = app  # PythonAnywhere ищет объект `application`

# Секрет в адресе webhook (защита от посторонних запросов)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret").strip()


@app.route("/", methods=["GET"])
def index():
    return "Бот приёмной комиссии работает.", 200


@app.route("/" + WEBHOOK_SECRET, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        raw = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(raw)
        b.bot.process_new_updates([update])
        return "ok", 200
    abort(403)
