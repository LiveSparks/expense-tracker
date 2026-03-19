from __future__ import annotations

import json
from pathlib import Path

from .models import Expense


class ExpenseStorage:
    def __init__(self, data_file: Path) -> None:
        self.data_file = data_file

    def load(self) -> list[Expense]:
        if not self.data_file.exists():
            return []

        payload = json.loads(self.data_file.read_text(encoding="utf-8"))
        return [Expense.from_dict(item) for item in payload]

    def save(self, expenses: list[Expense]) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        payload = [expense.to_dict() for expense in expenses]
        self.data_file.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
