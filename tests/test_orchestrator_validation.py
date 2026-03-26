import os
import unittest

from providers import VideoRequest
from services import build_orchestrator


class OrchestratorValidationTests(unittest.TestCase):
    def test_rejects_single_and_multi_references_together(self) -> None:
        prev_enable_veo = os.environ.get("ENABLE_VEO_PROVIDER")
        prev_enable_refs = os.environ.get("ENABLE_VEO_REFERENCE_IMAGES")
        try:
            os.environ["ENABLE_VEO_PROVIDER"] = "1"
            os.environ["ENABLE_VEO_REFERENCE_IMAGES"] = "1"
            orchestrator = build_orchestrator(api_key="stub")
            request = VideoRequest(
                provider="veo",
                prompt="test",
                model="veo-3.1-generate-preview",
                seconds=8,
                size="1280x720",
                input_reference_path="single.png",
                input_reference_paths=["a.png"],
                reference_image_type="asset",
            )
            with self.assertRaises(ValueError):
                orchestrator.start_generation(request)
        finally:
            if prev_enable_veo is None:
                os.environ.pop("ENABLE_VEO_PROVIDER", None)
            else:
                os.environ["ENABLE_VEO_PROVIDER"] = prev_enable_veo
            if prev_enable_refs is None:
                os.environ.pop("ENABLE_VEO_REFERENCE_IMAGES", None)
            else:
                os.environ["ENABLE_VEO_REFERENCE_IMAGES"] = prev_enable_refs


if __name__ == "__main__":
    unittest.main()
