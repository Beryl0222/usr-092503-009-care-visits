import unittest

from src.care_visits import RiskBand, VisitOutcome, validate_offline_token


class CareVisitContractTests(unittest.TestCase):
    def test_offline_token(self):
        token = "OFF-12AB34CD-56EF78GH"
        self.assertEqual(validate_offline_token(token), token)

    def test_invalid_token(self):
        with self.assertRaises(ValueError):
            validate_offline_token("OFF-local")

    def test_risk_outcome(self):
        self.assertEqual(RiskBand.URGENT.value, "urgent")
        self.assertEqual(VisitOutcome.RISK_FOUND.value, "risk_found")


if __name__ == "__main__":
    unittest.main()
