"""
Flask-веб интерфейс для генерации видео через ProxyAPI + Sora.

Роуты:
- GET  /                     -> HTML интерфейс
- POST /generate             -> запуск задачи, возвращает task_id
- GET  /status/<task_id>     -> статус и прогресс
- GET  /download/<task_id>   -> скачивание готового видео
"""

import os
import json
import sqlite3
import threading
import time
import uuid
import io
import csv
from pathlib import Path
from typing import Dict
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file

from billing import (
    apply_payment_credits_if_needed as billing_apply_payment_credits_if_needed,
    add_credits as billing_add_credits,
    count_ledger as billing_count_ledger,
    consume_credits as billing_consume_credits,
    create_payment as billing_create_payment,
    generation_credit_cost as billing_generation_credit_cost,
    get_last_payment_for_user as billing_get_last_payment_for_user,
    get_payment as billing_get_payment,
    get_payment_by_external_id as billing_get_payment_by_external_id,
    get_payment_by_order_id as billing_get_payment_by_order_id,
    get_credits as billing_get_credits,
    list_ledger as billing_list_ledger,
    list_recent_payments as billing_list_recent_payments,
    list_recent_payments_for_user as billing_list_recent_payments_for_user,
    list_users as billing_list_users,
    init_billing,
    resolve_user_id_by_token,
    set_client_token as billing_set_client_token,
    set_credits as billing_set_credits,
    update_payment as billing_update_payment,
)
from payments_tbank import TBankClient, verify_webhook_token
from providers import ProviderJobRef, VideoRequest
from services import (
    build_orchestrator,
    ensure_provider_available,
    list_provider_capabilities,
    normalize_provider,
    stub_provider_message,
    validate_generation_params,
)
from sora_client import SoraError

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv("WEB_OUTPUT_DIR", str(BASE_DIR / "web_videos")))
OUTPUT_DIR.mkdir(exist_ok=True)
INPUT_REF_DIR = OUTPUT_DIR / "_input_refs"
INPUT_REF_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.getenv("WEB_TASKS_DB_PATH", str(BASE_DIR / "web_tasks.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
BILLING_DB_PATH = Path(os.getenv("BILLING_DB_PATH", str(BASE_DIR / "billing.db")))
BILLING_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
WEB_DEFAULT_CREDITS = int(os.getenv("DEFAULT_NEW_CHAT_CREDITS", "20"))
TASK_RETENTION_HOURS = int(os.getenv("WEB_TASK_RETENTION_HOURS", "168"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("WEB_CLEANUP_INTERVAL_SECONDS", "600"))
WEB_ADMIN_TOKEN = os.getenv("WEB_ADMIN_TOKEN", "").strip()
TBANK_TERMINAL_KEY = (os.getenv("TBANK_TERMINAL_KEY") or "").strip()
TBANK_PASSWORD = (os.getenv("TBANK_PASSWORD") or "").strip()
TBANK_API_URL = (os.getenv("TBANK_API_URL") or "https://securepay.tinkoff.ru/v2").strip()
TBANK_SUCCESS_URL = (os.getenv("TBANK_SUCCESS_URL") or "").strip()
TBANK_FAIL_URL = (os.getenv("TBANK_FAIL_URL") or "").strip()
TBANK_NOTIFICATION_URL = (os.getenv("TBANK_NOTIFICATION_URL") or "").strip()
TBANK_PROVIDER_NAME = "tbank"
APP_ENV = (os.getenv("APP_ENV") or "").strip().lower()
APP_BASE_URL = (os.getenv("APP_BASE_URL") or "http://127.0.0.1:5000").strip().rstrip("/")
MOCK_PAYMENTS_TOKEN = (os.getenv("MOCK_PAYMENTS_TOKEN") or "").strip()
MOCK_PAYMENTS_ENABLED = (os.getenv("MOCK_PAYMENTS_ENABLED") or "0").strip().lower() in {"1", "true", "yes", "on"}
PAYMENT_STATUS_LABELS_RU = {
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
ADMIN_PAYMENT_STATUS_FILTERS = (
    "",
    "NEW",
    "FORM_SHOWED",
    "AUTHORIZED",
    "CONFIRMED",
    "REJECTED",
    "CANCELED",
    "DEADLINE_EXPIRED",
    "EXPIRED",
    "INIT_FAILED",
)

app = Flask(__name__)

TASKS: Dict[str, dict] = {}
TASKS_LOCK = threading.Lock()
_ORCHESTRATOR = None
_ORCHESTRATOR_LOCK = threading.Lock()
APP_STARTED_AT = time.time()


def _resolve_release_version() -> str:
    # Priority: explicit env from deploy pipeline, then local git metadata.
    for key in ("APP_VERSION", "RELEASE_VERSION", "GIT_COMMIT", "COMMIT_SHA"):
        val = (os.getenv(key) or "").strip()
        if val:
            return val

    git_meta_path = BASE_DIR / ".git"
    git_dir = git_meta_path
    if git_meta_path.is_file():
        try:
            content = git_meta_path.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                git_dir = (BASE_DIR / content.split(":", 1)[1].strip()).resolve()
        except Exception:
            return "unknown"
    head_path = git_dir / "HEAD"
    try:
        if not head_path.exists():
            return "unknown"
        head_value = head_path.read_text(encoding="utf-8").strip()
        if head_value.startswith("ref:"):
            ref_rel = head_value.split(" ", 1)[1].strip()
            ref_path = git_dir / ref_rel
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip()[:12]
            return "unknown"
        if head_value:
            return head_value[:12]
    except Exception:
        return "unknown"
    return "unknown"


RELEASE_VERSION = _resolve_release_version()


def _payment_packages() -> list[dict]:
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


def _get_package(package_id: str) -> dict | None:
    pid = (package_id or "").strip()
    for pkg in _payment_packages():
        if pkg["id"] == pid:
            return pkg
    return None


def _tbank_enabled() -> bool:
    return bool(TBANK_TERMINAL_KEY and TBANK_PASSWORD)


def _mock_payments_enabled() -> bool:
    return bool(MOCK_PAYMENTS_ENABLED and APP_ENV in {"dev", "development", "local", "test"})


def _payments_enabled() -> bool:
    return _mock_payments_enabled() or _tbank_enabled()


def _payment_provider_name() -> str:
    return "mock" if _mock_payments_enabled() else TBANK_PROVIDER_NAME


def _tbank_client() -> TBankClient:
    if not _tbank_enabled():
        raise RuntimeError("T-Bank не настроен (TBANK_TERMINAL_KEY/TBANK_PASSWORD).")
    return TBankClient(
        terminal_key=TBANK_TERMINAL_KEY,
        password=TBANK_PASSWORD,
        base_url=TBANK_API_URL,
    )


def payment_status_label_ru(status: str | None) -> str:
    code = str(status or "UNKNOWN").strip().upper()
    return PAYMENT_STATUS_LABELS_RU.get(code, code or "UNKNOWN")


def _mock_payment_url(payment_id: str) -> str:
    base = APP_BASE_URL or ""
    token_qs = f"?token={quote(MOCK_PAYMENTS_TOKEN)}" if MOCK_PAYMENTS_TOKEN else ""
    if base:
        return f"{base}/mock-pay/{payment_id}{token_qs}"
    return f"/mock-pay/{payment_id}{token_qs}"


def _mock_payment_token_ok() -> bool:
    if not MOCK_PAYMENTS_TOKEN:
        return True
    supplied = (
        request.args.get("token")
        or request.form.get("token")
        or request.headers.get("X-Mock-Payments-Token")
        or ""
    ).strip()
    return supplied == MOCK_PAYMENTS_TOKEN


def _refresh_payment_state(payment: dict) -> dict:
    if not payment:
        return payment

    payment_id = str(payment.get("payment_id") or "")
    provider_name = str(payment.get("provider") or "").strip().lower()
    ext_id = str(payment.get("external_payment_id") or "").strip()
    status = str(payment.get("status") or "").upper()
    terminal_statuses = {"CONFIRMED", "REJECTED", "CANCELED", "DEADLINE_EXPIRED", "EXPIRED"}

    if (
        _tbank_enabled()
        and provider_name != "mock"
        and ext_id
        and status not in terminal_statuses
    ):
        try:
            ext = _tbank_client().get_state(ext_id)
            payment = billing_update_payment(
                db_path=BILLING_DB_PATH,
                payment_id=payment_id,
                status=str(ext.get("Status") or payment.get("status") or "UNKNOWN"),
                external_payment_id=ext_id,
                raw=ext,
            ) or payment
        except Exception:
            pass

    if str(payment.get("status") or "").upper() == "CONFIRMED":
        try:
            billing_apply_payment_credits_if_needed(
                db_path=BILLING_DB_PATH,
                payment_id=payment_id,
                reason="payment_tbank_confirmed",
            )
            payment = billing_get_payment(BILLING_DB_PATH, payment_id) or payment
        except Exception:
            pass

    return payment


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _db_init() -> None:
    with _db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                user_id TEXT,
                provider TEXT,
                prompt TEXT,
                seconds INTEGER,
                model TEXT,
                size TEXT,
                credits_spent INTEGER,
                credits_after INTEGER,
                credits_refunded INTEGER DEFAULT 0,
                status TEXT,
                progress INTEGER,
                error TEXT,
                video_id TEXT,
                file_path TEXT,
                file_ready INTEGER DEFAULT 0,
                created_at INTEGER,
                updated_at INTEGER
            )
            """
        )
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "user_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN user_id TEXT")
        if "provider" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN provider TEXT")
        if "credits_spent" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN credits_spent INTEGER")
        if "credits_after" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN credits_after INTEGER")
        if "credits_refunded" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN credits_refunded INTEGER DEFAULT 0")


def _db_upsert_task(task_id: str, task: dict) -> None:
    now_ts = int(time.time())
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, user_id, provider, prompt, seconds, model, size, credits_spent, credits_after, credits_refunded,
                status, progress, error, video_id, file_path, file_ready, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                user_id=excluded.user_id,
                provider=excluded.provider,
                prompt=excluded.prompt,
                seconds=excluded.seconds,
                model=excluded.model,
                size=excluded.size,
                credits_spent=excluded.credits_spent,
                credits_after=excluded.credits_after,
                credits_refunded=excluded.credits_refunded,
                status=excluded.status,
                progress=excluded.progress,
                error=excluded.error,
                video_id=excluded.video_id,
                file_path=excluded.file_path,
                file_ready=excluded.file_ready,
                updated_at=excluded.updated_at
            """,
            (
                task_id,
                task.get("user_id"),
                task.get("provider", "sora"),
                task.get("prompt"),
                task.get("seconds"),
                task.get("model"),
                task.get("size"),
                task.get("credits_spent"),
                task.get("credits_after"),
                1 if task.get("credits_refunded") else 0,
                task.get("status"),
                int(task.get("progress", 0) or 0),
                task.get("error"),
                task.get("video_id"),
                task.get("file_path"),
                1 if task.get("file_ready") else 0,
                int(task.get("created_at", now_ts)),
                now_ts,
            ),
        )


def _db_get_task(task_id: str) -> dict:
    with _db_connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return {}
    data = dict(row)
    data["file_ready"] = bool(data.get("file_ready"))
    return data


def _db_get_expired_tasks(cutoff_ts: int) -> list:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT task_id, file_path
            FROM tasks
            WHERE updated_at < ?
              AND status IN ('completed', 'failed')
            """,
            (cutoff_ts,),
        ).fetchall()
    return [dict(r) for r in rows]


def _db_delete_tasks(task_ids: list[str]) -> None:
    if not task_ids:
        return
    placeholders = ",".join("?" for _ in task_ids)
    with _db_connect() as conn:
        conn.execute(f"DELETE FROM tasks WHERE task_id IN ({placeholders})", task_ids)


def _db_provider_stats(user_id: str | None = None, limit: int = 1500) -> list[dict]:
    limit = max(int(limit), 1)
    where_sql = ""
    params: tuple = (limit,)
    if user_id:
        where_sql = "WHERE user_id = ?"
        params = (str(user_id), limit)

    with _db_connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(provider, ''), 'unknown') AS provider,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status IN ('completed', 'failed') THEN 1 ELSE 0 END) AS finished,
                COUNT(*) AS total
            FROM (
                SELECT provider, status
                FROM tasks
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ?
            ) recent
            GROUP BY COALESCE(NULLIF(provider, ''), 'unknown')
            ORDER BY finished DESC, total DESC
            """
            ,
            params,
        ).fetchall()

    out = []
    for row in rows:
        data = dict(row)
        finished = int(data.get("finished") or 0)
        completed = int(data.get("completed") or 0)
        success_rate = round((completed / finished) * 100, 1) if finished > 0 else 0.0
        data["success_rate"] = success_rate
        out.append(data)
    return out


def _db_top_errors(user_id: str | None = None, limit: int = 2000, top_n: int = 8) -> list[dict]:
    limit = max(int(limit), 1)
    top_n = max(int(top_n), 1)
    where_sql = "WHERE status = 'failed' AND error IS NOT NULL AND TRIM(error) != ''"
    params: tuple = (limit, top_n)
    if user_id:
        where_sql += " AND user_id = ?"
        params = (str(user_id), limit, top_n)

    with _db_connect() as conn:
        rows = conn.execute(
            f"""
            SELECT error, COUNT(*) AS count
            FROM (
                SELECT error
                FROM tasks
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ?
            ) recent_failed
            GROUP BY error
            ORDER BY count DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    out = []
    for row in rows:
        data = dict(row)
        msg = str(data.get("error") or "").strip()
        if len(msg) > 220:
            msg = msg[:220] + "..."
        data["error"] = msg
        out.append(data)
    return out


def _db_veo_upstream_unavailable_count(user_id: str | None = None, limit: int = 2000) -> int:
    limit = max(int(limit), 1)
    where_sql = "WHERE status = 'failed' AND COALESCE(provider, '') = 'veo'"
    params: tuple = (limit,)
    if user_id:
        where_sql += " AND user_id = ?"
        params = (str(user_id), limit)

    with _db_connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM (
                SELECT error
                FROM tasks
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ?
            ) recent_failed
            WHERE
                LOWER(COALESCE(error, '')) LIKE '%service_disabled%'
                OR LOWER(COALESCE(error, '')) LIKE '%api_key_invalid%'
                OR LOWER(COALESCE(error, '')) LIKE '%generative language api has not been used%'
            """
            ,
            params,
        ).fetchone()
    return int((row["c"] if row else 0) or 0)


def _normalize_payment_status_filter(value: str | None) -> str:
    code = str(value or "").strip().upper()
    return code if code in ADMIN_PAYMENT_STATUS_FILTERS else ""


def _billing_recent_payments(
    user_id: str | None = None,
    limit: int = 20,
    status: str | None = None,
) -> list[dict]:
    normalized_status = _normalize_payment_status_filter(status)
    fetch_limit = limit
    if normalized_status:
        # Берем более широкий recent window, затем фильтруем уже после refresh,
        # чтобы не потерять платежи с устаревшим локальным status.
        fetch_limit = max(limit * 10, 100)
    rows = billing_list_recent_payments(
        BILLING_DB_PATH,
        limit=fetch_limit,
        user_id=user_id,
        status=None,
    )
    out = []
    for row in rows:
        item = _refresh_payment_state(dict(row))
        item_status = str(item.get("status") or "").strip().upper()
        if normalized_status and item_status != normalized_status:
            continue
        raw_mode = ""
        raw_json = item.get("raw_json")
        if raw_json:
            try:
                raw_data = json.loads(str(raw_json))
                raw_mode = str(raw_data.get("mode") or "").strip()
            except Exception:
                raw_mode = ""
        out.append(
            {
                "payment_id": item.get("payment_id"),
                "provider": item.get("provider"),
                "raw_mode": raw_mode,
                "user_id": item.get("user_id"),
                "package_id": item.get("package_id"),
                "amount_rub": item.get("amount_rub"),
                "credits": item.get("credits"),
                "status": item.get("status"),
                "status_label": payment_status_label_ru(item.get("status")),
                "credits_applied": bool(item.get("credits_applied")),
                "created_at": item.get("created_at"),
                "credited_at": item.get("credited_at"),
                "payment_url": item.get("payment_url"),
                "external_payment_id": item.get("external_payment_id"),
            }
        )
        if len(out) >= limit:
            break
    return out


def _build_admin_metrics_csv(
    provider_stats: list[dict],
    top_errors: list[dict],
    user_id_filter: str | None = None,
) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["section", "field", "value", "user_id_filter"])

    writer.writerow(["meta", "generated_at", int(time.time()), user_id_filter or ""])
    writer.writerow(["meta", "provider_rows", len(provider_stats), user_id_filter or ""])
    writer.writerow(["meta", "error_rows", len(top_errors), user_id_filter or ""])
    writer.writerow(
        [
            "meta",
            "veo_upstream_unavailable_count",
            _db_veo_upstream_unavailable_count(user_id_filter, limit=2000),
            user_id_filter or "",
        ]
    )

    writer.writerow([])
    writer.writerow(["provider_stats", "provider", "completed", "failed", "finished", "success_rate"])
    for row in provider_stats:
        writer.writerow(
            [
                "provider_stats",
                row.get("provider", "unknown"),
                row.get("completed", 0),
                row.get("failed", 0),
                row.get("finished", 0),
                row.get("success_rate", 0.0),
            ]
        )

    writer.writerow([])
    writer.writerow(["top_errors", "count", "error"])
    for row in top_errors:
        writer.writerow(["top_errors", row.get("count", 0), row.get("error", "")])

    return buf.getvalue()


