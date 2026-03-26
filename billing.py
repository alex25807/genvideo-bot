"""
Единый биллинг для бота и web.

Хранит:
- баланс кредитов пользователей;
- client_token для web-доступа;
- ledger операций (начисление/списание).
"""

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional


_LOCK = threading.Lock()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_billing(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                credits INTEGER NOT NULL DEFAULT 0,
                client_token TEXT UNIQUE,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                meta_json TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                user_id TEXT NOT NULL,
                client_token TEXT,
                package_id TEXT NOT NULL,
                amount_rub REAL NOT NULL,
                credits INTEGER NOT NULL,
                status TEXT NOT NULL,
                order_id TEXT UNIQUE NOT NULL,
                external_payment_id TEXT UNIQUE,
                payment_url TEXT,
                credits_applied INTEGER NOT NULL DEFAULT 0,
                meta_json TEXT,
                raw_json TEXT,
                credited_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )


def _upsert_user(db_path: Path, user_id: str, credits: int) -> None:
    now_ts = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, credits, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (user_id, max(credits, 0), now_ts, now_ts),
        )


def ensure_user(db_path: Path, user_id: str, default_credits: int = 0) -> None:
    with _LOCK:
        _upsert_user(db_path, user_id, default_credits)


def get_credits(db_path: Path, user_id: str, default_credits: int = 0) -> int:
    with _LOCK:
        _upsert_user(db_path, user_id, default_credits)
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT credits FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return int(row["credits"] if row else 0)


def set_credits(db_path: Path, user_id: str, amount: int, reason: str = "admin_set") -> int:
    amount = max(int(amount), 0)
    now_ts = int(time.time())
    with _LOCK:
        _upsert_user(db_path, user_id, 0)
        with _connect(db_path) as conn:
            old_row = conn.execute(
                "SELECT credits FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            old_credits = int(old_row["credits"] if old_row else 0)
            conn.execute(
                "UPDATE users SET credits = ?, updated_at = ? WHERE user_id = ?",
                (amount, now_ts, user_id),
            )
            delta = amount - old_credits
            conn.execute(
                "INSERT INTO ledger (user_id, delta, reason, meta_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, delta, reason, None, now_ts),
            )
    return amount


def add_credits(db_path: Path, user_id: str, amount: int, reason: str = "admin_add") -> int:
    amount = int(amount)
    if amount <= 0:
        raise ValueError("amount должен быть > 0")

    now_ts = int(time.time())
    with _LOCK:
        _upsert_user(db_path, user_id, 0)
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT credits FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            current = int(row["credits"] if row else 0)
            new_credits = current + amount
            conn.execute(
                "UPDATE users SET credits = ?, updated_at = ? WHERE user_id = ?",
                (new_credits, now_ts, user_id),
            )
            conn.execute(
                "INSERT INTO ledger (user_id, delta, reason, meta_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, amount, reason, None, now_ts),
            )
    return new_credits


def consume_credits(
    db_path: Path,
    user_id: str,
    amount: int,
    reason: str = "generation_consume",
    meta: Optional[dict] = None,
    default_credits: int = 0,
) -> tuple[bool, int]:
    amount = int(amount)
    if amount <= 0:
        raise ValueError("amount должен быть > 0")

    now_ts = int(time.time())
    with _LOCK:
        _upsert_user(db_path, user_id, default_credits)
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT credits FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            current = int(row["credits"] if row else 0)
            if current < amount:
                return False, current

            new_credits = current - amount
            conn.execute(
                "UPDATE users SET credits = ?, updated_at = ? WHERE user_id = ?",
                (new_credits, now_ts, user_id),
            )
            conn.execute(
                "INSERT INTO ledger (user_id, delta, reason, meta_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, -amount, reason, json.dumps(meta or {}, ensure_ascii=False), now_ts),
            )
    return True, new_credits


def set_client_token(db_path: Path, user_id: str, token: str, default_credits: int = 0) -> None:
    token = token.strip()
    if not token:
        raise ValueError("token не должен быть пустым")
    now_ts = int(time.time())
    with _LOCK:
        _upsert_user(db_path, user_id, default_credits)
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE users SET client_token = ?, updated_at = ? WHERE user_id = ?",
                (token, now_ts, user_id),
            )


