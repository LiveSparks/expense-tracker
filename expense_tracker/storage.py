from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Attachment, Category, LedgerData, Payee, Subcategory, Transaction
from .sqlite_utils import sqlite_connection


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
        self._ensure_database()
        with sqlite_connection(self.data_file) as connection:
            ledger = LedgerData(
                accounts=self._load_rows(connection, "SELECT id, name FROM accounts ORDER BY rowid", self._load_account),
                payees=self._load_rows(
                    connection,
                    "SELECT id, name, linked_account_id FROM payees ORDER BY rowid",
                    self._load_payee,
                ),
                categories=self._load_rows(connection, "SELECT id, name FROM categories ORDER BY rowid", self._load_category),
                subcategories=self._load_rows(
                    connection,
                    "SELECT id, name, category_id FROM subcategories ORDER BY rowid",
                    self._load_subcategory,
                ),
                transactions=self._load_rows(
                    connection,
                    """
                    SELECT id, entry_type, account_id, payee_id, category_id, subcategory_id,
                           amount, notes, spent_on, created_at, linked_transaction_id, transfer_group_id
                    FROM transactions
                    ORDER BY rowid
                    """,
                    self._load_transaction,
                ),
                attachments=self._load_rows(
                    connection,
                    "SELECT id, transaction_id, original_name, stored_path, uploaded_at FROM attachments ORDER BY rowid",
                    self._load_attachment,
                ),
            )
        if not ledger.categories and not ledger.subcategories and not ledger.accounts and not ledger.payees and not ledger.transactions:
            ledger = self.default_ledger()
            self.save(ledger)
        return ledger

    def save(self, ledger: LedgerData) -> None:
        self._ensure_database()
        with sqlite_connection(self.data_file) as connection:
            connection.execute("DELETE FROM attachments")
            connection.execute("DELETE FROM transactions")
            connection.execute("DELETE FROM payees")
            connection.execute("DELETE FROM accounts")
            connection.execute("DELETE FROM subcategories")
            connection.execute("DELETE FROM categories")

            connection.executemany(
                "INSERT INTO accounts (id, name) VALUES (?, ?)",
                [(account.id, account.name) for account in ledger.accounts],
            )
            connection.executemany(
                "INSERT INTO categories (id, name) VALUES (?, ?)",
                [(category.id, category.name) for category in ledger.categories],
            )
            connection.executemany(
                "INSERT INTO subcategories (id, name, category_id) VALUES (?, ?, ?)",
                [(subcategory.id, subcategory.name, subcategory.category_id) for subcategory in ledger.subcategories],
            )
            connection.executemany(
                "INSERT INTO payees (id, name, linked_account_id) VALUES (?, ?, ?)",
                [(payee.id, payee.name, payee.linked_account_id) for payee in ledger.payees],
            )
            connection.executemany(
                """
                INSERT INTO transactions (
                    id, entry_type, account_id, payee_id, category_id, subcategory_id,
                    amount, notes, spent_on, created_at, linked_transaction_id, transfer_group_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        transaction.id,
                        transaction.entry_type,
                        transaction.account_id,
                        transaction.payee_id,
                        transaction.category_id,
                        transaction.subcategory_id,
                        f"{transaction.amount:.2f}",
                        transaction.notes,
                        transaction.spent_on.isoformat(),
                        transaction.created_at.isoformat(),
                        transaction.linked_transaction_id,
                        transaction.transfer_group_id,
                    )
                    for transaction in ledger.transactions
                ],
            )
            connection.executemany(
                "INSERT INTO attachments (id, transaction_id, original_name, stored_path, uploaded_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        attachment.id,
                        attachment.transaction_id,
                        attachment.original_name,
                        attachment.stored_path,
                        attachment.uploaded_at.isoformat(),
                    )
                    for attachment in ledger.attachments
                ],
            )
    def _ensure_database(self) -> None:
        legacy_ledger = self._load_legacy_json_if_needed()
        with sqlite_connection(self.data_file) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payees (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    linked_account_id TEXT REFERENCES accounts(id)
                );
                CREATE TABLE IF NOT EXISTS categories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subcategories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category_id TEXT NOT NULL REFERENCES categories(id)
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    entry_type TEXT NOT NULL,
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    payee_id TEXT NOT NULL REFERENCES payees(id),
                    category_id TEXT NOT NULL REFERENCES categories(id),
                    subcategory_id TEXT REFERENCES subcategories(id),
                    amount TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    spent_on TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    linked_transaction_id TEXT,
                    transfer_group_id TEXT
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    transaction_id TEXT NOT NULL REFERENCES transactions(id),
                    original_name TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL
                );
                """
            )
        if legacy_ledger is not None:
            self.save(legacy_ledger)

    def _load_legacy_json_if_needed(self) -> LedgerData | None:
        legacy_path = self.data_file if self.data_file.exists() else self._legacy_default_json_path()
        if legacy_path is None or not legacy_path.exists():
            return None
        if legacy_path == self.data_file:
            header = self.data_file.read_bytes()[:16]
            if header.startswith(b"SQLite format 3\x00"):
                return None
        payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            ledger = self.default_ledger()
        else:
            merged = self.default_ledger().to_dict()
            merged.update(payload)
            ledger = LedgerData.from_dict(merged)
        backup_path = Path(f"{legacy_path}.bak")
        if not backup_path.exists():
            backup_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        legacy_path.unlink()
        return ledger

    def _legacy_default_json_path(self) -> Path | None:
        if self.data_file.name != "ledger.db":
            return None
        candidate = self.data_file.with_name("ledger.json")
        return candidate if candidate.exists() else None

    @staticmethod
    def _load_rows(connection: sqlite3.Connection, query: str, loader) -> list:
        return [loader(row) for row in connection.execute(query).fetchall()]

    @staticmethod
    def _load_account(row: sqlite3.Row):
        from .models import Account

        return Account(id=row["id"], name=row["name"])

    @staticmethod
    def _load_payee(row: sqlite3.Row):
        return Payee(id=row["id"], name=row["name"], linked_account_id=row["linked_account_id"])

    @staticmethod
    def _load_category(row: sqlite3.Row):
        return Category(id=row["id"], name=row["name"])

    @staticmethod
    def _load_subcategory(row: sqlite3.Row):
        return Subcategory(id=row["id"], name=row["name"], category_id=row["category_id"])

    @staticmethod
    def _load_transaction(row: sqlite3.Row):
        return Transaction.from_dict(
            {
                "id": row["id"],
                "entry_type": row["entry_type"],
                "account_id": row["account_id"],
                "payee_id": row["payee_id"],
                "category_id": row["category_id"],
                "subcategory_id": row["subcategory_id"],
                "amount": row["amount"],
                "notes": row["notes"],
                "spent_on": row["spent_on"],
                "created_at": row["created_at"],
                "linked_transaction_id": row["linked_transaction_id"],
                "transfer_group_id": row["transfer_group_id"],
            }
        )

    @staticmethod
    def _load_attachment(row: sqlite3.Row):
        return Attachment.from_dict(
            {
                "id": row["id"],
                "transaction_id": row["transaction_id"],
                "original_name": row["original_name"],
                "stored_path": row["stored_path"],
                "uploaded_at": row["uploaded_at"],
            }
        )
