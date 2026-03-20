from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from expense_tracker.sms_history import SmsHistoryStore
from expense_tracker.sms_pipeline import SmsMessage, extract_sms_markers
from expense_tracker.tracker import ExpenseTracker


class SmsHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.db"
        self.tracker = ExpenseTracker(self.data_file)
        self.sms_csv = Path(self.temp_dir.name) / "conversations.csv"

    def test_import_csv_persists_messages_and_matches_transactions(self) -> None:
        self.tracker.add_transaction(
            account_name="HDFC Savings 2054",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-499.00",
            spent_on="2026-03-20",
            notes="- Ref: 552880661565",
        )
        self.sms_csv.write_text(
            "\n".join(
                [
                    "Exported on 2026-03-20 10:33 with SMS Exporter android app https://smartpositive.com/sms-exporter",
                    "",
                    "Date,Time,Direction,Contact,Phone,Content,Type",
                    "2026-03-20,09:01:10,Received,JM-HDFCBK-S,JM-HDFCBK-S,Sent Rs.499.00 From HDFC Bank A/C *2054 To Amazon Seller Services On 20/03/26 Ref 552880661565,SMS",
                    "2026-03-20,11:00:00,Received,AD-PROMO,AD-PROMO,Flash sale this weekend only. Shop now.,SMS",
                ]
            ),
            encoding="utf-8",
        )

        summary = SmsHistoryStore(self.data_file).import_csv(self.sms_csv, self.tracker.list_transactions())

        self.assertEqual(summary.total_messages, 2)
        self.assertEqual(summary.useful_messages, 1)
        self.assertEqual(summary.matched_messages, 1)
        self.assertEqual(SmsHistoryStore(self.data_file).total_count(), 2)
        self.assertEqual(SmsHistoryStore(self.data_file).matched_count(), 1)

    def test_similar_examples_uses_stored_sms_history(self) -> None:
        self.tracker.add_transaction(
            account_name="HDFC Savings 2054",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-499.00",
            spent_on="2026-03-20",
            notes="- Ref: 552880661565",
        )
        self.sms_csv.write_text(
            "\n".join(
                [
                    "Exported on 2026-03-20 10:33 with SMS Exporter android app https://smartpositive.com/sms-exporter",
                    "",
                    "Date,Time,Direction,Contact,Phone,Content,Type",
                    "2026-03-20,09:01:10,Received,JM-HDFCBK-S,JM-HDFCBK-S,Sent Rs.499.00 From HDFC Bank A/C *2054 To Amazon Seller Services On 20/03/26 Ref 552880661565,SMS",
                ]
            ),
            encoding="utf-8",
        )
        store = SmsHistoryStore(self.data_file)
        store.import_csv(self.sms_csv, self.tracker.list_transactions())
        target = SmsMessage(
            row_number=99,
            received_at=datetime(2026, 3, 21, 10, 0, 0),
            direction="Received",
            contact="JM-HDFCBK-S",
            phone="JM-HDFCBK-S",
            content="Sent Rs.499.00 From HDFC Bank A/C *2054 To Amazon Seller Services On 21/03/26 Ref 99887766",
            message_type="SMS",
        )
        markers = extract_sms_markers(target).to_dict()

        examples = store.similar_examples(message=target, markers_payload=markers, transactions=self.tracker.list_transactions())

        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0]["payee_name"], "Amazon")
        self.assertEqual(examples[0]["historical_sender"], "JM-HDFCBK-S")

    def test_import_csv_requires_close_amount_match(self) -> None:
        self.tracker.add_transaction(
            account_name="HDFC Savings 2054",
            payee_name="Amazon",
            category_name="General",
            subcategory_name="Delivery",
            amount="-499.00",
            spent_on="2026-03-20",
            notes="- Ref: 552880661565",
        )
        self.sms_csv.write_text(
            "\n".join(
                [
                    "Exported on 2026-03-20 10:33 with SMS Exporter android app https://smartpositive.com/sms-exporter",
                    "",
                    "Date,Time,Direction,Contact,Phone,Content,Type",
                    "2026-03-20,09:01:10,Received,JM-HDFCBK-S,JM-HDFCBK-S,Sent Rs.650.00 From HDFC Bank A/C *2054 To Someone On 20/03/26 Ref 552880661565,SMS",
                ]
            ),
            encoding="utf-8",
        )

        summary = SmsHistoryStore(self.data_file).import_csv(self.sms_csv, self.tracker.list_transactions())

        self.assertEqual(summary.total_messages, 1)
        self.assertEqual(summary.useful_messages, 1)
        self.assertEqual(summary.matched_messages, 0)
        self.assertEqual(SmsHistoryStore(self.data_file).matched_count(), 0)

    def test_import_csv_skips_upcoming_auto_debit_reminders(self) -> None:
        self.tracker.add_transaction(
            account_name="HDFC Savings 2054",
            payee_name="Insurance",
            category_name="General",
            subcategory_name="Bills",
            amount="-999.00",
            spent_on="2026-03-20",
        )
        self.sms_csv.write_text(
            "\n".join(
                [
                    "Exported on 2026-03-20 10:33 with SMS Exporter android app https://smartpositive.com/sms-exporter",
                    "",
                    "Date,Time,Direction,Contact,Phone,Content,Type",
                    "2026-03-19,09:01:10,Received,JM-HDFCBK-S,JM-HDFCBK-S,Reminder: Rs.999.00 will be debited from HDFC Bank A/C *2054 tomorrow towards insurance premium auto-debit,SMS",
                ]
            ),
            encoding="utf-8",
        )

        summary = SmsHistoryStore(self.data_file).import_csv(self.sms_csv, self.tracker.list_transactions())

        self.assertEqual(summary.total_messages, 1)
        self.assertEqual(summary.useful_messages, 1)
        self.assertEqual(summary.matched_messages, 0)


if __name__ == "__main__":
    unittest.main()
