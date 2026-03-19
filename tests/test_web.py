from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from expense_tracker.web import create_app


class ExpenseTrackerWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "ledger.json"
        self.client = TestClient(create_app(self.data_file))

    def test_dashboard_shows_accounts_and_balances(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Employer", "category_value": "Income / Salary", "amount": "1000.00", "spent_on": "2026-03-01"})
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cash", response.text)
        self.assertIn("$1000.00", response.text)

    def test_transactions_page_can_be_filtered_by_account(self) -> None:
        self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Shop 1", "category_value": "General / Grocery", "amount": "-20.00", "spent_on": "2026-03-01"})
        self.client.post("/api/transactions", data={"account_name": "Credit", "payee_name": "Pharmacy", "category_value": "Medical / Meds", "amount": "-12.00", "spent_on": "2026-03-02"})
        response = self.client.get("/transactions", params={"account": "Credit"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Pharmacy", response.text)
        self.assertNotIn("Shop 1", response.text)

    def test_transaction_form_supports_file_upload(self) -> None:
        receipt = Path(self.temp_dir.name) / "receipt.txt"
        receipt.write_text("receipt", encoding="utf-8")
        with receipt.open("rb") as handle:
            response = self.client.post(
                "/transactions",
                data={"account_name": "Cash", "payee_name": "Amazon", "category_value": "General / Delivery", "amount": "-8.99", "spent_on": "2026-03-10", "notes": "Order"},
                files={"files": ("receipt.txt", handle, "text/plain")},
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Amazon", response.text)
        self.assertTrue((self.data_file.parent / "attachments").exists())

    def test_management_pages_can_create_metadata(self) -> None:
        self.client.post("/manage/accounts", data={"action": "create", "name": "HDFC"})
        self.client.post("/manage/payees", data={"action": "create", "name": "Amazon"})
        self.client.post("/manage/categories", data={"action": "create-category", "name": "Travel"})
        self.client.post("/manage/categories", data={"action": "create-subcategory", "category_name": "Travel", "subcategory_name": "Flights"})
        response = self.client.get("/manage/categories")
        self.assertIn("Travel", response.text)
        self.assertIn("Flights", response.text)
        self.assertIn("HDFC", self.client.get("/manage/accounts").text)
        self.assertIn("Amazon", self.client.get("/manage/payees").text)

    def test_api_returns_transaction_payloads(self) -> None:
        create_response = self.client.post("/api/transactions", data={"account_name": "Cash", "payee_name": "Employer", "category_value": "Income / Salary", "amount": "100.00", "spent_on": "2026-03-01"})
        transaction_id = create_response.json()["transactions"][0]["id"]
        list_response = self.client.get("/api/transactions")
        delete_response = self.client.delete(f"/api/transactions/{transaction_id}")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(list_response.json()["transactions"][0]["account_name"], "Cash")
        self.assertTrue(delete_response.json()["deleted"])


if __name__ == "__main__":
    unittest.main()
