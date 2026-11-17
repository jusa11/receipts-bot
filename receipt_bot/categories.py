from __future__ import annotations

import json
import posixpath
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class CategorySourceError(RuntimeError):
    pass


def _workbook_strings(book: zipfile.ZipFile) -> list[str]:
    try:
        shared_xml = ElementTree.fromstring(book.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t"))
        for item in shared_xml.findall(f"{{{MAIN_NS}}}si")
    ]


def _cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str | None:
    value = cell.find(f"{{{MAIN_NS}}}v")
    if value is None or value.text is None:
        return None
    if cell.attrib.get("t") == "s":
        try:
            return shared_strings[int(value.text)]
        except (ValueError, IndexError) as exc:
            raise CategorySourceError(f"Некорректная ссылка на текст в ячейке {cell.attrib.get('r')}") from exc
    return value.text


def read_category_records_from_workbook(path: Path, sheet_name: str | None = None) -> list[dict[str, str]]:
    """Read Category/Calculation Type columns from an Excel workbook without normalizing labels."""
    if not path.is_file():
        raise CategorySourceError(f"Не найден Excel-файл категорий: {path}")
    try:
        with zipfile.ZipFile(path) as book:
            workbook = ElementTree.fromstring(book.read("xl/workbook.xml"))
            relationships = ElementTree.fromstring(book.read("xl/_rels/workbook.xml.rels"))
            rel_targets = {
                relationship.attrib["Id"]: relationship.attrib["Target"]
                for relationship in relationships.findall(f"{{{PKG_REL_NS}}}Relationship")
            }
            sheets: list[tuple[str, str]] = []
            for sheet in workbook.findall(f".//{{{MAIN_NS}}}sheet"):
                if sheet_name is None or sheet.attrib.get("name") == sheet_name:
                    target = rel_targets.get(sheet.attrib.get(f"{{{REL_NS}}}id", ""))
                    if target:
                        sheets.append((sheet.attrib.get("name", ""), target))
            if not sheets:
                raise CategorySourceError(f"В Excel-файле нет листа «{sheet_name}»")
            shared_strings = _workbook_strings(book)
            sheet_errors: list[str] = []
            for current_sheet_name, target in sheets:
                worksheet_path = target.lstrip("/")
                if not worksheet_path.startswith("xl/"):
                    worksheet_path = posixpath.normpath(posixpath.join("xl", worksheet_path))
                worksheet = ElementTree.fromstring(book.read(worksheet_path))
                rows: list[dict[str, str]] = []
                for row in worksheet.findall(f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row"):
                    values: dict[str, str] = {}
                    for cell in row.findall(f"{{{MAIN_NS}}}c"):
                        address = cell.attrib.get("r", "")
                        column = "".join(character for character in address if character.isalpha())
                        value = _cell_text(cell, shared_strings)
                        if column and value is not None:
                            values[column] = value
                    if values:
                        rows.append(values)
                header_index = None
                category_column = calculation_column = None
                for index, row in enumerate(rows):
                    for column, value in row.items():
                        header = value.strip().casefold()
                        if header == "категории":
                            category_column = column
                        elif header in {"тип расчета", "тип расчёта"}:
                            calculation_column = column
                    if category_column and calculation_column:
                        header_index = index
                        break
                if header_index is None:
                    sheet_errors.append(f"Лист «{current_sheet_name}»: не найдены заголовки категорий и типа расчёта")
                    continue
                records: list[dict[str, str]] = []
                for row in rows[header_index + 1 :]:
                    name = row.get(category_column, "")
                    calculation_type = row.get(calculation_column, "").strip().lower()
                    if not name.strip() and not calculation_type:
                        continue
                    if not name.strip():
                        raise CategorySourceError(f"Лист «{current_sheet_name}»: тип расчёта указан без категории")
                    if calculation_type not in {"кг", "шт"}:
                        raise CategorySourceError(
                            f"Категория {name!r}: неизвестный тип расчёта {calculation_type!r}; ожидался «кг» или «шт»"
                        )
                    records.append({"name": name, "calculation_type": calculation_type})
                if records:
                    _validate_records(records)
                    return records
                sheet_errors.append(f"Лист «{current_sheet_name}»: нет строк категорий")
            raise CategorySourceError("; ".join(sheet_errors))
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise CategorySourceError(f"Не удалось прочитать категории из {path}: {exc}") from exc


def _validate_records(records: Any) -> list[dict[str, str]]:
    if not isinstance(records, list) or not records:
        raise CategorySourceError("Список категорий пуст")
    names: list[str] = []
    validated: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"name", "calculation_type"}:
            raise CategorySourceError("Запись категории должна содержать name и calculation_type")
        name = record["name"]
        calculation_type = record["calculation_type"]
        if not isinstance(name, str) or not name.strip():
            raise CategorySourceError("Название категории пустое")
        if calculation_type not in {"кг", "шт"}:
            raise CategorySourceError(f"Категория {name!r}: недопустимый тип расчёта {calculation_type!r}")
        names.append(name)
        validated.append({"name": name, "calculation_type": calculation_type})
    if len(names) != len(set(names)):
        raise CategorySourceError("В справочнике есть дубли категорий")
    return validated


def write_static_snapshot(source_path: Path, output_path: Path) -> list[dict[str, str]]:
    records = read_category_records_from_workbook(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "source_file": source_path.name,
        "categories": records,
    }
    output_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return records


def load_static_categories(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise CategorySourceError(f"Не найден сохранённый справочник категорий: {path}")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CategorySourceError(f"Не удалось прочитать справочник категорий: {exc}") from exc
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("categories"), list):
        raise CategorySourceError("Файл справочника должен содержать массив categories")
    return _validate_records(snapshot["categories"])
