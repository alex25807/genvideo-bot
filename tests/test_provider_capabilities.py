import os
import unittest

from services import build_orchestrator, list_provider_capabilities


class ProviderCapabilitiesTests(unittest.TestCase):
    def test_veo_reference_types_enabled_by_flag(self) -> None:
        prev_enable_veo = os.environ.get("ENABLE_VEO_PROVIDER")
        prev_enable_refs = os.environ.get("ENABLE_VEO_REFERENCE_IMAGES")
        try:
            os.environ["ENABLE_VEO_PROVIDER"] = "1"
            os.environ["ENABLE_VEO_REFERENCE_IMAGES"] = "1"
            orchestrator = build_orchestrator(api_key="stub")
            caps = list_provider_capabilities(orchestrator)
            self.assertIn("veo", caps)
            veo = caps["veo"]
            self.assertTrue(veo.supports_reference_images)
            self.assertEqual(veo.max_reference_images, 3)
            self.assertEqual(veo.supported_reference_types, ("asset", "style"))
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
