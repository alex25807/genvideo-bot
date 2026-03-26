from providers.models import VideoRequest


class PricingPolicy:
    def __init__(
        self,
        sora2_price_per_second_rub: float = 1.0,
        sora2_pro_price_per_second_rub: float = 2.5,
        veo31_price_per_second_rub: float = 2.0,
    ) -> None:
        self.sora2_price_per_second_rub = sora2_price_per_second_rub
        self.sora2_pro_price_per_second_rub = sora2_pro_price_per_second_rub
        self.veo31_price_per_second_rub = veo31_price_per_second_rub

    def estimate_rub(self, request: VideoRequest) -> float:
        provider = (request.provider or "").lower().strip()
        model = (request.model or "").lower().strip()

        if provider == "veo":
            return round(self.veo31_price_per_second_rub * request.seconds, 2)

        if provider != "sora":
            raise ValueError(f"Пока не настроена ценовая политика для provider='{provider}'")

        if model == "sora-2-pro":
            return round(self.sora2_pro_price_per_second_rub * request.seconds, 2)

        return round(self.sora2_price_per_second_rub * request.seconds, 2)

