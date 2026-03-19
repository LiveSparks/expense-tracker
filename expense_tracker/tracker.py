from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from shutil import copy2
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
        category_map = {category.id: category.name for category in ledger.categories}
        subcategories_by_category: dict[str, list[str]] = {
            category.name: [] for category in ledger.categories
        }
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
        transaction_type: str = "expense",
        attachment_paths: Sequence[str] | None = None,
    ) -> list[TransactionRecord]:
        ledger = self.storage.load()
        account = self._get_or_create_account(ledger, account_name)
        payee = self._get_or_create_payee(ledger, payee_name)
        created_at = datetime.now(UTC)
        parsed_amount = self._parse_amount(amount)
        parsed_date = self._parse_date(spent_on)
        normalized_notes = notes.strip()

        if transaction_type not in {"expense", "income", "transfer"}:
            raise ValueError("Transaction type must be expense, income, or transfer.")

        if transaction_type == "transfer" and payee.linked_account_id is None:
            destination_account = self._get_or_create_account(ledger, payee_name)
            payee = self._ensure_linked_payee(ledger, destination_account)

        if payee.linked_account_id == account.id:
            raise ValueError("Source and destination accounts must be different.")

        created_transactions: list[Transaction] = []
        if payee.linked_account_id is not None:
            transfer_category = self._get_or_create_category(ledger, "Transfers")
            transfer_subcategory = self._get_or_create_subcategory(
                ledger,
                "Transfer",
                transfer_category.id,
            )
            transfer_group_id = str(uuid4())
            source_transaction = Transaction(
                id=str(uuid4()),
                entry_type="transfer_out",
                account_id=account.id,
                payee_id=payee.id,
                category_id=transfer_category.id,
                subcategory_id=transfer_subcategory.id,
                amount=-parsed_amount,
                notes=normalized_notes,
                spent_on=parsed_date,
                created_at=created_at,
                transfer_group_id=transfer_group_id,
            )
            destination_account = self._require_account(ledger, payee.linked_account_id)
            source_account_payee = self._ensure_linked_payee(ledger, account)
            destination_transaction = Transaction(
                id=str(uuid4()),
                entry_type="transfer_in",
                account_id=destination_account.id,
                payee_id=source_account_payee.id,
                category_id=transfer_category.id,
                subcategory_id=transfer_subcategory.id,
                amount=parsed_amount,
                notes=normalized_notes,
                spent_on=parsed_date,
                created_at=created_at,
                transfer_group_id=transfer_group_id,
            )
            source_transaction.linked_transaction_id = destination_transaction.id
            destination_transaction.linked_transaction_id = source_transaction.id
            ledger.transactions.extend([source_transaction, destination_transaction])
            created_transactions.extend([source_transaction, destination_transaction])
        else:
            category = self._get_or_create_category(ledger, category_name)
            subcategory = self._get_or_create_subcategory(
                ledger,
                subcategory_name,
                category.id,
            )
            signed_amount = parsed_amount if transaction_type == "income" else -parsed_amount
            transaction = Transaction(
                id=str(uuid4()),
                entry_type=transaction_type,
                account_id=account.id,
                payee_id=payee.id,
                category_id=category.id,
                subcategory_id=subcategory.id if subcategory is not None else None,
                amount=signed_amount,
                notes=normalized_notes,
                spent_on=parsed_date,
                created_at=created_at,
            )
            ledger.transactions.append(transaction)
            created_transactions.append(transaction)

        if attachment_paths:
            source_transaction = created_transactions[0]
            for attachment_path in attachment_paths:
                if not attachment_path.strip():
                    continue
                ledger.attachments.append(
                    self._copy_attachment(
                        source_transaction.id,
                        Path(attachment_path.strip()),
                        created_at,
                    )
                )

        self.storage.save(ledger)
        return [self._build_record(ledger, item) for item in created_transactions]

    def delete_transaction(self, transaction_id: str) -> bool:
        ledger = self.storage.load()
        transaction_ids = {transaction_id}
        target = next((item for item in ledger.transactions if item.id == transaction_id), None)
        if target is None:
            return False
        if target.linked_transaction_id is not None:
            transaction_ids.add(target.linked_transaction_id)

        removed_attachments = [
            attachment for attachment in ledger.attachments if attachment.transaction_id in transaction_ids
        ]
        ledger.transactions = [
            transaction for transaction in ledger.transactions if transaction.id not in transaction_ids
        ]
        ledger.attachments = [
            attachment for attachment in ledger.attachments if attachment.transaction_id not in transaction_ids
        ]
        for attachment in removed_attachments:
            attachment_file = Path(attachment.stored_path)
            if attachment_file.exists():
                attachment_file.unlink()
        self.storage.save(ledger)
        return True

    def list_transactions(
        self,
        *,
        account: str | None = None,
        category: str | None = None,
        payee: str | None = None,
        exact_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[TransactionRecord]:
        ledger = self.storage.load()
        records = [self._build_record(ledger, transaction) for transaction in ledger.transactions]
        normalized_account = self._normalize(account)
        normalized_category = self._normalize(category)
        normalized_payee = self._normalize(payee)
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
            if parsed_exact_date and record.spent_on != parsed_exact_date:
                continue
            if parsed_start_date and record.spent_on < parsed_start_date:
                continue
            if parsed_end_date and record.spent_on > parsed_end_date:
                continue
            filtered.append(record)

        return sorted(
            filtered,
            key=lambda item: (item.spent_on, item.id),
            reverse=True,
        )

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
        exact_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for transaction in self.list_transactions(
            account=account,
            category=category,
            payee=payee,
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

    def _build_record(self, ledger: LedgerData, transaction: Transaction) -> TransactionRecord:
        account_map = {account.id: account.name for account in ledger.accounts}
        payee_map = {payee.id: payee.name for payee in ledger.payees}
        category_map = {category.id: category.name for category in ledger.categories}
        subcategory_map = {subcategory.id: subcategory.name for subcategory in ledger.subcategories}
        attachments = [
            attachment
            for attachment in ledger.attachments
            if attachment.transaction_id == transaction.id
        ]
        return TransactionRecord(
            id=transaction.id,
            entry_type=transaction.entry_type,
            account_name=account_map.get(transaction.account_id, "Unknown"),
            payee_name=payee_map.get(transaction.payee_id, "Unknown"),
            category_name=category_map.get(transaction.category_id, "Unknown"),
            subcategory_name=(
                subcategory_map.get(transaction.subcategory_id)
                if transaction.subcategory_id is not None
                else None
            ),
            amount=transaction.amount,
            notes=transaction.notes,
            spent_on=transaction.spent_on,
            linked_transaction_id=transaction.linked_transaction_id,
            transfer_group_id=transaction.transfer_group_id,
            attachments=attachments,
        )

    def _get_or_create_account(self, ledger: LedgerData, name: str) -> Account:
        normalized_name = self._require_name(name, "Account")
        existing = next(
            (account for account in ledger.accounts if self._normalize(account.name) == self._normalize(normalized_name)),
            None,
        )
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
        existing = next(
            (payee for payee in ledger.payees if self._normalize(payee.name) == self._normalize(normalized_name)),
            None,
        )
        if existing is not None:
            return existing

        matching_account = next(
            (account for account in ledger.accounts if self._normalize(account.name) == self._normalize(normalized_name)),
            None,
        )
        payee = Payee(
            id=str(uuid4()),
            name=normalized_name,
            linked_account_id=matching_account.id if matching_account is not None else None,
        )
        ledger.payees.append(payee)
        return payee

    def _ensure_linked_payee(self, ledger: LedgerData, account: Account) -> Payee:
        existing = next(
            (payee for payee in ledger.payees if payee.linked_account_id == account.id),
            None,
        )
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
        existing = next(
            (category for category in ledger.categories if self._normalize(category.name) == self._normalize(normalized_name)),
            None,
        )
        if existing is not None:
            return existing

        category = Category(id=str(uuid4()), name=normalized_name)
        ledger.categories.append(category)
        return category

    def _get_or_create_subcategory(
        self,
        ledger: LedgerData,
        name: str | None,
        category_id: str,
    ) -> Subcategory | None:
        if name is None or not name.strip():
            return None
        normalized_name = name.strip()
        existing = next(
            (
                subcategory
                for subcategory in ledger.subcategories
                if self._normalize(subcategory.name) == self._normalize(normalized_name)
            ),
            None,
        )
        if existing is not None:
            if existing.category_id != category_id:
                raise ValueError("Subcategory already belongs to a different category.")
            return existing

        subcategory = Subcategory(
            id=str(uuid4()),
            name=normalized_name,
            category_id=category_id,
        )
        ledger.subcategories.append(subcategory)
        return subcategory

    def _copy_attachment(
        self,
        transaction_id: str,
        source_path: Path,
        uploaded_at: datetime,
    ) -> Attachment:
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

    def _parse_amount(self, value: str) -> Decimal:
        try:
            amount = Decimal(value).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError("Amount must be a valid number.") from exc
        if amount <= Decimal("0.00"):
            raise ValueError("Amount must be greater than zero.")
        return amount

    def _parse_date(self, value: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Date must use YYYY-MM-DD format.") from exc

    def _normalize(self, value: str | None) -> str:
        return value.strip().casefold() if value else ""
