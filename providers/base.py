from abc import ABC, abstractmethod

from providers.models import (
    ProviderCapabilities,
    ProviderJobRef,
    ProviderResult,
    ProviderStatus,
    VideoRequest,
)


class VideoProvider(ABC):
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    def create(self, request: VideoRequest) -> ProviderJobRef:
        raise NotImplementedError

    @abstractmethod
    def get_status(self, job: ProviderJobRef) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    def download(self, job: ProviderJobRef, output_path: str) -> ProviderResult:
        raise NotImplementedError

    @abstractmethod
    def remix(self, source_id: str, prompt: str) -> ProviderJobRef:
        raise NotImplementedError

    @abstractmethod
    def cancel(self, job: ProviderJobRef) -> bool:
        raise NotImplementedError

