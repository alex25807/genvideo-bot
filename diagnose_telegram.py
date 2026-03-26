"""
Диагностика связи с Telegram (без запуска бота).

Запуск из корня проекта:
  python diagnose_telegram.py

Показывает: getMe, getWebhookInfo — так видно, доступен ли api.telegram.org
и не перехватывает ли апдейты webhook.
"""
from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("Ошибка: в .env не задан TELEGRAM_BOT_TOKEN")
        return 1

    timeout = float(os.getenv("TELEGRAM_HTTP_TIMEOUT", "45"))
    base = f"https://api.telegram.org/bot{token}"

    print(f"Запросы к api.telegram.org (timeout={timeout}s)...\n")

    try:
        r = requests.get(f"{base}/getMe", timeout=timeout)
        data = r.json()
        print("getMe:", r.status_code, data)
        if not data.get("ok"):
            print("\nТокен невалид или отказ API.")
            return 2
        res = data.get("result") or {}
        print(f"\nБот: @{res.get('username')} id={res.get('id')}")
    except requests.exceptions.RequestException as exc:
        print("\ngetMe НЕ УДАЛСЯ:", type(exc).__name__, exc)
        print(
            "\nЭто значит: с этой машины/сети до api.telegram.org сейчас нет стабильного доступа.\n"
            "Сайт (Flask на localhost) при этом может открываться — он не ходит в Telegram.\n"
            "Попробуйте: другая сеть, VPN, прокси (HTTPS_PROXY в .env), мобильный интернет."
        )
        return 3

    try:
        r = requests.get(f"{base}/getWebhookInfo", timeout=timeout)
        data = r.json()
        print("\ngetWebhookInfo:", r.status_code, data)
        res = data.get("result") or {}
        url = (res.get("url") or "").strip()
        pending = res.get("pending_update_count")
        if url:
            print(
                f"\n[!] Включён webhook: {url!r}\n"
                "Пока он активен, long polling (ваш бот) обычно НЕ получает входящие.\n"
                "При старте telegram_bot.py webhook теперь снимается автоматически."
            )
        else:
            print("\nWebhook пустой — для getUpdates/long polling это нормально.")
        if pending is not None:
            print(f"pending_update_count: {pending}")
    except requests.exceptions.RequestException as exc:
        print("\ngetWebhookInfo НЕ УДАЛСЯ:", type(exc).__name__, exc)
        return 4

    allowed = (os.getenv("TELEGRAM_ALLOWED_CHAT_ID") or "").strip()
    if allowed:
        print(f"\nВ .env задан TELEGRAM_ALLOWED_CHAT_ID={allowed} — сообщения принимаются только от этого chat_id.")
    else:
        print("\nTELEGRAM_ALLOWED_CHAT_ID не задан — бот отвечает всем.")

    print("\nГотово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