def _render_admin_template(
    *,
    token: str,
    filter_user_id: str | None,
    page: int,
    page_size: int,
    payment_status_filter: str,
    message: str = "",
    error: str = "",
):
    users = billing_list_users(BILLING_DB_PATH, limit=200)
    total_ledger = billing_count_ledger(BILLING_DB_PATH, user_id=filter_user_id)
    offset = (page - 1) * page_size
    ledger = billing_list_ledger(
        BILLING_DB_PATH,
        limit=page_size,
        offset=offset,
        user_id=filter_user_id,
    )
    has_prev = page > 1
    has_next = offset + len(ledger) < total_ledger
    provider_stats = _db_provider_stats(user_id=filter_user_id, limit=1500)
    top_errors = _db_top_errors(user_id=filter_user_id, limit=2000, top_n=8)
    recent_payments = _billing_recent_payments(
        user_id=filter_user_id,
        limit=20,
        status=payment_status_filter,
    )
    veo_upstream_unavailable_count = _db_veo_upstream_unavailable_count(
        user_id=filter_user_id,
        limit=2000,
    )
    return render_template(
        "admin.html",
        admin_token=token,
        users=users,
        ledger=ledger,
        filter_user_id=filter_user_id or "",
        payment_status_filter=payment_status_filter,
        payment_status_options=ADMIN_PAYMENT_STATUS_FILTERS,
        payment_status_labels=PAYMENT_STATUS_LABELS_RU,
        page=page,
        page_size=page_size,
        total_ledger=total_ledger,
        has_prev=has_prev,
        has_next=has_next,
        provider_stats=provider_stats,
        top_errors=top_errors,
        recent_payments=recent_payments,
        veo_upstream_unavailable_count=veo_upstream_unavailable_count,
        mock_payments_enabled=_mock_payments_enabled(),
        message=message,
        error=error,
    )


