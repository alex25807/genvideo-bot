import hashlib
from typing import Optional

import requests


def _is_primitive(value) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def make_token(payload: dict, password: str) -> str:
    """
    Token для Т-Банк API:
    - берутся только top-level primitive поля
    - поле Token исключается
    - добавляется Password
    - значения конкатенируются в порядке сортировки ключей
    - sha256 hex
    """
    values = {}
    for k, v in (payload or {}).items():
        if k == "Token":
            continue
        if _is_primitive(v):
            values[k] = "" if v is None else str(v)
    values["Password"] = str(password or "")
    raw = "".join(values[k] for k in sorted(values.keys()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_webhook_token(payload: dict, password: str) -> bool:
    got = str((payload or {}).get("Token") or "").strip().lower()
    if not got:
        return False
    expected = make_token(payload or {}, password=password).lower()
    return got == expected


class TBankClient:
    def __init__(
        self,
        terminal_key: str,
        password: str,
        base_url: str = "https://securepay.tinkoff.ru/v2",
        timeout_sec: int = 25,
    ) -> None:
        self.terminal_key = terminal_key.strip()
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = int(timeout_sec)
        if not self.terminal_key:
            raise ValueError("terminal_key обязателен")
        if not self.password:
            raise ValueError("password обязателен")

    def _post(self, method: str, payload: dict) -> dict:
        payload = dict(payload or {})
        payload["TerminalKey"] = self.terminal_key
        payload["Token"] = make_token(payload, password=self.password)
        resp = requests.post(
            f"{self.base_url}/{method}",
            json=payload,
            timeout=self.timeout_sec,
        )
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"T-Bank {method} HTTP {resp.status_code}: {resp.text}")
        data = resp.json()
        if not data.get("Success", False):
            raise RuntimeError(f"T-Bank {method} error: {data}")
        return data

    def init_payment(
        self,
        *,
        order_id: str,
        amount_rub: float,
        description: str,
        customer_key: str,
        success_url: Optional[str] = None,
        fail_url: Optional[str] = None,
        notification_url: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        amount_kopecks = int(round(float(amount_rub) * 100))
        if amount_kopecks <= 0:
            raise ValueError("amount_rub должен быть > 0")
        payload = {
            "OrderId": str(order_id),
            "Amount": amount_kopecks,
            "Description": description,
            "CustomerKey": str(customer_key),
        }
        if success_url:
            payload["SuccessURL"] = str(success_url)
        if fail_url:
            payload["FailURL"] = str(fail_url)
        if notification_url:
            payload["NotificationURL"] = str(notification_url)
        if metadata:
            # DATA поддерживается в T-Bank API для доп. полей
            payload["DATA"] = {str(k): str(v) for k, v in metadata.items()}
        return self._post("Init", payload)

    def get_state(self, payment_id: str) -> dict:
        return self._post("GetState", {"PaymentId": str(payment_id)})
