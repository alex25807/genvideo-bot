"""
CLI для генерации видео через ProxyAPI (OpenAI SORA 2).

Пример запуска:
    python main.py --prompt "Кот играет на пианино" --seconds 4

Переменные окружения:
    PROXYAPI_KEY — ключ от ProxyAPI (или передать через --api-key).
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from tqdm import tqdm

from providers import ProviderFactory, VideoRequest
from services import (
    PricingPolicy,
    VideoOrchestrator,
    ensure_provider_available,
    validate_generation_params,
)
from sora_client import SoraClient, SoraError
from telegram_integration import (
    TelegramProgressReporter,
    make_telegram_progress_callback,
)

load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Генерация видео через ProxyAPI + SORA 2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            '  python main.py --prompt "Закат над океаном" --seconds 4\n'
            '  python main.py --prompt "Кот на крыше" --seconds 8 --model sora-2-pro\n'
        ),
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="Текстовый запрос, описывающий видео",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=4,
        choices=[4, 6, 8, 12],
        help="Длительность видео в секундах (по умолчанию: 4)",
    )
    parser.add_argument(
        "--provider",
        default="sora",
        choices=["sora", "veo"],
        help="Провайдер генерации (по умолчанию: sora)",
    )
    parser.add_argument(
        "--model",
        default="sora-2",
        choices=["sora-2", "sora-2-pro", "veo-3.1-generate-preview"],
        help="Модель генерации (по умолчанию: sora-2)",
    )
    parser.add_argument(
        "--size",
        default="1280x720",
        choices=["720x1280", "1280x720", "1024x1792", "1792x1024"],
        help="Разрешение видео (по умолчанию: 1280x720)",
    )
    parser.add_argument(
        "--output",
        default="video.mp4",
        help="Имя выходного файла (по умолчанию: video.mp4)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Ключ ProxyAPI (или задайте PROXYAPI_KEY в .env)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=3.0,
        help="Интервал опроса статуса в секундах (по умолчанию: 3)",
    )
    parser.add_argument(
        "--log-file",
        default="generation.log",
        help="Файл для логов статуса (по умолчанию: generation.log)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Тестовый режим без API (эмуляция прогресс-бара)",
    )
    parser.add_argument(
        "--dry-run-seconds",
        type=int,
        default=8,
        help="Длительность dry-run в секундах (по умолчанию: 8)",
    )
    parser.add_argument(
        "--telegram-enable",
        action="store_true",
        help="Включить отправку прогресса в Telegram",
    )
    parser.add_argument(
        "--telegram-bot-token",
        default=None,
        help="Токен Telegram-бота (или TELEGRAM_BOT_TOKEN в .env)",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=None,
        help="ID чата Telegram (или TELEGRAM_CHAT_ID в .env)",
    )
    return parser.parse_args()


def write_log(
    log_file: str,
    status: str,
    progress: Optional[int] = None,
    video_id: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "status": status,
    }
    if progress is not None:
        record["progress"] = progress
    if video_id is not None:
        record["video_id"] = video_id
    if detail is not None:
        record["detail"] = detail

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def extract_available_balance(balance_data: dict) -> Optional[float]:
    value = balance_data.get("balance")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def run_dry_progress(
    pbar: tqdm,
    duration_seconds: int,
    on_progress,
) -> None:
    steps = max(duration_seconds, 1)
    for i in range(steps + 1):
        progress = min(int((i / steps) * 100), 100)
        status = "queued" if progress < 10 else "in_progress"
        if progress >= 100:
            status = "completed"
        on_progress(progress, status)
        if i < steps:
            time.sleep(1)


def main() -> None:
    args = parse_args()

    api_key = args.api_key or os.getenv("PROXYAPI_KEY")
    if not args.dry_run and not api_key:
        print(
            "Ошибка: укажите ключ ProxyAPI через --api-key "
            "или переменную окружения PROXYAPI_KEY (файл .env).",
            file=sys.stderr,
        )
        sys.exit(1)

    client = SoraClient(api_key) if api_key else None
    orchestrator: Optional[VideoOrchestrator] = None
    if client:
        orchestrator = VideoOrchestrator(
            factory=ProviderFactory(client),
            pricing_policy=PricingPolicy(),
        )
    current_video_id: Optional[str] = None
    telegram_reporter: Optional[TelegramProgressReporter] = None
    balance_before: Optional[float] = None

    if args.telegram_enable:
        bot_token = args.telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = (
            args.telegram_chat_id
            or os.getenv("TELEGRAM_CHAT_ID")
            or os.getenv("TELEGRAM_ALLOWED_CHAT_ID")
        )
        if not bot_token or not chat_id:
            print(
                "Ошибка: для Telegram укажите --telegram-bot-token и --telegram-chat-id "
                "или переменные TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID "
                "(можно использовать TELEGRAM_ALLOWED_CHAT_ID) в .env.",
                file=sys.stderr,
            )
            sys.exit(1)
        telegram_reporter = TelegramProgressReporter(bot_token=bot_token, chat_id=chat_id)

    print(f"Модель:      {args.model}")
    print(f"Provider:    {args.provider}")
    print(f"Разрешение:  {args.size}")
    print(f"Длительность: {args.seconds} сек")
    print(f"Промпт:      {args.prompt}")
    print(f"Лог-файл:    {args.log_file}")
    if args.dry_run:
        print("Режим:       DRY-RUN (без реального API)")
    if args.telegram_enable:
        print("Telegram:    включен (обновление одного сообщения)")
    print()

    pbar = tqdm(
        total=100,
        desc="Генерация видео",
        bar_format="{desc}: |{bar}| {n:.0f}% [{elapsed} < {remaining}]",
        colour="cyan",
        ncols=80,
    )
    last_progress = 0
    telegram_failed = False
    telegram_progress = make_telegram_progress_callback(
        sender=telegram_reporter.send_or_edit if telegram_reporter else None
    )

    def safe_telegram_call(func) -> None:
        nonlocal telegram_failed
        if not telegram_reporter or telegram_failed:
            return
        try:
            func()
        except Exception as exc:
            telegram_failed = True
            warn = f"Telegram временно отключен из-за ошибки: {exc}"
            print(f"\n[WARN] {warn}", file=sys.stderr)
            write_log(args.log_file, status="telegram_error", detail=str(exc))

    def on_progress(progress: int, status: str) -> None:
        nonlocal last_progress
        status_text = {
            "queued": "в очереди",
            "in_progress": "генерация",
            "completed": "завершено",
        }.get(status, status)

        pbar.set_description(f"Генерация видео ({status_text})")

        delta = progress - last_progress
        if delta > 0:
            pbar.update(delta)
            last_progress = progress
        write_log(args.log_file, status=status, progress=progress, video_id=current_video_id)
        safe_telegram_call(lambda: telegram_progress(progress, status))

    def on_job_created(video_id: str, status: str) -> None:
        nonlocal current_video_id
        current_video_id = video_id
        write_log(
            args.log_file,
            status=status,
            progress=0,
            video_id=video_id,
            detail="job_created",
        )
        print(f"ID задания: {video_id}")

    try:
        print("Генерация видео началась\n")
        write_log(args.log_file, status="started", progress=0, detail="cli_start")

        if args.dry_run:
            current_video_id = "dry-run-video-id"
            write_log(
                args.log_file,
                status="queued",
                progress=0,
                video_id=current_video_id,
                detail="dry_run_started",
            )
            run_dry_progress(
                pbar=pbar,
                duration_seconds=args.dry_run_seconds,
                on_progress=on_progress,
            )
            path = args.output
        else:
            try:
                balance_before = extract_available_balance(client.get_balance())
            except Exception:
                balance_before = None

            request = VideoRequest(
                provider=args.provider,
                prompt=args.prompt,
                seconds=args.seconds,
                model=args.model,
                size=args.size,
            )
            caps = validate_generation_params(
                orchestrator=orchestrator,
                provider=request.provider,
                seconds=request.seconds,
                model=request.model,
                size=request.size,
            )
            ensure_provider_available(caps)
            job = orchestrator.start_generation(request)
            on_job_created(job.external_id, job.status)
            on_progress(0, job.status)
            status = orchestrator.wait_until_done(
                provider_name=request.provider,
                job=job,
                poll_interval_sec=args.poll_interval,
                on_progress=lambda s, p, e: on_progress(p, s),
            )
            if status.status == "failed":
                raise SoraError(status.error or "Генерация видео не удалась")
            result = orchestrator.download_result(
                provider_name=request.provider,
                job=job,
                output_path=args.output,
            )
            path = result.file_path

        remaining = 100 - last_progress
        if remaining > 0:
            pbar.update(remaining)
        pbar.close()

        write_log(
            args.log_file,
            status="completed",
            progress=100,
            video_id=current_video_id,
            detail="generation_done",
        )
        if args.dry_run:
            print(f"\nDry-run завершен. Прогресс-бар отработал. Выходной файл: {path}")
        else:
            print(f"\nФайл {path} сохранен")
            if balance_before is not None:
                try:
                    balance_after = extract_available_balance(client.get_balance())
                    if balance_after is not None:
                        spent_rub = max(balance_before - balance_after, 0.0)
                        print(f"На генерацию израсходовано: {spent_rub:.2f} руб.")
                except Exception:
                    pass
        safe_telegram_call(
            lambda: telegram_reporter.send_final_with_actions(
                "Генерация видео завершена. Файл сохранен.\n"
                "Кнопки готовы для обработки в Telegram-боте."
            )
        )

    except SoraError as e:
        pbar.close()
        write_log(
            args.log_file,
            status="failed",
            progress=last_progress,
            video_id=current_video_id,
            detail=str(e),
        )
        print(f"\n{e}", file=sys.stderr)
        safe_telegram_call(
            lambda: telegram_reporter.send_final_with_actions(f"Генерация видео не удалась: {e}")
        )
        sys.exit(1)
    except KeyboardInterrupt:
        pbar.close()
        write_log(
            args.log_file,
            status="cancelled",
            progress=last_progress,
            video_id=current_video_id,
            detail="keyboard_interrupt",
        )
        print("\nГенерация прервана пользователем.")
        safe_telegram_call(
            lambda: telegram_reporter.send_final_with_actions("Генерация видео прервана пользователем.")
        )
        sys.exit(130)


if __name__ == "__main__":
    main()
