from providers import ProviderFactory
from providers.models import ProviderCapabilities
from services.orchestrator import VideoOrchestrator
from services.pricing_policy import PricingPolicy
from sora_client import SoraClient


def build_orchestrator(
    api_key: str,
    sora2_price_per_second_rub: float = 1.0,
    sora2_pro_price_per_second_rub: float = 2.5,
    veo31_price_per_second_rub: float = 2.0,
) -> VideoOrchestrator:
    client = SoraClient(api_key=api_key)
    return VideoOrchestrator(
        factory=ProviderFactory(client),
        pricing_policy=PricingPolicy(
            sora2_price_per_second_rub=sora2_price_per_second_rub,
            sora2_pro_price_per_second_rub=sora2_pro_price_per_second_rub,
            veo31_price_per_second_rub=veo31_price_per_second_rub,
        ),
    )


def list_provider_capabilities(orchestrator: VideoOrchestrator) -> dict[str, ProviderCapabilities]:
    out: dict[str, ProviderCapabilities] = {}
    for name in orchestrator.factory.list_names():
        out[name] = orchestrator.factory.get(name).capabilities()
    return out


def normalize_provider(provider: str | None, default_provider: str = "sora") -> str:
    p = (provider or "").strip().lower()
    return p if p else default_provider


def get_provider_capabilities(
    orchestrator: VideoOrchestrator,
    provider: str,
) -> ProviderCapabilities:
    return orchestrator.factory.get(normalize_provider(provider)).capabilities()


def validate_generation_params(
    orchestrator: VideoOrchestrator,
    provider: str,
    seconds: int,
    model: str,
    size: str,
) -> ProviderCapabilities:
    caps = get_provider_capabilities(orchestrator, provider)
    if seconds not in caps.allowed_seconds:
        raise ValueError(f"seconds должен быть одним из {caps.allowed_seconds}")
    if model not in caps.allowed_models:
        raise ValueError(f"model должен быть одним из {caps.allowed_models}")
    if size not in caps.allowed_sizes:
        raise ValueError(f"size должен быть одним из {caps.allowed_sizes}")
    return caps


def ensure_provider_available(caps: ProviderCapabilities) -> None:
    if caps.is_stub:
        raise ValueError(stub_provider_message(caps.provider))


def stub_provider_message(provider: str) -> str:
    return (
        f"Provider '{provider}' пока в режиме заглушки. "
        "Запуск генерации временно отключен."
    )

