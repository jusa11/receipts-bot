import unittest
from pathlib import Path
from urllib.parse import unquote

from receipt_bot.google_sheets import GoogleSheetsClient


HEADERS = [
    "Месяц", "Неделя", "продукт", "цена план", "Бренд", "Скидка", "Магазин", "Категория",
    "цена за кг ", "Количество ", "Тип расчета", "штук", "цена за шт", "Итого", "примечание",
]


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


class FakeSession:
    def __init__(self):
        self.metadata = []
        self.batch_requests = []

    def request(self, method, url, *, params=None, json=None, timeout=None):
        if "developerMetadata:search" in url:
            receipt_id = json["dataFilters"][0]["developerMetadataLookup"]["metadataValue"]
            found = [
                {"developerMetadata": {"metadataValue": receipt_id, "location": {"dimensionRange": row}}}
                for row in self.metadata
            ]
            return FakeResponse({"matchedDeveloperMetadata": found})
        if ":batchUpdate" in url:
            self.batch_requests.append(json["requests"])
            for request in json["requests"]:
                metadata = request.get("createDeveloperMetadata", {}).get("developerMetadata")
                if metadata:
                    self.metadata.append(metadata["location"]["dimensionRange"])
            return FakeResponse({"spreadsheetId": "test"})
        if "/values/" in url:
            a1 = unquote(url.split("/values/", 1)[1])
            if a1 == "'Продукты'!A2:O2":
                return FakeResponse({"values": [HEADERS]})
            if a1 == "'Продукты'!A3:O":
                return FakeResponse({"values": [["", "", "existing product"]]})
            if a1 == "'Продукты'!K4:N4":
                return FakeResponse({"values": [["=VLOOKUP(H4,Справочник!B:C,2,FALSE)", "", "", "="]]})
        if method == "GET" and url.endswith("/test-sheet"):
            return FakeResponse(
                {"sheets": [{"properties": {"sheetId": 42, "title": "Продукты", "gridProperties": {"rowCount": 1500}}}]}
            )
        raise AssertionError(f"Unexpected fake request: {method} {url}")


class GoogleSheetsWriteTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.client = GoogleSheetsClient("test-sheet", Path("not-used.json"), session=self.session)
        self.approved = {
            "store": "Магазин",
            "items": [
                {
                    "product": "Багет",
                    "brand": None,
                    "category": "Хлеб",
                    "quantity": 2,
                    "quantity_unit": "шт",
                    "unit_price": 70,
                    "unit_price_unit": None,
                    "total": 140,
                    "discount": False,
                }
            ],
        }

    def test_batch_preserves_existing_formula_and_is_idempotent(self):
        types = {"Хлеб": "шт"}
        first = self.client.write_receipt("12345_678", self.approved, types)
        self.assertEqual(first.rows, (4,))
        self.assertFalse(first.already_written)
        requests = self.session.batch_requests[0]
        updated_columns = {
            request["updateCells"]["range"]["startColumnIndex"]
            for request in requests
            if "updateCells" in request
        }
        self.assertIn(13, updated_columns)  # N formula was missing and is added.
        self.assertNotIn(10, updated_columns)  # K formula already existed and is untouched.
        self.assertEqual(sum("createDeveloperMetadata" in request for request in requests), 1)

        retried = self.client.write_receipt("12345_678", self.approved, types)
        self.assertEqual(retried.rows, (4,))
        self.assertTrue(retried.already_written)
        self.assertEqual(len(self.session.batch_requests), 1)

    def test_kilogram_mapping_writes_price_and_quantity(self):
        self.session.request = self._kg_session_request
        approved = {
            "store": "Магазин",
            "items": [
                {
                    "product": "Мясо",
                    "brand": None,
                    "category": "Мясо",
                    "quantity": 1.25,
                    "quantity_unit": "кг",
                    "unit_price": 300,
                    "unit_price_unit": "кг",
                    "total": 375,
                    "discount": False,
                }
            ],
        }
        self.client.write_receipt("12345_679", approved, {"Мясо": "кг"})
        requests = self.session.batch_requests[0]
        writes = {
            request["updateCells"]["range"]["startColumnIndex"]: request["updateCells"]["rows"][0]["values"][0]["userEnteredValue"]
            for request in requests
            if "updateCells" in request
        }
        self.assertEqual(writes[8], {"numberValue": 300})
        self.assertEqual(writes[9], {"numberValue": 1.25})

    def _kg_session_request(self, method, url, *, params=None, json=None, timeout=None):
        if "/values/" in url:
            a1 = unquote(url.split("/values/", 1)[1])
            if a1 == "'Продукты'!K4:N4":
                return FakeResponse({"values": [["=VLOOKUP(H4,Справочник!B:C,2,FALSE)", "", "", "="]]})
            if a1 == "'Продукты'!A3:O":
                return FakeResponse({"values": [["", "", "existing product"]]})
            if a1 == "'Продукты'!A2:O2":
                return FakeResponse({"values": [HEADERS]})
        if "developerMetadata:search" in url:
            return FakeResponse({"matchedDeveloperMetadata": []})
        if method == "GET" and url.endswith("/test-sheet"):
            return FakeResponse(
                {"sheets": [{"properties": {"sheetId": 42, "title": "Продукты", "gridProperties": {"rowCount": 1500}}}]}
            )
        if ":batchUpdate" in url:
            self.session.batch_requests.append(json["requests"])
            return FakeResponse({"spreadsheetId": "test"})
        raise AssertionError(f"Unexpected fake request: {method} {url}")


if __name__ == "__main__":
    unittest.main()
