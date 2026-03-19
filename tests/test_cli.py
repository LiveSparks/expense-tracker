from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from expense_tracker.cli import main
from expense_tracker.tracker import ExpenseTracker


class ExpenseTrackerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "expenses.json"

    def run_cli(self, *args: str) -> str:
        buffer = StringIO()
        with redirect_stdout(buffer):
            exit_code = main(["--data-file", str(self.data_file), *args])
        self.assertEqual(exit_code, 0)
        return buffer.getvalue().strip()

    def test_add_and_list_expenses(self) -> None:
        add_output = self.run_cli(
            "add",
            "--description",
            "Coffee",
            "--amount",
            "4.50",
            "--category",
            "Food",
            "--date",
            "2026-03-10",
        )
        self.assertIn("Added expense", add_output)

        list_output = self.run_cli("list")
        self.assertIn("Coffee", list_output)
        self.assertIn("$4.50", list_output)
        self.assertIn("Total: $4.50", list_output)

    def test_summary_groups_by_category(self) -> None:
        tracker = ExpenseTracker(self.data_file)
        tracker.add_expense(
            description="Groceries",
            amount="12.20",
            category="Food",
            spent_on="2026-03-01",
        )
        tracker.add_expense(
            description="Train ticket",
            amount="8.00",
            category="Travel",
            spent_on="2026-03-02",
        )
        tracker.add_expense(
            description="Lunch",
            amount="5.80",
            category="Food",
            spent_on="2026-03-03",
        )

        summary_output = self.run_cli("summary")
        self.assertIn("- Food: $18.00", summary_output)
        self.assertIn("- Travel: $8.00", summary_output)
        self.assertIn("Grand total: $26.00", summary_output)

    def test_delete_removes_an_expense(self) -> None:
        tracker = ExpenseTracker(self.data_file)
        expense = tracker.add_expense(
            description="Movie ticket",
            amount="14.00",
            category="Fun",
            spent_on="2026-03-05",
        )

        delete_output = self.run_cli("delete", expense.id)
        self.assertEqual(delete_output, f"Deleted expense {expense.id}.")
        self.assertEqual(self.run_cli("list"), "No expenses found.")


if __name__ == "__main__":
    unittest.main()
