from __future__ import annotations

import json
import tempfile
import unittest
from csv import DictWriter
from datetime import datetime
from pathlib import Path

from expense_tracker.sms_history import SmsHistoryStore
from expense_tracker.sms_pipeline import SmsMessage, extract_sms_markers
from expense_tracker.sqlite_utils import sqlite_connection
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

    def test_filters_accept_dd_mm_yyyy_dates(self) -> None:
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-20.00",
            spent_on="2026-03-01",
        )

        filtered = self.tracker.list_transactions(start_date="01/03/2026", end_date="02/03/2026")

        self.assertEqual(len(filtered), 1)

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

    def test_search_matches_payee_and_notes(self) -> None:
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-99.00",
            spent_on="2026-03-11",
            notes="Ref 7788 pantry",
        )
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Pharmacy",
            category_name="Medical",
            subcategory_name="Meds",
            amount="-20.00",
            spent_on="2026-03-12",
            notes="Prescription",
        )

        by_payee = self.tracker.list_transactions(search="amaz")
        by_notes = self.tracker.list_transactions(search="7788")

        self.assertEqual(len(by_payee), 1)
        self.assertEqual(by_payee[0].payee_name, "Amazon")
        self.assertEqual(len(by_notes), 1)
        self.assertEqual(by_notes[0].notes, "Ref 7788 pantry")

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

    def test_delete_transaction_clears_linked_sms_history(self) -> None:
        created = self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-25.00",
            spent_on="2026-03-02",
        )[0]
        sms = SmsMessage(
            row_number=1,
            received_at=datetime(2026, 3, 2, 9, 0, 0),
            direction="Received",
            contact="JM-HDFCBK-S",
            phone="JM-HDFCBK-S",
            content="Sent Rs.25.00 From HDFC Bank A/C *2054 To Amazon On 02/03/26 Ref 12345",
            message_type="SMS",
        )
        SmsHistoryStore(self.data_file).record_approved_review(
            sender=sms.contact,
            phone=sms.phone,
            content=sms.content,
            received_at=sms.received_at,
            markers_payload=extract_sms_markers(sms).to_dict(),
            matched_transaction_id=created.id,
        )

        deleted = self.tracker.delete_transaction(created.id)

        self.assertTrue(deleted)
        with sqlite_connection(self.data_file) as connection:
            matched_transaction_id = connection.execute(
                "SELECT matched_transaction_id FROM sms_messages LIMIT 1"
            ).fetchone()[0]
        self.assertIsNone(matched_transaction_id)

    def test_delete_transaction_succeeds_when_other_sms_links_exist(self) -> None:
        first = self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-25.00",
            spent_on="2026-03-02",
        )[0]
        second = self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Pharmacy",
            category_name="Medical",
            subcategory_name="Meds",
            amount="-40.00",
            spent_on="2026-03-03",
        )[0]
        sms = SmsMessage(
            row_number=2,
            received_at=datetime(2026, 3, 3, 9, 0, 0),
            direction="Received",
            contact="JM-HDFCBK-S",
            phone="JM-HDFCBK-S",
            content="Sent Rs.40.00 From HDFC Bank A/C *2054 To Pharmacy On 03/03/26 Ref 55555",
            message_type="SMS",
        )
        SmsHistoryStore(self.data_file).record_approved_review(
            sender=sms.contact,
            phone=sms.phone,
            content=sms.content,
            received_at=sms.received_at,
            markers_payload=extract_sms_markers(sms).to_dict(),
            matched_transaction_id=second.id,
        )

        deleted = self.tracker.delete_transaction(first.id)

        self.assertTrue(deleted)
        remaining = self.tracker.list_transactions()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].id, second.id)

    def test_delete_in_use_payee_can_migrate_transactions(self) -> None:
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-10.00",
            spent_on="2026-03-01",
        )

        self.tracker.delete_payee("Amazon", replacement_payee_name="Blinkit")
        transactions = self.tracker.list_transactions()

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].payee_name, "Blinkit")

    def test_delete_in_use_category_can_delete_associated_transactions(self) -> None:
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-10.00",
            spent_on="2026-03-01",
        )

        self.tracker.delete_category("General", delete_transactions=True)

        self.assertEqual(self.tracker.list_transactions(), [])
        self.assertNotIn("General", self.tracker.metadata_snapshot().categories)

    def test_legacy_json_file_is_migrated_to_sqlite(self) -> None:
        self.data_file.write_text(
            json.dumps(
                {
                    "accounts": [{"id": "account-cash", "name": "Cash"}],
                    "payees": [{"id": "payee-amazon", "name": "Amazon", "linked_account_id": None}],
                    "categories": [{"id": "category-general", "name": "General"}],
                    "subcategories": [{"id": "subcategory-delivery", "name": "Delivery", "category_id": "category-general"}],
                    "transactions": [
                        {
                            "id": "txn-1",
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

        tracker = ExpenseTracker(self.data_file)
        transactions = tracker.list_transactions()

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].payee_name, "Amazon")
        self.assertTrue(self.data_file.read_bytes().startswith(b"SQLite format 3"))
        self.assertTrue(Path(f"{self.data_file}.bak").exists())

    def test_default_ledger_db_migrates_sibling_legacy_json(self) -> None:
        legacy_json = self.data_file.parent / "ledger.json"
        target_db = self.data_file.parent / "ledger.db"
        legacy_json.write_text(
            json.dumps(
                {
                    "accounts": [{"id": "account-credit", "name": "Credit"}],
                    "payees": [{"id": "payee-pharmacy", "name": "Pharmacy", "linked_account_id": None}],
                    "categories": [{"id": "category-medical", "name": "Medical"}],
                    "subcategories": [{"id": "subcategory-meds", "name": "Meds", "category_id": "category-medical"}],
                    "transactions": [
                        {
                            "id": "txn-2",
                            "entry_type": "expense",
                            "account_id": "account-credit",
                            "payee_id": "payee-pharmacy",
                            "category_id": "category-medical",
                            "subcategory_id": "subcategory-meds",
                            "amount": "-35.00",
                            "notes": "Prescription",
                            "spent_on": "2026-03-15",
                            "created_at": datetime(2026, 3, 15, 12, 0, 0).isoformat(),
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

        tracker = ExpenseTracker(target_db)
        transactions = tracker.list_transactions()

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].account_name, "Credit")
        self.assertTrue(target_db.read_bytes().startswith(b"SQLite format 3"))
        self.assertFalse(legacy_json.exists())
        self.assertTrue(Path(f"{legacy_json}.bak").exists())

    def test_import_legacy_csv_replaces_existing_ledger_and_scopes_subcategories(self) -> None:
        self.tracker.add_transaction(
            account_name="Cash",
            payee_name="Demo",
            category_name="General",
            subcategory_name="Old",
            amount="-5.00",
            spent_on="2026-03-01",
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
                    "Category": "General:Common",
                    "Amount": "-250.00",
                    "Split_Amount": "",
                    "Cleared": "Y",
                }
            )
            writer.writerow(
                {
                    "Account": "HDFC",
                    "Date": "2026-03-03",
                    "Payee": "Pharmacy",
                    "Notes": "Ref 456",
                    "Category": "Medical:Common",
                    "Amount": "-50.00",
                    "Split_Amount": "",
                    "Cleared": "Y",
                }
            )

        summary = self.tracker.import_legacy_csv(csv_file)
        transactions = self.tracker.list_transactions()
        metadata = self.tracker.metadata_snapshot()

        self.assertEqual(summary["transactions"], 2)
        self.assertEqual(len(transactions), 2)
        self.assertEqual({item.payee_name for item in transactions}, {"Amazon", "Pharmacy"})
        self.assertNotIn("Demo", {item.payee_name for item in transactions})
        self.assertEqual(metadata.subcategories_by_category["General"], ["Common"])
        self.assertEqual(metadata.subcategories_by_category["Medical"], ["Common"])


if __name__ == "__main__":
    unittest.main()
