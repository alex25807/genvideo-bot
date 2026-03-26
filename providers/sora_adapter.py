from providers.base import VideoProvider
from providers.models import (
    ProviderCapabilities,
    ProviderJobRef,
    ProviderResult,
    ProviderStatus,
    VideoRequest,
)
from sora_client import ALLOWED_MODELS, ALLOWED_SECONDS, ALLOWED_SIZES, SoraClient, SoraError


class SoraProviderAdapter(VideoProvider):
    def __init__(self, client: SoraClient) -> None:
        self.client = client

    def name(self) -> str:
        return "sora"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name(),
            supports_remix=True,
            supports_input_reference=True,
            allowed_seconds=ALLOWED_SECONDS,
            allowed_models=ALLOWED_MODELS,
            allowed_sizes=ALLOWED_SIZES,
            supports_reference_images=False,
            max_reference_images=0,
            supported_reference_types=(),
            is_stub=False,
        )

    def create(self, request: VideoRequest) -> ProviderJobRef:
        if request.input_reference_paths:
            raise SoraError(
                "Sora в текущей реализации не поддерживает input_references (множественные referenceImages)."
            )
        payload = self.client.create_video(
            prompt=request.prompt,
            seconds=request.seconds,
            model=request.model,
            size=request.size,
            input_reference_path=request.input_reference_path,
        )
        external_id = payload.get("id")
        if not external_id:
            raise SoraError(f"Ответ create_video не содержит id: {payload}")
        status = payload.get("status", "queued")
        return ProviderJobRef(
            provider=self.name(),
            external_id=external_id,
            status=status,
            metadata={"raw_create": payload},
        )

    def get_status(self, job: ProviderJobRef) -> ProviderStatus:
        raw = self.client.get_status(job.external_id)
        progress = int(raw.get("progress", 0) or 0)
        return ProviderStatus(
            status=raw.get("status", "queued"),
            progress=progress,
            error=raw.get("error") or raw.get("message"),
            raw=raw,
        )

    def download(self, job: ProviderJobRef, output_path: str) -> ProviderResult:
        self.client.download_video(job.external_id, output_path=output_path)
        return ProviderResult(file_path=output_path, raw={"video_id": job.external_id})

    def remix(self, source_id: str, prompt: str) -> ProviderJobRef:
        payload = self.client.remix_video(source_video_id=source_id, prompt=prompt)
        external_id = payload.get("id")
        if not external_id:
            raise SoraError(f"Ответ remix_video не содержит id: {payload}")
        status = payload.get("status", "queued")
        return ProviderJobRef(
            provider=self.name(),
            external_id=external_id,
            status=status,
            metadata={"raw_create": payload, "remix_source_id": source_id},
        )

    def cancel(self, job: ProviderJobRef) -> bool:
        self.client.delete_video(job.external_id)
        return True

