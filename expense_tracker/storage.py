from __future__ import annotations

import json
from pathlib import Path

from .models import Category, LedgerData, Subcategory


class LedgerStorage:
    def __init__(self, data_file: Path) -> None:
        self.data_file = data_file
        self.attachments_dir = self.data_file.parent / "attachments"

    def default_ledger(self) -> LedgerData:
        categories = [
            Category(id="category-general", name="General"),
            Category(id="category-medical", name="Medical"),
            Category(id="category-income", name="Income"),
            Category(id="category-transfers", name="Transfers"),
        ]
        subcategories = [
            Subcategory(id="subcategory-grocery", name="Grocery", category_id="category-general"),
            Subcategory(id="subcategory-delivery", name="Delivery", category_id="category-general"),
            Subcategory(id="subcategory-meds", name="Meds", category_id="category-medical"),
            Subcategory(id="subcategory-salary", name="Salary", category_id="category-income"),
            Subcategory(id="subcategory-transfer", name="Transfer", category_id="category-transfers"),
        ]
        return LedgerData(categories=categories, subcategories=subcategories)

    def load(self) -> LedgerData:
        if not self.data_file.exists():
            return self.default_ledger()

        payload = json.loads(self.data_file.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return self.default_ledger()

        merged = self.default_ledger().to_dict()
        merged.update(payload)
        return LedgerData.from_dict(merged)

    def save(self, ledger: LedgerData) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.data_file.write_text(
            json.dumps(ledger.to_dict(), indent=2),
            encoding="utf-8",
        )
