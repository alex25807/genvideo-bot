"""
Telegram-бот для генерации видео через ProxyAPI + SORA.

Как использовать:
1) Заполнить .env:
   PROXYAPI_KEY=...
   TELEGRAM_BOT_TOKEN=...
2) Запустить:
   python telegram_bot.py

В чате с ботом:
- отправьте prompt, затем выберите параметры кнопками и нажмите "Запустить"
- /status
- /help
"""

import os
import threading
import time
import json
import uuid
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from billing import (
    apply_payment_credits_if_needed as billing_apply_payment_credits_if_needed,
    add_credits as billing_add_credits,
    consume_credits as billing_consume_credits,
    create_payment as billing_create_payment,
    generation_credit_cost as billing_generation_credit_cost,
    get_client_token_for_user as billing_get_client_token_for_user,
    get_credits as billing_get_credits,
    get_last_payment_for_user as billing_get_last_payment_for_user,
    init_billing,
    set_client_token as billing_set_client_token,
    set_credits as billing_set_credits,
    update_payment as billing_update_payment,
)
from payments_tbank import TBankClient
from providers import VideoRequest
from services import (
    build_orchestrator,
    ensure_provider_available,
    list_provider_capabilities,
    normalize_provider,
    stub_provider_message,
    validate_generation_params,
)
from sora_client import SoraClient, SoraError
from telegram_integration import make_telegram_progress_callback

load_dotenv()


class TelegramApi:
    def __init__(self, bot_token: str, timeout: float = 20.0):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{bot_token}"
        self.timeout = timeout

    def _post(self, method: str, payload: dict, timeout: Optional[float] = None) -> dict:
        resp = requests.post(
            f"{self.base_url}/{method}",
            json=payload,
            timeout=timeout if timeout is not None else self.timeout,
        )
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"Telegram API error (HTTP {resp.status_code}): {resp.text}")
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data

    def get_updates(self, offset: Optional[int], timeout_sec: int = 25) -> list:
        payload = {"timeout": timeout_sec}
        if offset is not None:
            payload["offset"] = offset
        # Клиентский таймаут long polling: чуть больше server-side timeout + запас под SSL.
        client_timeout = max(float(timeout_sec) + 15.0, float(self.timeout))
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                data = self._post("getUpdates", payload, timeout=client_timeout)
                return data.get("result", [])
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.SSLError,
            ) as exc:
                last_err = exc
                if attempt < 3:
                    time.sleep(1.5 * attempt)
        raise last_err  # type: ignore[misc]

    def get_me(self) -> dict:
        return self._post("getMe", {})

    def get_webhook_info(self) -> dict:
        return self._post("getWebhookInfo", {})

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict:
        payload: dict = {}
        if drop_pending_updates:
            payload["drop_pending_updates"] = True
        return self._post("deleteWebhook", payload)

    def set_my_commands(self, commands: list[dict]) -> dict:
        return self._post("setMyCommands", {"commands": commands})

    def set_chat_menu_button(self, menu_button: dict) -> dict:
        return self._post("setChatMenuButton", {"menu_button": menu_button})

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> int:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                data = self._post("sendMessage", payload)
                return data["result"]["message_id"]
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.SSLError,
            ) as exc:
                last_err = exc
                if attempt < 3:
                    time.sleep(1.5 * attempt)
        raise last_err  # type: ignore[misc]

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[dict] = None,
    ) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._post("editMessageText", payload)

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._post("answerCallbackQuery", payload)

    def send_video(
        self,
        chat_id: int,
        video_path: Path,
        caption: str,
        attempts: int = 3,
        upload_timeout_sec: int = 600,
        send_as_document: bool = False,
    ) -> None:
        last_error: Optional[Exception] = None
        method = "sendDocument" if send_as_document else "sendVideo"
        file_field = "document" if send_as_document else "video"
        for attempt in range(1, attempts + 1):
            try:
                with open(video_path, "rb") as f:
                    resp = requests.post(
                        f"{self.base_url}/{method}",
                        data={"chat_id": chat_id, "caption": caption},
                        files={file_field: f},
                        timeout=(20, upload_timeout_sec),
                    )
                if not (200 <= resp.status_code < 300):
                    raise RuntimeError(
                        f"Telegram {method} error (HTTP {resp.status_code}): {resp.text}"
                    )
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegram {method} error: {data}")
                return
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(2 * attempt)

        raise RuntimeError(f"Не удалось отправить видео в Telegram: {last_error}")

    def get_file_path(self, file_id: str) -> str:
        data = self._post("getFile", {"file_id": file_id})
        result = data.get("result") or {}
        file_path = result.get("file_path")
        if not file_path:
            raise RuntimeError(f"Telegram getFile не вернул file_path для file_id={file_id}")
        return str(file_path)

    def download_file(self, file_path: str, target_path: Path) -> Path:
        resp = requests.get(f"{self.file_base_url}/{file_path}", stream=True, timeout=self.timeout)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"Ошибка скачивания файла из Telegram (HTTP {resp.status_code}): {resp.text}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return target_path


def parse_command(text: str) -> tuple[str, Optional[str]]:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else None
    return cmd, arg


def build_help() -> str:
    return (
        "Шаги:\n"
        "1) Отправь prompt текстом\n"
        "2) Выбери seconds/model/size кнопками\n"
        "3) Нажми «Запустить»\n\n"
        "Команды:\n"
        "/credits — баланс кредитов\n"
        "/buy — купить пакет кредитов\n"
        "/paycheck — проверить статус последней оплаты\n"
        "/web — открыть web c уже подставленным client_token\n"
        "/myid — показать chat_id\n"
        "/admin [chat_id] — админ-панель кредитов\n"
        "/set_web_token <chat_id> <token> — привязать web токен к пользователю\n"
        "/remix <source_video_id> <prompt> — сделать ремикс завершенного видео\n"
        "/refs — показать текущие reference images в черновике\n"
        "/clearrefs — очистить reference images в черновике\n"
        "/status — текущие параметры\n"
        "/help — помощь"
    )


def control_keyboard(show_retry_sora: bool = False) -> dict:
    rows = [
        [
            {"text": "Повторить", "callback_data": "retry_last_generation"},
            {"text": "Отменить", "callback_data": "cancel_generation"},
        ]
    ]
    if show_retry_sora:
        rows.append([{"text": "🔁 Повторить в Sora", "callback_data": "retry_in_sora"}])
    return {"inline_keyboard": rows}


def start_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🤖 Генерировать в боте", "callback_data": "start:bot"},
                {"text": "🌐 Открыть веб", "callback_data": "start:web"},
            ]
        ]
    }