def _cleanup_old_tasks_once() -> int:
    retention_seconds = max(TASK_RETENTION_HOURS, 1) * 3600
    cutoff_ts = int(time.time()) - retention_seconds
    expired = _db_get_expired_tasks(cutoff_ts)
    if not expired:
        return 0

    deleted_ids = []
    for task in expired:
        task_id = task["task_id"]
        file_path = task.get("file_path")
        if file_path:
            try:
                p = Path(file_path)
                if p.exists():
                    p.unlink()
            except Exception:
                # Не прерываем очистку, если файл удалить не получилось.
                pass
        deleted_ids.append(task_id)

    _db_delete_tasks(deleted_ids)
    with TASKS_LOCK:
        for task_id in deleted_ids:
            TASKS.pop(task_id, None)
    return len(deleted_ids)


def _cleanup_loop() -> None:
    while True:
        try:
            _cleanup_old_tasks_once()
        except Exception:
            # Фоновая очистка не должна падать насовсем.
            pass
        time.sleep(max(CLEANUP_INTERVAL_SECONDS, 30))


def _start_cleanup_thread() -> None:
    t = threading.Thread(target=_cleanup_loop, daemon=True, name="web-task-cleanup")
    t.start()


_db_init()
init_billing(BILLING_DB_PATH)
_cleanup_old_tasks_once()
_start_cleanup_thread()


def _require_api_key() -> str:
    api_key = os.getenv("PROXYAPI_KEY")
    if not api_key:
        raise RuntimeError("Не найден PROXYAPI_KEY в переменных окружения.")
    return api_key


