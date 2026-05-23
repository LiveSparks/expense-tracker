from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(slots=True)
class Account:
    id: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> "Account":
        return cls(id=payload["id"], name=payload["name"])


@dataclass(slots=True)
class Payee:
    id: str
    name: str
    linked_account_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "id": self.id,
            "name": self.name,
            "linked_account_id": self.linked_account_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, str | None]) -> "Payee":
        return cls(
            id=str(payload["id"]),
            name=str(payload["name"]),
            linked_account_id=(
                str(payload["linked_account_id"])
                if payload.get("linked_account_id") is not None
                else None
            ),
        )


@dataclass(slots=True)
class Category:
    id: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> "Category":
        return cls(id=payload["id"], name=payload["name"])


@dataclass(slots=True)
class Subcategory:
    id: str
    name: str
    category_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "category_id": self.category_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> "Subcategory":
        return cls(
            id=payload["id"],
            name=payload["name"],
            category_id=payload["category_id"],
        )


@dataclass(slots=True)
class Attachment:
    id: str
    transaction_id: str
    original_name: str
    stored_path: str
    uploaded_at: datetime

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "transaction_id": self.transaction_id,
            "original_name": self.original_name,
            "stored_path": self.stored_path,
            "uploaded_at": self.uploaded_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> "Attachment":
        return cls(
            id=payload["id"],
            transaction_id=payload["transaction_id"],
            original_name=payload["original_name"],
            stored_path=payload["stored_path"],
            uploaded_at=datetime.fromisoformat(payload["uploaded_at"]),
        )


@dataclass(slots=True)
class Transaction:
    id: str
    entry_type: str
    account_id: str
    payee_id: str
    category_id: str
    subcategory_id: str | None
    amount: Decimal
    notes: str
    spent_on: date
    created_at: datetime
    linked_transaction_id: str | None = None
    transfer_group_id: str | None = None
    verified: bool = False

    def to_dict(self) -> dict[str, str | None]:
        return {
            "id": self.id,
            "entry_type": self.entry_type,
            "account_id": self.account_id,
            "payee_id": self.payee_id,
            "category_id": self.category_id,
            "subcategory_id": self.subcategory_id,
            "amount": f"{self.amount:.2f}",
            "notes": self.notes,
            "spent_on": self.spent_on.isoformat(),
            "created_at": self.created_at.isoformat(),
            "linked_transaction_id": self.linked_transaction_id,
            "transfer_group_id": self.transfer_group_id,
            "verified": int(self.verified),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, str | None]) -> "Transaction":
        return cls(
            id=str(payload["id"]),
            entry_type=str(payload["entry_type"]),
            account_id=str(payload["account_id"]),
            payee_id=str(payload["payee_id"]),
            category_id=str(payload["category_id"]),
            subcategory_id=(
                str(payload["subcategory_id"])
                if payload.get("subcategory_id") is not None
                else None
            ),
            amount=Decimal(str(payload["amount"])),
            notes=str(payload.get("notes", "")),
            spent_on=date.fromisoformat(str(payload["spent_on"])),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            linked_transaction_id=(
                str(payload["linked_transaction_id"])
                if payload.get("linked_transaction_id") is not None
                else None
            ),
            transfer_group_id=(
                str(payload["transfer_group_id"])
                if payload.get("transfer_group_id") is not None
                else None
            ),
            verified=bool(int(payload.get("verified", 0) or 0)),
        )


@dataclass(slots=True)
class LedgerData:
    accounts: list[Account] = field(default_factory=list)
    payees: list[Payee] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    subcategories: list[Subcategory] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[dict[str, str | None]]]:
        return {
            "accounts": [account.to_dict() for account in self.accounts],
            "payees": [payee.to_dict() for payee in self.payees],
            "categories": [category.to_dict() for category in self.categories],
            "subcategories": [subcategory.to_dict() for subcategory in self.subcategories],
            "transactions": [transaction.to_dict() for transaction in self.transactions],
            "attachments": [attachment.to_dict() for attachment in self.attachments],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, list[dict[str, str | None]]]) -> "LedgerData":
        return cls(
            accounts=[Account.from_dict(item) for item in payload.get("accounts", [])],
            payees=[Payee.from_dict(item) for item in payload.get("payees", [])],
            categories=[Category.from_dict(item) for item in payload.get("categories", [])],
            subcategories=[Subcategory.from_dict(item) for item in payload.get("subcategories", [])],
            transactions=[Transaction.from_dict(item) for item in payload.get("transactions", [])],
            attachments=[Attachment.from_dict(item) for item in payload.get("attachments", [])],
        )


@dataclass(slots=True)
class TransactionRecord:
    id: str
    entry_type: str
    account_name: str
    payee_name: str
    category_name: str
    subcategory_name: str | None
    amount: Decimal
    notes: str
    spent_on: date
    created_at: datetime
    linked_transaction_id: str | None
    transfer_group_id: str | None
    verified: bool
    attachments: list[Attachment]


@dataclass(slots=True)
class MetadataSnapshot:
    accounts: list[str]
    payees: list[str]
    categories: list[str]
    subcategories_by_category: dict[str, list[str]]
