# -*- coding: utf-8 -*-
"""
Запуск бота на Render (бесплатный Web Service) через polling.

Render бесплатно даёт только веб-сервис (а не фоновый worker), который к тому же
«засыпает» без обращений. Поэтому здесь:
  1) бот работает через polling в отдельном потоке (ИИ-ответы доступны — белого
     списка нет, в отличие от PythonAnywhere);
  2) параллельно поднят крошечный веб-сервер, который слушает порт Render и
     отвечает «OK» — это нужно, чтобы Render считал сервис «живым».

Опрос обёрнут в self-healing цикл: при кратковременной ошибке 409 (бывает в момент
передеплоя, когда старый и новый экземпляры на секунду пересекаются) поток не падает
насовсем, а повторяет попытку через 15 секунд — как только старый экземпляр
останавливается, опрос продолжается сам.

Чтобы сервис не засыпал, настройте внешний пинг (UptimeRobot / cron-job.org) на
адрес вашего сервиса раз в 5–10 минут. Подробности — в DEPLOY_Render_Railway.md.

Команда запуска на Render:  python run_render.py
"""

import os
import time
import logging
import threading

from flask import Flask

import bot as b  # импорт регистрирует обработчики на b.bot

log = logging.getLogger("run_render")
app = Flask(__name__)


@app.route("/")
def home():
    return "Бот приёмной комиссии работает (polling).", 200


@app.route("/health")
def health():
    return "ok", 200


def _start_polling():
    # Один процесс = один поток polling (иначе Telegram вернёт 409 Conflict).
    # Self-healing: при любой ошибке (включая временный 409 во время передеплоя)
    # ждём и пробуем снова, чтобы опрос не умирал насовсем.
    while True:
        try:
            b.bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.error("Опрос прервался (%s). Повтор через 15 секунд...", e)
            time.sleep(15)


if __name__ == "__main__":
    threading.Thread(target=_start_polling, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