def _get_orchestrator():
    global _ORCHESTRATOR
    if _ORCHESTRATOR is not None:
        return _ORCHESTRATOR
    with _ORCHESTRATOR_LOCK:
        if _ORCHESTRATOR is None:
            _ORCHESTRATOR = build_orchestrator(api_key=_require_api_key())
    return _ORCHESTRATOR


def _refund_task_credits(task_id: str, reason: str) -> None:
    task = _get_task(task_id)
    if not task:
        return
    if bool(task.get("credits_refunded")):
        return
    amount = int(task.get("credits_spent") or 0)
    user_id = str(task.get("user_id") or "").strip()
    if amount <= 0 or not user_id:
        return
    try:
        new_balance = billing_add_credits(
            db_path=BILLING_DB_PATH,
            user_id=user_id,
            amount=amount,
            reason=reason,
        )
        _set_task(task_id, credits_refunded=True, credits_after=new_balance)
    except Exception:
        # Не валим основной поток из-за ошибки возврата, но оставляем статус/refund флаг без изменений.
        pass


def _try_reconcile_task(task_id: str, task: dict) -> dict:
    """
    Self-heal для long-running задач:
    если локальный worker был прерван (например, из-за перезапуска debug-сервера),
    пытаемся дожать статус и скачать файл по video_id.
    """
    status = (task.get("status") or "").strip().lower()
    if status not in {"queued", "in_progress"}:
        return task

    video_id = (task.get("video_id") or "").strip()
    if not video_id:
        return task

    provider_name = (task.get("provider") or "sora").strip().lower() or "sora"
    file_path_raw = task.get("file_path") or ""
    if not file_path_raw:
        return task

    try:
        orchestrator = _get_orchestrator()
        job = orchestrator.factory.get(provider_name)
        # reuse provider status API to refresh stale in-memory/db state
        provider_status = job.get_status(ProviderJobRef(provider=provider_name, external_id=video_id))
    except Exception:
        return task

    if provider_status.status == "failed":
        _refund_task_credits(task_id, reason="web_generation_refund_failed")
        _set_task(task_id, status="failed", progress=0, error=provider_status.error or "Генерация не удалась")
        return _get_task(task_id)

    if provider_status.status == "completed":
        try:
            orch = _get_orchestrator()
            provider = orch.factory.get(provider_name)
            provider.download(
                ProviderJobRef(provider=provider_name, external_id=video_id),
                output_path=str(file_path_raw),
            )
            _set_task(task_id, status="completed", progress=100, file_ready=True, error=None)
        except Exception as exc:
            _refund_task_credits(task_id, reason="web_generation_refund_download_error")
            _set_task(task_id, status="failed", progress=0, error=str(exc))
        return _get_task(task_id)

    fresh_progress = max(0, min(100, int(provider_status.progress or 0)))
    if fresh_progress != int(task.get("progress", 0) or 0):
        _set_task(task_id, status=provider_status.status, progress=fresh_progress)
        return _get_task(task_id)
    return task


def _health_snapshot() -> dict:
    checks: dict[str, dict] = {}

    try:
        with _db_connect() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["tasks_db"] = {"ok": True}
    except Exception as exc:
        checks["tasks_db"] = {"ok": False, "error": str(exc)}

    try:
        with sqlite3.connect(BILLING_DB_PATH) as conn:
            conn.execute("SELECT 1").fetchone()
        checks["billing_db"] = {"ok": True}
    except Exception as exc:
        checks["billing_db"] = {"ok": False, "error": str(exc)}

    try:
        orch = _get_orchestrator()
        checks["providers"] = {"ok": True, "names": list(orch.factory.list_names())}
    except Exception as exc:
        checks["providers"] = {"ok": False, "error": str(exc)}

    checks["config"] = {"ok": True, "has_proxyapi_key": bool(os.getenv("PROXYAPI_KEY"))}

    overall_ok = all(v.get("ok", False) for v in checks.values())
    return {
        "ok": overall_ok,
        "checks": checks,
        "release_version": RELEASE_VERSION,
        "uptime_seconds": int(max(0, time.time() - APP_STARTED_AT)),
        "ts": int(time.time()),
    }


def _extract_admin_token(data: dict | None = None) -> str:
    return (
        request.headers.get("X-Admin-Token")
        or (data.get("admin_token") if data else None)
        or request.args.get("token")
        or ""
    ).strip()


def _is_admin_authorized(token: str) -> bool:
    return bool(WEB_ADMIN_TOKEN) and token == WEB_ADMIN_TOKEN


def _parse_int(value: str, default: int, min_value: int = 1, max_value: int = 1000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min(parsed, max_value), min_value)


def _set_task(task_id: str, **kwargs) -> None:
    with TASKS_LOCK:
        if task_id not in TASKS:
            TASKS[task_id] = {}
        TASKS[task_id].update(kwargs)
        _db_upsert_task(task_id, TASKS[task_id])


def _get_task(task_id: str) -> dict:
    with TASKS_LOCK:
        in_memory = dict(TASKS.get(task_id, {}))
    if in_memory:
        return in_memory
    db_task = _db_get_task(task_id)
    if db_task:
        with TASKS_LOCK:
            TASKS[task_id] = dict(db_task)
    return db_task


def _worker_generate(
    task_id: str,
    provider: str,
    prompt: str,
    seconds: int,
    model: str,
    size: str,
    input_reference_path: str | None = None,
    input_reference_paths: list[str] | None = None,
    reference_image_type: str = "asset",
    remix_source_video_id: str | None = None,
) -> None:
    output_path = OUTPUT_DIR / f"{task_id}.mp4"
    _set_task(
        task_id,
        status="queued",
        progress=0,
        error=None,
        video_id=None,
        file_path=str(output_path),
    )

    try:
        orchestrator = build_orchestrator(api_key=_require_api_key())
        provider_name = provider

        if remix_source_video_id:
            job = orchestrator.start_remix(
                provider_name=provider_name,
                source_video_id=remix_source_video_id,
                prompt=prompt,
            )
        else:
            request_model = VideoRequest(
                provider=provider_name,
                prompt=prompt,
                seconds=seconds,
                model=model,
                size=size,
                input_reference_path=input_reference_path,
                input_reference_paths=input_reference_paths,
                reference_image_type=reference_image_type,
            )
            job = orchestrator.start_generation(request_model)
        video_id = job.external_id
        _set_task(
            task_id,
            provider=provider_name,
            status=job.status,
            progress=0,
            video_id=video_id,
        )
        status = orchestrator.wait_until_done(
            provider_name=provider_name,
            job=job,
            poll_interval_sec=2.5,
            on_progress=lambda s, p, e: _set_task(task_id, status=s, progress=p, error=e or None),
        )
        if status.status == "failed":
            raise SoraError(status.error or "Генерация видео не удалась")

        orchestrator.download_result(provider_name=provider_name, job=job, output_path=str(output_path))
        _set_task(task_id, status="completed", progress=100, file_ready=True)

    except Exception as exc:
        _refund_task_credits(task_id, reason="web_generation_refund_worker_error")
        _set_task(task_id, status="failed", error=str(exc), progress=0)
    finally:
        if input_reference_path:
            try:
                p = Path(input_reference_path)
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        for p_raw in input_reference_paths or []:
            try:
                p = Path(p_raw)
                if p.exists():
                    p.unlink()
            except Exception:
                pass


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    snapshot = _health_snapshot()
    status_code = 200 if snapshot["ok"] else 503
    return jsonify(snapshot), status_code


