from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .models import Expense
from .storage import ExpenseStorage


class ExpenseTracker:
    def __init__(self, data_file: Path) -> None:
        self.storage = ExpenseStorage(data_file)

    def add_expense(
        self,
        *,
        description: str,
        amount: str,
        category: str,
        spent_on: str,
    ) -> Expense:
        expenses = self.storage.load()
        expense = Expense.create(
            expense_id=str(uuid4()),
            description=description,
            amount=amount,
            category=category,
            spent_on=spent_on,
            created_at=datetime.now(UTC),
        )
        expenses.append(expense)
        self.storage.save(expenses)
        return expense

    def list_expenses(
        self,
        *,
        category: str | None = None,
        month: str | None = None,
    ) -> list[Expense]:
        expenses = self.storage.load()
        filtered = expenses

        if category:
            normalized_category = category.casefold()
            filtered = [
                expense
                for expense in filtered
                if expense.category.casefold() == normalized_category
            ]

        if month:
            filtered = [
                expense
                for expense in filtered
                if expense.spent_on.strftime("%Y-%m") == month
            ]

        return sorted(filtered, key=lambda expense: expense.spent_on, reverse=True)

    def delete_expense(self, expense_id: str) -> bool:
        expenses = self.storage.load()
        remaining = [expense for expense in expenses if expense.id != expense_id]
        if len(remaining) == len(expenses):
            return False

        self.storage.save(remaining)
        return True

    def summary(self, *, month: str | None = None) -> dict[str, Decimal]:
        expenses = self.list_expenses(month=month)
        totals: dict[str, Decimal] = {}
        for expense in expenses:
            totals.setdefault(expense.category, Decimal("0.00"))
            totals[expense.category] += expense.amount
        return dict(sorted(totals.items()))

    def total_spend(self, expenses: Iterable[Expense]) -> Decimal:
        total = Decimal("0.00")
        for expense in expenses:
            total += expense.amount
        return total
