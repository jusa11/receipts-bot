import unittest

from receipt_bot.sheets_mapping import SheetMappingError, map_approved_item


class SheetMappingTests(unittest.TestCase):
    category_types = {"Мясо": "кг", "Хлеб": "шт"}

    def item(self, **changes):
        value = {
            "product": "  Мясо из чека  ",
            "brand": "Бренд",
            "category": "Мясо",
            "quantity": 250,
            "quantity_unit": "г",
            "unit_price": 400,
            "unit_price_unit": "кг",
            "total": 100,
            "discount": True,
        }
        value.update(changes)
        return value

    def test_maps_weight_rows_and_preserves_exact_product_name(self):
        mapped = map_approved_item(
            self.item(), store="Магазин", category_types=self.category_types, row_number=1000
        )
        self.assertEqual(mapped.values["C"], "  Мясо из чека  ")
        self.assertEqual(mapped.values["F"], "Да")
        self.assertEqual(mapped.values["I"], 400)
        self.assertEqual(mapped.values["J"], 0.25)
        self.assertNotIn("K", mapped.values)
        self.assertNotIn("N", mapped.values)
        self.assertIn("K", mapped.formulas)
        self.assertIn("N", mapped.formulas)

    def test_maps_piece_rows_without_clobbering_manual_or_formula_columns(self):
        mapped = map_approved_item(
            self.item(
                category="Хлеб",
                quantity=2,
                quantity_unit="шт",
                unit_price=70,
                unit_price_unit=None,
                discount=False,
            ),
            store="Магазин",
            category_types=self.category_types,
            row_number=1000,
        )
        self.assertEqual(mapped.values["F"], "Нет")
        self.assertEqual(mapped.values["L"], 2)
        self.assertEqual(mapped.values["M"], 70)
        self.assertNotIn("J", mapped.values)  # The user keeps package size empty for per-piece categories.
        self.assertNotIn("A", mapped.values)  # Month stays manual.
        self.assertNotIn("B", mapped.values)  # Week stays manual.
        self.assertNotIn("D", mapped.values)  # Planned price stays manual.
        self.assertNotIn("K", mapped.values)  # Formula is emitted separately.
        self.assertNotIn("N", mapped.values)

    def test_conflicting_quantity_units_are_left_blank_for_table_review(self):
        mapped = map_approved_item(
            self.item(category="Хлеб", quantity_unit="г", unit_price_unit="шт"),
            store="Магазин",
            category_types=self.category_types,
            row_number=20,
        )
        self.assertNotIn("L", mapped.values)
        self.assertTrue(any("единица не соответствует" in warning for warning in mapped.warnings))

    def test_missing_store_is_left_blank_but_category_must_be_valid(self):
        mapped = map_approved_item(self.item(), store=None, category_types=self.category_types, row_number=3)
        self.assertEqual(mapped.values["G"], "")
        with self.assertRaisesRegex(SheetMappingError, "справочнике"):
            map_approved_item(
                self.item(category="Новая категория"), store="Магазин", category_types=self.category_types, row_number=3
            )

    def test_missing_amounts_are_left_blank_for_correction_in_sheet(self):
        mapped = map_approved_item(
            self.item(quantity=None, unit_price=None),
            store=None,
            category_types=self.category_types,
            row_number=3,
        )
        self.assertNotIn("I", mapped.values)
        self.assertNotIn("J", mapped.values)
        self.assertTrue(mapped.warnings)


if __name__ == "__main__":
    unittest.main()