@app.get("/admin")
def admin_page():
    token = _extract_admin_token()
    if not WEB_ADMIN_TOKEN:
        return "WEB_ADMIN_TOKEN не задан в окружении.", 503
    if not _is_admin_authorized(token):
        return "Unauthorized", 401

    filter_user_id = (request.args.get("user_id") or "").strip() or None
    payment_status_filter = _normalize_payment_status_filter(request.args.get("payment_status"))
    page = _parse_int(request.args.get("page"), default=1, min_value=1, max_value=100000)
    page_size = _parse_int(request.args.get("page_size"), default=20, min_value=5, max_value=200)
    return _render_admin_template(
        token=token,
        filter_user_id=filter_user_id,
        page=page,
        page_size=page_size,
        payment_status_filter=payment_status_filter,
        message="",
        error="",
    )


@app.post("/admin/action")
def admin_action():
    form = request.form
    token = _extract_admin_token(form)
    if not WEB_ADMIN_TOKEN:
        return "WEB_ADMIN_TOKEN не задан в окружении.", 503
    if not _is_admin_authorized(token):
        return "Unauthorized", 401

    action = (form.get("action") or "").strip()
    user_id = (form.get("user_id") or "").strip()
    payment_status_filter = _normalize_payment_status_filter(form.get("payment_status"))
    page = _parse_int(form.get("page"), default=1, min_value=1, max_value=100000)
    page_size = _parse_int(form.get("page_size"), default=20, min_value=5, max_value=200)
    quick_amount = (form.get("quick_amount") or "").strip()
    message = ""
    error = ""

    try:
        if not user_id:
            raise ValueError("user_id обязателен.")

        if quick_amount:
            action = "add_credits"
            form_amount = int(quick_amount)
        else:
            form_amount = None

        if action == "add_credits":
            amount = int(form_amount if form_amount is not None else form.get("amount", "0"))
            if amount <= 0:
                raise ValueError("amount должен быть > 0")
            balance = billing_add_credits(
                BILLING_DB_PATH,
                user_id=user_id,
                amount=amount,
                reason="web_admin_add",
            )
            message = f"Начислено {amount}. Баланс {user_id}: {balance}"
        elif action == "set_credits":
            amount = int(form_amount if form_amount is not None else form.get("amount", "0"))
            if amount < 0:
                raise ValueError("amount должен быть >= 0")
            balance = billing_set_credits(
                BILLING_DB_PATH,
                user_id=user_id,
                amount=amount,
                reason="web_admin_set",
            )
            message = f"Установлен баланс {user_id}: {balance}"
        elif action == "set_token":
            client_token = (form.get("client_token") or "").strip()
            if not client_token:
                raise ValueError("client_token обязателен.")
            billing_set_client_token(
                BILLING_DB_PATH,
                user_id=user_id,
                token=client_token,
                default_credits=WEB_DEFAULT_CREDITS,
            )
            message = f"Токен для {user_id} обновлен."
        else:
            raise ValueError("Неизвестное действие.")
    except Exception as exc:
        error = str(exc)

    return _render_admin_template(
        token=token,
        filter_user_id=user_id or None,
        page=page,
        page_size=page_size,
        payment_status_filter=payment_status_filter,
        message=message,
        error=error,
    )


@app.post("/admin/payment_action")
def admin_payment_action():
    form = request.form
    token = _extract_admin_token(form)
    if not WEB_ADMIN_TOKEN:
        return "WEB_ADMIN_TOKEN не задан в окружении.", 503
    if not _is_admin_authorized(token):
        return "Unauthorized", 401

    filter_user_id = (form.get("user_id") or "").strip() or None
    payment_status_filter = _normalize_payment_status_filter(form.get("payment_status"))
    page = _parse_int(form.get("page"), default=1, min_value=1, max_value=100000)
    page_size = _parse_int(form.get("page_size"), default=20, min_value=5, max_value=200)
    payment_id = (form.get("payment_id") or "").strip()
    action = (form.get("action") or "").strip()
    message = ""
    error = ""

    try:
        if action not in {"confirm_mock", "cancel_mock"}:
            raise ValueError("Неизвестное действие платежа.")
        if not _mock_payments_enabled():
            raise ValueError("Mock payments выключен.")
        if not payment_id:
            raise ValueError("payment_id обязателен.")
        payment = billing_get_payment(BILLING_DB_PATH, payment_id)
        if not payment:
            raise ValueError("Платеж не найден.")
        if str(payment.get("provider") or "") != "mock":
            raise ValueError("Подтверждение доступно только для mock-платежей.")

        status = str(payment.get("status") or "").upper()
        if status in {"CANCELED", "REJECTED", "DEADLINE_EXPIRED", "EXPIRED"}:
            raise ValueError(f"Нельзя подтвердить платеж со статусом {status}.")
        if action == "confirm_mock":
            billing_update_payment(
                db_path=BILLING_DB_PATH,
                payment_id=payment_id,
                status="CONFIRMED",
                raw={"mode": "mock", "action": "confirm_from_admin"},
            )
            applied, balance = billing_apply_payment_credits_if_needed(
                db_path=BILLING_DB_PATH,
                payment_id=payment_id,
                reason="payment_mock_confirmed_admin",
            )
            if applied:
                message = f"Mock-платеж {payment_id} подтвержден. Баланс пользователя: {balance}."
            else:
                message = f"Mock-платеж {payment_id} уже был подтвержден ранее. Баланс: {balance}."
        else:
            if status == "CONFIRMED":
                raise ValueError("Нельзя отменить уже подтвержденный mock-платеж.")
            billing_update_payment(
                db_path=BILLING_DB_PATH,
                payment_id=payment_id,
                status="CANCELED",
                raw={"mode": "mock", "action": "cancel_from_admin"},
            )
            message = f"Mock-платеж {payment_id} отменен."
    except Exception as exc:
        error = str(exc)

    return _render_admin_template(
        token=token,
        filter_user_id=filter_user_id,
        page=page,
        page_size=page_size,
        payment_status_filter=payment_status_filter,
        message=message,
        error=error,
    )


