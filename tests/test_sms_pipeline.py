from __future__ import annotations

import csv
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from expense_tracker.cli import main
from expense_tracker.sms_pipeline import (
    analyze_legacy_sms_data,
    build_prompt_pack,
    extract_markers_regex,
    load_legacy_transactions,
    load_sms_messages,
    match_sms_to_transactions,
)


class SmsPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_path = Path(self.temp_dir.name)
        self.transactions_csv = self.temp_path / "transactions.csv"
        self.sms_csv = self.temp_path / "sms.csv"
        with self.transactions_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["Account", "Date", "Payee", "Notes", "Category", "Amount", "Split_Amount", "Cleared"])
            writer.writeheader()
            writer.writerow({"Account": "HDFC Savings 2054", "Date": "2026-03-20", "Payee": "Amazon", "Notes": "Household order", "Category": "Usual Expenses:General", "Amount": "-499", "Split_Amount": "0", "Cleared": "Not cleared"})
            writer.writerow({"Account": "HDFC Savings 2054", "Date": "2026-03-21", "Payee": "Employer", "Notes": "March salary", "Category": "Income:Income", "Amount": "50000", "Split_Amount": "0", "Cleared": "Cleared"})
        with self.sms_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["Date", "Time", "Direction", "Contact", "Phone", "Content", "Type"])
            writer.writeheader()
            writer.writerow({
                "Date": "19/03/2026",
                "Time": "11:49:16 am",
                "Direction": "Received",
                "Contact": "JM-HDFCBK-S",
                "Phone": "JM-HDFCBK-S",
                "Content": "Alert! Paid Rs.65 toll for HR10AK6691 at Skyway Toll on 2026-03-19 11:47:14. HDFC Bank A/C *2054 balance: Rs.1103.",
                "Type": "SMS",
            })
            writer.writerow({
                "Date": "20/03/2026",
                "Time": "09:01:10 am",
                "Direction": "Received",
                "Contact": "JM-HDFCBK-S",
                "Phone": "JM-HDFCBK-S",
                "Content": "Sent Rs.499.00 From HDFC Bank A/C *2054 To Amazon Seller Services On 20/03/26 Ref 552880661565",
                "Type": "SMS",
            })
            writer.writerow({
                "Date": "21/03/2026",
                "Time": "08:15:00 am",
                "Direction": "Received",
                "Contact": "VM-HDFCBK-S",
                "Phone": "VM-HDFCBK-S",
                "Content": "Salary credited with INR 50000 to A/C *2054 on 2026-03-21 08:10:00. Avl balance INR 80000.",
                "Type": "SMS",
            })
            writer.writerow({
                "Date": "21/03/2026",
                "Time": "10:00:00 am",
                "Direction": "Received",
                "Contact": "AD-PROMO",
                "Phone": "AD-PROMO",
                "Content": "Flash sale this weekend only. Shop now.",
                "Type": "SMS",
            })

    def test_extract_markers_regex_finds_amount_and_hints(self) -> None:
        message = load_sms_messages(self.sms_csv)[0]
        markers = extract_markers_regex(message)
        self.assertTrue(markers.useful)
        self.assertEqual(f"{markers.amount:.2f}", "65.00")
        self.assertEqual(markers.event_kind, "toll")
        self.assertEqual(markers.account_hint, "2054")
        self.assertEqual(markers.merchant_hint, "Skyway Toll")

    def test_matching_uses_amount_and_date_proximity(self) -> None:
        transactions = load_legacy_transactions(self.transactions_csv)
        messages = load_sms_messages(self.sms_csv)
        matches = match_sms_to_transactions(messages, transactions)
        matched = [match for match in matches if match.transaction is not None]
        toll_match = next(match for match in matches if match.markers.event_kind == "toll")
        self.assertEqual(len(matched), 2)
        self.assertIsNone(toll_match.transaction)
        self.assertIn("excluded from historical matching", toll_match.reasons[0])
        self.assertIn("Exact amount match", matched[0].reasons[0])
        self.assertIn("payee", matched[0].field_associations)

    def test_analysis_writes_prompt_and_mock_artifacts(self) -> None:
        output_dir = self.temp_path / "output"
        artifacts = analyze_legacy_sms_data(self.transactions_csv, self.sms_csv, output_dir)
        self.assertEqual(len(artifacts.useful_sms), 3)
        self.assertTrue((output_dir / "analysis_summary.json").exists())
        self.assertTrue((output_dir / "matched_examples.json").exists())
        self.assertTrue((output_dir / "prompt_pack.json").exists())
        self.assertTrue((output_dir / "prompt_pack.txt").exists())
        self.assertTrue((output_dir / "mock_ledger_seed.json").exists())
        self.assertTrue((output_dir / "mock_sms_samples.json").exists())
        self.assertIn("gpt-5-mini", (output_dir / "prompt_pack.txt").read_text(encoding="utf-8"))

    def test_prompt_pack_contains_examples_and_metadata_lists(self) -> None:
        transactions = load_legacy_transactions(self.transactions_csv)
        matches = [match for match in match_sms_to_transactions(load_sms_messages(self.sms_csv), transactions) if match.transaction is not None]
        prompt_pack = build_prompt_pack(matches, transactions)
        self.assertTrue(prompt_pack["examples"])
        self.assertIn("HDFC Savings 2054", prompt_pack["template_input"]["available_accounts"])
        self.assertIn("Usual Expenses / General", prompt_pack["template_input"]["available_categories"])
        self.assertEqual(prompt_pack["response_format"]["json_schema"]["name"], "expense_tracker_review_draft")
        self.assertTrue(any("- Ref:" in example["expected_output"]["notes"] for example in prompt_pack["examples"]))

    def test_cli_analyze_sms_command_runs(self) -> None:
        output_dir = self.temp_path / "cli-output"
        buffer = StringIO()
        with redirect_stdout(buffer):
            exit_code = main([
                "--data-file",
                str(self.temp_path / "ledger.json"),
                "analyze-sms",
                "--transactions-csv",
                str(self.transactions_csv),
                "--sms-csv",
                str(self.sms_csv),
                "--output-dir",
                str(output_dir),
            ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Artifacts written", buffer.getvalue())
        self.assertTrue((output_dir / "prompt_pack.json").exists())


if __name__ == "__main__":
    unittest.main()
