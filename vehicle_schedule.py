"""Optional per-unit physical damage amounts shared by saving and rendering."""

from decimal import Decimal, InvalidOperation

DEDUCTIBLE_FIELDS = ("comprehensive_deductible", "collision_deductible")


def deductible_amount(value):
    """Blank means unknown; an explicitly entered zero remains zero."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("Deductibles must be dollar amounts, not true/false.")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("Deductibles must be nonnegative dollar amounts.") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("999999999.99"):
        raise ValueError("Deductibles must be between $0 and $999,999,999.99.")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Deductibles may have at most two decimal places.")
    return float(amount)


def normalize_units(units):
    """Preserve existing schedule metadata while validating the new amounts."""
    if not isinstance(units, list):
        raise ValueError("The vehicle schedule must be a list.")
    result = []
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError("Each scheduled vehicle must be an object.")
        row = dict(unit)
        for key in DEDUCTIBLE_FIELDS:
            amount = deductible_amount(row.get(key))
            row.pop(key, None)
            if amount is not None:
                if not str(row.get("vin") or "").strip():
                    raise ValueError("Enter a VIN for each vehicle with deductibles.")
                row[key] = amount
        result.append(row)
    return result
