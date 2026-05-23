from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from decimal import Decimal
from typing import Sequence

from .models import TransactionRecord
from .sms_pipeline import (
    LegacyTransaction,
    SmsMessage,
    build_field_associations,
    extract_sms_markers,
    is_pending_transaction_reminder,
    load_sms_messages,
    score_sms_transaction_match,
    tokenize,
)
from .sqlite_utils import sqlite_connection


@dataclass(slots=True)
class SmsImportSummary:
    total_messages: int
    useful_messages: int
    matched_messages: int
    unmatched_useful_messages: int


class SmsHistoryStore:
    def __init__(self, data_file: Path) -> None:
        self.data_file = data_file

    def import_csv(
        self,
        csv_path: Path,
        transactions: Sequence[TransactionRecord],
        *,
        source_label: str | None = None,
    ) -> SmsImportSummary:
        messages = load_sms_messages(csv_path)
        return self.import_messages(messages, transactions, source_label=source_label or str(csv_path))

    def import_messages(
        self,
        messages: Sequence[SmsMessage],
        transactions: Sequence[TransactionRecord],
        *,
        source_label: str,
    ) -> SmsImportSummary:
        self._ensure_schema()
        legacy_transactions, transaction_id_map = self._legacy_transactions(transactions)
        useful_messages = 0
        matched_messages = 0
        with sqlite_connection(self.data_file) as connection:
            for message in messages:
                markers = extract_sms_markers(message)
                markers_payload = markers.to_dict()
                matched_transaction, match_score, match_reasons, field_associations = self._match_transaction(
                    message,
                    markers,
                    legacy_transactions,
                )
                if markers.useful:
                    useful_messages += 1
                if matched_transaction is not None:
                    matched_messages += 1
                self._upsert_sms_message(
                    connection,
                    message=message,
                    markers_payload=markers_payload,
                    source_label=source_label,
                    matched_transaction_id=(
                        transaction_id_map.get(matched_transaction.row_number)
                        if matched_transaction is not None
                        else None
                    ),
                    match_score=match_score,
                    match_reasons=match_reasons,
                    field_associations=field_associations,
                )
        return SmsImportSummary(
            total_messages=len(messages),
            useful_messages=useful_messages,
            matched_messages=matched_messages,
            unmatched_useful_messages=useful_messages - matched_messages,
        )

    def record_approved_review(
        self,
        *,
        sender: str,
        phone: str,
        content: str,
        received_at: datetime,
        markers_payload: dict[str, object],
        matched_transaction_id: str,
        source_label: str = "sms_review_approval",
    ) -> None:
        self._ensure_schema()
        message = SmsMessage(
            row_number=0,
            received_at=received_at,
            direction="Received",
            contact=sender,
            phone=phone,
            content=content,
            message_type="SMS",
        )
        with sqlite_connection(self.data_file) as connection:
            self._upsert_sms_message(
                connection,
                message=message,
                markers_payload=markers_payload,
                source_label=source_label,
                matched_transaction_id=matched_transaction_id,
                match_score=0.0,
                match_reasons=["Linked from an approved review draft."],
                field_associations={},
            )

    def similar_examples(
        self,
        *,
        message: SmsMessage,
        markers_payload: dict[str, object],
        transactions: Sequence[TransactionRecord],
        limit: int = 3,
    ) -> list[dict[str, object]]:
        self._ensure_schema()
        transaction_map = {transaction.id: transaction for transaction in transactions}
        target_key = self._message_key(message)
        grouped: dict[str, tuple[float, dict[str, object]]] = {}
        with sqlite_connection(self.data_file) as connection:
            rows = connection.execute(
                """
                SELECT matched_transaction_id, contact, content, received_at, markers_json,
                       match_score, match_reasons_json
                FROM sms_messages
                WHERE matched_transaction_id IS NOT NULL
                  AND is_useful = 1
                  AND source_key != ?
                ORDER BY received_at DESC
                """,
                (target_key,),
            ).fetchall()
        for row in rows:
            transaction = transaction_map.get(row["matched_transaction_id"])
            if transaction is None:
                continue
            canonical_transaction = self._canonical_transaction(transaction, transaction_map)
            group_key = canonical_transaction.transfer_group_id or canonical_transaction.id
            candidate_markers = json.loads(row["markers_json"])
            similarity, reasons = self._score_similarity(markers_payload, candidate_markers)
            if similarity <= 0:
                continue
            evidence = self._historical_sms_payload(
                row=row,
                transaction=transaction,
                markers_payload=candidate_markers,
            )
            existing = grouped.get(group_key)
            if existing is None:
                payload = self._similar_example_payload(
                    canonical_transaction=canonical_transaction,
                    matched_transaction=transaction,
                    similarity=similarity,
                    reasons=reasons or json.loads(row["match_reasons_json"]),
                    markers_payload=candidate_markers,
                    evidence=evidence,
                )
                grouped[group_key] = (similarity, payload)
                continue
            best_similarity, payload = existing
            if similarity > best_similarity:
                payload["score"] = round(similarity, 2)
                payload["reasons"] = reasons or json.loads(row["match_reasons_json"])
                payload["historical_sender"] = row["contact"]
                payload["historical_sms_received_at"] = row["received_at"]
                payload["historical_sms_excerpt"] = row["content"][:180]
                payload["historical_markers"] = candidate_markers
                best_similarity = similarity
            payload["historical_sms_messages"] = self._merge_sms_evidence(payload["historical_sms_messages"], evidence)
            grouped[group_key] = (best_similarity, payload)
        ranked = sorted(grouped.values(), key=lambda item: (item[0], item[1]["spent_on"]), reverse=True)
        results = [payload for _, payload in ranked[:limit]]
        for payload in results:
            if payload.get("transfer_group_id") and len(payload["historical_sms_messages"]) > 1:
                payload["reasons"] = list(payload["reasons"]) + [
                    f"Historical transfer example includes {len(payload['historical_sms_messages'])} linked SMS messages across owned accounts."
                ]
        return results

    def sender_examples(
        self,
        *,
        sender: str,
        transactions: Sequence[TransactionRecord],
        limit: int = 3,
    ) -> list[dict[str, object]]:
        self._ensure_schema()
        transaction_map = {transaction.id: transaction for transaction in transactions}
        grouped: dict[str, tuple[str, dict[str, object]]] = {}
        with sqlite_connection(self.data_file) as connection:
            rows = connection.execute(
                """
                SELECT matched_transaction_id, contact, content, received_at, markers_json,
                       match_reasons_json
                FROM sms_messages
                WHERE matched_transaction_id IS NOT NULL
                  AND is_useful = 1
                  AND contact = ?
                ORDER BY received_at DESC
                """,
                (sender,),
            ).fetchall()
        for row in rows:
            transaction = transaction_map.get(row["matched_transaction_id"])
            if transaction is None:
                continue
            canonical_transaction = self._canonical_transaction(transaction, transaction_map)
            group_key = canonical_transaction.transfer_group_id or canonical_transaction.id
            candidate_markers = json.loads(row["markers_json"])
            evidence = self._historical_sms_payload(
                row=row,
                transaction=transaction,
                markers_payload=candidate_markers,
            )
            existing = grouped.get(group_key)
            if existing is None:
                payload = self._similar_example_payload(
                    canonical_transaction=canonical_transaction,
                    matched_transaction=transaction,
                    similarity=1.0,
                    reasons=["Historical SMS sender matched the new message sender."],
                    markers_payload=candidate_markers,
                    evidence=evidence,
                )
                grouped[group_key] = (row["received_at"], payload)
                continue
            received_at, payload = existing
            payload["historical_sms_messages"] = self._merge_sms_evidence(payload["historical_sms_messages"], evidence)
            if row["received_at"] > received_at:
                payload["historical_sender"] = row["contact"]
                payload["historical_sms_received_at"] = row["received_at"]
                payload["historical_sms_excerpt"] = row["content"][:180]
                payload["historical_markers"] = candidate_markers
                received_at = row["received_at"]
            grouped[group_key] = (received_at, payload)
        ranked = sorted(grouped.values(), key=lambda item: item[0], reverse=True)
        return [payload for _, payload in ranked[:limit]]

    def merchant_examples(
        self,
        *,
        merchant_hint: str,
        transactions: Sequence[TransactionRecord],
        limit: int = 3,
    ) -> list[dict[str, object]]:
        self._ensure_schema()
        normalized_target = (merchant_hint or "").strip().casefold()
        if not normalized_target:
            return []
        transaction_map = {transaction.id: transaction for transaction in transactions}
        grouped: dict[str, tuple[str, dict[str, object]]] = {}
        with sqlite_connection(self.data_file) as connection:
            rows = connection.execute(
                """
                SELECT matched_transaction_id, contact, content, received_at, markers_json,
                       match_reasons_json
                FROM sms_messages
                WHERE matched_transaction_id IS NOT NULL
                  AND is_useful = 1
                ORDER BY received_at DESC
                """
            ).fetchall()
        for row in rows:
            transaction = transaction_map.get(row["matched_transaction_id"])
            if transaction is None:
                continue
            candidate_markers = json.loads(row["markers_json"])
            candidate_merchant_hint = str(candidate_markers.get("merchant_hint") or "")
            # Require exact case-insensitive match instead of token overlap
            if candidate_merchant_hint.strip().casefold() != normalized_target:
                continue
            canonical_transaction = self._canonical_transaction(transaction, transaction_map)
            group_key = canonical_transaction.transfer_group_id or canonical_transaction.id
            evidence = self._historical_sms_payload(
                row=row,
                transaction=transaction,
                markers_payload=candidate_markers,
            )
            existing = grouped.get(group_key)
            reasons = [
                f"Historical extracted merchant marker {candidate_merchant_hint or '(blank)'} matched the new merchant marker {merchant_hint}."
            ]
            if existing is None:
                payload = self._similar_example_payload(
                    canonical_transaction=canonical_transaction,
                    matched_transaction=transaction,
                    similarity=1.0,
                    reasons=reasons,
                    markers_payload=candidate_markers,
                    evidence=evidence,
                )
                grouped[group_key] = (row["received_at"], payload)
                continue
            received_at, payload = existing
            payload["historical_sms_messages"] = self._merge_sms_evidence(payload["historical_sms_messages"], evidence)
            if row["received_at"] > received_at:
                payload["historical_sender"] = row["contact"]
                payload["historical_sms_received_at"] = row["received_at"]
                payload["historical_sms_excerpt"] = row["content"][:180]
                payload["historical_markers"] = candidate_markers
                payload["reasons"] = reasons
                received_at = row["received_at"]
            grouped[group_key] = (received_at, payload)
        ranked = sorted(grouped.values(), key=lambda item: item[0], reverse=True)
        return [payload for _, payload in ranked[:limit]]

    def transaction_messages(
        self,
        *,
        transaction: TransactionRecord,
        transactions: Sequence[TransactionRecord],
    ) -> list[dict[str, object]]:
        self._ensure_schema()
        transaction_map = {item.id: item for item in transactions}
        transaction_ids = {transaction.id}
        if transaction.linked_transaction_id:
            transaction_ids.add(transaction.linked_transaction_id)
        placeholders = ", ".join("?" for _ in transaction_ids)
        with sqlite_connection(self.data_file) as connection:
            rows = connection.execute(
                f"""
                SELECT matched_transaction_id, source_label, received_at, contact, phone, content,
                       markers_json, match_score, match_reasons_json, field_associations_json
                FROM sms_messages
                WHERE matched_transaction_id IN ({placeholders})
                ORDER BY received_at DESC, imported_at DESC
                """,
                tuple(transaction_ids),
            ).fetchall()
        messages: list[dict[str, object]] = []
        for row in rows:
            matched_transaction = transaction_map.get(row["matched_transaction_id"])
            if matched_transaction is None:
                continue
            messages.append(
                {
                    **self._historical_sms_payload(
                        row=row,
                        transaction=matched_transaction,
                        markers_payload=json.loads(row["markers_json"]),
                    ),
                    "source_label": row["source_label"],
                    "match_score": round(float(row["match_score"]), 2),
                    "match_reasons": json.loads(row["match_reasons_json"]),
                    "field_associations": json.loads(row["field_associations_json"]),
                    "is_related_transfer_side": matched_transaction.id != transaction.id,
                }
            )
        return messages

    def matched_count(self) -> int:
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM sms_messages WHERE matched_transaction_id IS NOT NULL").fetchone()[0])

    def total_count(self) -> int:
        self._ensure_schema()
        with sqlite_connection(self.data_file) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM sms_messages").fetchone()[0])

    def _ensure_schema(self) -> None:
        with sqlite_connection(self.data_file) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sms_messages (
                    id TEXT PRIMARY KEY,
                    source_key TEXT NOT NULL UNIQUE,
                    source_label TEXT NOT NULL,
                    source_row_number INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    contact TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    content TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    markers_json TEXT NOT NULL,
                    is_useful INTEGER NOT NULL,
                    matched_transaction_id TEXT REFERENCES transactions(id),
                    match_score REAL NOT NULL,
                    match_reasons_json TEXT NOT NULL,
                    field_associations_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sms_messages_received_at ON sms_messages(received_at);
                CREATE INDEX IF NOT EXISTS idx_sms_messages_matched_transaction ON sms_messages(matched_transaction_id);
                """
            )

    def _upsert_sms_message(
        self,
        connection,
        *,
        message: SmsMessage,
        markers_payload: dict[str, object],
        source_label: str,
        matched_transaction_id: str | None,
        match_score: float,
        match_reasons: list[str],
        field_associations: dict[str, list[str]],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT INTO sms_messages (
                id, source_key, source_label, source_row_number, received_at, direction,
                contact, phone, content, message_type, markers_json, is_useful,
                matched_transaction_id, match_score, match_reasons_json,
                field_associations_json, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                source_label = excluded.source_label,
                source_row_number = excluded.source_row_number,
                markers_json = excluded.markers_json,
                is_useful = excluded.is_useful,
                matched_transaction_id = excluded.matched_transaction_id,
                match_score = excluded.match_score,
                match_reasons_json = excluded.match_reasons_json,
                field_associations_json = excluded.field_associations_json,
                imported_at = excluded.imported_at
            """,
            (
                self._message_key(message),
                self._message_key(message),
                source_label,
                message.row_number,
                message.received_at.isoformat(),
                message.direction,
                message.contact,
                message.phone,
                message.content,
                message.message_type,
                json.dumps(markers_payload),
                1 if markers_payload.get("useful") else 0,
                matched_transaction_id,
                match_score,
                json.dumps(match_reasons),
                json.dumps(field_associations),
                now,
            ),
        )

    @staticmethod
    def _message_key(message: SmsMessage) -> str:
        raw = "|".join(
            [
                message.received_at.isoformat(),
                message.direction,
                message.contact,
                message.phone,
                message.content,
                message.message_type,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _legacy_transactions(transactions: Sequence[TransactionRecord]) -> tuple[list[LegacyTransaction], dict[int, str]]:
        legacy: list[LegacyTransaction] = []
        transaction_id_map: dict[int, str] = {}
        for index, transaction in enumerate(transactions, start=1):
            legacy.append(
                LegacyTransaction(
                    row_number=index,
                    account_name=transaction.account_name,
                    spent_on=transaction.spent_on,
                    payee_name=transaction.payee_name,
                    notes=transaction.notes,
                    raw_category=f"{transaction.category_name}:{transaction.subcategory_name or 'General'}",
                    category_group=transaction.category_name,
                    category_name=transaction.subcategory_name or "General",
                    amount=transaction.amount,
                    cleared="",
                )
            )
            transaction_id_map[index] = transaction.id
        return legacy, transaction_id_map

    @staticmethod
    def _match_transaction(
        message: SmsMessage,
        markers,
        transactions: Sequence[LegacyTransaction],
        *,
        threshold: float = 10.0,
        amount_tolerance: Decimal = Decimal("0.50"),
    ) -> tuple[LegacyTransaction | None, float, list[str], dict[str, list[str]]]:
        if not markers.useful:
            return None, 0.0, ["Message was not classified as financially useful."], {}
        if markers.event_kind == "toll":
            return None, 0.0, ["Toll/FASTag SMS was not matched to transactions by design."], {}
        if is_pending_transaction_reminder(message.content):
            return None, 0.0, ["Upcoming auto-debit or reminder SMS was not matched to a completed transaction."], {}
        ranked: list[tuple[LegacyTransaction, float, list[str]]] = []
        for transaction in transactions:
            if markers.amount is None:
                continue
            if abs(abs(transaction.amount) - abs(markers.amount)) > amount_tolerance:
                continue
            score, reasons = score_sms_transaction_match(markers, message, transaction)
            day_gap = abs((transaction.spent_on - message.received_at.date()).days)
            if score > 0 and day_gap <= 5:
                ranked.append((transaction, score, reasons))
        ranked.sort(key=lambda item: (item[1], item[0].spent_on), reverse=True)
        if ranked and ranked[0][1] >= threshold:
            transaction, score, reasons = ranked[0]
            return transaction, score, reasons, build_field_associations(markers, transaction)
        if ranked:
            return None, ranked[0][1], ranked[0][2], {}
        return None, 0.0, ["No transaction exceeded the matching threshold."], {}

    @staticmethod
    def _score_similarity(target: dict[str, object], candidate: dict[str, object]) -> tuple[float, list[str]]:
        similarity = 0.0
        reasons: list[str] = []
        if target.get("amount") and target.get("amount") == candidate.get("amount"):
            similarity += 3.0
            reasons.append("Exact matched amount.")
        if target.get("event_kind") and target.get("event_kind") == candidate.get("event_kind") and target.get("event_kind") != "unknown":
            similarity += 1.5
            reasons.append("Same event type.")
        if target.get("sender_hint") and target.get("sender_hint") == candidate.get("sender_hint"):
            similarity += 1.5
            reasons.append("Same SMS sender pattern.")
        if tokenize(str(target.get("merchant_hint") or "")) & tokenize(str(candidate.get("merchant_hint") or "")):
            similarity += 2.0
            reasons.append("Merchant hints overlap.")
        if target.get("account_hint") and target.get("account_hint") == candidate.get("account_hint"):
            similarity += 1.0
            reasons.append("Account suffix hint overlaps.")
        return similarity, reasons

    @staticmethod
    def _transaction_category_value(transaction: TransactionRecord) -> str:
        if transaction.subcategory_name:
            return f"{transaction.category_name} / {transaction.subcategory_name}"
        return transaction.category_name

    @staticmethod
    def _canonical_transaction(
        transaction: TransactionRecord,
        transaction_map: dict[str, TransactionRecord],
    ) -> TransactionRecord:
        if not transaction.transfer_group_id or transaction.entry_type == "transfer_out":
            return transaction
        if transaction.linked_transaction_id:
            linked = transaction_map.get(transaction.linked_transaction_id)
            if linked is not None:
                return linked
        return transaction

    @staticmethod
    def _merge_sms_evidence(
        existing: list[dict[str, object]],
        candidate: dict[str, object],
    ) -> list[dict[str, object]]:
        if any(
            item["received_at"] == candidate["received_at"]
            and item["excerpt"] == candidate["excerpt"]
            and item["matched_transaction_id"] == candidate["matched_transaction_id"]
            for item in existing
        ):
            return existing
        return sorted(existing + [candidate], key=lambda item: item["received_at"], reverse=True)

    def _historical_sms_payload(
        self,
        *,
        row,
        transaction: TransactionRecord,
        markers_payload: dict[str, object],
    ) -> dict[str, object]:
        return {
            "matched_transaction_id": transaction.id,
            "matched_account_name": transaction.account_name,
            "matched_payee_name": transaction.payee_name,
            "matched_entry_type": transaction.entry_type,
            "received_at": row["received_at"],
            "sender": row["contact"],
            "phone": row["phone"] if "phone" in row.keys() else "",
            "excerpt": row["content"][:180],
            "content": row["content"],
            "markers": markers_payload,
        }

    def _similar_example_payload(
        self,
        *,
        canonical_transaction: TransactionRecord,
        matched_transaction: TransactionRecord,
        similarity: float,
        reasons: list[str],
        markers_payload: dict[str, object],
        evidence: dict[str, object],
    ) -> dict[str, object]:
        payload = {
            "transaction_id": canonical_transaction.id,
            "entry_type": canonical_transaction.entry_type,
            "account_name": canonical_transaction.account_name,
            "payee_name": canonical_transaction.payee_name,
            "category_value": self._transaction_category_value(canonical_transaction),
            "amount": f"{canonical_transaction.amount:.2f}",
            "spent_on": canonical_transaction.spent_on.isoformat(),
            "notes": canonical_transaction.notes,
            "score": round(similarity, 2),
            "reasons": reasons,
            "linked_transaction_id": canonical_transaction.linked_transaction_id,
            "transfer_group_id": canonical_transaction.transfer_group_id,
            "historical_sender": evidence["sender"],
            "historical_sms_received_at": evidence["received_at"],
            "historical_sms_excerpt": evidence["excerpt"],
            "historical_markers": markers_payload,
            "historical_sms_messages": [evidence],
        }
        if canonical_transaction.transfer_group_id and matched_transaction.id != canonical_transaction.id:
            payload["reasons"] = list(reasons) + ["A linked SMS from the paired transfer account also matched this history."]
        return payload
