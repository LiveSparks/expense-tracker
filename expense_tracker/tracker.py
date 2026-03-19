from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from shutil import copy2
from shutil import rmtree
from uuid import uuid4

from .models import (
    Account,
    Attachment,
    Category,
    LedgerData,
    MetadataSnapshot,
    Payee,
    Subcategory,
    Transaction,
    TransactionRecord,
)
from .storage import LedgerStorage


class ExpenseTracker:
    def __init__(self, data_file: Path) -> None:
        self.storage = LedgerStorage(data_file)

    def metadata_snapshot(self) -> MetadataSnapshot:
        ledger = self.storage.load()
        changed = self._ensure_account_payees(ledger)
        if changed:
            self.storage.save(ledger)
        return self._metadata_snapshot_from_ledger(ledger)

    def account_summaries(self) -> list[dict[str, str | bool]]:
        ledger = self.storage.load()
        balances = self.account_balances()
        transaction_counts: dict[str, int] = {}
        for transaction in ledger.transactions:
            transaction_counts[transaction.account_id] = transaction_counts.get(transaction.account_id, 0) + 1
        account_summaries: list[dict[str, str | bool]] = []
        for account in sorted(ledger.accounts, key=lambda item: item.name.casefold()):
            balance = balances.get(account.name, Decimal("0.00"))
            account_summaries.append(
                {
                    "name": account.name,
                    "balance": f"{balance:.2f}",
                    "can_delete": True,
                    "transaction_count": transaction_counts.get(account.id, 0),
                }
            )
        return account_summaries

    def payee_summaries(self) -> list[dict[str, str | None]]:
        ledger = self.storage.load()
        account_map = {account.id: account.name for account in ledger.accounts}
        transaction_counts: dict[str, int] = {}
        for transaction in ledger.transactions:
            transaction_counts[transaction.payee_id] = transaction_counts.get(transaction.payee_id, 0) + 1
        return [
            {
                "name": payee.name,
                "linked_account_name": account_map.get(payee.linked_account_id)
                if payee.linked_account_id is not None
                else None,
                "can_delete": payee.linked_account_id is None,
                "transaction_count": transaction_counts.get(payee.id, 0),
            }
            for payee in sorted(ledger.payees, key=lambda item: item.name.casefold())
        ]

    def category_summaries(self) -> list[dict[str, object]]:
        ledger = self.storage.load()
        category_transaction_counts: dict[str, int] = {}
        subcategory_transaction_counts: dict[str, int] = {}
        for transaction in ledger.transactions:
            category_transaction_counts[transaction.category_id] = category_transaction_counts.get(transaction.category_id, 0) + 1
            if transaction.subcategory_id is not None:
                subcategory_transaction_counts[transaction.subcategory_id] = subcategory_transaction_counts.get(transaction.subcategory_id, 0) + 1
        summaries: list[dict[str, object]] = []
        for category in sorted(ledger.categories, key=lambda item: item.name.casefold()):
            subcategories = [
                {
                    "name": subcategory.name,
                    "can_delete": True,
                    "transaction_count": subcategory_transaction_counts.get(subcategory.id, 0),
                }
                for subcategory in sorted(
                    (item for item in ledger.subcategories if item.category_id == category.id),
                    key=lambda item: item.name.casefold(),
                )
            ]
            summaries.append(
                {
                    "name": category.name,
                    "subcategories": subcategories,
                    "can_delete": True,
                    "transaction_count": category_transaction_counts.get(category.id, 0),
                }
            )
        return summaries

    def get_transaction(self, transaction_id: str) -> TransactionRecord | None:
        ledger = self.storage.load()
        target = next((item for item in ledger.transactions if item.id == transaction_id), None)
        if target is None:
            return None
        return self._build_record(ledger, target)

    def add_transaction(
        self,
        *,
        account_name: str,
        payee_name: str,
        category_name: str,
        subcategory_name: str | None = None,
        amount: str,
        spent_on: str,
        notes: str = "",
        attachment_paths: Sequence[str] | None = None,
    ) -> list[TransactionRecord]:
        ledger = self.storage.load()
        created = self._apply_transaction_change(
            ledger,
            original_transaction_id=None,
            account_name=account_name,
            payee_name=payee_name,
            category_name=category_name,
            subcategory_name=subcategory_name,
            amount=amount,
            spent_on=spent_on,
            notes=notes,
            attachment_paths=attachment_paths,
        )
        self.storage.save(ledger)
        return [self._build_record(ledger, item) for item in created]

    def update_transaction(
        self,
        transaction_id: str,
        *,
        account_name: str,
        payee_name: str,
        category_name: str,
        subcategory_name: str | None = None,
        amount: str,
        spent_on: str,
        notes: str = "",
        attachment_paths: Sequence[str] | None = None,
    ) -> list[TransactionRecord]:
        ledger = self.storage.load()
        created = self._apply_transaction_change(
            ledger,
            original_transaction_id=transaction_id,
            account_name=account_name,
            payee_name=payee_name,
            category_name=category_name,
            subcategory_name=subcategory_name,
            amount=amount,
            spent_on=spent_on,
            notes=notes,
            attachment_paths=attachment_paths,
        )
        self.storage.save(ledger)
        return [self._build_record(ledger, item) for item in created]

    def delete_transaction(self, transaction_id: str) -> bool:
        ledger = self.storage.load()
        transaction_ids = self._transaction_group_ids(ledger, transaction_id)
        if not transaction_ids:
            return False
        self._delete_transaction_ids(ledger, transaction_ids)
        self.storage.save(ledger)
        return True

    def list_transactions(
        self,
        *,
        account: str | None = None,
        category: str | None = None,
        payee: str | None = None,
        search: str | None = None,
        exact_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[TransactionRecord]:
        ledger = self.storage.load()
        records = [self._build_record(ledger, transaction) for transaction in ledger.transactions]
        normalized_account = self._normalize(account)
        normalized_category = self._normalize(category)
        normalized_payee = self._normalize(payee)
        normalized_search = self._normalize(search)
        parsed_exact_date = self._parse_date(exact_date) if exact_date else None
        parsed_start_date = self._parse_date(start_date) if start_date else None
        parsed_end_date = self._parse_date(end_date) if end_date else None
        filtered: list[TransactionRecord] = []
        for record in records:
            if normalized_account and self._normalize(record.account_name) != normalized_account:
                continue
            if normalized_category and self._normalize(record.category_name) != normalized_category:
                continue
            if normalized_payee and self._normalize(record.payee_name) != normalized_payee:
                continue
            if normalized_search and normalized_search not in self._search_text(record):
                continue
            if parsed_exact_date and record.spent_on != parsed_exact_date:
                continue
            if parsed_start_date and record.spent_on < parsed_start_date:
                continue
            if parsed_end_date and record.spent_on > parsed_end_date:
                continue
            filtered.append(record)
        return sorted(filtered, key=lambda item: (item.spent_on, item.id), reverse=True)

    def account_balances(self) -> dict[str, Decimal]:
        ledger = self.storage.load()
        balances = {account.name: Decimal("0.00") for account in ledger.accounts}
        account_map = {account.id: account.name for account in ledger.accounts}
        for transaction in ledger.transactions:
            account_name = account_map.get(transaction.account_id)
            if account_name is None:
                continue
            balances.setdefault(account_name, Decimal("0.00"))
            balances[account_name] += transaction.amount
        return dict(sorted(balances.items()))

    def category_totals(
        self,
        *,
        account: str | None = None,
        category: str | None = None,
        payee: str | None = None,
        search: str | None = None,
        exact_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for transaction in self.list_transactions(
            account=account,
            category=category,
            payee=payee,
            search=search,
            exact_date=exact_date,
            start_date=start_date,
            end_date=end_date,
        ):
            totals.setdefault(transaction.category_name, Decimal("0.00"))
            totals[transaction.category_name] += transaction.amount
        return dict(sorted(totals.items()))

    def total_amount(self, transactions: Iterable[TransactionRecord]) -> Decimal:
        total = Decimal("0.00")
        for transaction in transactions:
            total += transaction.amount
        return total

    def _delete_transaction_ids(self, ledger: LedgerData, transaction_ids: set[str]) -> None:
        removed_attachments = [attachment for attachment in ledger.attachments if attachment.transaction_id in transaction_ids]
        ledger.transactions = [transaction for transaction in ledger.transactions if transaction.id not in transaction_ids]
        ledger.attachments = [attachment for attachment in ledger.attachments if attachment.transaction_id not in transaction_ids]
        for attachment in removed_attachments:
            attachment_file = Path(attachment.stored_path)
            if attachment_file.exists():
                attachment_file.unlink()

    def bulk_delete_transactions(self, transaction_ids: Sequence[str]) -> int:
        deleted = 0
        processed: set[str] = set()
        for transaction_id in transaction_ids:
            if transaction_id in processed:
                continue
            ledger = self.storage.load()
            group_ids = self._transaction_group_ids(ledger, transaction_id)
            if not group_ids:
                continue
            processed.update(group_ids)
            if self.delete_transaction(transaction_id):
                deleted += 1
        return deleted

    def bulk_duplicate_transactions(self, transaction_ids: Sequence[str]) -> int:
        created = 0
        processed: set[str] = set()
        for transaction_id in transaction_ids:
            source = self.get_transaction(transaction_id)
            if source is None:
                continue
            if source.transfer_group_id:
                if source.transfer_group_id in processed:
                    continue
                processed.add(source.transfer_group_id)
                if source.entry_type == "transfer_in" and source.linked_transaction_id:
                    source = self.get_transaction(source.linked_transaction_id) or source
            else:
                if source.id in processed:
                    continue
                processed.add(source.id)
            self.add_transaction(
                account_name=source.account_name,
                payee_name=source.payee_name,
                category_name=source.category_name,
                subcategory_name=source.subcategory_name,
                amount=str(source.amount),
                spent_on=source.spent_on.isoformat(),
                notes=source.notes,
            )
            created += 1
        return created

    def bulk_move_transactions(self, transaction_ids: Sequence[str], target_account_name: str) -> int:
        ledger = self.storage.load()
        target_account = self._get_or_create_account(ledger, target_account_name)
        moved = 0
        for transaction_id in transaction_ids:
            transaction = next((item for item in ledger.transactions if item.id == transaction_id), None)
            if transaction is None:
                continue
            if transaction.entry_type.startswith("transfer_"):
                if not transaction.linked_transaction_id:
                    raise ValueError("Linked transfer transaction missing.")
                partner = next((item for item in ledger.transactions if item.id == transaction.linked_transaction_id), None)
                if partner is None:
                    raise ValueError("Linked transfer transaction missing.")
                if partner.account_id == target_account.id:
                    raise ValueError("Transfers cannot use the same account on both sides.")
                transaction.account_id = target_account.id
                partner.payee_id = self._ensure_linked_payee(ledger, target_account).id
            else:
                transaction.account_id = target_account.id
            moved += 1
        self.storage.save(ledger)
        return moved

    def bulk_change_transaction_date(self, transaction_ids: Sequence[str], spent_on: str) -> int:
        ledger = self.storage.load()
        parsed_date = self._parse_date(spent_on)
        changed = 0
        processed_transfer_groups: set[str] = set()
        for transaction_id in transaction_ids:
            transaction = next((item for item in ledger.transactions if item.id == transaction_id), None)
            if transaction is None:
                continue
            if transaction.transfer_group_id:
                if transaction.transfer_group_id in processed_transfer_groups:
                    continue
                processed_transfer_groups.add(transaction.transfer_group_id)
                transaction.spent_on = parsed_date
                if transaction.linked_transaction_id:
                    partner = next((item for item in ledger.transactions if item.id == transaction.linked_transaction_id), None)
                    if partner is None:
                        raise ValueError("Linked transfer transaction missing.")
                    partner.spent_on = parsed_date
            else:
                transaction.spent_on = parsed_date
            changed += 1
        self.storage.save(ledger)
        return changed

    def bulk_change_payee(self, transaction_ids: Sequence[str], target_payee_name: str) -> int:
        ledger = self.storage.load()
        target_payee = self._get_or_create_payee(ledger, target_payee_name)
        changed = 0
        processed_transfer_groups: set[str] = set()
        for transaction_id in transaction_ids:
            transaction = next((item for item in ledger.transactions if item.id == transaction_id), None)
            if transaction is None:
                continue
            if transaction.transfer_group_id:
                if transaction.transfer_group_id in processed_transfer_groups:
                    continue
                processed_transfer_groups.add(transaction.transfer_group_id)
                if target_payee.linked_account_id is not None:
                    raise ValueError("Transfers cannot be changed to an account payee.")
                transaction.payee_id = target_payee.id
                if transaction.linked_transaction_id:
                    partner = next((item for item in ledger.transactions if item.id == transaction.linked_transaction_id), None)
                    if partner is None:
                        raise ValueError("Linked transfer transaction missing.")
                    partner.payee_id = target_payee.id
            else:
                if target_payee.linked_account_id == transaction.account_id:
                    raise ValueError("Source and destination accounts must be different.")
                transaction.payee_id = target_payee.id
            changed += 1
        self.storage.save(ledger)
        return changed

    def create_account(self, name: str) -> None:
        ledger = self.storage.load()
        self._get_or_create_account(ledger, name)
        self.storage.save(ledger)

    def import_legacy_csv(self, csv_path: Path) -> dict[str, int]:
        if not csv_path.exists():
            raise ValueError(f"CSV file {csv_path} was not found.")
        ledger = self.storage.default_ledger()
        ledger.accounts = []
        ledger.payees = []
        ledger.categories = []
        ledger.subcategories = []
        ledger.transactions = []
        ledger.attachments = []
        skipped_rows = 0
        if self.storage.attachments_dir.exists():
            rmtree(self.storage.attachments_dir)
        with csv_path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        account_names = {self._normalize(row.get("Account", "")) for row in rows if (row.get("Account") or "").strip()}
        for row in rows:
            account_name = self._require_name(row.get("Account", ""), "Account")
            payee_name = (row.get("Payee", "") or "").strip() or "Opening Balance"
            category_name, subcategory_name = self._split_legacy_category(
                self._default_legacy_category(row.get("Category", ""), payee_name, account_names)
            )
            notes = (row.get("Notes", "") or "").strip()
            raw_amount = str(row.get("Amount", "0")).strip()
            if Decimal(raw_amount or "0").quantize(Decimal("0.01")) == Decimal("0.00"):
                skipped_rows += 1
                continue
            amount = self._parse_signed_amount(raw_amount)
            spent_on = self._parse_date(str(row.get("Date", "")).strip())
            account = self._get_or_create_account(ledger, account_name)
            payee = self._get_or_create_payee(ledger, payee_name)
            category = self._get_or_create_category(ledger, category_name)
            subcategory = self._get_or_create_subcategory(ledger, subcategory_name, category.id)
            ledger.transactions.append(
                Transaction(
                    id=str(uuid4()),
                    entry_type="income" if amount > Decimal("0.00") else "expense",
                    account_id=account.id,
                    payee_id=payee.id,
                    category_id=category.id,
                    subcategory_id=subcategory.id if subcategory is not None else None,
                    amount=amount,
                    notes=notes,
                    spent_on=spent_on,
                    created_at=datetime.combine(spent_on, datetime.min.time(), tzinfo=UTC),
                )
            )
        self.storage.save(ledger)
        return {
            "accounts": len(ledger.accounts),
            "payees": len([payee for payee in ledger.payees if payee.linked_account_id is None]),
            "categories": len(ledger.categories),
            "subcategories": len(ledger.subcategories),
            "transactions": len(ledger.transactions),
            "skipped_rows": skipped_rows,
        }

    def import_ledger_seed(self, seed_file: Path) -> dict[str, int]:
        if not seed_file.exists():
            raise ValueError(f"Seed file {seed_file} was not found.")
        payload = json.loads(seed_file.read_text(encoding="utf-8"))
        ledger = self.storage.load()
        seed = LedgerData.from_dict(payload)
        summary = self._merge_ledger_seed(ledger, seed)
        self.storage.save(ledger)
        return summary

    def rename_account(self, current_name: str, new_name: str) -> None:
        ledger = self.storage.load()
        account = self._find_account_by_name(ledger, current_name)
        if account is None:
            raise ValueError("Account was not found.")
        normalized_new_name = self._require_name(new_name, "Account")
        if self._find_account_by_name(ledger, normalized_new_name) is not None:
            raise ValueError("Account already exists.")
        account.name = normalized_new_name
        self._ensure_linked_payee(ledger, account)
        self.storage.save(ledger)

    def delete_account(
        self,
        name: str,
        *,
        replacement_account_name: str | None = None,
        delete_transactions: bool = False,
    ) -> None:
        ledger = self.storage.load()
        account = self._find_account_by_name(ledger, name)
        if account is None:
            raise ValueError("Account was not found.")
        linked_payee = next((item for item in ledger.payees if item.linked_account_id == account.id), None)
        associated_transaction_ids = {
            transaction.id
            for transaction in ledger.transactions
            if transaction.account_id == account.id or (linked_payee is not None and transaction.payee_id == linked_payee.id)
        }
        if associated_transaction_ids:
            if delete_transactions:
                self._delete_transaction_ids(ledger, associated_transaction_ids)
            elif replacement_account_name:
                replacement = self._get_or_create_account(ledger, replacement_account_name)
                if replacement.id == account.id:
                    raise ValueError("Choose a different account for migration.")
                replacement_payee = self._ensure_linked_payee(ledger, replacement)
                if linked_payee is not None and any(
                    transaction.account_id == replacement.id and transaction.payee_id == linked_payee.id
                    for transaction in ledger.transactions
                ):
                    raise ValueError("Cannot migrate because the target account already has transfers to this account.")
                for transaction in ledger.transactions:
                    if transaction.account_id == account.id:
                        transaction.account_id = replacement.id
                    if linked_payee is not None and transaction.payee_id == linked_payee.id:
                        transaction.payee_id = replacement_payee.id
            else:
                raise ValueError("Choose a migration account or delete the associated transactions.")
        ledger.accounts = [item for item in ledger.accounts if item.id != account.id]
        ledger.payees = [item for item in ledger.payees if item.linked_account_id != account.id]
        self.storage.save(ledger)

    def create_payee(self, name: str) -> None:
        ledger = self.storage.load()
        self._get_or_create_payee(ledger, name)
        self.storage.save(ledger)

    def rename_payee(self, current_name: str, new_name: str) -> None:
        ledger = self.storage.load()
        payee = self._find_payee_by_name(ledger, current_name)
        if payee is None:
            raise ValueError("Payee was not found.")
        if payee.linked_account_id is not None:
            raise ValueError("Linked account payees are managed through accounts.")
        normalized_new_name = self._require_name(new_name, "Payee")
        if self._find_payee_by_name(ledger, normalized_new_name) is not None:
            raise ValueError("Payee already exists.")
        payee.name = normalized_new_name
        self.storage.save(ledger)

    def delete_payee(
        self,
        name: str,
        *,
        replacement_payee_name: str | None = None,
        delete_transactions: bool = False,
    ) -> None:
        ledger = self.storage.load()
        payee = self._find_payee_by_name(ledger, name)
        if payee is None:
            raise ValueError("Payee was not found.")
        if payee.linked_account_id is not None:
            raise ValueError("Linked account payees are managed through accounts.")
        associated_transaction_ids = {transaction.id for transaction in ledger.transactions if transaction.payee_id == payee.id}
        if associated_transaction_ids:
            if delete_transactions:
                self._delete_transaction_ids(ledger, associated_transaction_ids)
            elif replacement_payee_name:
                replacement = self._get_or_create_payee(ledger, replacement_payee_name)
                if replacement.id == payee.id:
                    raise ValueError("Choose a different payee for migration.")
                if replacement.linked_account_id is not None:
                    raise ValueError("Payee migration cannot target an account payee.")
                for transaction in ledger.transactions:
                    if transaction.payee_id == payee.id:
                        transaction.payee_id = replacement.id
            else:
                raise ValueError("Choose a migration payee or delete the associated transactions.")
        ledger.payees = [item for item in ledger.payees if item.id != payee.id]
        self.storage.save(ledger)

    def create_category(self, name: str) -> None:
        ledger = self.storage.load()
        self._get_or_create_category(ledger, name)
        self.storage.save(ledger)

    def rename_category(self, current_name: str, new_name: str) -> None:
        ledger = self.storage.load()
        category = self._find_category_by_name(ledger, current_name)
        if category is None:
            raise ValueError("Category was not found.")
        normalized_new_name = self._require_name(new_name, "Category")
        if self._find_category_by_name(ledger, normalized_new_name) is not None:
            raise ValueError("Category already exists.")
        category.name = normalized_new_name
        self.storage.save(ledger)

    def delete_category(
        self,
        name: str,
        *,
        replacement_category_name: str | None = None,
        replacement_subcategory_name: str | None = None,
        delete_transactions: bool = False,
    ) -> None:
        ledger = self.storage.load()
        category = self._find_category_by_name(ledger, name)
        if category is None:
            raise ValueError("Category was not found.")
        category_subcategories = [subcategory for subcategory in ledger.subcategories if subcategory.category_id == category.id]
        category_subcategory_ids = {subcategory.id for subcategory in category_subcategories}
        associated_transaction_ids = {
            transaction.id
            for transaction in ledger.transactions
            if transaction.category_id == category.id or transaction.subcategory_id in category_subcategory_ids
        }
        if associated_transaction_ids:
            if delete_transactions:
                self._delete_transaction_ids(ledger, associated_transaction_ids)
            elif replacement_category_name:
                replacement_category = self._get_or_create_category(ledger, replacement_category_name)
                replacement_subcategory = self._get_or_create_subcategory(
                    ledger,
                    replacement_subcategory_name,
                    replacement_category.id,
                )
                if replacement_category.id == category.id:
                    raise ValueError("Choose a different category for migration.")
                for transaction in ledger.transactions:
                    if transaction.category_id == category.id or transaction.subcategory_id in category_subcategory_ids:
                        transaction.category_id = replacement_category.id
                        transaction.subcategory_id = replacement_subcategory.id if replacement_subcategory is not None else None
            else:
                raise ValueError("Choose a migration category or delete the associated transactions.")
        ledger.categories = [item for item in ledger.categories if item.id != category.id]
        ledger.subcategories = [item for item in ledger.subcategories if item.category_id != category.id]
        self.storage.save(ledger)

    def create_subcategory(self, category_name: str, subcategory_name: str) -> None:
        ledger = self.storage.load()
        category = self._get_or_create_category(ledger, category_name)
        self._get_or_create_subcategory(ledger, subcategory_name, category.id)
        self.storage.save(ledger)

    def rename_subcategory(self, category_name: str, current_name: str, new_name: str) -> None:
        ledger = self.storage.load()
        category = self._find_category_by_name(ledger, category_name)
        if category is None:
            raise ValueError("Category was not found.")
        subcategory = self._find_subcategory(ledger, category.id, current_name)
        if subcategory is None:
            raise ValueError("Subcategory was not found.")
        normalized_new_name = self._require_name(new_name, "Subcategory")
        if self._find_subcategory(ledger, category.id, normalized_new_name) is not None:
            raise ValueError("Subcategory already exists in this category.")
        subcategory.name = normalized_new_name
        self.storage.save(ledger)

    def delete_subcategory(
        self,
        category_name: str,
        subcategory_name: str,
        *,
        replacement_category_name: str | None = None,
        replacement_subcategory_name: str | None = None,
        delete_transactions: bool = False,
    ) -> None:
        ledger = self.storage.load()
        category = self._find_category_by_name(ledger, category_name)
        if category is None:
            raise ValueError("Category was not found.")
        subcategory = self._find_subcategory(ledger, category.id, subcategory_name)
        if subcategory is None:
            raise ValueError("Subcategory was not found.")
        associated_transaction_ids = {transaction.id for transaction in ledger.transactions if transaction.subcategory_id == subcategory.id}
        if associated_transaction_ids:
            if delete_transactions:
                self._delete_transaction_ids(ledger, associated_transaction_ids)
            elif replacement_category_name and replacement_subcategory_name:
                replacement_category = self._get_or_create_category(ledger, replacement_category_name)
                replacement_subcategory = self._get_or_create_subcategory(
                    ledger,
                    replacement_subcategory_name,
                    replacement_category.id,
                )
                if replacement_subcategory is None or replacement_subcategory.id == subcategory.id:
                    raise ValueError("Choose a different subcategory for migration.")
                for transaction in ledger.transactions:
                    if transaction.subcategory_id == subcategory.id:
                        transaction.category_id = replacement_category.id
                        transaction.subcategory_id = replacement_subcategory.id
            else:
                raise ValueError("Choose a migration subcategory or delete the associated transactions.")
        ledger.subcategories = [item for item in ledger.subcategories if item.id != subcategory.id]
        self.storage.save(ledger)

    def _apply_transaction_change(
        self,
        ledger: LedgerData,
        *,
        original_transaction_id: str | None,
        account_name: str,
        payee_name: str,
        category_name: str,
        subcategory_name: str | None,
        amount: str,
        spent_on: str,
        notes: str,
        attachment_paths: Sequence[str] | None,
    ) -> list[Transaction]:
        existing_primary_attachments: list[Attachment] = []
        primary_transaction_id = str(uuid4())
        secondary_transaction_id: str | None = None
        transfer_group_id: str | None = None

        if original_transaction_id is not None:
            target = next((item for item in ledger.transactions if item.id == original_transaction_id), None)
            if target is None:
                raise ValueError("Transaction was not found.")
            linked_ids = self._transaction_group_ids(ledger, original_transaction_id)
            primary_transaction_id = original_transaction_id
            secondary_transaction_id = target.linked_transaction_id
            transfer_group_id = target.transfer_group_id
            existing_primary_attachments = [attachment for attachment in ledger.attachments if attachment.transaction_id == original_transaction_id]
            removed_linked_attachments = [attachment for attachment in ledger.attachments if attachment.transaction_id in linked_ids and attachment.transaction_id != original_transaction_id]
            for attachment in removed_linked_attachments:
                attachment_file = Path(attachment.stored_path)
                if attachment_file.exists():
                    attachment_file.unlink()
            ledger.attachments = [attachment for attachment in ledger.attachments if attachment.transaction_id not in linked_ids or attachment.transaction_id == original_transaction_id]
            ledger.transactions = [transaction for transaction in ledger.transactions if transaction.id not in linked_ids]

        account = self._get_or_create_account(ledger, account_name)
        payee = self._get_or_create_payee(ledger, payee_name)
        created_at = datetime.now(UTC)
        parsed_amount = self._parse_signed_amount(amount)
        parsed_date = self._parse_date(spent_on)
        normalized_notes = notes.strip()

        if payee.linked_account_id == account.id:
            raise ValueError("Source and destination accounts must be different.")

        created_transactions: list[Transaction] = []
        if payee.linked_account_id is not None:
            transfer_category = self._get_or_create_category(ledger, "Transfers")
            transfer_subcategory = self._get_or_create_subcategory(ledger, "Transfer", transfer_category.id)
            transfer_amount = abs(parsed_amount)
            transfer_group_id = transfer_group_id or str(uuid4())
            secondary_transaction_id = secondary_transaction_id or str(uuid4())
            source_transaction = Transaction(
                id=primary_transaction_id,
                entry_type="transfer_out",
                account_id=account.id,
                payee_id=payee.id,
                category_id=transfer_category.id,
                subcategory_id=transfer_subcategory.id if transfer_subcategory is not None else None,
                amount=-transfer_amount,
                notes=normalized_notes,
                spent_on=parsed_date,
                created_at=created_at,
                linked_transaction_id=secondary_transaction_id,
                transfer_group_id=transfer_group_id,
            )
            destination_account = self._require_account(ledger, payee.linked_account_id)
            source_account_payee = self._ensure_linked_payee(ledger, account)
            destination_transaction = Transaction(
                id=secondary_transaction_id,
                entry_type="transfer_in",
                account_id=destination_account.id,
                payee_id=source_account_payee.id,
                category_id=transfer_category.id,
                subcategory_id=transfer_subcategory.id if transfer_subcategory is not None else None,
                amount=transfer_amount,
                notes=normalized_notes,
                spent_on=parsed_date,
                created_at=created_at,
                linked_transaction_id=primary_transaction_id,
                transfer_group_id=transfer_group_id,
            )
            ledger.transactions.extend([source_transaction, destination_transaction])
            created_transactions.extend([source_transaction, destination_transaction])
        else:
            category = self._get_or_create_category(ledger, category_name)
            subcategory = self._get_or_create_subcategory(ledger, subcategory_name, category.id)
            entry_type = "income" if parsed_amount > Decimal("0.00") else "expense"
            transaction = Transaction(
                id=primary_transaction_id,
                entry_type=entry_type,
                account_id=account.id,
                payee_id=payee.id,
                category_id=category.id,
                subcategory_id=subcategory.id if subcategory is not None else None,
                amount=parsed_amount,
                notes=normalized_notes,
                spent_on=parsed_date,
                created_at=created_at,
            )
            ledger.transactions.append(transaction)
            created_transactions.append(transaction)

        if existing_primary_attachments:
            ledger.attachments.extend(existing_primary_attachments)
        if attachment_paths:
            for attachment_path in attachment_paths:
                if not attachment_path.strip():
                    continue
                ledger.attachments.append(self._copy_attachment(primary_transaction_id, Path(attachment_path.strip()), created_at))
        return created_transactions

    def _merge_ledger_seed(self, ledger: LedgerData, seed: LedgerData) -> dict[str, int]:
        summary = {
            "accounts_added": 0,
            "payees_added": 0,
            "categories_added": 0,
            "subcategories_added": 0,
            "transactions_added": 0,
            "transactions_skipped": 0,
        }
        self._ensure_account_payees(ledger)
        account_id_map: dict[str, str] = {}
        for seed_account in seed.accounts:
            existing = self._find_account_by_name(ledger, seed_account.name)
            if existing is None:
                existing = Account(id=seed_account.id, name=seed_account.name)
                ledger.accounts.append(existing)
                summary["accounts_added"] += 1
            self._ensure_linked_payee(ledger, existing)
            account_id_map[seed_account.id] = existing.id

        category_id_map: dict[str, str] = {}
        seed_categories_by_id = {category.id: category for category in seed.categories}
        for seed_category in seed.categories:
            before = len(ledger.categories)
            category = self._get_or_create_category(ledger, seed_category.name)
            if len(ledger.categories) != before:
                summary["categories_added"] += 1
            category_id_map[seed_category.id] = category.id

        subcategory_id_map: dict[str, str | None] = {}
        for seed_subcategory in seed.subcategories:
            seed_category = seed_categories_by_id.get(seed_subcategory.category_id)
            if seed_category is None:
                continue
            before = len(ledger.subcategories)
            subcategory = self._get_or_create_subcategory(
                ledger,
                seed_subcategory.name,
                category_id_map[seed_category.id],
            )
            if subcategory is not None and len(ledger.subcategories) != before:
                summary["subcategories_added"] += 1
            subcategory_id_map[seed_subcategory.id] = subcategory.id if subcategory is not None else None

        payee_id_map: dict[str, str] = {}
        for seed_payee in seed.payees:
            mapped_linked_account_id = account_id_map.get(seed_payee.linked_account_id) if seed_payee.linked_account_id else None
            existing = next(
                (
                    payee
                    for payee in ledger.payees
                    if self._normalize(payee.name) == self._normalize(seed_payee.name)
                    and payee.linked_account_id == mapped_linked_account_id
                ),
                None,
            )
            if existing is None:
                if mapped_linked_account_id is not None:
                    existing = self._ensure_linked_payee(ledger, self._require_account(ledger, mapped_linked_account_id))
                else:
                    before = len(ledger.payees)
                    existing = self._get_or_create_payee(ledger, seed_payee.name)
                    if len(ledger.payees) != before:
                        summary["payees_added"] += 1
            payee_id_map[seed_payee.id] = existing.id

        existing_transaction_ids = {transaction.id for transaction in ledger.transactions}
        transaction_id_map = {transaction.id: transaction.id for transaction in seed.transactions if transaction.id not in existing_transaction_ids}
        for seed_transaction in seed.transactions:
            if seed_transaction.id in existing_transaction_ids:
                summary["transactions_skipped"] += 1
                continue
            ledger.transactions.append(
                Transaction(
                    id=seed_transaction.id,
                    entry_type=seed_transaction.entry_type,
                    account_id=account_id_map[seed_transaction.account_id],
                    payee_id=payee_id_map[seed_transaction.payee_id],
                    category_id=category_id_map[seed_transaction.category_id],
                    subcategory_id=subcategory_id_map.get(seed_transaction.subcategory_id) if seed_transaction.subcategory_id else None,
                    amount=seed_transaction.amount,
                    notes=seed_transaction.notes,
                    spent_on=seed_transaction.spent_on,
                    created_at=seed_transaction.created_at,
                    linked_transaction_id=transaction_id_map.get(seed_transaction.linked_transaction_id) if seed_transaction.linked_transaction_id else None,
                    transfer_group_id=seed_transaction.transfer_group_id,
                )
            )
            summary["transactions_added"] += 1
            existing_transaction_ids.add(seed_transaction.id)

        existing_attachment_ids = {attachment.id for attachment in ledger.attachments}
        for seed_attachment in seed.attachments:
            mapped_transaction_id = transaction_id_map.get(seed_attachment.transaction_id)
            if mapped_transaction_id is None or seed_attachment.id in existing_attachment_ids:
                continue
            ledger.attachments.append(
                Attachment(
                    id=seed_attachment.id,
                    transaction_id=mapped_transaction_id,
                    original_name=seed_attachment.original_name,
                    stored_path=seed_attachment.stored_path,
                    uploaded_at=seed_attachment.uploaded_at,
                )
            )
        return summary

    def _metadata_snapshot_from_ledger(self, ledger: LedgerData) -> MetadataSnapshot:
        category_map = {category.id: category.name for category in ledger.categories}
        subcategories_by_category: dict[str, list[str]] = {category.name: [] for category in ledger.categories}
        for subcategory in sorted(ledger.subcategories, key=lambda item: item.name.casefold()):
            category_name = category_map.get(subcategory.category_id)
            if category_name is None:
                continue
            subcategories_by_category.setdefault(category_name, []).append(subcategory.name)
        return MetadataSnapshot(
            accounts=sorted((account.name for account in ledger.accounts), key=str.casefold),
            payees=sorted((payee.name for payee in ledger.payees), key=str.casefold),
            categories=sorted((category.name for category in ledger.categories), key=str.casefold),
            subcategories_by_category=subcategories_by_category,
        )

    def _build_record(self, ledger: LedgerData, transaction: Transaction) -> TransactionRecord:
        account_map = {account.id: account.name for account in ledger.accounts}
        payee_map = {payee.id: payee.name for payee in ledger.payees}
        category_map = {category.id: category.name for category in ledger.categories}
        subcategory_map = {subcategory.id: subcategory.name for subcategory in ledger.subcategories}
        attachments = [attachment for attachment in ledger.attachments if attachment.transaction_id == transaction.id]
        return TransactionRecord(
            id=transaction.id,
            entry_type=transaction.entry_type,
            account_name=account_map.get(transaction.account_id, "Unknown"),
            payee_name=payee_map.get(transaction.payee_id, "Unknown"),
            category_name=category_map.get(transaction.category_id, "Unknown"),
            subcategory_name=subcategory_map.get(transaction.subcategory_id) if transaction.subcategory_id else None,
            amount=transaction.amount,
            notes=transaction.notes,
            spent_on=transaction.spent_on,
            linked_transaction_id=transaction.linked_transaction_id,
            transfer_group_id=transaction.transfer_group_id,
            attachments=attachments,
        )

    def _transaction_group_ids(self, ledger: LedgerData, transaction_id: str) -> set[str]:
        target = next((item for item in ledger.transactions if item.id == transaction_id), None)
        if target is None:
            return set()
        transaction_ids = {transaction_id}
        if target.linked_transaction_id is not None:
            transaction_ids.add(target.linked_transaction_id)
        return transaction_ids

    def _find_account_by_name(self, ledger: LedgerData, name: str) -> Account | None:
        return next((item for item in ledger.accounts if self._normalize(item.name) == self._normalize(name)), None)

    def _find_payee_by_name(self, ledger: LedgerData, name: str) -> Payee | None:
        return next((item for item in ledger.payees if self._normalize(item.name) == self._normalize(name)), None)

    def _find_category_by_name(self, ledger: LedgerData, name: str) -> Category | None:
        return next((item for item in ledger.categories if self._normalize(item.name) == self._normalize(name)), None)

    def _find_subcategory(self, ledger: LedgerData, category_id: str, name: str) -> Subcategory | None:
        return next((item for item in ledger.subcategories if item.category_id == category_id and self._normalize(item.name) == self._normalize(name)), None)

    def _get_or_create_account(self, ledger: LedgerData, name: str) -> Account:
        normalized_name = self._require_name(name, "Account")
        existing = self._find_account_by_name(ledger, normalized_name)
        if existing is not None:
            self._ensure_linked_payee(ledger, existing)
            return existing
        account = Account(id=str(uuid4()), name=normalized_name)
        ledger.accounts.append(account)
        self._ensure_linked_payee(ledger, account)
        return account

    def _require_account(self, ledger: LedgerData, account_id: str) -> Account:
        account = next((item for item in ledger.accounts if item.id == account_id), None)
        if account is None:
            raise ValueError("Linked account was not found.")
        return account

    def _get_or_create_payee(self, ledger: LedgerData, name: str) -> Payee:
        normalized_name = self._require_name(name, "Payee")
        existing = self._find_payee_by_name(ledger, normalized_name)
        if existing is not None:
            return existing
        matching_account = self._find_account_by_name(ledger, normalized_name)
        payee = Payee(id=str(uuid4()), name=normalized_name, linked_account_id=matching_account.id if matching_account is not None else None)
        ledger.payees.append(payee)
        return payee

    def _ensure_linked_payee(self, ledger: LedgerData, account: Account) -> Payee:
        existing = next((payee for payee in ledger.payees if payee.linked_account_id == account.id), None)
        if existing is not None:
            if existing.name != account.name:
                existing.name = account.name
            return existing
        payee = Payee(id=str(uuid4()), name=account.name, linked_account_id=account.id)
        ledger.payees.append(payee)
        return payee

    def _ensure_account_payees(self, ledger: LedgerData) -> bool:
        changed = False
        for account in ledger.accounts:
            before_count = len(ledger.payees)
            linked_payee = self._ensure_linked_payee(ledger, account)
            if len(ledger.payees) != before_count or linked_payee.name != account.name:
                changed = True
        return changed

    def _get_or_create_category(self, ledger: LedgerData, name: str) -> Category:
        normalized_name = self._require_name(name, "Category")
        existing = self._find_category_by_name(ledger, normalized_name)
        if existing is not None:
            return existing
        category = Category(id=str(uuid4()), name=normalized_name)
        ledger.categories.append(category)
        return category

    def _get_or_create_subcategory(self, ledger: LedgerData, name: str | None, category_id: str) -> Subcategory | None:
        if name is None or not name.strip():
            return None
        normalized_name = name.strip()
        existing = next(
            (
                subcategory
                for subcategory in ledger.subcategories
                if subcategory.category_id == category_id and self._normalize(subcategory.name) == self._normalize(normalized_name)
            ),
            None,
        )
        if existing is not None:
            return existing
        subcategory = Subcategory(id=str(uuid4()), name=normalized_name, category_id=category_id)
        ledger.subcategories.append(subcategory)
        return subcategory

    def _copy_attachment(self, transaction_id: str, source_path: Path, uploaded_at: datetime) -> Attachment:
        if not source_path.exists() or not source_path.is_file():
            raise ValueError(f"Attachment {source_path} was not found.")
        destination_dir = self.storage.attachments_dir / transaction_id
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination_path = destination_dir / source_path.name
        counter = 1
        while destination_path.exists():
            destination_path = destination_dir / f"{source_path.stem}-{counter}{source_path.suffix}"
            counter += 1
        copy2(source_path, destination_path)
        return Attachment(
            id=str(uuid4()),
            transaction_id=transaction_id,
            original_name=source_path.name,
            stored_path=str(destination_path),
            uploaded_at=uploaded_at,
        )

    def _require_name(self, value: str, field_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} is required.")
        return normalized

    def _parse_signed_amount(self, value: str) -> Decimal:
        try:
            amount = Decimal(value).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError("Amount must be a valid number.") from exc
        if amount == Decimal("0.00"):
            raise ValueError("Amount must be non-zero.")
        return amount

    def _parse_date(self, value: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Date must use YYYY-MM-DD format.") from exc

    def _split_legacy_category(self, value: str) -> tuple[str, str]:
        raw_value = value.strip()
        if ":" in raw_value:
            category_name, subcategory_name = raw_value.split(":", maxsplit=1)
            return self._require_name(category_name, "Category"), self._require_name(subcategory_name, "Subcategory")
        return self._require_name(raw_value or "Uncategorized", "Category"), "General"

    def _default_legacy_category(self, value: str | None, payee_name: str, account_names: set[str]) -> str:
        if value and value.strip():
            return value
        if self._normalize(payee_name) in account_names:
            return "Transfers:Internal"
        return "Uncategorized:General"

    def _search_text(self, record: TransactionRecord) -> str:
        parts = [
            record.account_name,
            record.payee_name,
            record.category_name,
            record.subcategory_name or "",
            record.notes,
            record.spent_on.isoformat(),
        ]
        return self._normalize(" ".join(parts))

    def _normalize(self, value: str | None) -> str:
        return value.strip().casefold() if value else ""
