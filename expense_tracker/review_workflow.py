from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .models import MetadataSnapshot, TransactionRecord
from .sms_pipeline import SmsMessage, build_structured_output_schema, extract_sms_markers, tokenize
from .sqlite_utils import sqlite_connection
from .tracker import ExpenseTracker


NOTE_PATTERN = re.compile(r"\b(?:note|remarks?)\s*[:=-]?\s*([A-Za-z0-9&./,'() -]{3,})", re.IGNORECASE)


class OpenAIDraftError(RuntimeError):
    pass


class OpenAIDraftClient:
    def __init__(self, *, key_path: Path = Path("/root/openai.key"), model: str = "gpt-5-mini") -> None:
        self.key_path = key_path
        self.model = model

    def is_configured(self) -> bool:
        return self.key_path.exists() and bool(self.key_path.read_text(encoding="utf-8").strip())

    def generate_draft(
        self,
        *,
        metadata: MetadataSnapshot,
        message: SmsMessage,
        markers: dict[str, object],
        history: list[dict[str, object]],
    ) -> dict[str, object]:
        if not self.is_configured():
            raise OpenAIDraftError("OpenAI key file is not configured.")
        request_payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Draft a single expense-tracker transaction for review. "
                        "Use the SMS, extracted markers, similar history, and allowed metadata values. "
                        "Return structured output only. Keep notes minimal and in bullet-list format, using only references/transaction IDs and concise bank-provided notes."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "new_sms": message.to_dict(),
                            "extracted_markers": markers,
                            "similar_examples": history,
                            "available_accounts": metadata.accounts,
                            "available_payees": metadata.payees,
                            "available_categories": self._available_category_values(metadata),
                        },
                        indent=2,
                    ),
                },
            ],
            "response_format": build_structured_output_schema(),
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(request_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.key_path.read_text(encoding='utf-8').strip()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise OpenAIDraftError(f"OpenAI request failed: {exc}") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
            draft = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OpenAIDraftError("OpenAI response did not contain valid structured JSON.") from exc
        return draft

    @staticmethod
    def _available_category_values(metadata: MetadataSnapshot) -> list[str]:
        values: list[str] = []
        for category_name in metadata.categories:
            for subcategory_name in metadata.subcategories_by_category.get(category_name, []):
                values.append(f"{category_name} / {subcategory_name}")
        return values


@dataclass(slots=True)
class ReviewDraft:
    id: str
    sender: str
    phone: str
    content: str
    received_at: datetime
    latitude: float | None
    longitude: float | None
    source: str
    markers: dict[str, object]
    draft_account_name: str
    draft_payee_name: str
    draft_category_value: str
    draft_amount: str
    draft_spent_on: str
    draft_notes: str
    review_status: str
    reasoning: list[str]
    similar_examples: list[dict[str, object]]
    created_at: datetime
    approved_transaction_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "sender": self.sender,
            "phone": self.phone,
            "content": self.content,
            "received_at": self.received_at.isoformat(),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "source": self.source,
            "markers": self.markers,
            "draft_account_name": self.draft_account_name,
            "draft_payee_name": self.draft_payee_name,
            "draft_category_value": self.draft_category_value,
            "draft_amount": self.draft_amount,
            "draft_spent_on": self.draft_spent_on,
            "draft_notes": self.draft_notes,
            "review_status": self.review_status,
            "reasoning": self.reasoning,
            "similar_examples": self.similar_examples,
            "created_at": self.created_at.isoformat(),
            "approved_transaction_id": self.approved_transaction_id,
        }


