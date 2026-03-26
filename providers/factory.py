import os

from providers.base import VideoProvider
from providers.sora_adapter import SoraProviderAdapter
from providers.veo_adapter import VeoProviderAdapter
from sora_client import SoraClient
from veo_client import VeoClient


class ProviderFactory:
    def __init__(self, sora_client: SoraClient, enable_veo: bool | None = None) -> None:
        self._providers: dict[str, VideoProvider] = {
            "sora": SoraProviderAdapter(sora_client),
        }
        if enable_veo is None:
            env_raw = os.getenv("ENABLE_VEO_PROVIDER", "0").strip().lower()
            enable_veo = env_raw in {"1", "true", "yes", "on"}
        if enable_veo:
            ref_env_raw = os.getenv("ENABLE_VEO_REFERENCE_IMAGES", "0").strip().lower()
            enable_reference_images = ref_env_raw in {"1", "true", "yes", "on"}
            self._providers["veo"] = VeoProviderAdapter(
                VeoClient(api_key=sora_client.api_key),
                enable_reference_images=enable_reference_images,
            )

    def get(self, provider_name: str) -> VideoProvider:
        key = (provider_name or "").strip().lower()
        if key not in self._providers:
            supported = ", ".join(sorted(self._providers.keys()))
            raise ValueError(f"Неподдерживаемый provider '{provider_name}'. Доступно: {supported}")
        return self._providers[key]

    def list_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers.keys()))

