from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


MONEY_TOLERANCE = Decimal("0.02")
WEIGHT_UNITS = {"г", "кг"}
COUNT_UNITS = {"шт", "упак"}


def validate_receipt_arithmetic(
    receipt: Mapping[str, Any],
    category_types: Mapping[str, str],
    tolerance: Decimal = MONEY_TOLERANCE,
) -> list[dict[str, Any]]:
    """Compare extracted values with the workbook's kg/each calculation rules.

    This function only reports discrepancies; it never changes model values or
    tries to infer a missing price/quantity.
    """
    issues: list[dict[str, Any]] = []
    items = receipt.get("items")
    if not isinstance(items, list):
        return [{"scope": "receipt", "code": "items_invalid", "message": "Список товарных позиций имеет неверный формат"}]

    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            continue
        category = item.get("category")
        calc_type = category_types.get(category) if isinstance(category, str) else None
        if calc_type not in {"кг", "шт"}:
            continue  # Missing/invalid category is already a schema/manual-review issue.
        if item.get("discount") is True:
            continue  # The table has no discount amount column; a discounted line need not equal qty × unit price.
        try:
            expected = _expected_total(item, calc_type)
        except (InvalidOperation, TypeError, ValueError):
            expected = None
        if expected is None:
            if _has_complete_amounts(item):
                issues.append(
                    {
                        "scope": "item",
                        "item_index": index,
                        "code": "calculation_unit_mismatch",
                        "message": f"Позиция {index}: единицы количества или цены не соответствуют типу расчёта «{calc_type}» из таблицы",
                    }
                )
            continue
        actual = _decimal(item.get("total"))
        if actual is None:
            continue
        if abs(actual - expected) > tolerance:
            issues.append(
                {
                    "scope": "item",
                    "item_index": index,
                    "code": "line_total_mismatch",
                    "message": f"Позиция {index}: сумма строки {actual:.2f} ₽ не совпадает с расчётом по таблице {expected:.2f} ₽",
                }
            )

    receipt_total = _decimal(receipt.get("receipt_total"))
    if receipt_total is not None and not any(item.get("skipped") for item in items if isinstance(item, Mapping)):
        totals = [_decimal(item.get("total")) for item in items if isinstance(item, Mapping)]
        if totals and all(value is not None for value in totals):
            item_sum = sum((value for value in totals if value is not None), Decimal("0"))
            if abs(item_sum - receipt_total) > tolerance:
                issues.append(
                    {
                        "scope": "receipt",
                        "code": "receipt_total_mismatch",
                        "message": f"Сумма позиций {item_sum:.2f} ₽ не совпадает с итогом чека {receipt_total:.2f} ₽",
                    }
                )
    return issues


def apply_receipt_validation(receipt: dict[str, Any], category_types: Mapping[str, str]) -> dict[str, Any]:
    """Attach recalculable arithmetic warnings while preserving model review flags."""
    if "_validation_base" not in receipt:
        receipt["_validation_base"] = {
            "needs_review": receipt.get("needs_review", False),
            "review_reason": receipt.get("review_reason"),
        }
    base = receipt["_validation_base"]
    items = receipt.get("items", [])
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and "_validation_base" not in item:
                item["_validation_base"] = {
                    "needs_review": item.get("needs_review", False),
                    "review_reason": item.get("review_reason"),
                }

    issues = validate_receipt_arithmetic(receipt, category_types)
    receipt["validation_issues"] = issues
    item_messages: dict[int, list[str]] = {}
    for issue in issues:
        if issue.get("scope") == "item" and isinstance(issue.get("item_index"), int):
            item_messages.setdefault(issue["item_index"], []).append(issue["message"])

    if isinstance(items, list):
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            item_base = item["_validation_base"]
            messages = item_messages.get(index, [])
            item["needs_review"] = bool(item_base.get("needs_review")) or bool(messages)
            reasons = [item_base.get("review_reason"), *messages]
            item["review_reason"] = "; ".join(value for value in reasons if value) or None

    base_needs_review = bool(base.get("needs_review"))
    item_needs_review = any(item.get("needs_review") for item in items if isinstance(item, dict)) if isinstance(items, list) else False
    receipt["needs_review"] = base_needs_review or item_needs_review or bool(issues)
    reasons = [base.get("review_reason"), *(issue["message"] for issue in issues)]
    receipt["review_reason"] = "; ".join(dict.fromkeys(value for value in reasons if value)) or None
    return receipt


def strip_validation_metadata(receipt: dict[str, Any]) -> dict[str, Any]:
    """Remove private recalculation baselines before saving the approved payload."""
    result = dict(receipt)
    result.pop("_validation_base", None)
    result["items"] = [
        {key: value for key, value in item.items() if key != "_validation_base" and key != "skipped"}
        for item in receipt.get("items", [])
        if not item.get("skipped")
    ]
    return result


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _has_complete_amounts(item: Mapping[str, Any]) -> bool:
    return (
        all(_decimal(item.get(key)) is not None for key in ("quantity", "unit_price", "total"))
        and item.get("quantity_unit") is not None
        and item.get("unit_price_unit") is not None
    )


def _expected_total(item: Mapping[str, Any], calc_type: str) -> Decimal | None:
    quantity = _decimal(item.get("quantity"))
    unit_price = _decimal(item.get("unit_price"))
    if quantity is None or unit_price is None:
        return None
    quantity_unit = item.get("quantity_unit")
    price_unit = item.get("unit_price_unit")
    if calc_type == "кг":
        if quantity_unit not in WEIGHT_UNITS or price_unit not in WEIGHT_UNITS:
            return None
        # Normalize both terms to grams: quantity in grams × price per gram.
        quantity_g = quantity * (Decimal("1000") if quantity_unit == "кг" else Decimal("1"))
        price_per_g = unit_price / Decimal("1000") if price_unit == "кг" else unit_price
        return quantity_g * price_per_g
    if quantity_unit not in COUNT_UNITS or price_unit not in COUNT_UNITS:
        return None
    return quantity * unit_price
