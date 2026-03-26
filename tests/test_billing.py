import unittest
import shutil
import tempfile
from pathlib import Path

from billing import (
    apply_payment_credits_if_needed,
    create_payment,
    generation_credit_cost,
    get_credits,
    init_billing,
)


class BillingCostTests(unittest.TestCase):
    def test_sora_costs(self) -> None:
        self.assertEqual(generation_credit_cost(4, "sora-2", "sora"), 1)
        self.assertEqual(generation_credit_cost(8, "sora-2", "sora"), 2)
        self.assertEqual(generation_credit_cost(12, "sora-2-pro", "sora"), 6)

    def test_veo_costs(self) -> None:
        self.assertEqual(generation_credit_cost(4, "veo-3.1-generate-preview", "veo"), 1)
        self.assertEqual(generation_credit_cost(6, "veo-3.1-generate-preview", "veo"), 2)
        self.assertEqual(generation_credit_cost(8, "veo-3.1-generate-preview", "veo"), 2)


class BillingPaymentSafetyTests(unittest.TestCase):
    def test_apply_payment_requires_confirmed_status(self) -> None:
        tmp_dir = tempfile.mkdtemp()
        try:
            db_path = Path(tmp_dir) / "billing.db"
            init_billing(db_path)
            payment = create_payment(
                db_path=db_path,
                provider="mock",
                user_id="u1",
                client_token="tok1",
                package_id="p5",
                amount_rub=100,
                credits=5,
                order_id="ord-test",
                meta={"source": "unit"},
            )

            with self.assertRaises(ValueError):
                apply_payment_credits_if_needed(db_path, payment["payment_id"])

            self.assertEqual(get_credits(db_path, "u1", 0), 0)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
