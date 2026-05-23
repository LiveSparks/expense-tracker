from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

try:
    from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
except ImportError:  # pragma: no cover - exercised only when dependency is missing
    APIConnectionError = APIStatusError = APITimeoutError = None
    OpenAI = None

from .config import AppConfig, load_app_config
from .migrations import apply_migrations
from .models import MetadataSnapshot, TransactionRecord
from .sms_history import SmsHistoryStore
from .sms_pipeline import (
    ReviewDraftTextFormat,
    SmsMessage,
    build_structured_output_schema,
    extract_sms_markers,
    regex_filter_reasons,
)
from .sqlite_utils import sqlite_connection
from .tracker import ExpenseTracker

HistoryBuckets = dict[str, list[dict[str, object]]]


NOTE_PATTERN = re.compile(r"\b(?:note|remarks?)\s*[:=-]?\s*([A-Za-z0-9&./,'() -]{3,})", re.IGNORECASE)


class OpenAIDraftError(RuntimeError):
    pass


class OpenAIDraftClient:
    def __init__(
        self,
        *,
        key_path: Path = Path("/root/openai.key"),
        api_key: str | None = None,
        model: str = "gpt-5-mini",
        prompt_path: Path | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.key_path = key_path
        self.api_key = api_key
        self.model = model
        self.prompt_path = prompt_path or Path(__file__).resolve().parent / "prompts" / "review_draft_prompt.txt"
        self.config = config or load_app_config()

    def _resolve_api_key(self) -> str | None:
        if self.api_key and self.api_key.strip():
            return self.api_key.strip()
        if self.key_path.exists():
            value = self.key_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        return self.config.resolved_openai_api_key

    def is_configured(self) -> bool:
        return bool(self._resolve_api_key())

    def build_request_preview(
        self,
        *,
        metadata: MetadataSnapshot,
        message: SmsMessage,
        markers: dict[str, object],
        history: HistoryBuckets,
    ) -> dict[str, Any]:
        system_prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        user_payload = {
            "new_sms": message.to_dict(),
            "extracted_markers": markers,
            "similar_examples": history,
            "available_accounts": metadata.accounts,
            "available_payees": metadata.payees,
            "available_categories": self._available_category_values(metadata),
            "transfer_handling": {
                "draft_one_side_only": True,
                "auto_created_sister_transaction": True,
            },
        }
        input_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, indent=2)},
        ]
        return {
            "model": self.model,
            "system_prompt": system_prompt,
            "user_payload": user_payload,
            "user_prompt": input_messages[1]["content"],
            "input": input_messages,
            "text_format_model": ReviewDraftTextFormat.__name__,
            "text_format_schema": ReviewDraftTextFormat.model_json_schema(),
            "response_format": build_structured_output_schema(),
        }

    def generate_draft(
        self,
        *,
        metadata: MetadataSnapshot,
        message: SmsMessage,
        markers: dict[str, object],
        history: HistoryBuckets,
    ) -> dict[str, object]:
        if not self.is_configured():
            raise OpenAIDraftError("OpenAI API key is not configured.")
        if OpenAI is None:
            raise OpenAIDraftError("The OpenAI Python SDK is not installed.")
        api_key = self._resolve_api_key()
        if not api_key:
            raise OpenAIDraftError("OpenAI API key is not configured.")
        request_preview = self.build_request_preview(
            metadata=metadata,
            message=message,
            markers=markers,
            history=history,
        )
        client = OpenAI(api_key=api_key, timeout=45.0)
        if not hasattr(client.responses, "parse"):
            raise OpenAIDraftError("The installed OpenAI SDK does not support responses.parse().")
        try:
            response = client.responses.parse(
                model=self.model,
                input=request_preview["input"],
                text_format=ReviewDraftTextFormat,
            )
        except tuple(item for item in (APIConnectionError, APITimeoutError, APIStatusError) if item is not None) as exc:
            raise OpenAIDraftError(f"OpenAI request failed: {exc}") from exc
        parsed = getattr(response, "output_parsed", None)
        if not isinstance(parsed, BaseModel):
            raise OpenAIDraftError("OpenAI response did not contain a parsed structured result.")
        return dict(parsed.model_dump())

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
    similar_examples: HistoryBuckets
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
    def __init__(
        self,
        data_file: Path,
        *,
        openai_client: OpenAIDraftClient | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.data_file = data_file
        self.config = config or load_app_config()
        self.openai_client = openai_client or OpenAIDraftClient(config=self.config)

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

    def queue_counts(self) -> dict[str, int]:
        self._ensure_schema()
        counts = {"queued": 0, "processing": 0, "ready": 0, "filtered": 0}
        with sqlite_connection(self.data_file) as connection:
            rows = connection.execute(
                "SELECT review_status, COUNT(*) AS review_count FROM sms_reviews GROUP BY review_status"
            ).fetchall()
        for row in rows:
            status = row["review_status"]
            if status == "for_review":
                counts["ready"] = int(row["review_count"])
            elif status in {"queued", "processing", "filtered"}:
                counts[status] = int(row["review_count"])
        return counts

    def clear_reviews(self) -> None:
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            connection.execute("DELETE FROM sms_reviews")

    def get_review(self, review_id: str) -> ReviewDraft | None:
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            row = connection.execute("SELECT * FROM sms_reviews WHERE id = ?", (review_id,)).fetchone()
        return self._row_to_review(row) if row is not None else None

    def llm_request_preview(self, review_id: str, *, metadata: MetadataSnapshot) -> dict[str, Any]:
        review = self.get_review(review_id)
        if review is None:
            raise ValueError("Review item was not found.")
        message = SmsMessage(
            row_number=0,
            received_at=review.received_at,
            direction="Received",
            contact=review.sender,
            phone=review.phone,
            content=review.content,
            message_type="SMS",
        )
        return self.openai_client.build_request_preview(
            metadata=metadata,
            message=message,
            markers=review.markers,
            history=review.similar_examples,
        )

    def delete_review(self, review_id: str) -> bool:
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            cursor = connection.execute("DELETE FROM sms_reviews WHERE id = ?", (review_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            self._log_event(
                event_type="deleted",
                review_id=review_id,
                sender="",
                content="",
                details={"review_id": review_id},
            )
        return deleted

    # ------------------------------------------------------------------
    # Log queries
    # ------------------------------------------------------------------

    def list_log_entries(
        self,
        *,
        event_type: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        """Return log entries ordered newest-first."""
        self._ensure_schema()
        apply_migrations(self.data_file)
        query = "SELECT * FROM sms_log"
        params: list[object] = []
        if event_type:
            query += " WHERE event_type = ?"
            params.append(event_type)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with sqlite_connection(self.data_file) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "event_type": row["event_type"],
                "review_id": row["review_id"],
                "sender": row["sender"],
                "content_excerpt": row["content_excerpt"],
                "event_details": json.loads(row["event_details_json"]),
            }
            for row in rows
        ]

    def log_entry_count(self, *, event_type: str | None = None) -> int:
        """Return total count of log entries."""
        self._ensure_schema()
        apply_migrations(self.data_file)
        if event_type:
            query = "SELECT COUNT(*) FROM sms_log WHERE event_type = ?"
            params: tuple[object, ...] = (event_type,)
        else:
            query = "SELECT COUNT(*) FROM sms_log"
            params = ()
        with sqlite_connection(self.data_file) as connection:
            return int(connection.execute(query, params).fetchone()[0])

    def enqueue_review(
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
        filter_reasons = regex_filter_reasons(message, markers)
        review = ReviewDraft(
            id=str(uuid4()),
            sender=message.contact,
            phone=message.phone,
            content=message.content,
            received_at=message.received_at,
            latitude=latitude,
            longitude=longitude,
            source=f"{source}:regex_filtered" if filter_reasons else source,
            markers=markers_payload,
            draft_account_name="",
            draft_payee_name="",
            draft_category_value="",
            draft_amount="",
            draft_spent_on=message.received_at.date().isoformat(),
            draft_notes="",
            review_status="filtered" if filter_reasons else "queued",
            reasoning=filter_reasons or ["Queued for background drafting."],
            similar_examples=[],
            created_at=datetime.now(UTC),
            approved_transaction_id=None,
        )
        self._insert_review(review)
        self._log_event(
            event_type="filtered" if filter_reasons else "intake",
            review_id=review.id,
            sender=message.contact,
            content=message.content,
            details={
                "status": review.review_status,
                "source": review.source,
                "filter_reasons": filter_reasons,
            },
        )
        return review

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
        message = SmsMessage(
            row_number=0,
            received_at=self._parse_received_at(received_at),
            direction="Received",
            contact=sender.strip(),
            phone=phone.strip() or sender.strip(),
            content=content.strip(),
            message_type="SMS",
        )
        review = self._build_processed_review(
            review_id=str(uuid4()),
            message=message,
            latitude=latitude,
            longitude=longitude,
            source=source,
        )
        self._insert_review(review)
        return review

    def process_pending_queue(self, *, max_items: int | None = None) -> int:
        processed = 0
        while max_items is None or processed < max_items:
            row = self._claim_next_queued_review()
            if row is None:
                break
            review = self._build_processed_review(
                review_id=row["id"],
                message=SmsMessage(
                    row_number=0,
                    received_at=datetime.fromisoformat(row["received_at"]),
                    direction="Received",
                    contact=row["sender"],
                    phone=row["phone"],
                    content=row["content"],
                    message_type="SMS",
                ),
                latitude=row["latitude"],
                longitude=row["longitude"],
                source=row["source"],
            )
            self._update_review(review)
            self._log_event(
                event_type="filtered" if review.review_status == "filtered" else "processed",
                review_id=review.id,
                sender=review.sender,
                content=review.content,
                details={
                    "status": review.review_status,
                    "source": review.source,
                    "draft_payee": review.draft_payee_name,
                    "draft_amount": review.draft_amount,
                    "reasoning": review.reasoning,
                },
            )
            processed += 1
        return processed

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
        SmsHistoryStore(self.data_file).record_approved_review(
            sender=review.sender,
            phone=review.phone,
            content=review.content,
            received_at=review.received_at,
            markers_payload=review.markers,
            matched_transaction_id=approved_transaction_id,
        )
        self._log_event(
            event_type="approved",
            review_id=review_id,
            sender=review.sender,
            content=review.content,
            details={
                "approved_transaction_id": approved_transaction_id,
                "account_name": account_name,
                "payee_name": payee_name,
                "category_value": category_value,
                "amount": amount,
                "spent_on": spent_on,
            },
        )
        return created

    def quick_approve_review(self, review_id: str):
        review = self.get_review(review_id)
        if review is None:
            raise ValueError("Review item was not found.")
        return self.approve_review(
            review_id,
            account_name=review.draft_account_name,
            payee_name=review.draft_payee_name,
            category_value=review.draft_category_value,
            amount=review.draft_amount,
            spent_on=review.draft_spent_on,
            notes=review.draft_notes,
        )

    def _insert_review(self, review: ReviewDraft) -> None:
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

    def _update_review(self, review: ReviewDraft) -> None:
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            connection.execute(
                """
                UPDATE sms_reviews
                SET source = ?, markers_json = ?, draft_account_name = ?, draft_payee_name = ?,
                    draft_category_value = ?, draft_amount = ?, draft_spent_on = ?, draft_notes = ?,
                    review_status = ?, reasoning_json = ?, similar_examples_json = ?
                WHERE id = ?
                """,
                (
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
                    review.id,
                ),
            )

    def _claim_next_queued_review(self) -> sqlite3.Row | None:
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM sms_reviews
                WHERE review_status = 'queued'
                ORDER BY received_at ASC, created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE sms_reviews SET review_status = 'processing' WHERE id = ? AND review_status = 'queued'",
                (row["id"],),
            )
            return connection.execute("SELECT * FROM sms_reviews WHERE id = ?", (row["id"],)).fetchone()

    def _build_processed_review(
        self,
        *,
        review_id: str,
        message: SmsMessage,
        latitude: float | None,
        longitude: float | None,
        source: str,
    ) -> ReviewDraft:
        tracker = ExpenseTracker(self.data_file)
        metadata = tracker.metadata_snapshot()
        markers = extract_sms_markers(message)
        markers_payload = markers.to_dict()
        filter_reasons = regex_filter_reasons(message, markers)
        created_at = datetime.now(UTC)
        if filter_reasons:
            return ReviewDraft(
                id=review_id,
                sender=message.contact,
                phone=message.phone,
                content=message.content,
                received_at=message.received_at,
                latitude=latitude,
                longitude=longitude,
                source=f"{source}:regex_filtered",
                markers=markers_payload,
                draft_account_name="",
                draft_payee_name="",
                draft_category_value="",
                draft_amount="",
                draft_spent_on=message.received_at.date().isoformat(),
                draft_notes="",
                review_status="filtered",
                reasoning=filter_reasons,
                similar_examples=[],
                created_at=created_at,
                approved_transaction_id=None,
            )
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
                if self._ai_marks_non_transactional(ai_draft):
                    return ReviewDraft(
                        id=review_id,
                        sender=message.contact,
                        phone=message.phone,
                        content=message.content,
                        received_at=message.received_at,
                        latitude=latitude,
                        longitude=longitude,
                        source=f"{source}:openai",
                        markers=markers_payload,
                        draft_account_name="",
                        draft_payee_name="",
                        draft_category_value="",
                        draft_amount="",
                        draft_spent_on=message.received_at.date().isoformat(),
                        draft_notes="",
                        review_status="filtered",
                        reasoning=self._non_transactional_reasoning(ai_draft),
                        similar_examples=history,
                        created_at=created_at,
                        approved_transaction_id=None,
                    )
                draft = self._merge_ai_draft(ai_draft, draft)
                review_source = f"{source}:openai"
            except OpenAIDraftError as exc:
                draft["reasoning"].append(f"OpenAI drafting failed: {exc}")
                review_source = f"{source}:heuristic_fallback"
        return ReviewDraft(
            id=review_id,
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
            created_at=created_at,
            approved_transaction_id=None,
        )

    def _similar_transactions(
        self,
        message: SmsMessage,
        markers: dict[str, object],
        transactions: list[TransactionRecord],
        *,
        limit: int = 3,
    ) -> HistoryBuckets:
        sms_history_store = SmsHistoryStore(self.data_file)
        historical_examples = sms_history_store.similar_examples(
            message=message,
            markers_payload=markers,
            transactions=transactions,
            limit=limit,
        )
        latest_transactions = [self._history_payload_from_transaction(transaction, ["Latest transaction history."]) for transaction in transactions[:limit]]
        amount = Decimal(str(markers["amount"])) if markers.get("amount") else None
        merchant_hint = str(markers.get("merchant_hint") or "")
        same_payee_transactions = (
            sms_history_store.merchant_examples(
                merchant_hint=merchant_hint,
                transactions=transactions,
                limit=limit,
            )
            if merchant_hint
            else []
        )

        same_amount_transactions: list[dict[str, object]] = []
        same_amount_keys: set[str] = set()
        if amount is not None:
            for transaction in transactions:
                dedupe_key = transaction.transfer_group_id or transaction.id
                if dedupe_key in same_amount_keys:
                    continue
                if abs(abs(transaction.amount) - abs(amount)) > Decimal("0.01"):
                    continue
                same_amount_transactions.append(
                    self._history_payload_from_transaction(
                        transaction,
                        [f"Extracted amount matched transaction amount {transaction.amount:.2f}."],
                    )
                )
                same_amount_keys.add(dedupe_key)
                if len(same_amount_transactions) >= limit:
                    break

        same_sender_transactions = sms_history_store.sender_examples(
            sender=message.contact,
            transactions=transactions,
            limit=limit,
        )

        return {
            "latest_transactions": latest_transactions,
            "same_payee_transactions": same_payee_transactions,
            "same_amount_transactions": same_amount_transactions,
            "same_sender_transactions": same_sender_transactions,
        }

    @staticmethod
    def _history_payload_from_transaction(transaction: TransactionRecord, reasons: list[str]) -> dict[str, object]:
        return {
            "transaction_id": transaction.id,
            "entry_type": transaction.entry_type,
            "account_name": transaction.account_name,
            "payee_name": transaction.payee_name,
            "category_value": ReviewWorkflowStore._transaction_category_value(transaction),
            "amount": f"{transaction.amount:.2f}",
            "spent_on": transaction.spent_on.isoformat(),
            "notes": transaction.notes,
            "score": 0.0,
            "reasons": reasons,
            "linked_transaction_id": transaction.linked_transaction_id,
            "transfer_group_id": transaction.transfer_group_id,
            "historical_sender": "",
            "historical_sms_received_at": "",
            "historical_sms_excerpt": "",
            "historical_markers": {},
            "historical_sms_messages": [],
        }

    @staticmethod
    def _flatten_history(history: HistoryBuckets) -> list[dict[str, object]]:
        ordered_keys = (
            "same_payee_transactions",
            "same_amount_transactions",
            "same_sender_transactions",
            "latest_transactions",
        )
        seen: set[str] = set()
        flattened: list[dict[str, object]] = []
        for key in ordered_keys:
            for item in history.get(key, []):
                transaction_id = str(item.get("transaction_id") or "")
                if transaction_id and transaction_id in seen:
                    continue
                if transaction_id:
                    seen.add(transaction_id)
                flattened.append(item)
        return flattened

    @staticmethod
    def _first_history_item(history: HistoryBuckets) -> dict[str, object] | None:
        flattened = ReviewWorkflowStore._flatten_history(history)
        return flattened[0] if flattened else None

    def _build_draft(
        self,
        metadata: MetadataSnapshot,
        message: SmsMessage,
        markers: dict[str, object],
        history: HistoryBuckets,
    ) -> dict[str, object]:
        reasoning: list[str] = []
        account_name = self._resolve_account_name(metadata, markers, history, reasoning)
        payee_name = self._resolve_payee_name(metadata, markers, history, reasoning)
        category_value = self._resolve_category_value(metadata, history, reasoning)
        amount = self._resolve_amount(markers, reasoning)
        notes = self._build_notes(markers, message.content)
        if notes:
            reasoning.append("Notes were limited to high-value bullets only.")
        first_history = self._first_history_item(history)
        if first_history and str(first_history.get("entry_type") or "") == "transfer_out":
            reasoning.append("Transfer history was normalized to one source-side draft because the tracker creates the sister transaction automatically.")
        spent_on = message.received_at.date().isoformat()
        reasoning.append("Draft date defaults to the SMS timestamp date.")
        return {
            "account_name": account_name,
            "payee_name": payee_name,
            "category_value": category_value,
            "amount": self._sanitize_draft_amount(amount),
            "spent_on": spent_on,
            "notes": notes,
            "reasoning": reasoning,
        }

    def _merge_ai_draft(self, ai_draft: dict[str, object], heuristic_draft: dict[str, object]) -> dict[str, object]:
        merged = dict(heuristic_draft)
        for key in ("account_name", "payee_name", "category_value", "spent_on"):
            value = str(ai_draft.get(key, "") or "").strip()
            if value:
                merged[key] = value
        amount = self._sanitize_draft_amount(str(ai_draft.get("amount", "") or "").strip())
        if amount:
            merged["amount"] = amount
        notes = self._normalize_notes(str(ai_draft.get("notes", "") or "").strip())
        if notes:
            merged["notes"] = notes
        reasoning = ai_draft.get("reasoning", [])
        if isinstance(reasoning, list):
            merged["reasoning"] = [str(item) for item in reasoning if str(item).strip()]
        merged["reasoning"].append("Draft fields were generated with OpenAI structured output.")
        return merged

    @staticmethod
    def _ai_marks_non_transactional(ai_draft: dict[str, object]) -> bool:
        message_kind = str(ai_draft.get("message_kind", "") or "").strip().lower()
        review_status = str(ai_draft.get("review_status", "") or "").strip().lower()
        return message_kind == "non_transactional" or review_status == "filtered"

    @staticmethod
    def _non_transactional_reasoning(ai_draft: dict[str, object]) -> list[str]:
        reasoning = ai_draft.get("reasoning", [])
        items = [str(item) for item in reasoning if str(item).strip()] if isinstance(reasoning, list) else []
        reason = str(ai_draft.get("non_transactional_reason", "") or "").strip()
        if reason:
            items.append(reason)
        if not items:
            items.append("The LLM marked this message as non-transactional.")
        items.append("Message was classified as non-transactional by OpenAI structured output.")
        return items

    def _resolve_account_name(
        self,
        metadata: MetadataSnapshot,
        markers: dict[str, object],
        history: HistoryBuckets,
        reasoning: list[str],
    ) -> str:
        account_hint = str(markers.get("account_hint") or "")
        if account_hint:
            for account_name in metadata.accounts:
                if account_hint in account_name:
                    reasoning.append(f"Selected account {account_name} from account suffix hint {account_hint}.")
                    return account_name
        first_history = self._first_history_item(history)
        if first_history:
            reasoning.append(f"Selected account {first_history['account_name']} from similar history.")
            return str(first_history["account_name"])
        if metadata.accounts:
            reasoning.append(f"Fell back to the first available account {metadata.accounts[0]}.")
            return metadata.accounts[0]
        return ""

    def _resolve_payee_name(
        self,
        metadata: MetadataSnapshot,
        markers: dict[str, object],
        history: HistoryBuckets,
        reasoning: list[str],
    ) -> str:
        merchant_hint = str(markers.get("merchant_hint") or "").strip()
        if merchant_hint:
            exact = next((payee for payee in metadata.payees if payee.casefold() == merchant_hint.casefold()), None)
            if exact is not None:
                reasoning.append(f"Matched payee {exact} directly from merchant text.")
                return exact
        first_history = self._first_history_item(history)
        if first_history:
            reasoning.append(f"Selected payee {first_history['payee_name']} from similar history.")
            return str(first_history["payee_name"])
        if merchant_hint:
            reasoning.append(f"Used merchant hint {merchant_hint} as the draft payee.")
            return merchant_hint
        return ""

    def _resolve_category_value(
        self,
        metadata: MetadataSnapshot,
        history: HistoryBuckets,
        reasoning: list[str],
    ) -> str:
        first_history = self._first_history_item(history)
        if first_history:
            reasoning.append(f"Selected category {first_history['category_value']} from similar history.")
            return str(first_history["category_value"])
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
    def _sanitize_draft_amount(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        try:
            amount = Decimal(normalized).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return ""
        return f"{amount:.2f}"

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
        apply_migrations(self.data_file)

    def _log_event(
        self,
        *,
        event_type: str,
        review_id: str | None,
        sender: str,
        content: str,
        details: dict[str, object],
    ) -> None:
        try:
            with sqlite_connection(self.data_file) as connection:
                connection.execute(
                    """
                    INSERT INTO sms_log (id, created_at, event_type, review_id, sender,
                                        content_excerpt, event_details_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        datetime.now(UTC).isoformat(),
                        event_type,
                        review_id,
                        sender,
                        content[:200],
                        json.dumps(details),
                    ),
                )
        except Exception:
            pass  # logging failures must never break the main flow

    def _row_to_review(self, row: sqlite3.Row) -> ReviewDraft:
        similar_examples = json.loads(row["similar_examples_json"])
        if isinstance(similar_examples, list):
            similar_examples = {
                "latest_transactions": [],
                "same_payee_transactions": similar_examples,
                "same_amount_transactions": [],
                "same_sender_transactions": [],
            }
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
            similar_examples=similar_examples,
            created_at=datetime.fromisoformat(row["created_at"]),
            approved_transaction_id=row["approved_transaction_id"],
        )