class ReviewWorkflowStore:
    def __init__(self, data_file: Path, *, openai_client: OpenAIDraftClient | None = None) -> None:
        self.data_file = data_file
        self.openai_client = openai_client or OpenAIDraftClient()

    def list_reviews(self, *, status: str | None = "for_review") -> list[ReviewDraft]:
        self._ensure_schema()
        query = "SELECT * FROM sms_reviews"
        params: tuple[object, ...] = ()
        if status is not None:
            query += " WHERE review_status = ?"
            params = (status,)
        query += " ORDER BY received_at DESC, created_at DESC"
        with sqlite_connection(self.data_file) as connection:
            return [self._row_to_review(row) for row in connection.execute(query, params).fetchall()]

    def pending_count(self) -> int:
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM sms_reviews WHERE review_status = 'for_review'").fetchone()[0])

    def clear_reviews(self) -> None:
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            connection.execute("DELETE FROM sms_reviews")

    def get_review(self, review_id: str) -> ReviewDraft | None:
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            row = connection.execute("SELECT * FROM sms_reviews WHERE id = ?", (review_id,)).fetchone()
        return self._row_to_review(row) if row is not None else None

    def create_review(
        self,
        *,
        sender: str,
        content: str,
        received_at: str,
        phone: str = "",
        latitude: float | None = None,
        longitude: float | None = None,
        source: str = "sms_intake",
    ) -> ReviewDraft:
        tracker = ExpenseTracker(self.data_file)
        metadata = tracker.metadata_snapshot()
        message = SmsMessage(
            row_number=0,
            received_at=self._parse_received_at(received_at),
            direction="Received",
            contact=sender.strip(),
            phone=phone.strip() or sender.strip(),
            content=content.strip(),
            message_type="SMS",
        )
        markers = extract_sms_markers(message)
        markers_payload = markers.to_dict()
        history = self._similar_transactions(message, markers_payload, tracker.list_transactions())
        draft = self._build_draft(metadata, message, markers_payload, history)
        review_source = source
        if self.openai_client.is_configured():
            try:
                ai_draft = self.openai_client.generate_draft(
                    metadata=metadata,
                    message=message,
                    markers=markers_payload,
                    history=history,
                )
                draft = self._merge_ai_draft(ai_draft, draft)
                review_source = f"{source}:openai"
            except OpenAIDraftError as exc:
                draft["reasoning"].append(f"OpenAI drafting failed: {exc}")
                review_source = f"{source}:heuristic_fallback"
        review = ReviewDraft(
            id=str(uuid4()),
            sender=message.contact,
            phone=message.phone,
            content=message.content,
            received_at=message.received_at,
            latitude=latitude,
            longitude=longitude,
            source=review_source,
            markers=markers_payload,
            draft_account_name=draft["account_name"],
            draft_payee_name=draft["payee_name"],
            draft_category_value=draft["category_value"],
            draft_amount=draft["amount"],
            draft_spent_on=draft["spent_on"],
            draft_notes=draft["notes"],
            review_status="for_review",
            reasoning=draft["reasoning"],
            similar_examples=history,
            created_at=datetime.now(UTC),
            approved_transaction_id=None,
        )
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            connection.execute(
                """
                INSERT INTO sms_reviews (
                    id, sender, phone, content, received_at, latitude, longitude, source,
                    markers_json, draft_account_name, draft_payee_name, draft_category_value,
                    draft_amount, draft_spent_on, draft_notes, review_status, reasoning_json,
                    similar_examples_json, created_at, approved_transaction_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.id,
                    review.sender,
                    review.phone,
                    review.content,
                    review.received_at.isoformat(),
                    review.latitude,
                    review.longitude,
                    review.source,
                    json.dumps(review.markers),
                    review.draft_account_name,
                    review.draft_payee_name,
                    review.draft_category_value,
                    review.draft_amount,
                    review.draft_spent_on,
                    review.draft_notes,
                    review.review_status,
                    json.dumps(review.reasoning),
                    json.dumps(review.similar_examples),
                    review.created_at.isoformat(),
                    review.approved_transaction_id,
                ),
            )
        return review

    def approve_review(
        self,
        review_id: str,
        *,
        account_name: str,
        payee_name: str,
        category_value: str,
        amount: str,
        spent_on: str,
        notes: str,
    ):
        review = self.get_review(review_id)
        if review is None:
            raise ValueError("Review item was not found.")
        if review.review_status != "for_review":
            raise ValueError("Only queued review items can be approved.")
        category_name, subcategory_name = self._split_category_value(category_value)
        if not category_name or not subcategory_name:
            raise ValueError("Choose a valid category and subcategory.")
        tracker = ExpenseTracker(self.data_file)
        created = tracker.add_transaction(
            account_name=account_name,
            payee_name=payee_name,
            category_name=category_name,
            subcategory_name=subcategory_name,
            amount=amount,
            spent_on=spent_on,
            notes=notes,
        )
        approved_transaction_id = created[0].id
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            connection.execute(
                """
                UPDATE sms_reviews
                SET draft_account_name = ?, draft_payee_name = ?, draft_category_value = ?,
                    draft_amount = ?, draft_spent_on = ?, draft_notes = ?, review_status = 'approved',
                    approved_transaction_id = ?
                WHERE id = ?
                """,
                (
                    account_name,
                    payee_name,
                    category_value,
                    amount,
                    spent_on,
                    notes,
                    approved_transaction_id,
                    review_id,
                ),
            )
        return created

    def _similar_transactions(
        self,
        message: SmsMessage,
        markers: dict[str, object],
        transactions: list[TransactionRecord],
        *,
        limit: int = 3,
    ) -> list[dict[str, object]]:
        amount = Decimal(str(markers["amount"])) if markers.get("amount") else None
        event_kind = str(markers.get("event_kind") or "unknown")
        sender_hint = str(markers.get("sender_hint") or "")
        merchant_hint = str(markers.get("merchant_hint") or "")
        account_hint = str(markers.get("account_hint") or "")
        ranked: list[tuple[float, dict[str, object]]] = []
        for transaction in transactions:
            score = 0.0
            reasons: list[str] = []
            if amount is not None:
                txn_amount = abs(transaction.amount)
                if txn_amount == abs(amount):
                    score += 4.0
                    reasons.append("Exact amount match.")
                elif abs(txn_amount - abs(amount)) <= Decimal("10.00"):
                    score += 1.0
                    reasons.append("Near amount match.")
            day_gap = abs((transaction.spent_on - message.received_at.date()).days)
            if day_gap == 0:
                score += 3.0
                reasons.append("Same-day history.")
            elif day_gap <= 3:
                score += 1.0
                reasons.append("Near-date history.")
            if account_hint and account_hint in transaction.account_name:
                score += 2.0
                reasons.append("Account hint matched.")
            if tokenize(sender_hint) & tokenize(transaction.account_name):
                score += 1.0
                reasons.append("Sender/account overlap.")
            if merchant_hint and (tokenize(merchant_hint) & tokenize(transaction.payee_name)):
                score += 2.5
                reasons.append("Merchant/payee overlap.")
            if event_kind == "credit" and transaction.amount > 0:
                score += 1.0
                reasons.append("Credit direction aligns.")
            if event_kind in {"debit", "transfer"} and transaction.amount < 0:
                score += 1.0
                reasons.append("Debit direction aligns.")
            if score <= 0:
                continue
            ranked.append(
                (
                    score,
                    {
                        "transaction_id": transaction.id,
                        "account_name": transaction.account_name,
                        "payee_name": transaction.payee_name,
                        "category_value": self._transaction_category_value(transaction),
                        "amount": f"{transaction.amount:.2f}",
                        "spent_on": transaction.spent_on.isoformat(),
                        "notes": transaction.notes,
                        "score": round(score, 2),
                        "reasons": reasons,
                    },
                )
            )
        ranked.sort(key=lambda item: (item[0], item[1]["spent_on"]), reverse=True)
        return [item for _, item in ranked[:limit]]

    def _build_draft(
        self,
        metadata: MetadataSnapshot,
        message: SmsMessage,
        markers: dict[str, object],
        history: list[dict[str, object]],
    ) -> dict[str, object]:
        reasoning: list[str] = []
        account_name = self._resolve_account_name(metadata, markers, history, reasoning)
        payee_name = self._resolve_payee_name(metadata, markers, history, reasoning)
        category_value = self._resolve_category_value(metadata, history, reasoning)
        amount = self._resolve_amount(markers, reasoning)
        notes = self._build_notes(markers, message.content)
        if notes:
            reasoning.append("Notes were limited to high-value bullets only.")
        spent_on = message.received_at.date().isoformat()
        reasoning.append("Draft date defaults to the SMS timestamp date.")
        return {
            "account_name": account_name,
            "payee_name": payee_name,
            "category_value": category_value,
            "amount": amount,
            "spent_on": spent_on,
            "notes": notes,
            "reasoning": reasoning,
        }

    def _merge_ai_draft(self, ai_draft: dict[str, object], heuristic_draft: dict[str, object]) -> dict[str, object]:
        merged = dict(heuristic_draft)
        for key in ("account_name", "payee_name", "category_value", "amount", "spent_on"):
            value = str(ai_draft.get(key, "") or "").strip()
            if value:
                merged[key] = value
        notes = self._normalize_notes(str(ai_draft.get("notes", "") or "").strip())
        if notes:
            merged["notes"] = notes
        reasoning = ai_draft.get("reasoning", [])
        if isinstance(reasoning, list):
            merged["reasoning"] = [str(item) for item in reasoning if str(item).strip()]
        merged["reasoning"].append("Draft fields were generated with OpenAI structured output.")
        return merged

    def _resolve_account_name(
        self,
        metadata: MetadataSnapshot,
        markers: dict[str, object],
        history: list[dict[str, object]],
        reasoning: list[str],
    ) -> str:
        account_hint = str(markers.get("account_hint") or "")
        if account_hint:
            for account_name in metadata.accounts:
                if account_hint in account_name:
                    reasoning.append(f"Selected account {account_name} from account suffix hint {account_hint}.")
                    return account_name
        if history:
            reasoning.append(f"Selected account {history[0]['account_name']} from the closest historical match.")
            return str(history[0]["account_name"])
        if metadata.accounts:
            reasoning.append(f"Fell back to the first available account {metadata.accounts[0]}.")
            return metadata.accounts[0]
        return ""

    def _resolve_payee_name(
        self,
        metadata: MetadataSnapshot,
        markers: dict[str, object],
        history: list[dict[str, object]],
        reasoning: list[str],
    ) -> str:
        merchant_hint = str(markers.get("merchant_hint") or "").strip()
        if merchant_hint:
            exact = next((payee for payee in metadata.payees if payee.casefold() == merchant_hint.casefold()), None)
            if exact is not None:
                reasoning.append(f"Matched payee {exact} directly from merchant text.")
                return exact
        if history:
            reasoning.append(f"Selected payee {history[0]['payee_name']} from the closest historical match.")
            return str(history[0]["payee_name"])
        if merchant_hint:
            reasoning.append(f"Used merchant hint {merchant_hint} as the draft payee.")
            return merchant_hint
        return ""

    def _resolve_category_value(
        self,
        metadata: MetadataSnapshot,
        history: list[dict[str, object]],
        reasoning: list[str],
    ) -> str:
        if history:
            reasoning.append(f"Selected category {history[0]['category_value']} from the closest historical match.")
            return str(history[0]["category_value"])
        for category_name in metadata.categories:
            subcategories = metadata.subcategories_by_category.get(category_name, [])
            if subcategories:
                fallback = f"{category_name} / {subcategories[0]}"
                reasoning.append(f"Fell back to the first available category {fallback}.")
                return fallback
        return ""

    def _resolve_amount(self, markers: dict[str, object], reasoning: list[str]) -> str:
        raw_amount = markers.get("amount")
        if raw_amount is None:
            return ""
        amount = Decimal(str(raw_amount))
        event_kind = str(markers.get("event_kind") or "unknown")
        if event_kind in {"debit", "transfer"}:
            amount = -abs(amount)
        elif event_kind == "credit":
            amount = abs(amount)
        reasoning.append(f"Used extracted amount {amount:.2f} with sign inferred from the SMS event type.")
        return f"{amount:.2f}"

    def _build_notes(self, markers: dict[str, object], content: str) -> str:
        bullets: list[str] = []
        reference_hint = str(markers.get("reference_hint") or "").strip()
        if reference_hint:
            bullets.append(f"- Ref: {reference_hint}")
        note_match = NOTE_PATTERN.search(content)
        if note_match:
            bullets.append(f"- Note: {note_match.group(1).strip(' .')}")
        return "\n".join(bullets)

    @staticmethod
    def _normalize_notes(value: str) -> str:
        lines = [line.strip("- ").strip() for line in value.splitlines() if line.strip()]
        return "\n".join(f"- {line}" for line in lines[:3])

    @staticmethod
    def _transaction_category_value(transaction: TransactionRecord) -> str:
        if transaction.subcategory_name:
            return f"{transaction.category_name} / {transaction.subcategory_name}"
        return transaction.category_name

    @staticmethod
    def _split_category_value(value: str) -> tuple[str, str | None]:
        if " / " not in value:
            return value.strip(), None
        category_name, subcategory_name = value.split(" / ", maxsplit=1)
        return category_name.strip(), subcategory_name.strip() or None

    @staticmethod
    def _parse_received_at(value: str) -> datetime:
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed

    def _ensure_schema(self) -> None:
        with sqlite_connection(self.data_file) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sms_reviews (
                    id TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    content TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    latitude REAL,
                    longitude REAL,
                    source TEXT NOT NULL,
                    markers_json TEXT NOT NULL,
                    draft_account_name TEXT NOT NULL,
                    draft_payee_name TEXT NOT NULL,
                    draft_category_value TEXT NOT NULL,
                    draft_amount TEXT NOT NULL,
                    draft_spent_on TEXT NOT NULL,
                    draft_notes TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    reasoning_json TEXT NOT NULL,
                    similar_examples_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    approved_transaction_id TEXT
                )
                """
            )

    def _row_to_review(self, row: sqlite3.Row) -> ReviewDraft:
        return ReviewDraft(
            id=row["id"],
            sender=row["sender"],
            phone=row["phone"],
            content=row["content"],
            received_at=datetime.fromisoformat(row["received_at"]),
            latitude=row["latitude"],
            longitude=row["longitude"],
            source=row["source"],
            markers=json.loads(row["markers_json"]),
            draft_account_name=row["draft_account_name"],
            draft_payee_name=row["draft_payee_name"],
            draft_category_value=row["draft_category_value"],
            draft_amount=row["draft_amount"],
            draft_spent_on=row["draft_spent_on"],
            draft_notes=row["draft_notes"],
            review_status=row["review_status"],
            reasoning=json.loads(row["reasoning_json"]),
            similar_examples=json.loads(row["similar_examples_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            approved_transaction_id=row["approved_transaction_id"],
        )
