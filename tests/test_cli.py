from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from expense_tracker.cli import main


class ExpenseTrackerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.json"

    def run_cli(self, *args: str) -> str:
        buffer = StringIO()
        with redirect_stdout(buffer):
            exit_code = main(["--data-file", str(self.data_file), *args])
        self.assertEqual(exit_code, 0)
        return buffer.getvalue().strip()

    def test_add_and_list_transactions(self) -> None:
        add_output = self.run_cli(
            "add",
            "--account",
            "Cash",
            "--payee",
            "Amazon",
            "--category",
            "General",
            "--subcategory",
            "Delivery",
            "--amount",
            "14.50",
            "--date",
            "2026-03-10",
            "--notes",
            "Order #1",
        )
        self.assertIn("Added transaction", add_output)

        list_output = self.run_cli("list")
        self.assertIn("Cash", list_output)
        self.assertIn("Amazon", list_output)
        self.assertIn("Delivery", list_output)
        self.assertIn("Order #1", list_output)
        self.assertIn("Net total: $-14.50", list_output)

    def test_summary_shows_account_balances(self) -> None:
        self.run_cli(
            "add",
            "--account",
            "Cash",
            "--payee",
            "Employer",
            "--category",
            "Income",
            "--subcategory",
            "Salary",
            "--amount",
            "1000.00",
            "--type",
            "income",
            "--date",
            "2026-03-01",
        )
        self.run_cli(
            "add",
            "--account",
            "Cash",
            "--payee",
            "Shop 1",
            "--category",
            "General",
            "--subcategory",
            "Grocery",
            "--amount",
            "120.00",
            "--date",
            "2026-03-02",
        )

        summary_output = self.run_cli("summary")
        self.assertIn("- Cash: $880.00", summary_output)
        self.assertIn("- General: $-120.00", summary_output)
        self.assertIn("- Income: $1000.00", summary_output)

    def test_delete_removes_transaction(self) -> None:
        add_output = self.run_cli(
            "add",
            "--account",
            "Cash",
            "--payee",
            "Cinema",
            "--category",
            "General",
            "--amount",
            "18.00",
            "--date",
            "2026-03-05",
        )
        transaction_id = add_output.split()[2]

        delete_output = self.run_cli("delete", transaction_id)
        self.assertEqual(delete_output, f"Deleted transaction {transaction_id}.")
        self.assertEqual(self.run_cli("list"), "No transactions found.")


if __name__ == "__main__":
    unittest.main()
