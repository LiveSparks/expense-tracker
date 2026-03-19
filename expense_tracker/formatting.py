from __future__ import annotations

from decimal import Decimal


def format_inr(value: Decimal | str | int | float) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    integer_part, decimal_part = f"{amount:.2f}".split(".")
    grouped = _indian_grouping(integer_part)
    return f"{sign}₹{grouped}.{decimal_part}"


def _indian_grouping(integer_part: str) -> str:
    if len(integer_part) <= 3:
        return integer_part
    last_three = integer_part[-3:]
    remaining = integer_part[:-3]
    groups: list[str] = []
    while len(remaining) > 2:
        groups.append(remaining[-2:])
        remaining = remaining[:-2]
    if remaining:
        groups.append(remaining)
    return ",".join(reversed(groups)) + f",{last_three}"
