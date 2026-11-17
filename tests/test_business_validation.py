import unittest

from receipt_bot.business_validation import apply_receipt_validation, strip_validation_metadata, validate_receipt_arithmetic


class ReceiptArithmeticTests(unittest.TestCase):
    category_types = {"Мясо": "кг", "Хлеб": "шт"}

    def item(self, **changes):
        value = {
            "product": "Товар",
            "category": "Мясо",
            "quantity": 1.25,
            "quantity_unit": "кг",
            "unit_price": 200,
            "unit_price_unit": "кг",
            "total": 250,
            "discount": False,
            "needs_review": False,
            "review_reason": None,
        }
        value.update(changes)
        return value

    def receipt(self, item=None, **changes):
        value = {
            "store": "Магазин",
            "items": [item or self.item()],
            "receipt_total": 250,
            "needs_review": False,
            "review_reason": None,
        }
        value.update(changes)
        return value

    def test_weight_calculation_matches_table_formula(self):
        self.assertEqual(validate_receipt_arithmetic(self.receipt(), self.category_types), [])

    def test_grams_are_converted_to_kilograms(self):
        item = self.item(quantity=250, quantity_unit="г", unit_price=400, unit_price_unit="кг", total=100)
        self.assertEqual(validate_receipt_arithmetic(self.receipt(item, receipt_total=100), self.category_types), [])

    def test_piece_calculation_matches_table_formula(self):
        item = self.item(
            category="Хлеб",
            quantity=2,
            quantity_unit="шт",
            unit_price=69.99,
            unit_price_unit="шт",
            total=139.98,
        )
        self.assertEqual(validate_receipt_arithmetic(self.receipt(item, receipt_total=139.98), self.category_types), [])

    def test_line_total_mismatch_is_reported(self):
        issues = validate_receipt_arithmetic(self.receipt(self.item(total=240), receipt_total=240), self.category_types)
        self.assertIn("line_total_mismatch", [issue["code"] for issue in issues])

    def test_incompatible_units_are_sent_to_review(self):
        item = self.item(quantity_unit="шт", unit_price_unit="шт")
        issues = validate_receipt_arithmetic(self.receipt(item), self.category_types)
        self.assertIn("calculation_unit_mismatch", [issue["code"] for issue in issues])

    def test_missing_price_unit_is_not_misreported_as_a_unit_conflict(self):
        item = self.item(unit_price_unit=None)
        issues = validate_receipt_arithmetic(self.receipt(item), self.category_types)
        self.assertNotIn("calculation_unit_mismatch", [issue["code"] for issue in issues])

    def test_discounted_line_is_not_recalculated_without_discount_amount(self):
        item = self.item(total=225, discount=True)
        issues = validate_receipt_arithmetic(self.receipt(item, receipt_total=225), self.category_types)
        self.assertEqual(issues, [])

    def test_receipt_sum_mismatch_is_reported(self):
        issues = validate_receipt_arithmetic(self.receipt(receipt_total=251), self.category_types)
        self.assertIn("receipt_total_mismatch", [issue["code"] for issue in issues])

    def test_apply_validation_is_recalculable_and_approved_data_is_clean(self):
        receipt = self.receipt(self.item(total=240), receipt_total=240)
        apply_receipt_validation(receipt, self.category_types)
        self.assertTrue(receipt["needs_review"])
        self.assertEqual(receipt["validation_issues"][0]["code"], "line_total_mismatch")
        receipt["items"][0]["total"] = 250
        receipt["receipt_total"] = 250
        apply_receipt_validation(receipt, self.category_types)
        self.assertFalse(receipt["needs_review"])
        approved = strip_validation_metadata(receipt)
        self.assertNotIn("_validation_base", approved)
        self.assertNotIn("_validation_base", approved["items"][0])


if __name__ == "__main__":
    unittest.main()
