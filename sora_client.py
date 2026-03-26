"""
Клиент для генерации видео через ProxyAPI (OpenAI SORA 2).

Модуль предоставляет класс SoraClient, который можно использовать
как из CLI (main.py), так и встроить в веб-приложение или Telegram-бота.
"""

import time
import mimetypes
from pathlib import Path
from typing import Callable, Optional

import requests


ALLOWED_SECONDS = (4, 8, 12)
ALLOWED_SIZES = ("720x1280", "1280x720", "1024x1792", "1792x1024")
ALLOWED_MODELS = ("sora-2", "sora-2-pro")

BASE_URL = "https://api.proxyapi.ru/openai/v1"


class SoraError(Exception):
    """Ошибка, связанная с API SORA."""


class SoraClient:
    """Клиент для работы с SORA 2 через ProxyAPI."""

    def __init__(self, api_key: str, base_url: str = BASE_URL):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def create_video(
        self,
        prompt: str,
        seconds: int = 4,
        model: str = "sora-2",
        size: str = "1280x720",
        input_reference_path: Optional[str] = None,
    ) -> dict:
        """Создаёт задание на генерацию видео. Возвращает JSON-ответ API."""
        if seconds not in ALLOWED_SECONDS:
            raise SoraError(
                f"Параметр seconds должен быть одним из {ALLOWED_SECONDS}, "
                f"получено: {seconds}"
            )
        if model not in ALLOWED_MODELS:
            raise SoraError(
                f"Модель должна быть одной из {ALLOWED_MODELS}, получено: {model}"
            )
        if size not in ALLOWED_SIZES:
            raise SoraError(
                f"Размер должен быть одним из {ALLOWED_SIZES}, получено: {size}"
            )

        url = f"{self.base_url}/videos"
        payload = {
            "prompt": prompt,
            "model": model,
            "size": size,
            "seconds": str(seconds),
        }

        # Если передан input_reference, отправляем только multipart/form-data.
        if input_reference_path:
            ref_path = Path(input_reference_path)
            if not ref_path.exists():
                raise SoraError(f"Файл input_reference не найден: {input_reference_path}")
            content_type = mimetypes.guess_type(str(ref_path))[0] or "application/octet-stream"
            with open(ref_path, "rb") as ref_file:
                form_fields = {
                    "prompt": (None, payload["prompt"]),
                    "model": (None, payload["model"]),
                    "size": (None, payload["size"]),
                    "seconds": (None, payload["seconds"]),
                    "input_reference": (ref_path.name, ref_file, content_type),
                }
                resp = self.session.post(url, files=form_fields)
            if not (200 <= resp.status_code < 300):
                raise SoraError(
                    f"Ошибка создания видео (HTTP {resp.status_code}): {resp.text}"
                )
            return resp.json()

        # Некоторые прокси-инсталляции ожидают JSON, а некоторые multipart/form-data.
        # Пробуем JSON, и при характерной ошибке формата автоматически переключаемся.
        resp = self.session.post(url, json=payload)

        if (
            resp.status_code == 400
            and "Invalid JSON format" in resp.text
        ):
            form_fields = {key: (None, value) for key, value in payload.items()}
            resp = self.session.post(url, files=form_fields)

        if not (200 <= resp.status_code < 300):
            raise SoraError(
                f"Ошибка создания видео (HTTP {resp.status_code}): {resp.text}"
            )
        return resp.json()

    def get_status(self, video_id: str) -> dict:
        """Получает текущий статус задания по video_id."""
        url = f"{self.base_url}/videos/{video_id}"
        resp = self.session.get(url)
        if not (200 <= resp.status_code < 300):
            raise SoraError(
                f"Ошибка получения статуса (HTTP {resp.status_code}): {resp.text}"
            )
        return resp.json()

    def download_video(self, video_id: str, output_path: str = "video.mp4") -> Path:
        """Скачивает готовое видео и сохраняет в файл."""
        url = f"{self.base_url}/videos/{video_id}/content"
        resp = self.session.get(url, stream=True)
        if not (200 <= resp.status_code < 300):
            raise SoraError(
                f"Ошибка скачивания видео (HTTP {resp.status_code}): {resp.text}"
            )

        path = Path(output_path)
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return path

    def delete_video(self, video_id: str) -> dict:
        """Удаляет видео из хранилища OpenAI."""
        url = f"{self.base_url}/videos/{video_id}"
        resp = self.session.delete(url)
        if not (200 <= resp.status_code < 300):
            raise SoraError(
                f"Ошибка удаления видео (HTTP {resp.status_code}): {resp.text}"
            )
        return resp.json()

    def remix_video(self, source_video_id: str, prompt: str) -> dict:
        """
        Создаёт remix-задание на основе завершённого видео.
        """
        if not source_video_id:
            raise SoraError("source_video_id не должен быть пустым")
        if not prompt.strip():
            raise SoraError("prompt для remix не должен быть пустым")

        url = f"{self.base_url}/videos/{source_video_id}/remix"
        resp = self.session.post(url, json={"prompt": prompt})
        if not (200 <= resp.status_code < 300):
            raise SoraError(
                f"Ошибка remix (HTTP {resp.status_code}): {resp.text}"
            )
        return resp.json()

    def get_balance(self) -> dict:
        """
        Возвращает данные баланса ProxyAPI.

        Требует включенного разрешения "Запрос баланса" для ключа API.
        """
        url = "https://api.proxyapi.ru/proxyapi/balance"
        resp = self.session.get(url)
        if not (200 <= resp.status_code < 300):
            raise SoraError(
                f"Ошибка получения баланса (HTTP {resp.status_code}): {resp.text}"
            )
        return resp.json()

    def generate(
        self,
        prompt: str,
        seconds: int = 4,
        model: str = "sora-2",
        size: str = "1280x720",
        input_reference_path: Optional[str] = None,
        output_path: str = "video.mp4",
        poll_interval: float = 3.0,
        on_progress: Optional[Callable[[int, str], None]] = None,
        on_job_created: Optional[Callable[[str, str], None]] = None,
    ) -> Path:
        """
        Полный цикл: создание → ожидание → скачивание.

        Args:
            prompt: текстовый запрос для генерации.
            seconds: длительность видео (4, 8 или 12).
            model: модель SORA (sora-2 или sora-2-pro).
            size: разрешение видео.
            input_reference_path: путь к изображению-референсу.
            output_path: путь для сохранения файла.
            poll_interval: интервал опроса статуса (сек).
            on_progress: callback(progress_percent, status) для отображения прогресса.
            on_job_created: callback(video_id, status) после старта задания.

        Returns:
            Path к сохранённому видеофайлу.

        Raises:
            SoraError: при ошибке API или неудачной генерации.
        """
        job = self.create_video(
            prompt=prompt,
            seconds=seconds,
            model=model,
            size=size,
            input_reference_path=input_reference_path,
        )
        video_id = job["id"]
        job_status = job.get("status", "queued")

        if on_job_created:
            on_job_created(video_id, job_status)

        if on_progress:
            on_progress(job.get("progress", 0), job_status)

        while True:
            time.sleep(poll_interval)

            status_data = self.get_status(video_id)
            status = status_data.get("status", "unknown")
            progress = status_data.get("progress", 0)

            if on_progress:
                on_progress(progress, status)

            if status == "completed":
                break
            elif status == "failed":
                error_info = status_data.get("error", {})
                error_msg = error_info.get("message", "Неизвестная ошибка")
                raise SoraError(f"Генерация видео не удалась: {error_msg}")
            elif status not in ("queued", "in_progress"):
                raise SoraError(f"Неожиданный статус: {status}")

        path = self.download_video(video_id, output_path)
        return path

    def generate_remix(
        self,
        source_video_id: str,
        prompt: str,
        output_path: str = "video.mp4",
        poll_interval: float = 3.0,
        on_progress: Optional[Callable[[int, str], None]] = None,
        on_job_created: Optional[Callable[[str, str], None]] = None,
    ) -> Path:
        """
        Полный цикл remix: создание remix-задания -> ожидание -> скачивание.
        """
        job = self.remix_video(source_video_id=source_video_id, prompt=prompt)
        video_id = job["id"]
        job_status = job.get("status", "queued")

        if on_job_created:
            on_job_created(video_id, job_status)
        if on_progress:
            on_progress(job.get("progress", 0), job_status)

        while True:
            time.sleep(poll_interval)
            status_data = self.get_status(video_id)
            status = status_data.get("status", "unknown")
            progress = status_data.get("progress", 0)

            if on_progress:
                on_progress(progress, status)

            if status == "completed":
                break
            if status == "failed":
                error_info = status_data.get("error", {})
                error_msg = error_info.get("message", "Неизвестная ошибка")
                raise SoraError(f"Ремикс видео не удался: {error_msg}")
            if status not in ("queued", "in_progress"):
                raise SoraError(f"Неожиданный статус: {status}")

        return self.download_video(video_id, output_path)
