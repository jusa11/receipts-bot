from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


class SheetMappingError(ValueError):
    pass


@dataclass(frozen=True)
class MappedProductRow:
    """Sparse values and existing table formulas for one row in «Продукты».

    Month/week/planned price/notes are deliberately omitted for manual entry.
    Formula cells are separate so a caller cannot accidentally overwrite them
    with empty values.
    """

    values: dict[str, Any]
    formulas: dict[str, str]
    warnings: tuple[str, ...] = ()


def map_approved_item(
    item: Mapping[str, Any],
    *,
    store: str | None,
    category_types: Mapping[str, str],
    row_number: int,
) -> MappedProductRow:
    """Map a confirmed receipt item to the existing «Продукты» layout.

    Column K (calculation type) and N (line total) retain the workbook's
    formulas. Columns A/B/D/O are intentionally left for manual entry.
    """
    product = item.get("product")
    category = item.get("category")
    if not isinstance(product, str) or not product.strip():
        raise SheetMappingError("В позиции нет названия товара")
    if not isinstance(category, str) or category not in category_types:
        raise SheetMappingError("Категория позиции отсутствует в справочнике таблицы")
    if row_number < 3:
        raise SheetMappingError("Строки покупок начинаются с 3-й строки")

    calc_type = category_types[category]
    quantity = _number(item.get("quantity"), "Количество")
    price = _number(item.get("unit_price"), "Цена")
    if (quantity is not None and quantity < 0) or (price is not None and price < 0):
        raise SheetMappingError("Количество и цена не могут быть отрицательными")

    values: dict[str, Any] = {
        "C": product,  # product name is preserved exactly
        "E": item.get("brand") or "",
        "F": "Да" if item.get("discount") is True else "Нет" if item.get("discount") is False else "",
        "G": store.strip() if isinstance(store, str) else "",
        "H": category,
    }
    warnings: list[str] = []
    if not isinstance(store, str) or not store.strip():
        warnings.append("Магазин в G оставлен пустым")
    if item.get("discount") is None:
        warnings.append("Скидка в F оставлена пустой")
    quantity_unit = item.get("quantity_unit")
    price_unit = item.get("unit_price_unit")

    if calc_type == "кг":
        if quantity is not None:
            if quantity_unit in {None, "кг", "г"}:
                values["J"] = quantity / 1000 if quantity_unit == "г" else quantity
            else:
                warnings.append("Количество не записано: единица не соответствует категории «кг»")
        else:
            warnings.append("Количество в J оставлено пустым")
        if price is not None:
            if price_unit in {None, "кг", "г"}:
                values["I"] = price * 1000 if price_unit == "г" else price
            else:
                warnings.append("Цена за кг в I оставлена пустой: единица цены не соответствует категории «кг»")
        else:
            warnings.append("Цена за кг в I оставлена пустой")
    elif calc_type == "шт":
        if quantity is not None and quantity_unit in {None, "шт", "упак"}:
            values["L"] = quantity
        else:
            warnings.append("Количество штук в L оставлено пустым" if quantity is None else "Количество не записано в L: единица не соответствует категории «шт»")
        if price is not None and price_unit in {None, "шт", "упак"}:
            values["M"] = price
        else:
            warnings.append("Цена за шт в M оставлена пустой" if price is None else "Цена не записана в M: единица не соответствует категории «шт»")
    else:
        raise SheetMappingError(f"Неизвестный тип расчёта категории: {calc_type}")

    formulas = {
        "K": f"=VLOOKUP(H{row_number},Справочник!B:C,2,FALSE)",
        "N": f'=IF(K{row_number}="кг",I{row_number}*J{row_number},L{row_number}*M{row_number})',
    }
    return MappedProductRow(values=values, formulas=formulas, warnings=tuple(warnings))


def _number(value: Any, field: str) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SheetMappingError(f"Поле «{field}» должно быть числом") from exc
    if not math.isfinite(number):
        raise SheetMappingError(f"Поле «{field}» должно быть конечным числом")
    return number
