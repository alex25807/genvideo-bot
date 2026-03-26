import time
import base64
import mimetypes
from pathlib import Path
from typing import Callable, Optional

import requests


ALLOWED_VEO_SECONDS = (4, 6, 8)
ALLOWED_VEO_MODELS = ("veo-3.1-generate-preview",)
ALLOWED_VEO_SIZES = ("1280x720", "720x1280")

BASE_URL = "https://api.proxyapi.ru/google/v1beta"


class VeoError(Exception):
    """Ошибка, связанная с Gemini Veo API через ProxyAPI."""


class VeoClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"x-goog-api-key": api_key})

    @staticmethod
    def _size_to_aspect_ratio(size: str) -> str:
        mapping = {
            "1280x720": "16:9",
            "720x1280": "9:16",
        }
        if size not in mapping:
            raise VeoError(f"Размер должен быть одним из {ALLOWED_VEO_SIZES}, получено: {size}")
        return mapping[size]

    def create_video(
        self,
        prompt: str,
        seconds: int = 8,
        model: str = "veo-3.1-generate-preview",
        size: str = "1280x720",
        input_reference_path: Optional[str] = None,
        reference_image_paths: Optional[list[str]] = None,
        reference_image_type: str = "asset",
        negative_prompt: Optional[str] = None,
        generate_audio: bool = False,
        resize_mode: str = "pad",
    ) -> dict:
        if seconds not in ALLOWED_VEO_SECONDS:
            raise VeoError(
                f"Параметр seconds должен быть одним из {ALLOWED_VEO_SECONDS}, получено: {seconds}"
            )
        if model not in ALLOWED_VEO_MODELS:
            raise VeoError(f"Модель должна быть одной из {ALLOWED_VEO_MODELS}, получено: {model}")
        if size not in ALLOWED_VEO_SIZES:
            raise VeoError(f"Размер должен быть одним из {ALLOWED_VEO_SIZES}, получено: {size}")
        if resize_mode not in ("pad", "crop"):
            raise VeoError("resize_mode должен быть 'pad' или 'crop'")
        ref_type = (reference_image_type or "asset").strip().lower()
        if ref_type not in ("asset", "style"):
            raise VeoError("reference_image_type должен быть 'asset' или 'style'")

        reference_image_paths = reference_image_paths or []
        if input_reference_path and reference_image_paths:
            raise VeoError("Нельзя использовать одновременно input_reference и referenceImages.")
        if len(reference_image_paths) > 3:
            raise VeoError("Veo поддерживает максимум 3 referenceImages.")
        if reference_image_paths:
            if seconds != 8:
                raise VeoError("Для referenceImages в Veo требуется seconds=8.")
            if size != "1280x720":
                raise VeoError("Для referenceImages в Veo требуется размер 1280x720 (16:9).")
            if ref_type == "style" and len(reference_image_paths) != 1:
                raise VeoError("Для referenceType='style' нужно ровно 1 изображение.")

        aspect_ratio = self._size_to_aspect_ratio(size)
        url = f"{self.base_url}/models/{model}:predictLongRunning"
        instance = {"prompt": prompt}
        if input_reference_path:
            ref_path = Path(input_reference_path)
            if not ref_path.exists():
                raise VeoError(f"Файл input_reference не найден: {input_reference_path}")
            mime_type = mimetypes.guess_type(str(ref_path))[0] or "application/octet-stream"
            if mime_type not in ("image/png", "image/jpeg", "image/webp"):
                raise VeoError(
                    "Для Veo input_reference должен быть PNG/JPEG/WEBP. "
                    f"Получено: {mime_type}"
                )
            with open(ref_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("ascii")
            instance["image"] = {
                "bytesBase64Encoded": encoded,
                "mimeType": mime_type,
            }
        if reference_image_paths:
            refs = []
            for p in reference_image_paths:
                ref_path = Path(p)
                if not ref_path.exists():
                    raise VeoError(f"Файл referenceImage не найден: {p}")
                mime_type = mimetypes.guess_type(str(ref_path))[0] or "application/octet-stream"
                if mime_type not in ("image/png", "image/jpeg", "image/webp"):
                    raise VeoError(
                        "Для Veo referenceImages должны быть PNG/JPEG/WEBP. "
                        f"Получено: {mime_type}"
                    )
                with open(ref_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("ascii")
                refs.append(
                    {
                        "image": {
                            "bytesBase64Encoded": encoded,
                            "mimeType": mime_type,
                        },
                        "referenceType": ref_type,
                    }
                )
            instance["referenceImages"] = refs

        payload = {
            "instances": [instance],
            "parameters": {
                "aspectRatio": aspect_ratio,
                "resolution": "720p",
                "durationSeconds": int(seconds),
            },
        }
        # Некоторые модели Veo отклоняют параметр generateAudio полностью.
        # Передаем его только если явно включен.
        if generate_audio:
            payload["parameters"]["generateAudio"] = True
        if negative_prompt:
            payload["parameters"]["negativePrompt"] = negative_prompt
        if input_reference_path:
            payload["parameters"]["resizeMode"] = resize_mode

        resp = self.session.post(url, json=payload)
        if not (200 <= resp.status_code < 300):
            raise VeoError(f"Ошибка создания видео (HTTP {resp.status_code}): {resp.text}")
        return resp.json()

    def get_operation(self, operation_name: str) -> dict:
        op = operation_name.lstrip("/")
        url = f"{self.base_url}/{op}"
        resp = self.session.get(url)
        if not (200 <= resp.status_code < 300):
            raise VeoError(f"Ошибка получения статуса операции (HTTP {resp.status_code}): {resp.text}")
        return resp.json()

    def cancel_operation(self, operation_name: str) -> bool:
        op = operation_name.lstrip("/")
        url = f"{self.base_url}/{op}:cancel"
        resp = self.session.post(url, json={})
        if 200 <= resp.status_code < 300:
            return True
        if resp.status_code in (404, 405):
            return False
        raise VeoError(f"Ошибка отмены операции (HTTP {resp.status_code}): {resp.text}")

    @staticmethod
    def extract_video_uri(operation: dict) -> Optional[str]:
        response = operation.get("response") or {}
        gvr = response.get("generateVideoResponse") or {}
        samples = gvr.get("generatedSamples") or []
        if not samples:
            return None
        video = samples[0].get("video") or {}
        uri = video.get("uri")
        return uri if isinstance(uri, str) and uri.strip() else None

    @staticmethod
    def extract_error(operation: dict) -> Optional[str]:
        op_err = operation.get("error") or {}
        if isinstance(op_err, dict):
            msg = op_err.get("message")
            if msg:
                return str(msg)
        return None

    def download_by_uri(self, uri: str, output_path: str = "video.mp4") -> Path:
        # Для некоторых URI Veo нельзя передавать x-goog-api-key из текущей сессии:
        # endpoint ожидает либо подписанный URL, либо иной тип авторизации.
        # Делаем безопасный fallback на запрос без auth-заголовков.
        resp = self.session.get(uri, stream=True, allow_redirects=True)
        if not (200 <= resp.status_code < 300):
            text = resp.text or ""
            needs_plain_retry = (
                resp.status_code in (400, 401, 403)
                and ("API key not valid" in text or "API_KEY_INVALID" in text)
            )
            if needs_plain_retry:
                plain = requests.get(uri, stream=True, allow_redirects=True, timeout=120)
                if 200 <= plain.status_code < 300:
                    resp = plain
                else:
                    raise VeoError(
                        f"Ошибка скачивания видео (HTTP {plain.status_code}): {plain.text}"
                    )
            else:
                raise VeoError(f"Ошибка скачивания видео (HTTP {resp.status_code}): {text}")
        path = Path(output_path)
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return path

    def generate(
        self,
        prompt: str,
        seconds: int = 8,
        model: str = "veo-3.1-generate-preview",
        size: str = "1280x720",
        input_reference_path: Optional[str] = None,
        output_path: str = "video.mp4",
        poll_interval: float = 5.0,
        timeout_sec: int = 1200,
        on_progress: Optional[Callable[[int, str], None]] = None,
        on_job_created: Optional[Callable[[str, str], None]] = None,
    ) -> Path:
        op = self.create_video(
            prompt=prompt,
            seconds=seconds,
            model=model,
            size=size,
            input_reference_path=input_reference_path,
        )
        operation_name = op.get("name")
        if not operation_name:
            raise VeoError(f"Ответ create_video не содержит operation name: {op}")

        if on_job_created:
            on_job_created(operation_name, "queued")
        if on_progress:
            on_progress(0, "queued")

        started = time.time()
        while True:
            if time.time() - started > timeout_sec:
                raise VeoError("Превышено время ожидания генерации Veo")

            status = self.get_operation(operation_name)
            done = bool(status.get("done", False))
            if done:
                err = self.extract_error(status)
                if err:
                    raise VeoError(f"Генерация Veo не удалась: {err}")
                uri = self.extract_video_uri(status)
                if not uri:
                    raise VeoError(f"Операция завершена, но video uri не найден: {status}")
                if on_progress:
                    on_progress(100, "completed")
                return self.download_by_uri(uri, output_path=output_path)

            if on_progress:
                on_progress(0, "in_progress")
            time.sleep(poll_interval)

