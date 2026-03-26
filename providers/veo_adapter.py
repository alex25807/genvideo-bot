from providers.base import VideoProvider
from providers.models import (
    ProviderCapabilities,
    ProviderJobRef,
    ProviderResult,
    ProviderStatus,
    VideoRequest,
)
from veo_client import (
    ALLOWED_VEO_MODELS,
    ALLOWED_VEO_SECONDS,
    ALLOWED_VEO_SIZES,
    VeoClient,
    VeoError,
)


class VeoProviderAdapter(VideoProvider):
    def __init__(self, client: VeoClient, enable_reference_images: bool = False) -> None:
        self.client = client
        self.enable_reference_images = enable_reference_images

    def name(self) -> str:
        return "veo"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name(),
            supports_remix=False,
            supports_input_reference=True,
            allowed_seconds=ALLOWED_VEO_SECONDS,
            allowed_models=ALLOWED_VEO_MODELS,
            allowed_sizes=ALLOWED_VEO_SIZES,
            supports_reference_images=self.enable_reference_images,
            max_reference_images=3 if self.enable_reference_images else 0,
            supported_reference_types=("asset", "style") if self.enable_reference_images else (),
            is_stub=False,
        )

    def create(self, request: VideoRequest) -> ProviderJobRef:
        reference_paths = request.input_reference_paths or []
        if reference_paths and not self.enable_reference_images:
            raise VeoError(
                "referenceImages для Veo отключены. Установите ENABLE_VEO_REFERENCE_IMAGES=1."
            )
        payload = self.client.create_video(
            prompt=request.prompt,
            seconds=request.seconds,
            model=request.model,
            size=request.size,
            input_reference_path=request.input_reference_path,
            reference_image_paths=reference_paths or None,
            reference_image_type=request.reference_image_type or "asset",
        )
        operation_name = payload.get("name")
        if not operation_name:
            raise VeoError(f"Ответ Veo create_video не содержит operation name: {payload}")
        return ProviderJobRef(
            provider=self.name(),
            external_id=operation_name,
            status="queued",
            metadata={"raw_create": payload},
        )

    def get_status(self, job: ProviderJobRef) -> ProviderStatus:
        raw = self.client.get_operation(job.external_id)
        done = bool(raw.get("done", False))
        err = self.client.extract_error(raw)
        if err:
            return ProviderStatus(status="failed", progress=0, error=err, raw=raw)
        if done:
            uri = self.client.extract_video_uri(raw)
            if not uri:
                return ProviderStatus(
                    status="failed",
                    progress=0,
                    error="Операция завершена, но video uri не найден.",
                    raw=raw,
                )
            return ProviderStatus(status="completed", progress=100, raw=raw)
        return ProviderStatus(status="in_progress", progress=0, raw=raw)

    def download(self, job: ProviderJobRef, output_path: str) -> ProviderResult:
        raw = self.client.get_operation(job.external_id)
        err = self.client.extract_error(raw)
        if err:
            raise VeoError(f"Генерация Veo не удалась: {err}")
        uri = self.client.extract_video_uri(raw)
        if not uri:
            raise VeoError("Видео еще не готово или отсутствует uri для скачивания.")
        path = self.client.download_by_uri(uri, output_path=output_path)
        return ProviderResult(file_path=str(path), raw={"operation": job.external_id, "uri": uri})

    def remix(self, source_id: str, prompt: str) -> ProviderJobRef:
        raise VeoError("Remix для Veo пока не поддерживается в текущей реализации.")

    def cancel(self, job: ProviderJobRef) -> bool:
        return self.client.cancel_operation(job.external_id)

