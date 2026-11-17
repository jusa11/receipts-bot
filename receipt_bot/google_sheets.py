from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from .sheets_mapping import MappedProductRow, SheetMappingError, map_approved_item


SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
PRODUCTS_TAB = "Продукты"
METADATA_KEY = "grocery_receipt_bot_id"
EXPECTED_HEADERS = [
    "Месяц",
    "Неделя",
    "продукт",
    "цена план",
    "Бренд",
    "Скидка",
    "Магазин",
    "Категория",
    "цена за кг",
    "Количество",
    "Тип расчета",
    "штук",
    "цена за шт",
    "Итого",
    "примечание",
]
COLUMN_INDEX = {chr(ord("A") + index): index for index in range(15)}
USER_COLUMNS = tuple(range(10)) + (11, 12, 14)


class GoogleSheetsError(RuntimeError):
    pass


@dataclass(frozen=True)
class WriteReceiptResult:
    rows: tuple[int, ...]
    already_written: bool = False
    warnings: tuple[str, ...] = ()


class GoogleSheetsClient:
    """Sheets v4 client; all receipt writes happen in one atomic batchUpdate."""

    def __init__(
        self,
        spreadsheet_id: str,
        service_account_file: Path,
        timeout: int = 45,
        session: Any | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
            raise GoogleSheetsError("Некорректный Google Sheets ID")
        self.spreadsheet_id = spreadsheet_id
        self.service_account_file = service_account_file
        self.timeout = timeout
        if session is None:
            try:
                from google.auth.transport.requests import AuthorizedSession
                from google.oauth2 import service_account
            except ImportError as exc:
                raise GoogleSheetsError("Для Google Sheets установите зависимости: pip install -r requirements.txt") from exc
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    str(service_account_file), scopes=[SHEETS_SCOPE]
                )
            except (OSError, ValueError) as exc:
                raise GoogleSheetsError("Не удалось прочитать JSON-ключ сервисного аккаунта") from exc
            session = AuthorizedSession(credentials)
        self.session = session

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        suffix = path if path.startswith(":") else (f"/{path}" if path else "")
        url = f"{SHEETS_API}/{self.spreadsheet_id}{suffix}"
        response = self.session.request(method, url, params=params, json=body, timeout=self.timeout)
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise GoogleSheetsError("Google Sheets API вернул ответ не в JSON") from exc
        if response.status_code >= 400:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            message = error.get("message", "ошибка API") if isinstance(error, dict) else "ошибка API"
            raise GoogleSheetsError(f"Google Sheets API HTTP {response.status_code}: {str(message)[:300]}")
        if not isinstance(data, dict):
            raise GoogleSheetsError("Google Sheets API вернул неожиданный формат")
        return data

    def _values(self, a1_range: str) -> list[list[Any]]:
        encoded = quote(a1_range, safe="!:$'")
        result = self._request(
            "GET",
            f"values/{encoded}",
            params={"valueRenderOption": "FORMULA"},
        )
        values = result.get("values", [])
        return values if isinstance(values, list) else []

    def _spreadsheet_metadata(self) -> tuple[int, int]:
        result = self._request(
            "GET",
            "",
            params={"fields": "sheets(properties(sheetId,title,gridProperties(rowCount)))"},
        )
        for sheet in result.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == PRODUCTS_TAB:
                return int(props["sheetId"]), int(props.get("gridProperties", {}).get("rowCount", 0))
        raise GoogleSheetsError(f"В таблице не найден лист «{PRODUCTS_TAB}»")

    def _validate_headers(self) -> None:
        values = self._values(f"'{PRODUCTS_TAB}'!A2:O2")
        actual = values[0] if values else []
        normalized = [str(value).strip().casefold() for value in actual]
        expected = [value.casefold() for value in EXPECTED_HEADERS]
        if normalized != expected:
            raise GoogleSheetsError("Заголовки листа «Продукты» отличаются от ожидаемых; запись остановлена")

    def check_connection(self) -> int:
        self._validate_headers()
        _, row_count = self._spreadsheet_metadata()
        return row_count

    def check_connection(self) -> int:
        self._validate_headers()
        _, row_count = self._spreadsheet_metadata()
        return row_count

    def _find_next_row(self) -> int:
        rows = self._values(f"'{PRODUCTS_TAB}'!A3:O")
        last_occupied = -1
        for offset, row in enumerate(rows):
            if any(index < len(row) and row[index] not in (None, "") for index in USER_COLUMNS):
                last_occupied = offset
        return 3 + last_occupied + 1

    def _existing_receipt_rows(self, receipt_id: str) -> tuple[int, ...]:
        body = {
            "dataFilters": [
                {
                    "developerMetadataLookup": {
                        "metadataKey": METADATA_KEY,
                        "metadataValue": receipt_id,
                        "visibility": "DOCUMENT",
                        "locationType": "ROW",
                    }
                }
            ]
        }
        result = self._request("POST", "developerMetadata:search", body=body)
        rows = []
        for match in result.get("matchedDeveloperMetadata", []):
            location = match.get("developerMetadata", {}).get("location", {}).get("dimensionRange", {})
            if location.get("dimension") == "ROWS" and isinstance(location.get("startIndex"), int):
                rows.append(location["startIndex"] + 1)  # zero-based index -> sheet row
        return tuple(sorted(set(rows)))

    @staticmethod
    def _cell_value(value: Any) -> dict[str, Any]:
        if isinstance(value, bool):
            return {"userEnteredValue": {"stringValue": "Да" if value else "Нет"}}
        if isinstance(value, (int, float)):
            return {"userEnteredValue": {"numberValue": value}}
        return {"userEnteredValue": {"stringValue": str(value)}}

    @staticmethod
    def _cell_request(sheet_id: int, row_number: int, column: str, cell_value: dict[str, Any]) -> dict[str, Any]:
        column_index = COLUMN_INDEX[column]
        return {
            "updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_number - 1,
                    "endRowIndex": row_number,
                    "startColumnIndex": column_index,
                    "endColumnIndex": column_index + 1,
                },
                "rows": [{"values": [cell_value]}],
                "fields": "userEnteredValue",
            }
        }

    def _formula_state(self, start_row: int, count: int) -> list[list[Any]]:
        end_row = start_row + count - 1
        rows = self._values(f"'{PRODUCTS_TAB}'!K{start_row}:N{end_row}")
        return rows + [[] for _ in range(count - len(rows))]

    def write_receipt(
        self,
        receipt_id: str,
        approved_receipt: Mapping[str, Any],
        category_types: Mapping[str, str],
    ) -> WriteReceiptResult:
        if not re.fullmatch(r"-?\d+_\d+", receipt_id):
            raise GoogleSheetsError("Некорректный ID чека")
        items = approved_receipt.get("items")
        store = approved_receipt.get("store")
        if not isinstance(items, list) or not items:
            raise GoogleSheetsError("В подтверждённом чеке нет товарных позиций")

        existing_rows = self._existing_receipt_rows(receipt_id)
        if existing_rows:
            if len(existing_rows) != len(items):
                raise GoogleSheetsError("Найдена неполная предыдущая запись чека; автоматический повтор остановлен")
            return WriteReceiptResult(rows=existing_rows, already_written=True)

        self._validate_headers()
        sheet_id, grid_rows = self._spreadsheet_metadata()
        first_row = self._find_next_row()
        last_row = first_row + len(items) - 1
        if grid_rows and last_row > grid_rows:
            raise GoogleSheetsError("В листе недостаточно строк; добавьте строки вручную и повторите запись")

        mapped: list[MappedProductRow] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise GoogleSheetsError(f"Позиция {index + 1} имеет неверный формат")
            try:
                mapped.append(
                    map_approved_item(
                        item,
                        store=store if isinstance(store, str) else None,
                        category_types=category_types,
                        row_number=first_row + index,
                    )
                )
            except SheetMappingError as exc:
                raise GoogleSheetsError(f"Позиция {index + 1} не соответствует структуре листа: {exc}") from exc

        formula_rows = self._formula_state(first_row, len(mapped))
        requests: list[dict[str, Any]] = []
        mapping_warnings: list[str] = []
        for offset, mapped_row in enumerate(mapped):
            row_number = first_row + offset
            formula_row = formula_rows[offset]
            existing_k = formula_row[0] if len(formula_row) > 0 else None
            existing_n = formula_row[3] if len(formula_row) > 3 else None
            product_name = str(items[offset].get("product") or f"Позиция {offset + 1}")
            mapping_warnings.extend(f"{product_name}: {warning}" for warning in mapped_row.warnings)
            for column, value in mapped_row.values.items():
                requests.append(self._cell_request(sheet_id, row_number, column, self._cell_value(value)))
            for column, formula, existing in (
                ("K", mapped_row.formulas["K"], existing_k),
                ("N", mapped_row.formulas["N"], existing_n),
            ):
                if existing in (None, "", "="):
                    requests.append(self._cell_request(sheet_id, row_number, column, {"userEnteredValue": {"formulaValue": formula}}))
                elif not (isinstance(existing, str) and existing.startswith("=")):
                    raise GoogleSheetsError(f"В новой строке {row_number}, столбце {column} уже есть значение; запись остановлена")
            requests.append(
                {
                    "createDeveloperMetadata": {
                        "developerMetadata": {
                            "metadataKey": METADATA_KEY,
                            "metadataValue": receipt_id,
                            "visibility": "DOCUMENT",
                            "location": {
                                "dimensionRange": {
                                    "sheetId": sheet_id,
                                    "dimension": "ROWS",
                                    "startIndex": row_number - 1,
                                    "endIndex": row_number,
                                }
                            },
                        }
                    }
                }
            )

        # Sheets validates every subrequest and applies the whole batch atomically.
        self._request("POST", ":batchUpdate", body={"requests": requests})
        return WriteReceiptResult(rows=tuple(range(first_row, last_row + 1)), warnings=tuple(mapping_warnings))
