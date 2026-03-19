from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


@dataclass(slots=True)
class Expense:
    id: str
    description: str
    amount: Decimal
    category: str
    spent_on: date
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        expense_id: str,
        description: str,
        amount: str,
        category: str,
        spent_on: str,
        created_at: datetime,
    ) -> "Expense":
        normalized_description = description.strip()
        normalized_category = category.strip()
        if not normalized_description:
            raise ValueError("Description is required.")
        if not normalized_category:
            raise ValueError("Category is required.")

        try:
            parsed_amount = Decimal(amount).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError("Amount must be a valid number.") from exc

        if parsed_amount <= Decimal("0.00"):
            raise ValueError("Amount must be greater than zero.")

        try:
            parsed_date = date.fromisoformat(spent_on)
        except ValueError as exc:
            raise ValueError("Date must use YYYY-MM-DD format.") from exc

        return cls(
            id=expense_id,
            description=normalized_description,
            amount=parsed_amount,
            category=normalized_category,
            spent_on=parsed_date,
            created_at=created_at,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "description": self.description,
            "amount": f"{self.amount:.2f}",
            "category": self.category,
            "spent_on": self.spent_on.isoformat(),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> "Expense":
        return cls(
            id=payload["id"],
            description=payload["description"],
            amount=Decimal(payload["amount"]),
            category=payload["category"],
            spent_on=date.fromisoformat(payload["spent_on"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
        )
