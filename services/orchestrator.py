import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from providers.factory import ProviderFactory
from providers.models import ProviderJobRef, ProviderResult, ProviderStatus, VideoRequest
from services.pricing_policy import PricingPolicy


ProgressCallback = Optional[Callable[[str, int, str], None]]
CancelCheck = Optional[Callable[[], bool]]


@dataclass
class OrchestratorOutcome:
    job: ProviderJobRef
    status: ProviderStatus
    result: Optional[ProviderResult]
    estimated_cost_rub: float


class VideoOrchestrator:
    """
    Phase 1-2 orchestrator:
    - unified create/poll/download flow
    - provider is selected through ProviderFactory
    - pricing is estimated through PricingPolicy
    """

    def __init__(self, factory: ProviderFactory, pricing_policy: PricingPolicy) -> None:
        self.factory = factory
        self.pricing_policy = pricing_policy

    def estimate_cost_rub(self, request: VideoRequest) -> float:
        return self.pricing_policy.estimate_rub(request)

    def start_generation(self, request: VideoRequest) -> ProviderJobRef:
        provider = self.factory.get(request.provider)
        self._validate_request(request)
        return provider.create(request)

    def start_remix(self, provider_name: str, source_video_id: str, prompt: str) -> ProviderJobRef:
        provider = self.factory.get(provider_name)
        return provider.remix(source_id=source_video_id, prompt=prompt)

    def wait_until_done(
        self,
        provider_name: str,
        job: ProviderJobRef,
        poll_interval_sec: float = 3.0,
        on_progress: ProgressCallback = None,
        timeout_sec: int = 1800,
        should_cancel: CancelCheck = None,
        cancel_on_request: bool = True,
    ) -> ProviderStatus:
        provider = self.factory.get(provider_name)
        started = time.time()
        last_progress = -1

        while True:
            if should_cancel and should_cancel():
                if cancel_on_request:
                    try:
                        provider.cancel(job)
                    except Exception:
                        pass
                raise RuntimeError("Генерация отменена пользователем")

            if time.time() - started > timeout_sec:
                raise TimeoutError("Превышено время ожидания генерации видео")

            status = provider.get_status(job)
            progress = max(0, min(100, int(status.progress or 0)))
            if on_progress and progress != last_progress:
                on_progress(status.status, progress, status.error or "")
            last_progress = progress

            if status.status == "completed":
                if on_progress and progress < 100:
                    on_progress(status.status, 100, "")
                return status

            if status.status == "failed":
                return status

            time.sleep(poll_interval_sec)

    def download_result(self, provider_name: str, job: ProviderJobRef, output_path: str) -> ProviderResult:
        provider = self.factory.get(provider_name)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        return provider.download(job=job, output_path=str(output))

    def cancel(self, provider_name: str, job: ProviderJobRef) -> bool:
        provider = self.factory.get(provider_name)
        return provider.cancel(job)

    def run_sync(
        self,
        request: VideoRequest,
        output_path: str,
        poll_interval_sec: float = 3.0,
        on_progress: ProgressCallback = None,
        timeout_sec: int = 1800,
        should_cancel: CancelCheck = None,
    ) -> OrchestratorOutcome:
        job = self.start_generation(request)
        status = self.wait_until_done(
            provider_name=request.provider,
            job=job,
            poll_interval_sec=poll_interval_sec,
            on_progress=on_progress,
            timeout_sec=timeout_sec,
            should_cancel=should_cancel,
        )

        result = None
        if status.status == "completed":
            result = self.download_result(
                provider_name=request.provider,
                job=job,
                output_path=output_path,
            )

        return OrchestratorOutcome(
            job=job,
            status=status,
            result=result,
            estimated_cost_rub=self.estimate_cost_rub(request),
        )

    def _validate_request(self, request: VideoRequest) -> None:
        provider = self.factory.get(request.provider)
        caps = provider.capabilities()

        if request.seconds not in caps.allowed_seconds:
            raise ValueError(
                f"Недопустимая длительность {request.seconds}. Разрешено: {caps.allowed_seconds}"
            )

        if request.model not in caps.allowed_models:
            raise ValueError(f"Недопустимая модель '{request.model}'. Разрешено: {caps.allowed_models}")

        if request.size not in caps.allowed_sizes:
            raise ValueError(f"Недопустимый размер '{request.size}'. Разрешено: {caps.allowed_sizes}")

        if request.input_reference_path and not caps.supports_input_reference:
            raise ValueError(f"Provider '{caps.provider}' не поддерживает input_reference")

        ref_images = request.input_reference_paths or []
        if request.input_reference_path and ref_images:
            raise ValueError("Нельзя передавать одновременно input_reference и input_reference_paths")
        if ref_images:
            if not caps.supports_reference_images:
                raise ValueError(f"Provider '{caps.provider}' не поддерживает referenceImages")
            if caps.max_reference_images and len(ref_images) > caps.max_reference_images:
                raise ValueError(
                    f"Provider '{caps.provider}' принимает максимум "
                    f"{caps.max_reference_images} referenceImages"
                )
            ref_type = (request.reference_image_type or "asset").strip().lower()
            if caps.supported_reference_types and ref_type not in caps.supported_reference_types:
                raise ValueError(
                    f"Provider '{caps.provider}' не поддерживает reference_type='{ref_type}'. "
                    f"Разрешено: {caps.supported_reference_types}"
                )

