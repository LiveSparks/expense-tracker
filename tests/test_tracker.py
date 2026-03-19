from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from expense_tracker.tracker import ExpenseTracker


class ExpenseTrackerDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.json"
        self.tracker = ExpenseTracker(self.data_file)

    def test_accounts_are_available_as_payees_for_transfers(self) -> None:
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Shop 1",
            category_name="General",
            amount="-10.00",
            spent_on="2026-03-01",
        )
        metadata = self.tracker.metadata_snapshot()

        self.assertIn("Cash", metadata.accounts)
        self.assertIn("Cash", metadata.payees)

    def test_transfer_creates_opposite_transaction_and_balances(self) -> None:
        self.tracker.add_transaction(
            account_name="HDFC",
            payee_name="Opening Balance",
            category_name="Income",
            amount="1.00",
            spent_on="2026-03-01",
        )
        opening_id = self.tracker.list_transactions(account="HDFC")[0].id
        self.tracker.delete_transaction(opening_id)

        created = self.tracker.add_transaction(
            account_name="Cash",
            payee_name="HDFC",
            category_name="General",
            amount="250.00",
            spent_on="2026-03-02",
            notes="Move to bank",
        )

        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].entry_type, "transfer_out")
        self.assertEqual(created[1].entry_type, "transfer_in")

        balances = self.tracker.account_balances()
        self.assertEqual(balances["Cash"], created[0].amount)
        self.assertEqual(balances["HDFC"], created[1].amount)

    def test_amount_sign_controls_income_vs_expense(self) -> None:
        income = self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Employer",
            category_name="Income",
            amount="100.00",
            spent_on="2026-03-01",
        )[0]
        expense = self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Cafe",
            category_name="General",
            amount="-8.50",
            spent_on="2026-03-02",
        )[0]

        self.assertEqual(income.entry_type, "income")
        self.assertEqual(expense.entry_type, "expense")

    def test_filters_by_account_payee_and_date_range(self) -> None:
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-20.00",
            spent_on="2026-03-01",
        )
        self.tracker.add_transaction(
            account_name="Credit",
            payee_name="Pharmacy",
            category_name="Medical",
            subcategory_name="Meds",
            amount="-35.00",
            spent_on="2026-03-15",
        )

        filtered = self.tracker.list_transactions(
            account="Credit",
            payee="Pharmacy",
            start_date="2026-03-10",
            end_date="2026-03-20",
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].account_name, "Credit")
        self.assertEqual(filtered[0].payee_name, "Pharmacy")

    def test_attachment_is_copied_locally(self) -> None:
        receipt_path = Path(self.temp_dir.name) / "receipt.txt"
        receipt_path.write_text("receipt data", encoding="utf-8")

        created = self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            amount="-9.99",
            spent_on="2026-03-11",
            attachment_paths=[str(receipt_path)],
        )

        self.assertEqual(len(created[0].attachments), 1)
        stored_path = Path(created[0].attachments[0].stored_path)
        self.assertTrue(stored_path.exists())
        self.assertEqual(stored_path.read_text(encoding="utf-8"), "receipt data")

    def test_deleting_one_transfer_side_removes_both_transactions(self) -> None:
        self.tracker.add_transaction(
            account_name="HDFC",
            payee_name="Opening Balance",
            category_name="Income",
            amount="1.00",
            spent_on="2026-03-01",
        )
        opening_id = self.tracker.list_transactions(account="HDFC")[0].id
        self.tracker.delete_transaction(opening_id)

        created = self.tracker.add_transaction(
            account_name="Cash",
            payee_name="HDFC",
            category_name="General",
            amount="25.00",
            spent_on="2026-03-02",
        )

        deleted = self.tracker.delete_transaction(created[0].id)

        self.assertTrue(deleted)
        self.assertEqual(self.tracker.list_transactions(), [])


if __name__ == "__main__":
    unittest.main()
