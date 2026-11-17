import tempfile
import unittest
import zipfile
from pathlib import Path

from receipt_bot.categories import load_static_categories, read_category_records_from_workbook


WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Лист1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
RELATIONSHIPS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/>
</Relationships>"""
SHARED_STRINGS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="6" uniqueCount="6">
  <si><t>Категории</t></si><si><t>Тип расчета</t></si><si><t>Кг категория</t></si><si><t>кг</t></si><si><t>Полуфабрикаты </t></si><si><t>шт</t></si>
</sst>"""
SHEET = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
  <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
  <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c></row>
  <row r="3"><c r="A3" t="s"><v>4</v></c><c r="B3" t="s"><v>5</v></c></row>
</sheetData></worksheet>"""


class CategorySnapshotTests(unittest.TestCase):
    def test_import_preserves_labels_and_reads_calculation_types(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workbook_path = Path(temp_dir) / "categories.xlsx"
            with zipfile.ZipFile(workbook_path, "w") as book:
                book.writestr("xl/workbook.xml", WORKBOOK)
                book.writestr("xl/_rels/workbook.xml.rels", RELATIONSHIPS)
                book.writestr("xl/sharedStrings.xml", SHARED_STRINGS)
                book.writestr("xl/worksheets/sheet1.xml", SHEET)
            self.assertEqual(
                read_category_records_from_workbook(workbook_path),
                [
                    {"name": "Кг категория", "calculation_type": "кг"},
                    {"name": "Полуфабрикаты ", "calculation_type": "шт"},
                ],
            )

    def test_static_snapshot_has_all_categories_and_types(self):
        snapshot = Path(__file__).resolve().parents[1] / "receipt_bot" / "data" / "categories.json"
        records = load_static_categories(snapshot)
        self.assertEqual(len(records), 38)
        self.assertEqual(sum(record["calculation_type"] == "кг" for record in records), 10)
        self.assertEqual(sum(record["calculation_type"] == "шт" for record in records), 28)
        self.assertIn({"name": "Творог", "calculation_type": "шт"}, records)
        self.assertIn({"name": "Полуфабрикаты ", "calculation_type": "кг"}, records)


if __name__ == "__main__":
    unittest.main()
