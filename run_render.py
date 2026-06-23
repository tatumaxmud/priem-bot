# -*- coding: utf-8 -*-
"""
Запуск бота на Render (бесплатный Web Service) через polling.

Render бесплатно даёт только веб-сервис (а не фоновый worker), который к тому же
«засыпает» без обращений. Поэтому здесь:
  1) бот работает через polling в отдельном потоке (ИИ-ответы доступны — белого
     списка нет, в отличие от PythonAnywhere);
  2) параллельно поднят крошечный веб-сервер, который слушает порт Render и
     отвечает «OK» — это нужно, чтобы Render считал сервис «живым».

Чтобы сервис не засыпал, настройте внешний пинг (UptimeRobot / cron-job.org) на
адрес вашего сервиса раз в 5–10 минут. Подробности — в DEPLOY_Render_Railway.md.

Команда запуска на Render:  python run_render.py
"""

import os
import threading

from flask import Flask

import bot as b  # импорт регистрирует обработчики на b.bot

app = Flask(__name__)


@app.route("/")
def home():
    return "Бот приёмной комиссии работает (polling).", 200


@app.route("/health")
def health():
    return "ok", 200


def _start_polling():
    # Один процесс = один поток polling (важно: иначе Telegram вернёт 409 Conflict)
    b.bot.infinity_polling(skip_pending=True, timeout=30)


if __name__ == "__main__":
    threading.Thread(target=_start_polling, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