def command_keyboard() -> dict:
    # Постоянная клавиатура внизу чата: быстрый доступ к основным командам.
    return {
        "keyboard": [
            [{"text": "/start"}, {"text": "/web"}],
            [{"text": "/credits"}, {"text": "/buy"}],
            [{"text": "/status"}, {"text": "/help"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def admin_keyboard(target_chat_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Обновить", "callback_data": f"admin:refresh:{target_chat_id}"},
                {"text": "💳 Баланс", "callback_data": f"admin:view:{target_chat_id}"},
            ],
            [
                {"text": "+10", "callback_data": f"admin:add:{target_chat_id}:10"},
                {"text": "+50", "callback_data": f"admin:add:{target_chat_id}:50"},
                {"text": "+100", "callback_data": f"admin:add:{target_chat_id}:100"},
            ],
            [
                {"text": "+500", "callback_data": f"admin:add:{target_chat_id}:500"},
                {"text": "Сброс в 0", "callback_data": f"admin:set:{target_chat_id}:0"},
            ],
            [
                {"text": "✍️ Добавить вручную", "callback_data": f"admin:ask_add:{target_chat_id}"},
                {"text": "✍️ Установить вручную", "callback_data": f"admin:ask_set:{target_chat_id}"},
            ],
        ]
    }


def payment_packages_keyboard(packages: list[dict]) -> dict:
    rows = []
    for pkg in packages:
        rows.append(
            [
                {
                    "text": f"{pkg['title']} — {pkg['credits']} кр / {pkg['rub']} ₽",
                    "callback_data": f"pay:buy:{pkg['id']}",
                }
            ]
        )
    rows.append([{"text": "Проверить оплату", "callback_data": "pay:check:last"}])
    return {"inline_keyboard": rows}


def extract_available_balance(balance_data: dict) -> Optional[float]:
    value = balance_data.get("balance")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def compact_generation_error(err: Exception | str, max_len: int = 600) -> str:
    text = str(err or "").strip()
    low = text.lower()

    if "service_disabled" in low and "generativelanguage.googleapis.com" in low:
        return (
            "Veo временно недоступен у провайдера (Generative Language API disabled). "
            "Попробуй позже или переключись на Sora."
        )
    if "api_key_invalid" in low or "api key not valid" in low:
        return (
            "Veo временно недоступен: upstream отклонил ключ при скачивании результата. "
            "Попробуй позже или переключись на Sora."
        )

    if len(text) > max_len:
        return text[:max_len].rstrip() + " ...[сообщение сокращено]"
    return text


def parse_payment_packages() -> list[dict]:
    defaults = [
        {"id": "p5", "rub": 100, "credits": 5, "title": "Старт 5"},
        {"id": "p18", "rub": 300, "credits": 18, "title": "Базовый 18"},
        {"id": "p45", "rub": 700, "credits": 45, "title": "Профи 45"},
    ]
    raw = (os.getenv("PAYMENT_PACKAGES_JSON") or "").strip()
    if not raw:
        return defaults
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return defaults
        out = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            pkg_id = str(item.get("id") or "").strip()
            rub = float(item.get("rub") or 0)
            credits = int(item.get("credits") or 0)
            if not pkg_id or rub <= 0 or credits <= 0:
                continue
            out.append(
                {
                    "id": pkg_id,
                    "rub": rub,
                    "credits": credits,
                    "title": str(item.get("title") or pkg_id),
                }
            )
        return out or defaults
    except Exception:
        return defaults


def payment_status_label_ru(status: str | None) -> str:
    code = str(status or "UNKNOWN").strip().upper()
    labels = {
        "NEW": "Ожидает оплаты",
        "FORM_SHOWED": "Ожидает оплаты",
        "AUTHORIZED": "Авторизован",
        "CONFIRMED": "Оплачен",
        "REJECTED": "Отклонен",
        "CANCELED": "Отменен",
        "DEADLINE_EXPIRED": "Истек срок оплаты",
        "EXPIRED": "Истек срок оплаты",
        "INIT_FAILED": "Ошибка инициализации",
        "UNKNOWN": "Неизвестно",
    }
    return labels.get(code, code or "UNKNOWN")


def estimate_generation_cost_rub(
    settings: dict,
    rate_sora2: float,
    rate_sora2_pro: float,
    rate_veo31: float,
) -> float:
    provider = settings.get("provider", "sora")
    seconds = int(settings.get("seconds", 4))
    model = settings.get("model", "sora-2")
    if provider == "veo":
        return max(seconds * rate_veo31, 0.0)
    rate = rate_sora2_pro if model == "sora-2-pro" else rate_sora2
    return max(seconds * rate, 0.0)


def generation_credit_cost(settings: dict) -> int:
    seconds = int(settings.get("seconds", 4))
    model = settings.get("model", "sora-2")
    provider = settings.get("provider", "sora")
    return billing_generation_credit_cost(seconds=seconds, model=model, provider=provider)


def settings_text(draft: dict, credits_balance: int) -> str:
    prompt = draft.get("prompt", "")
    preview = prompt if len(prompt) <= 220 else f"{prompt[:220]}..."
    credits_need = generation_credit_cost(draft)
    refs_count = int(draft.get("reference_images_count", 0) or 0)
    return (
        "Проверь параметры перед генерацией:\n\n"
        f"Prompt:\n{preview}\n\n"
        f"provider: {draft.get('provider', 'sora')}\n"
        f"reference_type: {draft.get('reference_image_type', 'asset')}\n"
        f"reference_images: {refs_count}\n"
        f"seconds: {draft['seconds']}\n"
        f"model: {draft['model']}\n"
        f"size: {draft['size']}\n\n"
        f"Нужно кредитов: {credits_need}\n"
        f"Баланс кредитов: {credits_balance}"
    )


def settings_keyboard(
    draft: dict,
    provider_names: tuple[str, ...],
    provider_caps: dict,
) -> dict:
    def mark(value: str, current: str) -> str:
        return "✅" if value == current else "▫️"

    current_provider = normalize_provider(draft.get("provider"), default_provider="sora")
    caps = provider_caps.get(current_provider) or next(iter(provider_caps.values()))
    allowed_seconds = tuple(caps.allowed_seconds)
    allowed_models = tuple(caps.allowed_models)
    allowed_sizes = tuple(caps.allowed_sizes)
    allowed_ref_types = tuple(caps.supported_reference_types or ())

    sec_buttons = [
        {
            "text": f"{mark(str(v), str(draft['seconds']))} {v}s",
            "callback_data": f"cfg:set:seconds:{v}",
        }
        for v in allowed_seconds
    ]
    provider_buttons = [
        {
            "text": f"{mark(v, draft.get('provider', 'sora'))} {v}",
            "callback_data": f"cfg:set:provider:{v}",
        }
        for v in provider_names
    ]
    ref_type_buttons = [
        {
            "text": f"{mark(v, draft.get('reference_image_type', 'asset'))} ref:{v}",
            "callback_data": f"cfg:set:ref_type:{v}",
        }
        for v in allowed_ref_types
    ]
    model_buttons = [
        {
            "text": f"{mark(v, draft['model'])} {v}",
            "callback_data": f"cfg:set:model:{v}",
        }
        for v in allowed_models
    ]
    size_buttons = [
        {
            "text": f"{mark(v, draft['size'])} {v}",
            "callback_data": f"cfg:set:size:{v}",
        }
        for v in allowed_sizes
    ]

    size_rows = []
    for i in range(0, len(size_buttons), 2):
        size_rows.append(size_buttons[i : i + 2])

    return {
        "inline_keyboard": (
            [
            provider_buttons,
            ]
            + ([ref_type_buttons] if ref_type_buttons else [])
            + [
            sec_buttons,
            model_buttons,
            *size_rows,
            [
                {"text": "📎 Refs", "callback_data": "cfg:refs"},
                {"text": "🧹 Clear refs", "callback_data": "cfg:clearrefs"},
            ],
            [
                {"text": "▶️ Запустить", "callback_data": "cfg:start"},
                {"text": "✖️ Отмена", "callback_data": "cfg:cancel"},
            ],
            ]
        )
    }


def main() -> None:
    api_key = os.getenv("PROXYAPI_KEY")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed_chat_id_raw = os.getenv("TELEGRAM_ALLOWED_CHAT_ID")
    admin_chat_ids_raw = os.getenv("TELEGRAM_ADMIN_CHAT_IDS", "")

    if not api_key:
        raise RuntimeError("Не найден PROXYAPI_KEY в .env")
    if not bot_token:
        raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в .env")

    allowed_chat_id = int(allowed_chat_id_raw) if allowed_chat_id_raw else None
    admin_chat_ids = {
        int(item.strip())
        for item in admin_chat_ids_raw.split(",")
        if item.strip().lstrip("-").isdigit()
    }
    if allowed_chat_id is not None and not admin_chat_ids:
        # Удобный fallback: если админы явно не заданы, админом считается allowed_chat_id.
        admin_chat_ids.add(allowed_chat_id)

    client = SoraClient(api_key=api_key)
    http_timeout = float(os.getenv("TELEGRAM_HTTP_TIMEOUT", "45"))
    tg = TelegramApi(bot_token=bot_token, timeout=http_timeout)

    # Настройки по умолчанию можно переопределить в .env
    default_seconds = int(os.getenv("DEFAULT_SECONDS", "4"))
    default_model = os.getenv("DEFAULT_MODEL", "sora-2")
    default_size = os.getenv("DEFAULT_SIZE", "1280x720")
    poll_interval = float(os.getenv("POLL_INTERVAL", "3"))
    upload_timeout_sec = int(os.getenv("TELEGRAM_UPLOAD_TIMEOUT", "600"))
    upload_mode = (os.getenv("TELEGRAM_UPLOAD_MODE", "auto") or "auto").strip().lower()
    if upload_mode not in {"video", "document", "auto"}:
        upload_mode = "auto"
    upload_document_threshold_mb = int(os.getenv("TELEGRAM_UPLOAD_DOCUMENT_THRESHOLD_MB", "8"))
    rate_sora2 = float(os.getenv("SORA2_PRICE_PER_SECOND_RUB", "20"))
    rate_sora2_pro = float(os.getenv("SORA2_PRO_PRICE_PER_SECOND_RUB", "30"))
    rate_veo31 = float(os.getenv("VEO31_PRICE_PER_SECOND_RUB", "25"))
    billing_db_path = Path(os.getenv("BILLING_DB_PATH", os.getenv("CREDITS_DB_PATH", "billing.db")))
    default_new_chat_credits = int(os.getenv("DEFAULT_NEW_CHAT_CREDITS", "20"))
    input_ref_dir = Path(os.getenv("TELEGRAM_INPUT_REF_DIR", "telegram_input_refs"))
    input_ref_dir.mkdir(parents=True, exist_ok=True)
    payment_packages = parse_payment_packages()
    tbank_terminal_key = (os.getenv("TBANK_TERMINAL_KEY") or "").strip()
    tbank_password = (os.getenv("TBANK_PASSWORD") or "").strip()
    tbank_api_url = (os.getenv("TBANK_API_URL") or "https://securepay.tinkoff.ru/v2").strip()
    tbank_success_url = (os.getenv("TBANK_SUCCESS_URL") or "").strip()
    tbank_fail_url = (os.getenv("TBANK_FAIL_URL") or "").strip()
    tbank_notification_url = (os.getenv("TBANK_NOTIFICATION_URL") or "").strip()
    app_env = (os.getenv("APP_ENV") or "").strip().lower()
    app_base_url = (os.getenv("APP_BASE_URL") or "http://127.0.0.1:5000").strip().rstrip("/")
    mock_payments_token = (os.getenv("MOCK_PAYMENTS_TOKEN") or "").strip()
    mock_payments_requested = (
        (os.getenv("MOCK_PAYMENTS_ENABLED") or "0").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    mock_payments_enabled = bool(
        mock_payments_requested and app_env in {"dev", "development", "local", "test"}
    )
    tbank_provider_name = "tbank"
    mock_provider_name = "mock"

    default_provider = "sora"
    orchestrator = build_orchestrator(
        api_key=api_key,
        sora2_price_per_second_rub=rate_sora2,
        sora2_pro_price_per_second_rub=rate_sora2_pro,
        veo31_price_per_second_rub=rate_veo31,
    )
    provider_caps = list_provider_capabilities(orchestrator)
    provider_names = tuple(provider_caps.keys())
    if default_provider not in provider_caps:
        default_provider = provider_names[0]
    default_caps = provider_caps[default_provider]
    if default_seconds not in default_caps.allowed_seconds:
        default_seconds = int(default_caps.allowed_seconds[0])
    if default_model not in default_caps.allowed_models:
        default_model = str(default_caps.allowed_models[0])
    if default_size not in default_caps.allowed_sizes:
        default_size = str(default_caps.allowed_sizes[0])

    # Состояние по чатам
    chat_settings: Dict[int, dict] = {}
    draft_by_chat: Dict[int, dict] = {}
    draft_message_id: Dict[int, int] = {}
    draft_reference_paths: Dict[int, list[str]] = {}
    running_jobs: Dict[int, bool] = {}
    cancel_requested: Dict[int, bool] = {}
    last_prompt_by_chat: Dict[int, str] = {}
    active_video_id: Dict[int, str] = {}
    admin_pending_input: Dict[int, dict] = {}
    lock = threading.Lock()
    init_billing(billing_db_path)

    def build_web_url(chat_id: int) -> Optional[str]:
        token = billing_get_client_token_for_user(
            db_path=billing_db_path,
            user_id=str(chat_id),
        )
        if not token:
            return None
        return f"{app_base_url}/?client_token={quote(token)}"

    def tbank_enabled() -> bool:
        return bool(tbank_terminal_key and tbank_password)

    def payments_enabled() -> bool:
        return bool(mock_payments_enabled or tbank_enabled())

    def get_payment_package(package_id: str) -> Optional[dict]:
        pid = (package_id or "").strip()
        for pkg in payment_packages:
            if pkg["id"] == pid:
                return pkg
        return None

    def tbank_client() -> TBankClient:
        if not tbank_enabled():
            raise RuntimeError("T-Bank не настроен (TBANK_TERMINAL_KEY/TBANK_PASSWORD).")
        return TBankClient(
            terminal_key=tbank_terminal_key,
            password=tbank_password,
            base_url=tbank_api_url,
        )

    def mock_payment_url(payment_id: str) -> str:
        token_qs = f"?token={quote(mock_payments_token)}" if mock_payments_token else ""
        return f"{app_base_url}/mock-pay/{payment_id}{token_qs}"

    def create_gateway_payment(chat_id: int, package_id: str) -> dict:
        pkg = get_payment_package(package_id)
        if not pkg:
            raise ValueError("Пакет не найден.")
        if not payments_enabled():
            raise RuntimeError("Платежи сейчас отключены администратором.")
        order_id = f"tg-{chat_id}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        payment = billing_create_payment(
            db_path=billing_db_path,
            provider=mock_provider_name if mock_payments_enabled else tbank_provider_name,
            user_id=str(chat_id),
            client_token="",
            package_id=pkg["id"],
            amount_rub=float(pkg["rub"]),
            credits=int(pkg["credits"]),
            order_id=order_id,
            meta={"source": "telegram", "chat_id": chat_id},
        )
        if mock_payments_enabled:
            payment_url = mock_payment_url(payment["payment_id"])
            external_payment_id = f"mock_{payment['payment_id']}"
            status = "FORM_SHOWED"
            raw = {"mode": "mock", "status": status}
        else:
            init_resp = tbank_client().init_payment(
                order_id=order_id,
                amount_rub=float(pkg["rub"]),
                description=f"Пакет {pkg['title']} ({pkg['credits']} credits)",
                customer_key=str(chat_id),
                success_url=tbank_success_url or None,
                fail_url=tbank_fail_url or None,
                notification_url=tbank_notification_url or None,
                metadata={"payment_id": payment.get("payment_id", ""), "chat_id": str(chat_id)},
            )
            payment_url = str(init_resp.get("PaymentURL") or "").strip()
            external_payment_id = str(init_resp.get("PaymentId") or "").strip()
            status = str(init_resp.get("Status") or "NEW")
            raw = init_resp
        updated = billing_update_payment(
            db_path=billing_db_path,
            payment_id=payment["payment_id"],
            status=status,
            external_payment_id=external_payment_id or None,
            payment_url=payment_url or None,
            raw=raw,
        )
        return updated or payment

    def check_last_payment(chat_id: int) -> str:
        payment = billing_get_last_payment_for_user(billing_db_path, user_id=str(chat_id))
        if not payment:
            return "Платежей пока нет. Используй /buy."
        payment_id = str(payment.get("payment_id") or "")
        ext_id = str(payment.get("external_payment_id") or "")
        status = str(payment.get("status") or "new")
        if not ext_id:
            label = payment_status_label_ru(status)
            return f"Последний платеж {payment_id}: {label}. Ссылка могла быть не создана."
        if tbank_enabled() and str(payment.get("provider") or "") != mock_provider_name:
            state = tbank_client().get_state(ext_id)
            status = str(state.get("Status") or status)
            payment = billing_update_payment(
                db_path=billing_db_path,
                payment_id=payment_id,
                status=status,
                raw=state,
            ) or payment
        if status == "CONFIRMED":
            applied, balance = billing_apply_payment_credits_if_needed(
                db_path=billing_db_path,
                payment_id=payment_id,
                reason="telegram_payment_confirmed",
            )
            if applied:
                return (
                    f"Платеж {payment_id} подтвержден. "
                    f"Начислено {int(payment.get('credits') or 0)} кр. Баланс: {balance}."
                )
            return f"Платеж {payment_id} уже подтвержден ранее. Баланс: {balance}."
        return f"Платеж {payment_id}: статус {payment_status_label_ru(status)}."

    def is_admin(chat_id: int) -> bool:
        return chat_id in admin_chat_ids

    def get_credits(chat_id: int) -> int:
        return billing_get_credits(
            db_path=billing_db_path,
            user_id=str(chat_id),
            default_credits=default_new_chat_credits,
        )

    def consume_credits(chat_id: int, amount: int) -> tuple[bool, int]:
        return billing_consume_credits(
            db_path=billing_db_path,
            user_id=str(chat_id),
            amount=int(amount),
            default_credits=default_new_chat_credits,
            reason="telegram_generation_consume",
        )

    def set_credits(chat_id: int, amount: int) -> int:
        return billing_set_credits(
            db_path=billing_db_path,
            user_id=str(chat_id),
            amount=int(amount),
            reason="telegram_admin_set",
        )

    def add_credits(chat_id: int, amount: int) -> int:
        return billing_add_credits(
            db_path=billing_db_path,
            user_id=str(chat_id),
            amount=int(amount),
            reason="telegram_admin_add",
        )

    def refund_credits(chat_id: int, amount: int, reason: str) -> int:
        if int(amount) <= 0:
            return get_credits(chat_id)
        return billing_add_credits(
            db_path=billing_db_path,
            user_id=str(chat_id),
            amount=int(amount),
            reason=reason,
        )

    def admin_text(target_chat_id: int) -> str:
        balance = get_credits(target_chat_id)
        return (
            "Админ-панель кредитов\n"
            f"target_chat_id: {target_chat_id}\n"
            f"Текущий баланс: {balance}"
        )

    def get_settings(chat_id: int) -> dict:
        with lock:
            if chat_id not in chat_settings:
                chat_settings[chat_id] = {
                    "provider": default_provider,
                    "reference_image_type": "asset",
                    "seconds": default_seconds,
                    "model": default_model,
                    "size": default_size,
                }
            chat_settings[chat_id] = sanitize_settings(chat_settings[chat_id])
            return dict(chat_settings[chat_id])

    def update_settings(chat_id: int, **kwargs) -> dict:
        with lock:
            if chat_id not in chat_settings:
                chat_settings[chat_id] = {
                    "provider": default_provider,
                    "reference_image_type": "asset",
                    "seconds": default_seconds,
                    "model": default_model,
                    "size": default_size,
                }
            chat_settings[chat_id].update(kwargs)
            chat_settings[chat_id] = sanitize_settings(chat_settings[chat_id])
            return dict(chat_settings[chat_id])

    def sanitize_settings(settings: dict) -> dict:
        provider = normalize_provider(settings.get("provider"), default_provider=default_provider)
        caps = provider_caps.get(provider)
        if caps is None:
            provider = default_provider
            caps = provider_caps[provider]

        seconds = int(settings.get("seconds", default_seconds))
        if seconds not in caps.allowed_seconds:
            seconds = int(caps.allowed_seconds[0])

        model = str(settings.get("model", default_model))
        if model not in caps.allowed_models:
            model = str(caps.allowed_models[0])

        size = str(settings.get("size", default_size))
        if size not in caps.allowed_sizes:
            size = str(caps.allowed_sizes[0])
        ref_type = str(settings.get("reference_image_type", "asset"))
        allowed_ref_types = tuple(caps.supported_reference_types or ())
        if allowed_ref_types:
            if ref_type not in allowed_ref_types:
                ref_type = str(allowed_ref_types[0])
        else:
            ref_type = "asset"

        return {
            "provider": provider,
            "reference_image_type": ref_type,
            "seconds": seconds,
            "model": model,
            "size": size,
        }

    def provider_block_reason(settings: dict) -> Optional[str]:
        provider = normalize_provider(settings.get("provider"), default_provider=default_provider)
        caps = provider_caps.get(provider)
        if caps is None:
            return f"Provider '{provider}' недоступен."
        if caps.is_stub:
            return stub_provider_message(provider)
        return None

    def is_running(chat_id: int) -> bool:
        with lock:
            return running_jobs.get(chat_id, False)

    def set_running(chat_id: int, value: bool) -> None:
        with lock:
            running_jobs[chat_id] = value
            if not value:
                active_video_id.pop(chat_id, None)

    def request_cancel(chat_id: int) -> None:
        with lock:
            cancel_requested[chat_id] = True

    def consume_cancel(chat_id: int) -> bool:
        with lock:
            if cancel_requested.get(chat_id, False):
                cancel_requested[chat_id] = False
                return True
            return False

    def set_active_video_id(chat_id: int, video_id: str) -> None:
        with lock:
            active_video_id[chat_id] = video_id

    def create_or_update_draft(chat_id: int, prompt: str) -> dict:
        old_paths: list[str] = []
        with lock:
            old_paths = list(draft_reference_paths.get(chat_id, []))
            draft_reference_paths[chat_id] = []
            if chat_id not in chat_settings:
                chat_settings[chat_id] = {
                    "provider": default_provider,
                    "reference_image_type": "asset",
                    "seconds": default_seconds,
                    "model": default_model,
                    "size": default_size,
                }
            base = sanitize_settings(chat_settings[chat_id])
            chat_settings[chat_id] = dict(base)
            draft_by_chat[chat_id] = {
                "prompt": prompt,
                "provider": base["provider"],
                "reference_image_type": base["reference_image_type"],
                "seconds": base["seconds"],
                "model": base["model"],
                "size": base["size"],
            }
            out = dict(draft_by_chat[chat_id])
        for p_raw in old_paths:
            try:
                p = Path(p_raw)
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        return out

    def get_draft(chat_id: int) -> Optional[dict]:
        with lock:
            d = draft_by_chat.get(chat_id)
            return dict(d) if d else None

    def update_draft(chat_id: int, **kwargs) -> Optional[dict]:
        with lock:
            if chat_id not in draft_by_chat:
                return None
            draft_by_chat[chat_id].update(kwargs)
            return dict(draft_by_chat[chat_id])

    def clear_draft(chat_id: int, delete_files: bool = True) -> None:
        paths: list[str] = []
        with lock:
            paths = list(draft_reference_paths.get(chat_id, []))
            draft_by_chat.pop(chat_id, None)
            draft_message_id.pop(chat_id, None)
            draft_reference_paths.pop(chat_id, None)
        if delete_files:
            for p_raw in paths:
                try:
                    p = Path(p_raw)
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass

    def render_draft_ui(chat_id: int) -> None:
        draft = get_draft(chat_id)
        if not draft:
            tg.send_message(chat_id, "Сначала отправь prompt текстом.")
            return
        with lock:
            draft["reference_images_count"] = len(draft_reference_paths.get(chat_id, []))
        msg_text = settings_text(draft, credits_balance=get_credits(chat_id))
        kb = settings_keyboard(draft, provider_names=provider_names, provider_caps=provider_caps)
        existing_msg_id = draft_message_id.get(chat_id)
        if existing_msg_id:
            try:
                tg.edit_message(chat_id, existing_msg_id, msg_text, reply_markup=kb)
                return
            except Exception:
                pass
        message_id = tg.send_message(chat_id, msg_text, reply_markup=kb)
        draft_message_id[chat_id] = message_id

    def _cleanup_reference_paths(paths: list[str]) -> None:
        for p_raw in paths:
            try:
                p = Path(p_raw)
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    def add_reference_image_for_draft(chat_id: int, photo_sizes: list[dict]) -> None:
        draft = get_draft(chat_id)
        if not draft:
            tg.send_message(chat_id, "Сначала отправь prompt текстом, затем отправь фото.")
            return
        settings = sanitize_settings(draft)
        provider = settings.get("provider", "sora")
        caps = provider_caps.get(provider) or provider_caps[default_provider]
        if not caps.supports_reference_images:
            tg.send_message(
                chat_id,
                f"Provider '{provider}' не поддерживает referenceImages в текущей конфигурации.",
            )
            return
        if not photo_sizes:
            tg.send_message(chat_id, "Фото не распознано. Попробуй отправить изображение еще раз.")
            return

        ref_type = settings.get("reference_image_type", "asset")
        max_refs = int(caps.max_reference_images or 0)
        if max_refs <= 0:
            tg.send_message(chat_id, "ReferenceImages сейчас недоступны.")
            return

        with lock:
            existing = list(draft_reference_paths.get(chat_id, []))
        if ref_type == "style":
            max_refs = 1
        if len(existing) >= max_refs:
            tg.send_message(
                chat_id,
                f"Лимит referenceImages для типа '{ref_type}': {max_refs}. "
                "Очисти черновик (/cancel) или отправь новый prompt.",
            )
            return

        best_photo = photo_sizes[-1]
        file_id = best_photo.get("file_id")
        if not file_id:
            tg.send_message(chat_id, "Не удалось получить file_id изображения.")
            return

        try:
            file_path = tg.get_file_path(file_id)
            suffix = Path(file_path).suffix or ".jpg"
            local_name = f"{chat_id}_{int(time.time() * 1000)}{suffix}"
            local_path = input_ref_dir / local_name
            tg.download_file(file_path=file_path, target_path=local_path)
            with lock:
                current = list(draft_reference_paths.get(chat_id, []))
                if ref_type == "style":
                    old_paths = current
                    current = [str(local_path)]
                else:
                    old_paths = []
                    current.append(str(local_path))
                draft_reference_paths[chat_id] = current
            _cleanup_reference_paths(old_paths)
            render_draft_ui(chat_id)
            tg.send_message(
                chat_id,
                f"Reference image добавлен ({len(current)}/{max_refs}) для type='{ref_type}'.",
            )
        except Exception as exc:
            tg.send_message(chat_id, f"Не удалось загрузить reference image: {exc}")

    def refs_summary(chat_id: int) -> str:
        draft = get_draft(chat_id)
        if not draft:
            return "Черновик отсутствует. Сначала отправь prompt."
        settings = sanitize_settings(draft)
        ref_type = settings.get("reference_image_type", "asset")
        with lock:
            refs = list(draft_reference_paths.get(chat_id, []))
        names = [Path(p).name for p in refs]
        lines = [
            f"provider={settings.get('provider', 'sora')}",
            f"reference_type={ref_type}",
            f"reference_images={len(refs)}",
        ]
        if names:
            lines.append("Файлы:")
            lines.extend(f"- {n}" for n in names)
        return "\n".join(lines)

    def clear_refs(chat_id: int) -> int:
        with lock:
            refs = list(draft_reference_paths.get(chat_id, []))
            draft_reference_paths[chat_id] = []
        _cleanup_reference_paths(refs)
        return len(refs)

    def run_generation(
        chat_id: int,
        prompt: str,
        settings: dict,
        remix_source_video_id: Optional[str] = None,
        reference_paths: Optional[list[str]] = None,
        charged_credits: int = 0,
    ) -> None:
        settings = sanitize_settings(settings)
        reference_paths = list(reference_paths or [])
        charged_credits = max(int(charged_credits or 0), 0)
        refunded = False
        caps = validate_generation_params(
            orchestrator=orchestrator,
            provider=settings["provider"],
            seconds=settings["seconds"],
            model=settings["model"],
            size=settings["size"],
        )
        ensure_provider_available(caps)
        set_running(chat_id, True)
        progress_message_id: Optional[int] = None
        balance_before: Optional[float] = None
        balance_after: Optional[float] = None
        spent_rub: Optional[float] = None
        estimated_spent_rub = estimate_generation_cost_rub(
            settings=settings,
            rate_sora2=rate_sora2,
            rate_sora2_pro=rate_sora2_pro,
            rate_veo31=rate_veo31,
        )
        try:
            try:
                balance_before = extract_available_balance(client.get_balance())
            except Exception:
                balance_before = None

            progress_message_id = tg.send_message(
                chat_id,
                "Генерация видео началась...",
                reply_markup=control_keyboard(),
            )
            output_path = Path(f"video_{chat_id}_{int(time.time())}.mp4")
            last_video_id = {"value": "pending"}

            def on_job_created(video_id: str, status: str) -> None:
                last_video_id["value"] = video_id
                set_active_video_id(chat_id, video_id)
                if progress_message_id is not None:
                    tg.edit_message(
                        chat_id,
                        progress_message_id,
                        f"Задание создано: {video_id}\nСтатус: {status}",
                        reply_markup=control_keyboard(),
                    )

            def sender(text: str) -> None:
                if progress_message_id is not None:
                    tg.edit_message(
                        chat_id,
                        progress_message_id,
                        text,
                        reply_markup=control_keyboard(),
                    )

            on_progress = make_telegram_progress_callback(sender=sender, min_step=5)
            if remix_source_video_id:
                job = orchestrator.start_remix(
                    provider_name=settings.get("provider", "sora"),
                    source_video_id=remix_source_video_id,
                    prompt=prompt,
                )
            else:
                job = orchestrator.start_generation(
                    VideoRequest(
                        provider=settings.get("provider", "sora"),
                        prompt=prompt,
                        seconds=settings["seconds"],
                        model=settings["model"],
                        size=settings["size"],
                        input_reference_paths=reference_paths or None,
                        reference_image_type=settings.get("reference_image_type", "asset"),
                    )
                )
            on_job_created(job.external_id, job.status)
            on_progress(0, job.status)

            try:
                status = orchestrator.wait_until_done(
                    provider_name=settings.get("provider", "sora"),
                    job=job,
                    poll_interval_sec=poll_interval,
                    on_progress=lambda s, p, e: on_progress(p, s),
                    should_cancel=lambda: consume_cancel(chat_id),
                )
            except RuntimeError as exc:
                raise SoraError(str(exc)) from exc
            if status.status == "failed":
                raise SoraError(status.error or "Генерация видео не удалась")

            result = orchestrator.download_result(
                provider_name=settings.get("provider", "sora"),
                job=job,
                output_path=str(output_path),
            )
            path = Path(result.file_path)
            file_size_mb = path.stat().st_size / (1024 * 1024) if path.exists() else 0.0
            send_as_document = (
                upload_mode == "document"
                or (upload_mode == "auto" and file_size_mb >= upload_document_threshold_mb)
            )
            upload_mode_text = "document" if send_as_document else "video"
            if progress_message_id is not None:
                tg.edit_message(
                    chat_id,
                    progress_message_id,
                    (
                        "Видео сгенерировано. Идёт загрузка в Telegram...\n"
                        f"Режим: {upload_mode_text}, размер: {file_size_mb:.1f} MB"
                    ),
                    reply_markup=control_keyboard(),
                )

            tg.send_video(
                chat_id=chat_id,
                video_path=path,
                caption=(
                    "Видео готово.\n"
                    f"ID: {last_video_id['value']}\n"
                    f"Provider: {settings.get('provider', 'sora')}\n"
                    f"Reference type: {settings.get('reference_image_type', 'asset')}\n"
                    f"Модель: {settings['model']}, {settings['seconds']} сек, {settings['size']}"
                ),
                upload_timeout_sec=upload_timeout_sec,
                send_as_document=send_as_document,
            )
            if progress_message_id is not None:
                if balance_before is not None:
                    try:
                        balance_after = extract_available_balance(client.get_balance())
                        if balance_after is not None:
                            spent_rub = max(balance_before - balance_after, 0.0)
                    except Exception:
                        spent_rub = None

                final_text = "Готово: видео отправлено в чат."
                if spent_rub is not None:
                    final_text += f"\nНа генерацию израсходовано: {spent_rub:.2f} руб."
                else:
                    final_text += (
                        f"\nОценка стоимости: ~{estimated_spent_rub:.2f} руб."
                        "\n(точная сумма недоступна без разрешения «Запрос баланса» у ключа)"
                    )
                tg.edit_message(
                    chat_id,
                    progress_message_id,
                    final_text,
                    reply_markup=control_keyboard(),
                )

        except SoraError as exc:
            err_text = compact_generation_error(exc)
            refunded_balance: Optional[int] = None
            if charged_credits > 0 and not refunded:
                try:
                    refunded_balance = refund_credits(
                        chat_id,
                        charged_credits,
                        reason="telegram_generation_refund_failed",
                    )
                    refunded = True
                except Exception:
                    refunded_balance = None
            extra = ""
            if balance_before is not None:
                try:
                    balance_after = extract_available_balance(client.get_balance())
                    if balance_after is not None:
                        spent_rub = max(balance_before - balance_after, 0.0)
                        extra = f"\nНа попытку израсходовано: {spent_rub:.2f} руб."
                except Exception:
                    pass
            if not extra:
                extra = (
                    f"\nОценка стоимости попытки: ~{estimated_spent_rub:.2f} руб."
                    "\n(точная сумма недоступна без разрешения «Запрос баланса» у ключа)"
                )
            refund_line = ""
            if refunded_balance is not None:
                refund_line = (
                    f"\nКредиты возвращены: +{charged_credits}. "
                    f"Баланс: {refunded_balance}."
                )
            show_retry_sora = (
                settings.get("provider", "sora") == "veo"
                and err_text.startswith("Veo временно недоступен")
            )
            tg.send_message(
                chat_id,
                f"Генерация не удалась: {err_text}{refund_line}{extra}",
                reply_markup=control_keyboard(show_retry_sora=show_retry_sora),
            )
        except Exception as exc:
            err_text = compact_generation_error(exc)
            refund_line = ""
            if charged_credits > 0 and not refunded:
                try:
                    refunded_balance = refund_credits(
                        chat_id,
                        charged_credits,
                        reason="telegram_generation_refund_exception",
                    )
                    refunded = True
                    refund_line = (
                        f"\nКредиты возвращены: +{charged_credits}. "
                        f"Баланс: {refunded_balance}."
                    )
                except Exception:
                    pass
            tg.send_message(chat_id, f"Ошибка: {err_text}{refund_line}")
        finally:
            set_running(chat_id, False)
            _cleanup_reference_paths(reference_paths)

    def handle_text(chat_id: int, text: str) -> None:
        if allowed_chat_id is not None and chat_id != allowed_chat_id:
            tg.send_message(chat_id, "Доступ к боту ограничен.")
            return

        pending_admin_action = admin_pending_input.get(chat_id)

        if text.startswith("/"):
            cmd, arg = parse_command(text)
            if cmd in ("/start", "/help"):
                tg.send_message(chat_id, build_help(), reply_markup=command_keyboard())
                tg.send_message(
                    chat_id,
                    "Выберите режим работы:",
                    reply_markup=start_keyboard(),
                )
                return
            if cmd == "/cancel":
                if pending_admin_action:
                    admin_pending_input.pop(chat_id, None)
                    tg.send_message(chat_id, "Операция админ-ввода отменена.")
                    return
                tg.send_message(chat_id, "Нечего отменять.")
                return
            if cmd == "/myid":
                tg.send_message(chat_id, f"Ваш chat_id: {chat_id}")
                return
            if cmd == "/web":
                web_url = build_web_url(chat_id)
                if not web_url:
                    tg.send_message(
                        chat_id,
                        "Для вас ещё не задан web token.\n"
                        "Попросите админа выполнить:\n"
                        f"/set_web_token {chat_id} <token>",
                    )
                    return
                tg.send_message(
                    chat_id,
                    "Откройте сайт по этой ссылке (token подставится автоматически):\n"
                    f"{web_url}",
                )
                return
            if cmd == "/credits":
                tg.send_message(chat_id, f"Баланс кредитов: {get_credits(chat_id)}")
                return
            if cmd == "/buy":
                if not payments_enabled():
                    tg.send_message(chat_id, "Покупка временно недоступна: платежный провайдер не настроен.")
                    return
                if not payment_packages:
                    tg.send_message(chat_id, "Пакеты оплаты не настроены.")
                    return
                tg.send_message(
                    chat_id,
                    (
                        f"Баланс: {get_credits(chat_id)} кр.\n"
                        f"Режим оплаты: {'mock' if mock_payments_enabled else 'tbank'}\n"
                        "Выбери пакет для пополнения кредитов:"
                    ),
                    reply_markup=payment_packages_keyboard(payment_packages),
                )
                return
            if cmd == "/paycheck":
                try:
                    result = check_last_payment(chat_id)
                    tg.send_message(chat_id, f"{result}\nТекущий баланс: {get_credits(chat_id)} кр.")
                except Exception as exc:
                    tg.send_message(chat_id, f"Не удалось проверить оплату: {exc}")
                return
            if cmd == "/refs":
                tg.send_message(chat_id, refs_summary(chat_id))
                return
            if cmd == "/clearrefs":
                deleted = clear_refs(chat_id)
                tg.send_message(chat_id, f"Очищено reference images: {deleted}")
                render_draft_ui(chat_id)
                return
            if cmd == "/admin":
                if not is_admin(chat_id):
                    tg.send_message(chat_id, "Команда доступна только администратору.")
                    return
                target_chat_id = chat_id
                if arg:
                    if not arg.strip().lstrip("-").isdigit():
                        tg.send_message(chat_id, "Формат: /admin или /admin <chat_id>")
                        return
                    target_chat_id = int(arg.strip())
                tg.send_message(
                    chat_id,
                    admin_text(target_chat_id),
                    reply_markup=admin_keyboard(target_chat_id),
                )
                return
            if cmd in ("/add_credits", "/set_credits"):
                if not is_admin(chat_id):
                    tg.send_message(chat_id, "Команда доступна только администратору.")
                    return
                if not arg:
                    tg.send_message(
                        chat_id,
                        "Формат:\n"
                        "/add_credits <chat_id> <amount>\n"
                        "/set_credits <chat_id> <amount>",
                    )
                    return
                parts = arg.split()
                if len(parts) != 2:
                    tg.send_message(chat_id, "Нужно 2 аргумента: <chat_id> <amount>")
                    return
                chat_raw, amount_raw = parts
                if not chat_raw.lstrip("-").isdigit() or not amount_raw.lstrip("-").isdigit():
                    tg.send_message(chat_id, "chat_id и amount должны быть целыми числами.")
                    return
                target_chat_id = int(chat_raw)
                amount = int(amount_raw)
                if cmd == "/add_credits":
                    if amount <= 0:
                        tg.send_message(chat_id, "Для add_credits amount должен быть > 0.")
                        return
                    new_balance = add_credits(target_chat_id, amount)
                    tg.send_message(
                        chat_id,
                        f"Готово: пользователю {target_chat_id} начислено {amount} кредитов.\n"
                        f"Новый баланс: {new_balance}",
                    )
                else:
                    if amount < 0:
                        tg.send_message(chat_id, "Для set_credits amount должен быть >= 0.")
                        return
                    new_balance = set_credits(target_chat_id, amount)
                    tg.send_message(
                        chat_id,
                        f"Готово: пользователю {target_chat_id} установлен баланс {new_balance} кредитов.",
                    )
                return
            if cmd == "/set_web_token":
                if not is_admin(chat_id):
                    tg.send_message(chat_id, "Команда доступна только администратору.")
                    return
                if not arg:
                    tg.send_message(chat_id, "Формат: /set_web_token <chat_id> <token>")
                    return
                parts = arg.split(maxsplit=1)
                if len(parts) != 2:
                    tg.send_message(chat_id, "Формат: /set_web_token <chat_id> <token>")
                    return
                target_chat_raw, token = parts[0], parts[1].strip()
                if not target_chat_raw.lstrip("-").isdigit() or not token:
                    tg.send_message(chat_id, "Неверный формат: /set_web_token <chat_id> <token>")
                    return
                target_chat_id = int(target_chat_raw)
                billing_set_client_token(
                    db_path=billing_db_path,
                    user_id=str(target_chat_id),
                    token=token,
                    default_credits=default_new_chat_credits,
                )
                tg.send_message(
                    chat_id,
                    f"Web token привязан к пользователю {target_chat_id}.",
                )
                return
            if cmd == "/remix":
                if not arg:
                    tg.send_message(chat_id, "Формат: /remix <source_video_id> <prompt>")
                    return
                parts = arg.split(maxsplit=1)
                if len(parts) != 2:
                    tg.send_message(chat_id, "Формат: /remix <source_video_id> <prompt>")
                    return
                source_video_id, remix_prompt = parts[0].strip(), parts[1].strip()
                if not source_video_id.startswith("video_") or not remix_prompt:
                    tg.send_message(chat_id, "Неверный формат: /remix <source_video_id> <prompt>")
                    return
                if is_running(chat_id):
                    tg.send_message(chat_id, "Генерация уже идет. Дождись завершения.")
                    return
                settings = get_settings(chat_id)
                block_reason = provider_block_reason(settings)
                if block_reason:
                    tg.send_message(chat_id, block_reason)
                    return
                need = generation_credit_cost(settings)
                ok, balance_after = consume_credits(chat_id, need)
                if not ok:
                    tg.send_message(
                        chat_id,
                        f"Недостаточно кредитов: нужно {need}, доступно {balance_after}.",
                    )
                    return
                tg.send_message(
                    chat_id,
                    "Запускаю remix:\n"
                    f"source={source_video_id}\n"
                    f"Списано кредитов: {need}. Остаток: {balance_after}.",
                )
                threading.Thread(
                    target=run_generation,
                    args=(chat_id, remix_prompt, settings, source_video_id, None, need),
                    daemon=True,
                ).start()
                return
            if cmd == "/status":
                s = get_settings(chat_id)
                d = get_draft(chat_id)
                with lock:
                    refs_count = len(draft_reference_paths.get(chat_id, []))
                draft_part = (
                    f"\n\nЧерновик:\nmodel={d['model']}\nseconds={d['seconds']}\nsize={d['size']}"
                    f"\nprovider={d.get('provider', 'sora')}"
                    f"\nreference_type={d.get('reference_image_type', 'asset')}"
                    f"\nreference_images={refs_count}"
                    if d
                    else "\n\nЧерновик: отсутствует"
                )
                tg.send_message(
                    chat_id,
                    "Текущие параметры по умолчанию:\n"
                    f"provider={s.get('provider', 'sora')}\n"
                    f"reference_type={s.get('reference_image_type', 'asset')}\n"
                    f"model={s['model']}\nseconds={s['seconds']}\nsize={s['size']}\n"
                    f"credits={get_credits(chat_id)}{draft_part}",
                )
                return
            tg.send_message(chat_id, "Неизвестная команда. /help")
            return

        if pending_admin_action and is_admin(chat_id):
            raw = text.strip()
            if not raw.lstrip("-").isdigit():
                tg.send_message(
                    chat_id,
                    "Нужно ввести целое число. Либо /cancel для отмены.",
                )
                return
            amount = int(raw)
            target_chat_id = int(pending_admin_action["target_chat_id"])
            mode = pending_admin_action["mode"]
            if mode == "add":
                if amount <= 0:
                    tg.send_message(chat_id, "Для add сумма должна быть > 0.")
                    return
                new_balance = add_credits(target_chat_id, amount)
                tg.send_message(
                    chat_id,
                    f"Готово: начислено {amount} кредитов пользователю {target_chat_id}.\n"
                    f"Новый баланс: {new_balance}",
                )
            else:
                if amount < 0:
                    tg.send_message(chat_id, "Для set сумма должна быть >= 0.")
                    return
                new_balance = set_credits(target_chat_id, amount)
                tg.send_message(
                    chat_id,
                    f"Готово: установлен баланс {new_balance} для {target_chat_id}.",
                )
            admin_pending_input.pop(chat_id, None)
            tg.send_message(
                chat_id,
                admin_text(target_chat_id),
                reply_markup=admin_keyboard(target_chat_id),
            )
            return

        if is_running(chat_id):
            tg.send_message(chat_id, "Генерация уже идет. Дождись завершения текущего задания.")
            return

        prompt = text.strip()
        if not prompt:
            tg.send_message(chat_id, "Отправь текстовый prompt для генерации.")
            return

        last_prompt_by_chat[chat_id] = prompt
        create_or_update_draft(chat_id, prompt)
        render_draft_ui(chat_id)

    def handle_photo(chat_id: int, photo_sizes: list[dict]) -> None:
        if allowed_chat_id is not None and chat_id != allowed_chat_id:
            tg.send_message(chat_id, "Доступ к боту ограничен.")
            return
        if is_running(chat_id):
            tg.send_message(chat_id, "Генерация уже идет. Дождись завершения текущего задания.")
            return
        add_reference_image_for_draft(chat_id, photo_sizes)

    def handle_callback(callback_query: dict) -> None:
        callback_id = callback_query.get("id")
        data = callback_query.get("data", "")
        message = callback_query.get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")

        if callback_id:
            tg.answer_callback(callback_id)
        if chat_id is None:
            return
        if allowed_chat_id is not None and chat_id != allowed_chat_id:
            tg.send_message(chat_id, "Доступ к боту ограничен.")
            return

        if data == "cancel_generation":
            if is_running(chat_id):
                request_cancel(chat_id)
                tg.send_message(chat_id, "Запрос на отмену принят.")
            else:
                tg.send_message(chat_id, "Сейчас нет активной генерации.")
            return

        if data == "start:bot":
            tg.send_message(
                chat_id,
                "Режим бота активен. Пришлите текстовый prompt — открою параметры генерации.",
            )
            return

        if data == "start:web":
            web_url = build_web_url(chat_id)
            if not web_url:
                tg.send_message(
                    chat_id,
                    "Для вас ещё не задан web token.\n"
                    "Попросите админа выполнить:\n"
                    f"/set_web_token {chat_id} <token>",
                )
                return
            tg.send_message(
                chat_id,
                "Откройте сайт по этой ссылке (token подставится автоматически):\n"
                f"{web_url}",
            )
            return

        if data.startswith("pay:"):
            parts = data.split(":")
            if len(parts) >= 3 and parts[1] == "buy":
                package_id = parts[2]
                try:
                    payment = create_gateway_payment(chat_id, package_id)
                    payment_url = str(payment.get("payment_url") or "").strip()
                    if not payment_url:
                        tg.send_message(
                            chat_id,
                            "Платеж создан, но ссылка не получена. Попробуй позже /buy.",
                        )
                        return
                    tg.send_message(
                        chat_id,
                        "Платеж создан.\n"
                        f"Пакет: {payment.get('package_id')}\n"
                        f"Сумма: {payment.get('amount_rub')} ₽\n"
                        f"Провайдер: {payment.get('provider')}\n"
                        f"Статус: {payment_status_label_ru(payment.get('status'))}\n"
                        f"Ссылка на оплату:\n{payment_url}\n\n"
                        "После оплаты нажми кнопку «Проверить оплату» или команду /paycheck.\n"
                        f"Текущий баланс: {get_credits(chat_id)} кр.",
                        reply_markup=payment_packages_keyboard(payment_packages),
                    )
                except Exception as exc:
                    tg.send_message(chat_id, f"Не удалось создать платеж: {exc}")
                return
            if data == "pay:check:last":
                try:
                    tg.send_message(
                        chat_id,
                        check_last_payment(chat_id),
                        reply_markup=payment_packages_keyboard(payment_packages),
                    )
                except Exception as exc:
                    tg.send_message(chat_id, f"Не удалось проверить оплату: {exc}")
                return

        if data.startswith("admin:"):
            if not is_admin(chat_id):
                tg.send_message(chat_id, "Команда доступна только администратору.")
                return
            parts = data.split(":")
            if len(parts) < 3:
                return
            action = parts[1]
            if not parts[2].lstrip("-").isdigit():
                return
            target_chat_id = int(parts[2])

            if action in ("ask_add", "ask_set"):
                mode = "add" if action == "ask_add" else "set"
                admin_pending_input[chat_id] = {
                    "mode": mode,
                    "target_chat_id": target_chat_id,
                }
                tg.send_message(
                    chat_id,
                    (
                        f"Введите сумму для {'начисления' if mode == 'add' else 'установки'} "
                        f"кредитов пользователю {target_chat_id}.\n"
                        "Отправьте целое число или /cancel."
                    ),
                )
                return

            if action in ("add", "set"):
                if len(parts) != 4 or not parts[3].lstrip("-").isdigit():
                    return
                amount = int(parts[3])
                if action == "add":
                    if amount <= 0:
                        tg.send_message(chat_id, "Для add значение должно быть > 0.")
                        return
                    new_balance = add_credits(target_chat_id, amount)
                    tg.send_message(
                        chat_id,
                        f"Начислено {amount} кредитов для {target_chat_id}. Баланс: {new_balance}",
                    )
                else:
                    if amount < 0:
                        tg.send_message(chat_id, "Для set значение должно быть >= 0.")
                        return
                    new_balance = set_credits(target_chat_id, amount)
                    tg.send_message(
                        chat_id,
                        f"Установлен баланс {new_balance} для {target_chat_id}",
                    )
                return

            if action in ("view", "refresh"):
                message_obj = callback_query.get("message", {})
                message_id = message_obj.get("message_id")
                if message_id:
                    try:
                        tg.edit_message(
                            chat_id,
                            message_id,
                            admin_text(target_chat_id),
                            reply_markup=admin_keyboard(target_chat_id),
                        )
                        return
                    except Exception:
                        pass
                tg.send_message(
                    chat_id,
                    admin_text(target_chat_id),
                    reply_markup=admin_keyboard(target_chat_id),
                )
                return

        if data == "retry_last_generation":
            if is_running(chat_id):
                tg.send_message(chat_id, "Сначала дождись завершения текущей генерации.")
                return
            prompt = last_prompt_by_chat.get(chat_id)
            if not prompt:
                tg.send_message(chat_id, "Нет последнего prompt. Отправь текст для генерации.")
                return
            settings = get_settings(chat_id)
            block_reason = provider_block_reason(settings)
            if block_reason:
                tg.send_message(chat_id, block_reason)
                return
            need = generation_credit_cost(settings)
            ok, balance_after = consume_credits(chat_id, need)
            if not ok:
                tg.send_message(
                    chat_id,
                    f"Недостаточно кредитов: нужно {need}, доступно {balance_after}.",
                )
                return
            tg.send_message(
                chat_id,
                f"Списано кредитов: {need}. Остаток: {balance_after}. Повторяю генерацию...",
            )
            threading.Thread(
                target=run_generation,
                args=(chat_id, prompt, settings, None, None, need),
                daemon=True,
            ).start()
            return

        if data == "retry_in_sora":
            if is_running(chat_id):
                tg.send_message(chat_id, "Сначала дождись завершения текущей генерации.")
                return
            prompt = last_prompt_by_chat.get(chat_id)
            if not prompt:
                tg.send_message(chat_id, "Нет последнего prompt. Отправь текст для генерации.")
                return
            prev = get_settings(chat_id)
            sora_caps = provider_caps.get("sora")
            if not sora_caps:
                tg.send_message(chat_id, "Sora сейчас недоступен.")
                return
            fallback_model = str(sora_caps.allowed_models[0])
            fallback_seconds = int(prev.get("seconds", 4))
            if fallback_seconds not in sora_caps.allowed_seconds:
                fallback_seconds = int(sora_caps.allowed_seconds[0])
            fallback_size = str(prev.get("size", "1280x720"))
            if fallback_size not in sora_caps.allowed_sizes:
                fallback_size = str(sora_caps.allowed_sizes[0])
            settings = {
                "provider": "sora",
                "reference_image_type": "asset",
                "seconds": fallback_seconds,
                "model": fallback_model,
                "size": fallback_size,
            }
            need = generation_credit_cost(settings)
            ok, balance_after = consume_credits(chat_id, need)
            if not ok:
                tg.send_message(
                    chat_id,
                    f"Недостаточно кредитов: нужно {need}, доступно {balance_after}.",
                )
                return
            update_settings(chat_id, **settings)
            tg.send_message(
                chat_id,
                "Повторяю в Sora:\n"
                f"model={settings['model']}, seconds={settings['seconds']}, size={settings['size']}\n"
                f"Списано кредитов: {need}. Остаток: {balance_after}.",
            )
            threading.Thread(
                target=run_generation,
                args=(chat_id, prompt, settings, None, None, need),
                daemon=True,
            ).start()
            return

        if data.startswith("cfg:set:"):
            if is_running(chat_id):
                tg.send_message(chat_id, "Нельзя менять параметры во время генерации.")
                return
            parts = data.split(":", 3)
            if len(parts) != 4:
                return
            field, value = parts[2], parts[3]
            draft = get_draft(chat_id)
            if not draft:
                tg.send_message(chat_id, "Сначала отправь prompt текстом.")
                return
            current_provider = normalize_provider(draft.get("provider"), default_provider=default_provider)
            caps = provider_caps.get(current_provider) or provider_caps[default_provider]
            if field == "seconds":
                if not value.isdigit() or int(value) not in caps.allowed_seconds:
                    return
                update_draft(chat_id, seconds=int(value))
            elif field == "provider":
                if value not in provider_names:
                    return
                new_caps = provider_caps[value]
                with lock:
                    old_paths = list(draft_reference_paths.get(chat_id, []))
                if not new_caps.supports_reference_images and old_paths:
                    _cleanup_reference_paths(old_paths)
                    with lock:
                        draft_reference_paths[chat_id] = []
                update_draft(
                    chat_id,
                    provider=value,
                    reference_image_type=(
                        str((new_caps.supported_reference_types or ("asset",))[0])
                    ),
                    seconds=int(new_caps.allowed_seconds[0]),
                    model=str(new_caps.allowed_models[0]),
                    size=str(new_caps.allowed_sizes[0]),
                )
            elif field == "ref_type":
                draft = get_draft(chat_id)
                provider = normalize_provider(
                    (draft or {}).get("provider"),
                    default_provider=default_provider,
                )
                caps = provider_caps.get(provider) or provider_caps[default_provider]
                allowed_ref_types = tuple(caps.supported_reference_types or ())
                if not allowed_ref_types or value not in allowed_ref_types:
                    return
                if value == "style":
                    with lock:
                        existing = list(draft_reference_paths.get(chat_id, []))
                    if len(existing) > 1:
                        keep = existing[-1]
                        _cleanup_reference_paths(existing[:-1])
                        with lock:
                            draft_reference_paths[chat_id] = [keep]
                update_draft(chat_id, reference_image_type=value)
            elif field == "model":
                if value not in caps.allowed_models:
                    return
                update_draft(chat_id, model=value)
            elif field == "size":
                if value not in caps.allowed_sizes:
                    return
                update_draft(chat_id, size=value)
            render_draft_ui(chat_id)
            return

        if data == "cfg:cancel":
            clear_draft(chat_id)
            tg.send_message(chat_id, "Черновик сброшен. Пришли новый prompt.")
            return

        if data == "cfg:refs":
            tg.send_message(chat_id, refs_summary(chat_id))
            return

        if data == "cfg:clearrefs":
            deleted = clear_refs(chat_id)
            tg.send_message(chat_id, f"Очищено reference images: {deleted}")
            render_draft_ui(chat_id)
            return

        if data == "cfg:start":
            if is_running(chat_id):
                tg.send_message(chat_id, "Генерация уже идет. Дождись завершения.")
                return
            draft = get_draft(chat_id)
            if not draft:
                tg.send_message(chat_id, "Сначала отправь prompt текстом.")
                return
            settings = {
                "provider": draft.get("provider", "sora"),
                "reference_image_type": draft.get("reference_image_type", "asset"),
                "seconds": draft["seconds"],
                "model": draft["model"],
                "size": draft["size"],
            }
            with lock:
                draft_refs = list(draft_reference_paths.get(chat_id, []))
            block_reason = provider_block_reason(settings)
            if block_reason:
                tg.send_message(chat_id, block_reason)
                return
            need = generation_credit_cost(settings)
            ok, balance_after = consume_credits(chat_id, need)
            if not ok:
                tg.send_message(
                    chat_id,
                    f"Недостаточно кредитов: нужно {need}, доступно {balance_after}.",
                )
                return
            update_settings(chat_id, **settings)
            last_prompt_by_chat[chat_id] = draft["prompt"]
            clear_draft(chat_id, delete_files=False)
            tg.send_message(
                chat_id,
                "Принято. Запускаю генерацию:\n"
                f"provider={settings.get('provider', 'sora')}, "
                f"reference_type={settings.get('reference_image_type', 'asset')}, "
                f"model={settings['model']}, seconds={settings['seconds']}, size={settings['size']}\n"
                f"Списано кредитов: {need}. Остаток: {balance_after}.",
            )
            threading.Thread(
                target=run_generation,
                args=(chat_id, last_prompt_by_chat[chat_id], settings, None, draft_refs, need),
                daemon=True,
            ).start()
            return

    def _telegram_bootstrap_polling() -> None:
        """getMe + сброс webhook: иначе getUpdates не получает апдейты."""
        try:
            me = tg.get_me()
            res = me.get("result") or {}
            un = res.get("username") or "?"
            bid = res.get("id")
            print(f"Telegram getMe: @{un} (bot_id={bid})", flush=True)
        except Exception as exc:
            print(
                f"[ERROR] Не удалось связаться с api.telegram.org (getMe): {exc}\n"
                "Проверьте сеть, VPN, файрвол, переменную HTTPS_PROXY при блокировках.",
                flush=True,
            )
            raise SystemExit(1) from exc
        try:
            wh = tg.get_webhook_info()
            wres = wh.get("result") or {}
            url = (wres.get("url") or "").strip()
            pending = wres.get("pending_update_count") or 0
            if url:
                print(
                    f"[INFO] У бота включён webhook URL={url!r}, pending_updates≈{pending}. "
                    "Отключаю webhook для режима long polling...",
                    flush=True,
                )
            drop = (
                os.getenv("TELEGRAM_DROP_PENDING_ON_WEBHOOK_DELETE", "0").strip().lower()
                in {"1", "true", "yes", "on"}
            )
            tg.delete_webhook(drop_pending_updates=drop)
            if url:
                print("[INFO] deleteWebhook выполнен. Можно слать /start боту.", flush=True)
        except Exception as exc:
            print(f"[WARN] getWebhookInfo/deleteWebhook: {exc}", flush=True)

    def _ensure_bot_menu() -> None:
        try:
            tg.set_my_commands(
                [
                    {"command": "start", "description": "Старт и быстрые кнопки"},
                    {"command": "web", "description": "Открыть веб-кабинет"},
                    {"command": "credits", "description": "Показать баланс"},
                    {"command": "buy", "description": "Купить пакет кредитов"},
                    {"command": "status", "description": "Текущие параметры"},
                    {"command": "help", "description": "Справка"},
                ]
            )
            # Кнопка меню открывает список команд (где есть /web).
            tg.set_chat_menu_button({"type": "commands"})
        except Exception as exc:
            print(f"[WARN] Не удалось настроить меню команд: {exc}", flush=True)

    _telegram_bootstrap_polling()
    _ensure_bot_menu()

    if allowed_chat_id is not None:
        print(
            f"[INFO] Бот принимает сообщения только от TELEGRAM_ALLOWED_CHAT_ID={allowed_chat_id}. "
            "Остальным ответ: «Доступ ограничен».",
            flush=True,
        )

    print("Telegram-бот запущен. Нажмите Ctrl+C для остановки.")
    offset = None

    while True:
        try:
            updates = tg.get_updates(offset=offset, timeout_sec=25)
            next_offset = offset
            for upd in updates:
                uid = upd["update_id"]
                try:
                    callback_query = upd.get("callback_query")
                    if callback_query:
                        handle_callback(callback_query)
                    else:
                        message = upd.get("message")
                        if not message:
                            pass
                        else:
                            chat = message.get("chat", {})
                            chat_id = chat.get("id")
                            photo_sizes = message.get("photo") or []
                            text = message.get("text", "")
                            if chat_id is None:
                                pass
                            elif photo_sizes:
                                handle_photo(chat_id, photo_sizes)
                            else:
                                handle_text(chat_id, text)
                    next_offset = uid + 1
                except Exception as exc:
                    print(f"[WARN] Ошибка обработки update {uid}: {exc}")
                    break
            offset = next_offset
        except KeyboardInterrupt:
            print("Остановка бота...")
            break
        except Exception as exc:
            print(f"[WARN] Ошибка polling: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    main()