@app.get("/admin/export")
def admin_export():
    token = _extract_admin_token()
    if not WEB_ADMIN_TOKEN:
        return "WEB_ADMIN_TOKEN не задан в окружении.", 503
    if not _is_admin_authorized(token):
        return "Unauthorized", 401

    user_id = (request.args.get("user_id") or "").strip() or None
    provider_stats = _db_provider_stats(user_id=user_id, limit=1500)
    top_errors = _db_top_errors(user_id=user_id, limit=2000, top_n=50)
    csv_content = _build_admin_metrics_csv(
        provider_stats=provider_stats,
        top_errors=top_errors,
        user_id_filter=user_id,
    )
    ts = int(time.time())
    suffix = user_id or "all"
    filename = f"admin_metrics_{suffix}_{ts}.csv"
    return Response(
        csv_content,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/generate")
def generate():
    payload_json = request.get_json(silent=True)
    payload = payload_json if payload_json is not None else request.form

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Пустой prompt"}), 400

    try:
        seconds = int(payload.get("seconds", 4))
    except ValueError:
        return jsonify({"error": "seconds должен быть числом"}), 400

    model = payload.get("model", "sora-2")
    size = payload.get("size", "1280x720")
    provider = normalize_provider(payload.get("provider"), default_provider="sora")
    reference_image_type = (payload.get("input_reference_type") or "asset").strip().lower()
    remix_source_video_id = (payload.get("remix_source_video_id") or "").strip()
    try:
        caps = validate_generation_params(
            orchestrator=_get_orchestrator(),
            provider=provider,
            seconds=seconds,
            model=model,
            size=size,
        )
        ensure_provider_available(caps)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    if remix_source_video_id and not caps.supports_remix:
        return jsonify({"error": f"provider '{provider}' не поддерживает remix"}), 400

    has_input_reference = False
    input_references_count = 0
    if payload_json is None:
        input_reference_file = request.files.get("input_reference")
        has_input_reference = bool(input_reference_file and input_reference_file.filename)
        input_references_count = sum(
            1 for f in request.files.getlist("input_references") if f and f.filename
        )
    if has_input_reference and input_references_count:
        return jsonify({"error": "Нельзя передавать одновременно input_reference и input_references"}), 400
    if input_references_count:
        if not caps.supports_reference_images:
            return jsonify({"error": f"provider '{provider}' не поддерживает referenceImages"}), 400
        if caps.max_reference_images and input_references_count > caps.max_reference_images:
            return jsonify(
                {"error": f"Максимум referenceImages: {caps.max_reference_images}"}
            ), 400
        if caps.supported_reference_types and reference_image_type not in caps.supported_reference_types:
            return jsonify(
                {
                    "error": (
                        f"input_reference_type должен быть одним из "
                        f"{caps.supported_reference_types}"
                    )
                }
            ), 400
        if provider == "veo":
            if seconds != 8:
                return jsonify({"error": "Для Veo referenceImages требуется seconds=8"}), 400
            if size != "1280x720":
                return jsonify({"error": "Для Veo referenceImages требуется size=1280x720"}), 400
            if reference_image_type == "style" and input_references_count != 1:
                return jsonify({"error": "Для Veo referenceType=style нужно ровно 1 изображение"}), 400

    client_token = (
        request.headers.get("X-Client-Token")
        or payload.get("client_token")
        or ""
    ).strip()
    if not client_token:
        return jsonify({"error": "Не передан client_token"}), 401

    user_id = resolve_user_id_by_token(BILLING_DB_PATH, client_token)
    if not user_id:
        return jsonify({"error": "Неверный client_token"}), 401

    credit_cost = billing_generation_credit_cost(seconds=seconds, model=model, provider=provider)
    ok, credits_after = billing_consume_credits(
        db_path=BILLING_DB_PATH,
        user_id=user_id,
        amount=credit_cost,
        reason="web_generation_consume",
        meta={
            "provider": provider,
            "seconds": seconds,
            "model": model,
            "size": size,
            "input_reference_type": reference_image_type if input_references_count else None,
        },
        default_credits=WEB_DEFAULT_CREDITS,
    )
    if not ok:
        return (
            jsonify(
                {
                    "error": "Недостаточно кредитов",
                    "credits_required": credit_cost,
                    "credits_available": credits_after,
                }
            ),
            402,
        )

    task_id = str(uuid.uuid4())
    input_reference_path = None
    input_reference_paths: list[str] = []
    if payload_json is None:
        input_reference_file = request.files.get("input_reference")
        if input_reference_file and input_reference_file.filename:
            safe_name = Path(input_reference_file.filename).name
            ref_path = INPUT_REF_DIR / f"{task_id}_{safe_name}"
            input_reference_file.save(ref_path)
            input_reference_path = str(ref_path)
        input_reference_files = [f for f in request.files.getlist("input_references") if f and f.filename]
        for i, f in enumerate(input_reference_files[: (caps.max_reference_images or 3)]):
            if not f or not f.filename:
                continue
            safe_name = Path(f.filename).name
            ref_path = INPUT_REF_DIR / f"{task_id}_ref{i+1}_{safe_name}"
            f.save(ref_path)
            input_reference_paths.append(str(ref_path))

    _set_task(
        task_id,
        user_id=user_id,
        provider=provider,
        prompt=prompt,
        seconds=seconds,
        model=model,
        size=size,
        remix_source_video_id=remix_source_video_id or None,
        credits_spent=credit_cost,
        credits_after=credits_after,
        credits_refunded=False,
        status="queued",
        progress=0,
        error=None,
        file_ready=False,
    )

    thread = threading.Thread(
        target=_worker_generate,
        args=(
            task_id,
            provider,
            prompt,
            seconds,
            model,
            size,
            input_reference_path,
            input_reference_paths or None,
            reference_image_type,
            remix_source_video_id or None,
        ),
        daemon=True,
    )
    thread.start()

    return jsonify(
        {
            "task_id": task_id,
            "provider": provider,
            "credits_spent": credit_cost,
            "credits_after": credits_after,
        }
    ), 202


@app.get("/providers")
def providers():
    try:
        orchestrator = _get_orchestrator()
        out = {}
        for name, caps in list_provider_capabilities(orchestrator).items():
            out[name] = {
                "supports_remix": caps.supports_remix,
                "supports_input_reference": caps.supports_input_reference,
                "supports_reference_images": caps.supports_reference_images,
                "max_reference_images": int(caps.max_reference_images or 0),
                "supported_reference_types": list(caps.supported_reference_types or ()),
                "allowed_seconds": list(caps.allowed_seconds),
                "allowed_models": list(caps.allowed_models),
                "allowed_sizes": list(caps.allowed_sizes),
                "is_stub": bool(caps.is_stub),
                "stub_message": stub_provider_message(name) if caps.is_stub else "",
            }
        return jsonify({"default_provider": "sora", "providers": out})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/credits")
def credits():
    client_token = (request.args.get("client_token") or "").strip()
    if not client_token:
        return jsonify({"error": "Не передан client_token"}), 401

    user_id = resolve_user_id_by_token(BILLING_DB_PATH, client_token)
    if not user_id:
        return jsonify({"error": "Неверный client_token"}), 401

    balance = billing_get_credits(
        db_path=BILLING_DB_PATH,
        user_id=user_id,
        default_credits=WEB_DEFAULT_CREDITS,
    )
    return jsonify({"user_id": user_id, "credits": balance})


@app.get("/payments/packages")
def payments_packages():
    return jsonify(
        {
            "provider": _payment_provider_name(),
            "enabled": _payments_enabled(),
            "mode": "mock" if _mock_payments_enabled() else "tbank",
            "packages": _payment_packages(),
        }
    )


@app.post("/payments/create")
def payments_create():
    payload = request.get_json(silent=True) or {}
    client_token = (
        request.headers.get("X-Client-Token")
        or payload.get("client_token")
        or ""
    ).strip()
    if not client_token:
        return jsonify({"error": "Не передан client_token"}), 401
    user_id = resolve_user_id_by_token(BILLING_DB_PATH, client_token)
    if not user_id:
        return jsonify({"error": "Неверный client_token"}), 401
    if not _payments_enabled():
        return jsonify({"error": "Платежи временно отключены"}), 503

    package_id = (payload.get("package_id") or "").strip()
    package = _get_package(package_id)
    if not package:
        return jsonify({"error": "Неизвестный package_id"}), 400

    order_id = f"ord_{uuid.uuid4().hex[:20]}"
    payment = billing_create_payment(
        db_path=BILLING_DB_PATH,
        provider=_payment_provider_name(),
        user_id=str(user_id),
        client_token=client_token,
        package_id=package["id"],
        amount_rub=float(package["rub"]),
        credits=int(package["credits"]),
        order_id=order_id,
        meta={"source": "web", "package_title": package.get("title", package["id"])},
    )
    try:
        if _mock_payments_enabled():
            payment = billing_update_payment(
                db_path=BILLING_DB_PATH,
                payment_id=payment["payment_id"],
                status="FORM_SHOWED",
                external_payment_id=f"mock_{payment['payment_id']}",
                payment_url=_mock_payment_url(payment["payment_id"]),
                raw={"mode": "mock", "status": "FORM_SHOWED"},
            ) or payment
        else:
            tbank = _tbank_client()
            ext = tbank.init_payment(
                order_id=order_id,
                amount_rub=float(package["rub"]),
                description=f"Пакет {package.get('title', package['id'])}: {package['credits']} кредитов",
                customer_key=str(user_id),
                success_url=TBANK_SUCCESS_URL or None,
                fail_url=TBANK_FAIL_URL or None,
                notification_url=TBANK_NOTIFICATION_URL or None,
                metadata={"user_id": str(user_id), "package_id": package["id"]},
            )
            payment = billing_update_payment(
                db_path=BILLING_DB_PATH,
                payment_id=payment["payment_id"],
                status=str(ext.get("Status") or "FORM_SHOWED"),
                external_payment_id=str(ext.get("PaymentId") or "") or None,
                payment_url=str(ext.get("PaymentURL") or "") or None,
                raw=ext,
            ) or payment
    except Exception as exc:
        payment = billing_update_payment(
            db_path=BILLING_DB_PATH,
            payment_id=payment["payment_id"],
            status="INIT_FAILED",
            raw={"error": str(exc)},
        ) or payment
        return jsonify({"error": f"Не удалось инициализировать платеж: {exc}"}), 502

    return jsonify(
        {
            "payment_id": payment["payment_id"],
            "order_id": payment["order_id"],
            "status": payment["status"],
            "status_label": payment_status_label_ru(payment.get("status")),
            "payment_url": payment.get("payment_url"),
            "amount_rub": payment["amount_rub"],
            "credits": payment["credits"],
            "package_id": payment["package_id"],
            "provider": payment.get("provider"),
        }
    )


@app.get("/payments/status/<payment_id>")
def payments_status(payment_id: str):
    client_token = (
        request.headers.get("X-Client-Token")
        or request.args.get("client_token")
        or ""
    ).strip()
    if not client_token:
        return jsonify({"error": "Не передан client_token"}), 401
    user_id = resolve_user_id_by_token(BILLING_DB_PATH, client_token)
    if not user_id:
        return jsonify({"error": "Неверный client_token"}), 401

    payment = billing_get_payment(BILLING_DB_PATH, payment_id)
    if not payment:
        return jsonify({"error": "payment_id не найден"}), 404
    if str(payment.get("user_id")) != str(user_id):
        return jsonify({"error": "Доступ запрещен"}), 403

    payment = _refresh_payment_state(payment)

    return jsonify(
        {
            "payment_id": payment["payment_id"],
            "status": payment["status"],
            "status_label": payment_status_label_ru(payment.get("status")),
            "amount_rub": payment["amount_rub"],
            "credits": payment["credits"],
            "credits_applied": bool(payment.get("credits_applied")),
            "payment_url": payment.get("payment_url"),
            "order_id": payment.get("order_id"),
            "external_payment_id": payment.get("external_payment_id"),
        }
    )


@app.get("/payments/last")
def payments_last():
    client_token = (
        request.headers.get("X-Client-Token")
        or request.args.get("client_token")
        or ""
    ).strip()
    if not client_token:
        return jsonify({"error": "Не передан client_token"}), 401
    user_id = resolve_user_id_by_token(BILLING_DB_PATH, client_token)
    if not user_id:
        return jsonify({"error": "Неверный client_token"}), 401

    payment = billing_get_last_payment_for_user(BILLING_DB_PATH, user_id=str(user_id))
    if not payment:
        return jsonify({"payment": None})
    payment = _refresh_payment_state(payment)
    return jsonify(
        {
            "payment": {
                "payment_id": payment["payment_id"],
                "status": payment["status"],
                "status_label": payment_status_label_ru(payment.get("status")),
                "amount_rub": payment["amount_rub"],
                "credits": payment["credits"],
                "credits_applied": bool(payment.get("credits_applied")),
                "payment_url": payment.get("payment_url"),
                "order_id": payment.get("order_id"),
                "external_payment_id": payment.get("external_payment_id"),
            }
        }
    )


@app.get("/payments/recent")
def payments_recent():
    client_token = (
        request.headers.get("X-Client-Token")
        or request.args.get("client_token")
        or ""
    ).strip()
    if not client_token:
        return jsonify({"error": "Не передан client_token"}), 401
    user_id = resolve_user_id_by_token(BILLING_DB_PATH, client_token)
    if not user_id:
        return jsonify({"error": "Неверный client_token"}), 401

    limit_raw = request.args.get("limit") or "3"
    try:
        limit = int(limit_raw)
    except Exception:
        limit = 3
    limit = max(1, min(limit, 10))

    payments = billing_list_recent_payments_for_user(
        BILLING_DB_PATH,
        user_id=str(user_id),
        limit=limit,
    )
    normalized = []
    for item in payments:
        p = _refresh_payment_state(item)
        normalized.append(
            {
                "payment_id": p["payment_id"],
                "status": p["status"],
                "status_label": payment_status_label_ru(p.get("status")),
                "amount_rub": p["amount_rub"],
                "credits": p["credits"],
                "credits_applied": bool(p.get("credits_applied")),
                "payment_url": p.get("payment_url"),
                "order_id": p.get("order_id"),
                "external_payment_id": p.get("external_payment_id"),
                "created_at": p.get("created_at"),
            }
        )

    return jsonify({"payments": normalized})


@app.post("/payments/webhook/tbank")
def payments_webhook_tbank():
    payload = request.get_json(silent=True) or {}
    if not _tbank_enabled():
        return jsonify({"ok": False, "error": "T-Bank не настроен"}), 503
    if not verify_webhook_token(payload, TBANK_PASSWORD):
        return jsonify({"ok": False, "error": "Bad token"}), 401

    ext_payment_id = str(payload.get("PaymentId") or "").strip()
    order_id = str(payload.get("OrderId") or "").strip()
    status = str(payload.get("Status") or "UNKNOWN").strip().upper()

    payment = None
    if ext_payment_id:
        payment = billing_get_payment_by_external_id(BILLING_DB_PATH, ext_payment_id)
    if not payment and order_id:
        payment = billing_get_payment_by_order_id(BILLING_DB_PATH, order_id)
    if not payment:
        return jsonify({"ok": False, "error": "Payment not found"}), 404

    payment = billing_update_payment(
        db_path=BILLING_DB_PATH,
        payment_id=payment["payment_id"],
        status=status,
        external_payment_id=ext_payment_id or payment.get("external_payment_id"),
        raw=payload,
    ) or payment

    applied = False
    balance = None
    if status == "CONFIRMED":
        applied, balance = billing_apply_payment_credits_if_needed(
            db_path=BILLING_DB_PATH,
            payment_id=payment["payment_id"],
            reason="payment_tbank_confirmed",
        )
        payment = billing_get_payment(BILLING_DB_PATH, payment["payment_id"]) or payment

    return jsonify(
        {
            "ok": True,
            "payment_id": payment["payment_id"],
            "status": payment["status"],
            "credits_applied": bool(payment.get("credits_applied")),
            "applied_now": bool(applied),
            "balance": balance,
        }
    )


@app.get("/mock-pay/<payment_id>")
def mock_payment_page(payment_id: str):
    if not _mock_payments_enabled():
        return jsonify({"error": "Mock payments disabled"}), 404
    if not _mock_payment_token_ok():
        return jsonify({"error": "Unauthorized mock payment access"}), 401
    payment = billing_get_payment(BILLING_DB_PATH, payment_id)
    if not payment:
        return jsonify({"error": "payment_id не найден"}), 404
    payment = _refresh_payment_state(payment)
    status_upper = str(payment.get("status") or "").upper()
    terminal_statuses = {"CONFIRMED", "REJECTED", "CANCELED", "DEADLINE_EXPIRED", "EXPIRED"}
    credits_applied = bool(payment.get("credits_applied"))
    can_confirm = status_upper not in terminal_statuses and not credits_applied
    can_cancel = status_upper not in terminal_statuses and not credits_applied
    return render_template(
        "mock_payment.html",
        payment=payment,
        status_label=payment_status_label_ru(payment.get("status")),
        credits_applied=credits_applied,
        can_confirm=can_confirm,
        can_cancel=can_cancel,
        mock_payments_token=MOCK_PAYMENTS_TOKEN,
    )


@app.post("/mock-pay/<payment_id>/<action>")
def mock_payment_action(payment_id: str, action: str):
    if not _mock_payments_enabled():
        return jsonify({"error": "Mock payments disabled"}), 404
    if not _mock_payment_token_ok():
        return jsonify({"error": "Unauthorized mock payment access"}), 401
    payment = billing_get_payment(BILLING_DB_PATH, payment_id)
    if not payment:
        return jsonify({"error": "payment_id не найден"}), 404
    if str(payment.get("provider") or "").strip().lower() != "mock":
        return jsonify({"error": "Поддерживаются только mock-платежи"}), 400

    action_name = (action or "").strip().lower()
    status_upper = str(payment.get("status") or "").upper()
    terminal_statuses = {"CONFIRMED", "REJECTED", "CANCELED", "DEADLINE_EXPIRED", "EXPIRED"}
    credits_applied = bool(payment.get("credits_applied"))
    if action_name == "confirm":
        if status_upper in terminal_statuses:
            return jsonify({"error": f"Нельзя подтвердить платеж со статусом {status_upper}"}), 400
        if credits_applied:
            return jsonify({"error": "Кредиты по платежу уже начислены"}), 400
        billing_update_payment(
            db_path=BILLING_DB_PATH,
            payment_id=payment_id,
            status="CONFIRMED",
            raw={"mode": "mock", "action": "confirm"},
        )
        billing_apply_payment_credits_if_needed(
            db_path=BILLING_DB_PATH,
            payment_id=payment_id,
            reason="payment_mock_confirmed",
        )
    elif action_name == "cancel":
        if status_upper in terminal_statuses:
            return jsonify({"error": f"Нельзя отменить платеж со статусом {status_upper}"}), 400
        if credits_applied:
            return jsonify({"error": "Нельзя отменить платеж после начисления кредитов"}), 400
        billing_update_payment(
            db_path=BILLING_DB_PATH,
            payment_id=payment_id,
            status="CANCELED",
            raw={"mode": "mock", "action": "cancel"},
        )
    else:
        return jsonify({"error": "Unsupported action"}), 400

    return redirect(_mock_payment_url(payment_id))


@app.get("/status/<task_id>")
def status(task_id: str):
    task = _get_task(task_id)
    if not task:
        return jsonify({"error": "task_id не найден"}), 404
    task = _try_reconcile_task(task_id, task)

    response = {
        "task_id": task_id,
        "provider": task.get("provider", "sora"),
        "status": task.get("status"),
        "progress": task.get("progress", 0),
        "error": task.get("error"),
        "video_id": task.get("video_id"),
        "credits_spent": task.get("credits_spent"),
        "credits_after": task.get("credits_after"),
        "credits_refunded": bool(task.get("credits_refunded")),
    }
    if task.get("status") == "completed":
        response["download_url"] = f"/download/{task_id}"
        response["video_url"] = f"/content/{task_id}"
    return jsonify(response)


@app.get("/download/<task_id>")
def download(task_id: str):
    task = _get_task(task_id)
    if not task:
        return jsonify({"error": "task_id не найден"}), 404
    if task.get("status") != "completed":
        return jsonify({"error": "Видео еще не готово"}), 400

    file_path = Path(task.get("file_path", ""))
    if not file_path.exists():
        return jsonify({"error": "Файл не найден на сервере"}), 404

    return send_file(
        file_path,
        as_attachment=True,
        download_name=f"video_{task_id}.mp4",
        mimetype="video/mp4",
    )


@app.get("/content/<task_id>")
def content(task_id: str):
    task = _get_task(task_id)
    if not task:
        return jsonify({"error": "task_id не найден"}), 404
    if task.get("status") != "completed":
        return jsonify({"error": "Видео еще не готово"}), 400

    file_path = Path(task.get("file_path", ""))
    if not file_path.exists():
        return jsonify({"error": "Файл не найден на сервере"}), 404

    # conditional=True — поддержка Range (частично полезно для <video src>); предпросмотр в UI также грузит целиком через fetch+blob.
    return send_file(
        file_path,
        mimetype="video/mp4",
        conditional=True,
        max_age=0,
    )


if __name__ == "__main__":
    _db_init()
    debug_raw = (os.getenv("WEB_DEBUG") or "0").strip().lower()
    debug_enabled = debug_raw in {"1", "true", "yes", "on"}
    app.run(host="0.0.0.0", port=5000, debug=debug_enabled)
