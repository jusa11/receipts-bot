import unittest

from receipt_bot.parser import ParserError, _decode_json_response, receipt_schema, validate_receipt


class ReceiptValidationTests(unittest.TestCase):
    categories = ["Молоко", "Творог"]

    def item(self, **changes):
        item = {
            "product": "Молоко 3,2%",
            "brand": "Домик в деревне",
            "category": "Молоко",
            "quantity": 1,
            "quantity_unit": "шт",
            "unit_price": 154.99,
            "unit_price_unit": "шт",
            "total": 154.99,
            "discount": False,
            "needs_review": False,
            "review_reason": None,
        }
        item.update(changes)
        return item

    def receipt(self, item=None, **changes):
        receipt = {
            "store": "Магнит",
            "items": [item or self.item()],
            "receipt_total": 154.99,
            "needs_review": False,
            "review_reason": None,
        }
        receipt.update(changes)
        return receipt

    def test_schema_limits_category_to_reference(self):
        schema = receipt_schema(self.categories)
        item_schema = schema["properties"]["items"]["items"]
        self.assertEqual(item_schema["properties"]["category"]["enum"], ["Молоко", "Творог", None])

    def test_accepts_valid_receipt(self):
        value = validate_receipt(self.receipt(), self.categories)
        self.assertEqual(value["items"][0]["product"], "Молоко 3,2%")

    def test_rejects_category_outside_reference(self):
        with self.assertRaisesRegex(ParserError, "категория отсутствует"):
            validate_receipt(self.receipt(self.item(category="Молочные продукты")), self.categories)

    def test_marks_missing_required_measurements_for_review(self):
        value = validate_receipt(
            self.receipt(self.item(category=None, quantity=None, total=None)),
            self.categories,
        )
        self.assertTrue(value["items"][0]["needs_review"])
        self.assertTrue(value["needs_review"])

    def test_rejects_extra_fields(self):
        with self.assertRaisesRegex(ParserError, "ожидаемым полям"):
            validate_receipt(self.receipt(unexpected="value"), self.categories)

    def test_decodes_plain_and_fenced_json(self):
        self.assertEqual(_decode_json_response('{"store":null}'), {"store": None})
        self.assertEqual(_decode_json_response('```json\n{"store":null}\n```'), {"store": None})


if __name__ == "__main__":
    unittest.main()
