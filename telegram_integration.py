"""
Утилиты для интеграции прогресса генерации в Telegram.

По умолчанию callback ничего не отправляет. Чтобы подключить бота,
передайте функцию sender(text), которая отправляет сообщение в чат.
"""

import requests
from typing import Callable, Optional


def render_progress_bar(progress: int, width: int = 20) -> str:
    progress = max(0, min(progress, 100))
    filled = int((progress / 100) * width)
    return "█" * filled + "░" * (width - filled)


def build_progress_message(progress: int, status: str) -> str:
    status_text = {
        "queued": "в очереди",
        "in_progress": "генерация",
        "completed": "завершено",
        "failed": "ошибка",
    }.get(status, status)
    bar = render_progress_bar(progress)
    return f"Видео: {status_text}\n[{bar}] {progress}%"


def make_telegram_progress_callback(
    sender: Optional[Callable[[str], None]] = None,
    min_step: int = 5,
) -> Callable[[int, str], None]:
    """
    Возвращает callback(progress, status) для SoraClient/main.py.

    sender: функция отправки текста в Telegram.
            Например, lambda text: bot.send_message(chat_id=..., text=text)
    min_step: минимальный шаг изменения прогресса в %, чтобы не спамить чат.
    """
    last_sent = {"progress": -1, "status": ""}

    def callback(progress: int, status: str) -> None:
        progress = max(0, min(progress, 100))
        should_send = (
            status != last_sent["status"]
            or progress == 100
            or last_sent["progress"] < 0
            or progress - last_sent["progress"] >= min_step
        )
        if not should_send:
            return

        message = build_progress_message(progress, status)
        if sender is not None:
            sender(message)

        last_sent["progress"] = progress
        last_sent["status"] = status

    return callback


class TelegramProgressReporter:
    """
    Отправляет прогресс в Telegram и редактирует одно сообщение.
    """

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.message_id: Optional[int] = None

    def _post(self, method: str, payload: dict) -> dict:
        resp = requests.post(
            f"{self.base_url}/{method}",
            json=payload,
            timeout=self.timeout,
        )
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"Telegram API error (HTTP {resp.status_code}): {resp.text}")
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data

    def send_or_edit(self, text: str) -> None:
        self.send_or_edit_with_markup(text=text, reply_markup=None)

    def send_or_edit_with_markup(self, text: str, reply_markup: Optional[dict]) -> None:
        if self.message_id is None:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            data = self._post(
                "sendMessage",
                payload,
            )
            self.message_id = data["result"]["message_id"]
            return

        payload = {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        self._post("editMessageText", payload)

    def action_keyboard(self) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "Повторить", "callback_data": "retry_last_generation"},
                    {"text": "Отменить", "callback_data": "cancel_generation"},
                ]
            ]
        }

    def send_final_with_actions(self, text: str) -> None:
        self.send_or_edit_with_markup(text=text, reply_markup=self.action_keyboard())
