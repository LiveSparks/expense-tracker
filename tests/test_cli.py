from __future__ import annotations

import json
import tempfile
import unittest
from csv import DictWriter
from contextlib import redirect_stdout
from datetime import datetime
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
            "-14.50",
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
        self.assertIn("Net total: -₹14.50", list_output)

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
            "-120.00",
            "--date",
            "2026-03-02",
        )

        summary_output = self.run_cli("summary")
        self.assertIn("- Cash: ₹880.00", summary_output)
        self.assertIn("- General: -₹120.00", summary_output)
        self.assertIn("- Income: ₹1,000.00", summary_output)

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
            "-18.00",
            "--date",
            "2026-03-05",
        )
        transaction_id = add_output.split()[2]

        delete_output = self.run_cli("delete", transaction_id)
        self.assertEqual(delete_output, f"Deleted transaction {transaction_id}.")
        self.assertEqual(self.run_cli("list"), "No transactions found.")

    def test_seed_demo_data_imports_mock_ledger(self) -> None:
        seed_file = Path(self.temp_dir.name) / "mock_ledger_seed.json"
        seed_file.write_text(
            json.dumps(
                {
                    "accounts": [{"id": "account-cash", "name": "Cash"}],
                    "payees": [{"id": "payee-amazon", "name": "Amazon", "linked_account_id": None}],
                    "categories": [{"id": "category-general", "name": "General"}],
                    "subcategories": [{"id": "subcategory-delivery", "name": "Delivery", "category_id": "category-general"}],
                    "transactions": [
                        {
                            "id": "txn-seed-1",
                            "entry_type": "expense",
                            "account_id": "account-cash",
                            "payee_id": "payee-amazon",
                            "category_id": "category-general",
                            "subcategory_id": "subcategory-delivery",
                            "amount": "-14.50",
                            "notes": "Order #1",
                            "spent_on": "2026-03-10",
                            "created_at": datetime(2026, 3, 10, 12, 0, 0).isoformat(),
                            "linked_transaction_id": None,
                            "transfer_group_id": None,
                        }
                    ],
                    "attachments": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        output = self.run_cli("seed-demo-data", "--seed-file", str(seed_file))

        self.assertIn("1 transactions added", output)
        self.assertIn("Amazon", self.run_cli("list"))

    def test_import_ledger_csv_replaces_existing_data(self) -> None:
        self.run_cli(
            "add",
            "--account",
            "Cash",
            "--payee",
            "Demo",
            "--category",
            "General",
            "--subcategory",
            "Test",
            "--amount",
            "-1.00",
            "--date",
            "2026-03-01",
        )
        csv_file = Path(self.temp_dir.name) / "legacy.csv"
        with csv_file.open("w", newline="", encoding="utf-8") as handle:
            writer = DictWriter(handle, fieldnames=["Account", "Date", "Payee", "Notes", "Category", "Amount", "Split_Amount", "Cleared"])
            writer.writeheader()
            writer.writerow(
                {
                    "Account": "HDFC",
                    "Date": "2026-03-02",
                    "Payee": "Amazon",
                    "Notes": "Ref 123",
                    "Category": "General:Delivery",
                    "Amount": "-250.00",
                    "Split_Amount": "",
                    "Cleared": "Y",
                }
            )

        output = self.run_cli("import-ledger-csv", "--csv-file", str(csv_file))

        self.assertIn("1 transactions", output)
        list_output = self.run_cli("list")
        self.assertIn("HDFC", list_output)
        self.assertIn("Amazon", list_output)
        self.assertNotIn("Demo", list_output)


if __name__ == "__main__":
    unittest.main()
