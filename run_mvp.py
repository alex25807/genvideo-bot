#!/usr/bin/env python3
"""
Запуск Flask (app.py) и Telegram-бота (telegram_bot.py) одной командой.

Использование (из корня проекта):
  python run_mvp.py

На Windows можно дважды щёлкнуть run_mvp.bat
"""
from __future__ import annotations

import atexit
import os
import signal
import sys
import time
from pathlib import Path
from subprocess import Popen

ROOT = Path(__file__).resolve().parent
_PROCS: list[Popen] = []


def _terminate_all() -> None:
    for p in _PROCS:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    deadline = time.time() + 10.0
    for p in _PROCS:
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


def _on_signal(_signum: int, _frame: object | None) -> None:
    print("\nОстанавливаю web и bot...", flush=True)
    _terminate_all()
    sys.exit(0)


def main() -> int:
    os.chdir(ROOT)
    atexit.register(_terminate_all)

    exe = sys.executable
    web = Popen([exe, "app.py"], cwd=str(ROOT))
    bot = Popen([exe, "telegram_bot.py"], cwd=str(ROOT))
    _PROCS.extend([web, bot])

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)

    print("Запущены: app.py (web :5000) и telegram_bot.py", flush=True)
    print("Ctrl+C — остановить оба процесса.", flush=True)

    try:
        while True:
            for p in _PROCS:
                code = p.poll()
                if code is not None:
                    print(f"Один из процессов завершился (код {code}), останавливаю остальные...", flush=True)
                    _terminate_all()
                    return int(code) if code is not None else 1
            time.sleep(0.4)
    except KeyboardInterrupt:
        _on_signal(signal.SIGINT, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