def resolve_user_id_by_token(db_path: Path, token: str) -> Optional[str]:
    token = (token or "").strip()
    if not token:
        return None
    with _LOCK:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT user_id FROM users WHERE client_token = ?",
                (token,),
            ).fetchone()
            return str(row["user_id"]) if row else None


def get_client_token_for_user(db_path: Path, user_id: str) -> Optional[str]:
    user_id = (user_id or "").strip()
    if not user_id:
        return None
    with _LOCK:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT client_token FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            token = (row["client_token"] or "").strip()
            return token or None


def list_users(db_path: Path, limit: int = 200) -> list[dict]:
    with _LOCK:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT user_id, credits, client_token, created_at, updated_at
                FROM users
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(int(limit), 1),),
            ).fetchall()
    return [dict(r) for r in rows]


def list_ledger(
    db_path: Path,
    limit: int = 200,
    user_id: Optional[str] = None,
    offset: int = 0,
) -> list[dict]:
    limit = max(int(limit), 1)
    offset = max(int(offset), 0)
    with _LOCK:
        with _connect(db_path) as conn:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT id, user_id, delta, reason, meta_json, created_at
                    FROM ledger
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (str(user_id), limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, user_id, delta, reason, meta_json, created_at
                    FROM ledger
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
    return [dict(r) for r in rows]


def count_ledger(db_path: Path, user_id: Optional[str] = None) -> int:
    with _LOCK:
        with _connect(db_path) as conn:
            if user_id:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM ledger WHERE user_id = ?",
                    (str(user_id),),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS c FROM ledger").fetchone()
    return int(row["c"] if row else 0)


def generation_credit_cost(seconds: int, model: str, provider: str = "sora") -> int:
    provider = (provider or "sora").strip().lower()
    sec = int(seconds)

    if provider == "veo":
        seconds_mult = {4: 1, 6: 2, 8: 2}.get(sec, 1)
        return max(seconds_mult, 1)

    seconds_mult = {4: 1, 8: 2, 12: 3}.get(sec, 1)
    model_mult = 2 if model == "sora-2-pro" else 1
    return max(seconds_mult * model_mult, 1)


def create_payment(
    db_path: Path,
    provider: str,
    user_id: str,
    client_token: str,
    package_id: str,
    amount_rub: float,
    credits: int,
    order_id: str,
    meta: Optional[dict] = None,
) -> dict:
    now_ts = int(time.time())
    payment_id = uuid.uuid4().hex
    with _LOCK:
        _upsert_user(db_path, user_id, 0)
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, provider, user_id, client_token, package_id, amount_rub, credits,
                    status, order_id, external_payment_id, payment_url, credits_applied,
                    meta_json, raw_json, credited_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, NULL, NULL, ?, ?)
                """,
                (
                    payment_id,
                    provider,
                    user_id,
                    client_token or None,
                    package_id,
                    float(amount_rub),
                    int(credits),
                    "new",
                    order_id,
                    json.dumps(meta or {}, ensure_ascii=False),
                    now_ts,
                    now_ts,
                ),
            )
    return get_payment(db_path, payment_id) or {}


def get_payment(db_path: Path, payment_id: str) -> Optional[dict]:
    with _LOCK:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM payments WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
    return dict(row) if row else None


def get_payment_by_order_id(db_path: Path, order_id: str) -> Optional[dict]:
    with _LOCK:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchone()
    return dict(row) if row else None


def get_payment_by_external_id(db_path: Path, external_payment_id: str) -> Optional[dict]:
    with _LOCK:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM payments WHERE external_payment_id = ?",
                (external_payment_id,),
            ).fetchone()
    return dict(row) if row else None


def get_last_payment_for_user(db_path: Path, user_id: str) -> Optional[dict]:
    with _LOCK:
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM payments
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(user_id),),
            ).fetchone()
    return dict(row) if row else None


def list_recent_payments_for_user(db_path: Path, user_id: str, limit: int = 3) -> list[dict]:
    lim = max(1, min(int(limit), 20))
    with _LOCK:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM payments
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (str(user_id), lim),
            ).fetchall()
    return [dict(row) for row in rows]


def list_recent_payments(
    db_path: Path,
    limit: int = 20,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    lim = max(1, min(int(limit), 200))
    status_value = str(status or "").strip().upper() or None
    with _LOCK:
        with _connect(db_path) as conn:
            if user_id is not None and status_value is not None:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM payments
                    WHERE user_id = ? AND UPPER(COALESCE(status, '')) = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (str(user_id), status_value, lim),
                ).fetchall()
            elif user_id is not None:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM payments
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (str(user_id), lim),
                ).fetchall()
            elif status_value is not None:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM payments
                    WHERE UPPER(COALESCE(status, '')) = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (status_value, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM payments
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (lim,),
                ).fetchall()
    return [dict(row) for row in rows]


def update_payment(
    db_path: Path,
    payment_id: str,
    *,
    status: Optional[str] = None,
    external_payment_id: Optional[str] = None,
    payment_url: Optional[str] = None,
    raw: Optional[dict] = None,
) -> Optional[dict]:
    now_ts = int(time.time())
    with _LOCK:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM payments WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            if not row:
                return None
            current = dict(row)
            next_status = status if status is not None else current.get("status")
            next_external_id = (
                external_payment_id if external_payment_id is not None else current.get("external_payment_id")
            )
            next_payment_url = payment_url if payment_url is not None else current.get("payment_url")
            next_raw_json = (
                json.dumps(raw, ensure_ascii=False)
                if raw is not None
                else current.get("raw_json")
            )
            conn.execute(
                """
                UPDATE payments
                SET status = ?, external_payment_id = ?, payment_url = ?, raw_json = ?, updated_at = ?
                WHERE payment_id = ?
                """,
                (
                    next_status,
                    next_external_id,
                    next_payment_url,
                    next_raw_json,
                    now_ts,
                    payment_id,
                ),
            )
    return get_payment(db_path, payment_id)


def apply_payment_credits_if_needed(
    db_path: Path,
    payment_id: str,
    reason: str = "payment_confirmed",
) -> tuple[bool, int]:
    """
    Идемпотентное начисление кредитов по платежу.
    Возвращает:
    - applied: было ли выполнено начисление сейчас
    - balance: итоговый баланс пользователя

    Безопасность:
    - начисление допускается только для платежей в статусе CONFIRMED
    """
    now_ts = int(time.time())
    with _LOCK:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM payments WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            if not row:
                raise ValueError("payment_id не найден")
            p = dict(row)
            user_id = str(p["user_id"])
            _upsert_user(db_path, user_id, 0)

            user_row = conn.execute(
                "SELECT credits FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            current_credits = int(user_row["credits"] if user_row else 0)

            status = str(p.get("status") or "").strip().upper()
            if status != "CONFIRMED":
                raise ValueError(
                    f"Нельзя начислить кредиты для платежа в статусе {status or 'UNKNOWN'}"
                )

            if int(p.get("credits_applied") or 0):
                return False, current_credits

            credits = int(p.get("credits") or 0)
            if credits <= 0:
                return False, current_credits

            new_credits = current_credits + credits
            conn.execute(
                "UPDATE users SET credits = ?, updated_at = ? WHERE user_id = ?",
                (new_credits, now_ts, user_id),
            )
            conn.execute(
                "INSERT INTO ledger (user_id, delta, reason, meta_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    user_id,
                    credits,
                    reason,
                    json.dumps({"payment_id": payment_id}, ensure_ascii=False),
                    now_ts,
                ),
            )
            conn.execute(
                """
                UPDATE payments
                SET credits_applied = 1, credited_at = ?, updated_at = ?
                WHERE payment_id = ?
                """,
                (now_ts, now_ts, payment_id),
            )
            return True, new_credits
