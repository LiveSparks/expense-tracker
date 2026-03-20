from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import date, datetime


def format_inr(value: Decimal | str | int | float) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return "--"
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


def format_display_date(value: date | datetime | str) -> str:
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            value = datetime.fromisoformat(value)
    return value.strftime("%d/%m/%Y")


def format_list_date(value: date | datetime | str) -> str:
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            value = datetime.fromisoformat(value)
    return value.strftime("%d %B, %Y")


def format_display_datetime(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime("%d/%m/%Y %H:%M")
